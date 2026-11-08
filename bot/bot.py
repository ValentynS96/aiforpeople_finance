"""
Telegram-бот «AI замість фінансиста» — демо для вебінару.

Архітектура (всі 4 сценарії лендинга):
    Користувач шле сирий текст виписки в Telegram
      → ai_engine.categorize()     : DeepSeek розносить по 8 категоріях
      → db_bridge.save_result()    : зберігає в SQLite (дашборд бачить одразу)
      → compute_metrics() + detect_anomalies() (пороги з БД)
      → build_summary() → відповідь у чат зі зведенням + алертом
      → (опц.) sheets_writer, якщо експорт увімкнений в адмінці

Два режими запуску:
  1. Окремо:        python bot/bot.py        (тільки бот)
  2. З дашбордом:   uvicorn server.app:app   (бот стартує у lifespan)

Ключі читаються з БД (адмінка) з фоллбеком на .env.
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

# Локальні модулі + репозиторій в path (для bot.db_bridge → dashboard)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
import ai_engine as ai          # noqa: E402
import sheets_writer as sheets  # noqa: E402
import db_bridge                # noqa: E402

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx логирует полный URL запроса, а в URL Telegram API лежит токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("webinar-bot")

MIN_TEXT_LEN = 50  # короткі повідомлення — не виписки


# ============================================================
# Кабінеты: клиент (доступ по телефону) / владелец / аноним
# ============================================================
def _workspace(update: Update) -> tuple[dict | None, bool]:
    """(grant|None, is_owner) для текущего чата.

    Клиент: чат привязан к активному доступу по телефону → grant.
    Владелец: чат привязан командой /owner <пароль> → (None, True).
    Аноним: (None, False) — просим номер телефона.
    """
    chat_id = update.effective_chat.id if update.effective_chat else update.message.chat_id
    grant = db_bridge.find_grant_by_telegram(chat_id)
    if grant:
        return grant, False
    return None, db_bridge.is_owner_chat(chat_id)


def _bind_prompt(update: Update):
    """Просит анонима привязать кабинет кнопкой или номером текстом."""
    from telegram import KeyboardButton, ReplyKeyboardMarkup
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Поділитися номером телефону", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True)
    return update.message.reply_text(
        "👋 Привіт! Це твій особистий фінансовий асистент.\n\n"
        "Натисни кнопку нижче або надішли свій номер телефону — створю кабінет "
        "(без паролів і SMS).",
        reply_markup=kb)


# ============================================================
# Команди
# ============================================================
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    grant, is_owner = _workspace(update)
    if grant:
        await update.message.reply_text(
            f"👋 Кабінет: *{grant['label'] or db_bridge.mask_phone(grant['phone'])}*\n\n"
            "📎 Надішли PDF/CSV виписку — або просто запитай текстом "
            "«скільки витратив на рекламу?»\n"
            "Команди: /summary · /accounts · /reset",
            parse_mode=ParseMode.MARKDOWN)
        return
    if is_owner:
        await update.message.reply_text(
            "👑 Режим власника — видно всі рахунки.\n\n"
            "📎 Надішли виписку або запитай щось. Команди: /summary · /report · /accounts")
        return
    await _bind_prompt(update)


async def handle_contact(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Користувач поділився номером (кнопка) → створюємо/прив'язуємо кабінет."""
    chat_id = update.message.chat_id
    raw = update.message.contact.phone_number if update.message.contact else ""
    # Telegram надсилає номер і без «+» (38067…) — нормалізуємо до +380…
    phone = db_bridge.normalize_phone(raw) or raw
    if not phone:
        from telegram import ReplyKeyboardRemove
        await update.message.reply_text(
            "❌ Не вдалося розпізнати номер. Надішли його текстом: +380 67 123 45 67",
            reply_markup=ReplyKeyboardRemove(selective=False))
        return
    grant = db_bridge.ensure_grant(phone)
    grant = db_bridge.bind_telegram(chat_id, grant["phone"]) or grant
    from telegram import ReplyKeyboardRemove
    await update.message.reply_text(
        f"✅ Кабінет прив'язаний: {grant['label'] or db_bridge.mask_phone(grant['phone'])}.\n\n"
        "Тепер надсилай виписки 📎 та питай що завгодно про свої фінанси.",
        reply_markup=ReplyKeyboardRemove(selective=False))


def _first_client_iban(phone: str) -> str:
    """Перший (найстаріший) рахунок клієнта — для доступу без мультирахунку."""
    accs = db_bridge.list_user_accounts(phone)
    if not accs:
        return ""
    return sorted(accs, key=lambda a: a.get("first_seen") or "")[0]["iban"]


def _ws_accounts(update: Update) -> list[dict] | None:
    """Счета кабинета или None, если аноним (тогда нужно просить привязку).

    Клиент — только свои счета (ключ = телефон); без мультирахунку — лише перший.
    Владелец — свои, при пустоте — все счета системы."""
    grant, is_owner = _workspace(update)
    if grant:
        accs = db_bridge.list_user_accounts(grant["phone"])
        if not grant.get("multi_account") and accs:
            single = _first_client_iban(grant["phone"])
            accs = [a for a in accs if a["iban"] == single]
        return accs
    if is_owner:
        return (db_bridge.list_user_accounts(update.message.chat_id)
                or db_bridge.list_user_accounts(""))
    return None


async def cmd_summary(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """ИИ-генерируемая сводка по счетам кабинета."""
    grant, is_owner = _workspace(update)
    if not grant and not is_owner:
        await _bind_prompt(update)
        return
    if grant:
        if grant.get("multi_account"):
            summary_text = db_bridge.get_summary_for_user(grant["phone"])
        else:
            single = _first_client_iban(grant["phone"])
            summary_text = db_bridge.get_summary_by_iban(single) if single else ""
        if not summary_text:
            await update.message.reply_text(
                "У тебе ще немає завантажених виписок. Надішли PDF або CSV виписку 📎"
            )
            return
    else:  # владелец: свои счета, иначе все
        summary_text = (db_bridge.get_summary_for_user(update.message.chat_id)
                        or db_bridge.get_summary_for_user(""))
    # отправим как plain text (без markdown, т.к. в сводке могут быть спецсимволы)
    await update.message.reply_text(summary_text or "Дані поки що порожні.")


async def cmd_accounts(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Список счетов кабинета с краткой статистикой."""
    accounts = _ws_accounts(update)
    if accounts is None:
        await _bind_prompt(update)
        return
    if not accounts:
        await update.message.reply_text(
            "У тебе ще немає рахунків. Надішли виписку (PDF/CSV) 📎"
        )
        return

    from bot.iban import mask
    lines = ["💼 *Твої рахунки:*\n"]
    for a in accounts:
        line = f"• `{mask(a['iban'])}`"
        if a.get("holder"):
            line += f" ({a['holder']})"
        line += f"\n  {a['tx_count']} транзакцій | дохід {int(a['income']):,} | витрати {int(a['expense']):,}".replace(",", " ")
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_reset(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Очищает историю чата (транзакции остаются)."""
    chat_id = update.message.chat_id
    n = db_bridge.clear_chat_history(chat_id)
    await update.message.reply_text(
        f"🧹 Історію чату очищено ({n} повідомлень).\n"
        "Транзакції збережено — їх не видалив."
    )


async def cmd_report(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """P&L-отчёт за прошлый месяц по счетам кабинета (по запросу)."""
    accounts = _ws_accounts(update)
    if accounts is None:
        await _bind_prompt(update)
        return
    if not accounts:
        await update.message.reply_text(
            "У тебе ще немає рахунків з даними. Надішли виписку (PDF/CSV) 📎"
        )
        return

    from server.scheduler import previous_month, build_monthly_report
    month = previous_month()
    for a in accounts:
        text = build_monthly_report(a["iban"], month)
        if text:
            await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            from bot.iban import mask
            await update.message.reply_text(
                f"📭 За {month} по рахунку `{mask(a['iban'])}` транзакцій немає.",
                parse_mode=ParseMode.MARKDOWN,
            )


# ============================================================
# Обработчик ФАЙЛОВ (PDF/CSV виписки)
# ============================================================
async def handle_document(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Принимает PDF/CSV, парсит, извлекает IBAN, категоризует, копит по IBAN."""
    chat_id = update.message.chat_id
    import file_handler as fh

    # кабинет: анонимам — привязка, клиентам без полного уровня — отказ
    grant, is_owner = _workspace(update)
    if not grant and not is_owner:
        await _bind_prompt(update)
        return
    if grant and grant["level"] != "full":
        await update.message.reply_text(
            "🔒 Завантаження виписок доступне лише з повним рівнем доступу.")
        return
    ws_id = grant["phone"] if grant else str(chat_id)
    ws_label = (grant["label"] or db_bridge.mask_phone(grant["phone"])) if grant else None

    processing = await update.message.reply_text(
        "⏳ Завантажую файл і розпізнаю виписку... (PDF — до 2 хв)"
    )

    try:
        # 1. Скачать файл
        content, filename = await fh.download_document(update)
        log.info("chat=%s: отримано файл %s (%d KB)", chat_id, filename, len(content) // 1024)

        # 2. Распарсить в текст
        await processing.edit_text("⏳ Розпізнаю текст виписки...")
        text = fh.parse_to_text(content, filename)

        # 3. Извлечь IBAN
        info = fh.extract_account_info(text)
        iban = info["iban"]
        if not iban:
            try:
                await processing.delete()
            except Exception:
                pass
            await update.message.reply_text(
                "🤔 Не знайшов IBAN у файлі. Введіть номер рахунку вручну\n"
                "(формат UA + 27 цифр):"
            )
            # TODO: можно сохранить ожидание IBAN в state; пока просим прислать текстом
            return

        # 4. Категоризация
        await processing.edit_text(
            f"✅ Рахунок: {info['iban_mask']}\n⏳ Розношу транзакції через AI..."
        )
        deepseek_key = db_bridge.get_deepseek_key()
        categories = db_bridge.get_categories()
        model = db_bridge.get_deepseek_model()
        meta: dict = {}
        transactions = await ai.categorize_async(text, deepseek_key, categories=categories,
                                                 model=model, usage_meta=meta)
        log.info("chat=%s (кабінет %s): розібрано %d транзакцій (iban=%s)",
                 chat_id, ws_id, len(transactions), iban)
        from dashboard import db as dashboard_db
        tg_label = ws_label or (update.message.from_user.username
                                or update.message.from_user.first_name or str(chat_id))
        dashboard_db.log_usage(f"Telegram · {tg_label}", "загрузка виписки", source="telegram",
                               model=meta.get("model", ""),
                               api_calls=meta.get("api_calls", 0),
                               prompt_tokens=meta.get("prompt_tokens", 0),
                               completion_tokens=meta.get("completion_tokens", 0),
                               cached_tokens=meta.get("cached_tokens", 0),
                               seconds=meta.get("seconds", 0.0),
                               details=f"{filename}: {len(transactions)} транзакцій")

        # 5. Сохранить с IBAN (в кабинет: телефон клиента / chat_id владельца)
        username = tg_label
        res = db_bridge.save_result(transactions, ws_id, username, iban=iban) or {}
        db_saved = res.get("session_id") is not None
        inserted = res.get("inserted", len(transactions))
        skipped = res.get("skipped", 0)
        if skipped:
            await processing.edit_text(
                f"💾 Збережено: {inserted} нових транзакцій "
                f"(ще {skipped} дублікатів пропущено — цей файл уже був)")

        # 5б. Автоэкспорт в Google Sheets (если включён в админке; не валит бота)
        sheets_ok = db_bridge.auto_export_to_sheets(transactions)

        # 6. Метрики + аномалии (по всем транзакциях кабинета по этому счёту)
        from dashboard import db as dbmod
        all_tx = dbmod.get_transactions(chat_id=str(ws_id), iban=iban, limit=100000)
        metrics = ai.compute_metrics(all_tx)
        thresholds = db_bridge.get_alert_thresholds()
        anomalies = ai.detect_anomalies(metrics, thresholds)

        # 7. Ответ
        summary = ai.build_summary(metrics, anomalies)
        summary += f"\n\n🏦 Рахунок: `{info['iban_mask']}`"
        summary += f"\n📊 Дашборд: {ai.dashboard_url()}"
        if not db_saved:
            summary += "\n\n⚠️ Не вдалося зберегти (див. логи)."
        if sheets_ok:
            summary += "\n✅ Експортовано у Google Таблицю 📋"

        try:
            await processing.delete()
        except Exception:
            pass
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)

    except fh.FileParseError as e:
        log.warning("chat=%s: помилка файлу: %s", chat_id, e)
        try:
            await processing.delete()
        except Exception:
            pass
        await update.message.reply_text(f"⚠️ {e}")
    except ai.AIError as e:
        log.error("chat=%s: AI помилка: %s", chat_id, e)
        try:
            await processing.delete()
        except Exception:
            pass
        await update.message.reply_text(
            "⚠️ Не вдалося розпізнати транзакції. Перевірте ключ DeepSeek у админці."
        )


# ============================================================
# Обработчик ТЕКСТА = чат с ИИ
# ============================================================
async def handle_chat(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Текст = вопрос к ИИ. Анониму — привязка кабинета по номеру телефона."""
    chat_id = update.message.chat_id
    user_msg = (update.message.text or "").strip()

    if not user_msg:
        return

    # кабинет: аноним → пробуем привязать по номеру в тексте
    grant, is_owner = _workspace(update)
    if not grant and not is_owner:
        norm = db_bridge.normalize_phone(user_msg)
        if norm:
            # локальна редакція: кабінет створюється автоматично
            g = db_bridge.ensure_grant(norm)
            g = db_bridge.bind_telegram(chat_id, g["phone"]) or g
            from telegram import ReplyKeyboardRemove
            await update.message.reply_text(
                f"✅ Кабінет прив'язаний: {g['label'] or db_bridge.mask_phone(g['phone'])}.\n\n"
                "Тепер надсилай виписки 📎 та питай що завгодно про свої фінанси.",
                reply_markup=ReplyKeyboardRemove(selective=False))
        else:
            await _bind_prompt(update)
        return

    if grant and grant["level"] not in ("chat", "full"):
        await update.message.reply_text("🔒 Чат із ШІ у вашому доступі вимкнено (лише перегляд).")
        return

    # ключ кабинета: телефон клиента / chat_id владельца
    key = grant["phone"] if grant else str(chat_id)
    ws_label = (grant["label"] or db_bridge.mask_phone(grant["phone"])) if grant else None

    # сохраняем вопрос в историю
    db_bridge.add_chat_message(key, "user", user_msg)

    # «ШІ думає»: повідомлення редагується з обертовими фразами
    processing = await update.message.reply_text("🧠 Аналізую ваші транзакції…")

    async def _rotate_thinking():
        phrases = ["🔍 Звертаюсь до виписки…", "📊 Порівнюю категорії…",
                   "🧮 Рахую суми…", "✨ Майже готово…"]
        i = 0
        while True:
            await asyncio.sleep(2.4)
            try:
                await processing.edit_text(phrases[i % len(phrases)])
            except Exception:
                return  # повідомлення видалено/флуд-ліміт — тихо зупиняємось
            i += 1

    thinking_task = asyncio.create_task(_rotate_thinking())

    try:
        deepseek_key = db_bridge.get_deepseek_key()
        # живая сводка по счетам кабинета + история + RAG-поиск по вопросу
        if grant and not grant.get("multi_account"):
            # без мультирахунку — лише перший рахунок клієнта
            single = _first_client_iban(key)
            summary_text = db_bridge.get_summary_by_iban(single) if single else ""
            relevant = db_bridge.find_relevant(user_msg, iban=single) if single else ""
        else:
            summary_text = db_bridge.get_summary_for_user(key)
            if not summary_text and is_owner:
                # у владельца виписки могут лежать под другими ключами — показываем все
                summary_text = db_bridge.get_summary_for_user("")
            relevant = db_bridge.find_relevant(user_msg, chat_id=key)
        system_prompt = ai.build_chat_system_prompt(summary_text, relevant=relevant)
        history = db_bridge.get_chat_history(key, limit=10)
        model = db_bridge.get_deepseek_model()

        messages = [{"role": "system", "content": system_prompt}] + history
        meta: dict = {}
        reply = await ai.chat_async(messages, deepseek_key, model=model, usage_meta=meta)
        from dashboard import db as dashboard_db
        tg_label = ws_label or (update.message.from_user.username
                                or update.message.from_user.first_name or str(chat_id))
        dashboard_db.log_usage(f"Telegram · {tg_label}", "чат", source="telegram",
                               model=meta.get("model", ""),
                               api_calls=meta.get("api_calls", 0),
                               prompt_tokens=meta.get("prompt_tokens", 0),
                               completion_tokens=meta.get("completion_tokens", 0),
                               cached_tokens=meta.get("cached_tokens", 0),
                               seconds=meta.get("seconds", 0.0),
                               details=f"питання: {user_msg[:80]}")

        db_bridge.add_chat_message(key, "assistant", reply)

        thinking_task.cancel()
        try:
            await processing.delete()
        except Exception:
            pass
        # жирні цифри (**…** → HTML-жирний)
        safe = (reply.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        import re as _re
        html_reply = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", safe)
        await update.message.reply_text(html_reply, parse_mode=ParseMode.HTML)
    except ai.AIError as e:
        thinking_task.cancel()
        log.error("chat=%s: AI помилка чату: %s", chat_id, e)
        try:
            await processing.delete()
        except Exception:
            pass
    except ai.AIError as e:
        log.error("chat=%s: AI помилка чату: %s", chat_id, e)
        try:
            await processing.delete()
        except Exception:
            pass
        await update.message.reply_text(
            "⚠️ Не вдалося відповісти. Перевірте ключ DeepSeek у адмінці."
        )


# ============================================================
# Запуск
# ============================================================
def build_application() -> Application | None:
    """Создаёт и настраивает Telegram Application (без запуска polling).

    Используется server/app.py для старта бота в lifespan. Возвращает None,
    если токен не задан (бот не стартует, сервер работает).
    """
    db_bridge.init()  # убедимся что БД создана
    token = db_bridge.get_telegram_token()
    if not token:
        log.warning("⚠️ TELEGRAM_BOT_TOKEN не заданий (ні в БД, ні в .env) — бот не стартує")
        return None

    app = Application.builder().token(token).build()
    # команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("report", cmd_report))
    # контакт = привязка кабинета, файл = виписка, текст = чат с ИИ
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_chat))
    return app


def main():
    """Отдельный запуск только бота (без дашборда)."""
    db_bridge.init()
    if not db_bridge.get_deepseek_key():
        log.warning("⚠️ DEEPSEEK_API_KEY не заданий — категоризація не працюватиме!")

    app = build_application()
    if not app:
        sys.exit("❌ TELEGRAM_BOT_TOKEN не заданий (ні в БД, ні в .env)")

    log.info("🤖 Бот запущено. Натисни Ctrl+C для зупинки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
