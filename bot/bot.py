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
log = logging.getLogger("webinar-bot")

MIN_TEXT_LEN = 50  # короткі повідомлення — не виписки


# ============================================================
# Команди
# ============================================================
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Привіт! Я — AI-фінансовий радник.\n\n"
        "📊 *Що я вмію:*\n"
        "  • Надішли *PDF або CSV виписку* — рознесу по категоріях через AI\n"
        "  • Запиши *питання текстом* — відповім по твоїх даних\n"
        "  • Дані копляться по *рахунку (IBAN)* — завантажуй скільки завгодно\n\n"
        "*Команди:*\n"
        "  /summary — зведення по всіх рахунках\n"
        "  /report — P&L-звіт за минулий місяць\n"
        "  /accounts — список твоїх рахунків\n"
        "  /reset — очистити історію чату\n\n"
        "Просто скинь файл виписки або напиши питання 🙂",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_summary(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """ИИ-генерируемая сводка по всем счетам пользователя."""
    chat_id = update.message.chat_id
    accounts = db_bridge.list_user_accounts(chat_id)
    if not accounts:
        await update.message.reply_text(
            "У тебе ще немає завантажених виписок. Надішли PDF або CSV виписку 📎"
        )
        return
    summary_text = db_bridge.get_summary_for_user(chat_id)
    # отправим как plain text (без markdown, т.к. в сводке могут быть спецсимволы)
    await update.message.reply_text(summary_text or "Дані поки що порожні.")


async def cmd_accounts(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Список счетов пользователя с краткой статистикой."""
    chat_id = update.message.chat_id
    accounts = db_bridge.list_user_accounts(chat_id)
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
    """P&L-отчёт за прошлый месяц по всем счетам пользователя (по запросу)."""
    chat_id = update.message.chat_id
    accounts = db_bridge.list_user_accounts(chat_id)
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

    processing = await update.message.reply_text(
        "⏳ Завантажую файл і розпізнаю виписку... (PDF — до 1 хв)"
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
        transactions = await ai.categorize_async(text, deepseek_key, categories=categories, model=model)
        log.info("chat=%s: розібрано %d транзакцій (iban=%s)", chat_id, len(transactions), iban)

        # 5. Сохранить с IBAN
        username = update.message.from_user.username or update.message.from_user.first_name or ""
        session_id = db_bridge.save_result(transactions, chat_id, username, iban=iban)
        db_saved = session_id is not None

        # 5б. Автоэкспорт в Google Sheets (если включён в админке; не валит бота)
        sheets_ok = db_bridge.auto_export_to_sheets(transactions)

        # 6. Метрики + аномалии (по всем транзакциям этого счёта, накопительно)
        from dashboard import db as dbmod
        all_tx = dbmod.get_transactions(chat_id=str(chat_id), iban=iban, limit=100000)
        metrics = ai.compute_metrics(all_tx)
        thresholds = db_bridge.get_alert_thresholds()
        anomalies = ai.detect_anomalies(metrics, thresholds)

        # 7. Ответ
        summary = ai.build_summary(metrics, anomalies)
        summary += f"\n\n🏦 Рахунок: `{info['iban_mask']}`"
        summary += f"\n📊 Дашборд: http://localhost:8000"
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
    """Текст = вопрос к ИИ. ИИ отвечает по данным пользователя (живая сводка)."""
    chat_id = update.message.chat_id
    user_msg = (update.message.text or "").strip()

    if not user_msg:
        return

    # сохраняем вопрос в историю
    db_bridge.add_chat_message(chat_id, "user", user_msg)

    processing = await update.message.reply_text("🤔 Думаю...")

    try:
        deepseek_key = db_bridge.get_deepseek_key()
        # живая сводка по счетам + история
        summary_text = db_bridge.get_summary_for_user(chat_id)
        system_prompt = ai.build_chat_system_prompt(summary_text)
        history = db_bridge.get_chat_history(chat_id, limit=10)
        model = db_bridge.get_deepseek_model()

        messages = [{"role": "system", "content": system_prompt}] + history
        reply = await ai.chat_async(messages, deepseek_key, model=model)

        db_bridge.add_chat_message(chat_id, "assistant", reply)

        try:
            await processing.delete()
        except Exception:
            pass
        await update.message.reply_text(reply)
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
    # файл = виписка, текст = чат с ИИ
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
