"""
Мост между ботом и SQLite-хранилищем дашборда.

Бот вызывает save_result() после обработки выписки — транзакции и сессия
попадают в SQLite, и дашборд мгновенно (без рестарта) видит свежие данные.

Также предоставляет чтение ключей/настроек из БД с фоллбэком на .env —
чтобы бот работал и через админку, и через .env (обратная совместимость).
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# Добавляем корень проекта в path, чтобы импорт dashboard был доступен из bot/
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dashboard import db  # noqa: E402

_log = logging.getLogger("db_bridge")


def init() -> None:
    """Инициализация БД. Вызывается при старте бота."""
    db.init_db()
    _log.info("SQLite готов: %s", db.DB_PATH)


def _iso_week(date_str: str) -> str:
    """'2026-07-05' → '2026-W27'."""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    except (ValueError, TypeError):
        return "unknown"


def save_result(transactions: list[dict], chat_id: str, username: str = "",
                iban: str = "") -> int | None:
    """Сохраняет результат обработки выписки в БД.

    iban: если задан — регистрирует счёт (привязка к chat_id) и проставляет
    всем транзакциям. Возвращает session_id или None при ошибке.
    """
    if not transactions:
        return None
    try:
        chat_id = str(chat_id)
        sid = db.create_session(chat_id, username or "")
        enriched = []
        for t in transactions:
            row = dict(t)
            row.setdefault("week", _iso_week(t.get("date", "")))
            row.setdefault("country", "UA")  # AI определяет; дефолт UA
            if iban:
                row["iban"] = iban
            enriched.append(row)
        db.add_transactions(sid, enriched, iban=iban)
        if iban:
            db.register_account(chat_id, iban)
        _log.info("Сессия %s: сохранено %d транзакций (iban=%s)", sid, len(enriched), iban or "—")
        return sid
    except Exception as e:
        _log.error("Не удалось сохранить в БД: %s", e)
        return None


# ============================================================
# Сводка по счетам пользователя (для контекста ИИ-чата)
# ============================================================
# Стоп-слова для извлечения поискового запроса из вопроса пользователя
_STOPWORDS = {
    "скільки", "сколька", "я", "мені", "ми", "він", "вона", "вони", "це", "що", "як",
    "де", "кому", "чому", "коли", "чи", "а", "і", "й", "та", "в", "у", "на", "з",
    "із", "зі", "до", "за", "по", "від", "для", "про", "при", "без", "через",
    "заплатив", "заплатила", "платив", "платила", "заплатити", "сплатив", "сплачував",
    "витратив", "витратила", "витратив", "отримав", "отримала", "приніс", "принесла",
    "принесли", "дав", "дала", "надав", "надала", "прийшов", "прийшла", "пришло",
    "сума", "суму", "суми", "всього", "загалом", "разом", "був", "була", "були",
    "є", "нема", "немає", "мій", "моя", "мої", "могли", "можеш", "можна",
    "витрата", "витрати", "надходження", "платіж", "платежі", "категорія",
    "категорії", "контрагент", "контрагенти", "бізнес", "рахунок", "виписка",
    "скажи", "покажи", "порахуй", "рахуй", "треба", "хочу", "дізнатись", "дізнатися",
}

# Синоніми для пошуку: як користувач пише → як названо у виписці.
# У виписках бренди латинкою (Zoom, SWIFT), а питання — кирилицею/скороченнями.
_QUERY_ALIASES = {
    "зум": "zoom",
    "свіфт": "swift",
    "єп": "єдиний податок",
    "есв": "єсв",
    "гугл": "google",
    "мета": "meta",
    "дропбокс": "dropbox",
    "ноушн": "notion",
    "апл": "apple",
}


def extract_query(text: str) -> str:
    """Из вопроса пользователя извлекает значимые токены для поиска.

    «скільки я заплатив військового збору» → «військового збору»
    «а тут скільки витратив Кур'єрська доставка #3397» → «курєрська доставка 3397»
    """
    import re
    words = re.findall(r"[\w#]+", text.lower())
    tokens = []
    for w in words:
        w2 = w.lstrip("#")
        # числа важны (номера клиентов/доставок: 3397), стоп-слова и 1-буквенные — нет
        if w2.isdigit() or (w not in _STOPWORDS and len(w2) > 1):
            tokens.append(w2)
    return " ".join(tokens)


def mask_iban(iban: str) -> str:
    """UA4421334422…7891 → UA44••••••••7891 (для позначок у промпті)."""
    from bot.iban import mask
    return mask(iban)


def _detect_account(query: str, ibans: list[str]) -> str | None:
    """Шукає вказівку на конкретний рахунок у питанні користувача.

    Розпізнає повний IBAN («по UA442133…») та останні 4 цифри
    («по рахунку 7891»). Останні-4 зараховуємо лише коли в питанні є
    слово «рахун…», щоб не плутати з номерами транзакцій (#9923).
    """
    import re
    squeezed = re.sub(r"\s+", "", query).lower()
    mentions_account = "рахун" in query.lower()
    for ib in ibans:
        if ib.lower() in squeezed:                # повний IBAN без пробілів
            return ib
        if mentions_account and ib[-4:] in re.findall(r"\d{4}", query):
            return ib
    return None


def find_relevant(query: str, iban: str = "", chat_id: str = "", limit: int = 30) -> str:
    """FTS5-поиск транзакций по словам вопроса → блок для промпта чата (RAG).

    Возвращает '' если запрос пустой или ничего не нашлось.
    Агрегирует по описанию: «Оплата від клієнта #5619: 2 плат., надходження 68 340 грн».
    """
    q = extract_query(query)
    if not q or len(q) < 3:
        return ""
    # розширюємо синонімами («зум» → zoom, «єп» → «єдиний податок»):
    # інакше FTS не збереже різні алфавіти/скорочення
    words = q.split()
    extra = [a for a in (_QUERY_ALIASES.get(w, "") for w in words) if a]
    if extra:
        q = " ".join(words + extra)
    from bot import ai_engine as ai

    ibans = [iban] if iban and iban != "all" else []
    if not ibans and chat_id:
        ibans = [a["iban"] for a in db.list_accounts(chat_id=str(chat_id))]
    if not ibans:
        ibans = [""]

    # вказівка на конкретний рахунок у питанні → шукаємо тільки по ньому
    scope_note = ""
    if len(ibans) > 1:
        detected = _detect_account(query, ibans)
        if detected:
            ibans = [detected]
            scope_note = (f"КОРИСТУВАЧ ПИТАЄ ЛИШЕ ПРО РАХУНОК {mask_iban(detected)}. "
                          f"Дані нижче — тільки по ньому.\n")

    agg: dict[tuple, dict] = {}
    multi = len(ibans) > 1
    for ib in ibans:
        try:
            # stems+OR: нечёткий поиск — стволы слов, хотя бы одно совпадение
            # («військового збору» → військ* OR збору* → «Військовий збір»)
            for t in db.search_transactions(q, iban=ib, limit=100, stems=True, op="OR"):
                d = t["description"] or "—"
                key = (d, ib if multi else "")    # окремі рядки по рахунках, якщо їх кілька
                if key not in agg:
                    agg[key] = {"count": 0, "income": 0.0, "expense": 0.0,
                                "category": t.get("category", ""), "iban": ib}
                amt = t.get("amount") or 0
                agg[key]["count"] += 1
                if amt > 0:
                    agg[key]["income"] += amt
                else:
                    agg[key]["expense"] += abs(amt)
        except Exception:
            continue

    if not agg:
        return ""
    lines = []
    for (d, _), v in list(agg.items())[:limit]:
        label = f"[{mask_iban(v['iban'])}] " if multi else ""
        parts = [f"- {label}{d} (категорія: {v['category'] or '—'}): {v['count']} плат."]
        if v["income"] > 0:
            parts.append(f"надходження {ai.fmt(v['income'])} грн")
        if v["expense"] > 0:
            parts.append(f"списання {ai.fmt(v['expense'])} грн")
        lines.append(", ".join(parts))
    return (scope_note +
            "ЗНАЙДЕНО ЗА ЗАПИТОМ КОРИСТУВАЧА (це точні дані з транзакцій):\n  " + "\n  ".join(lines))


def _counterparties_block(iban: str, max_lines: int = 40) -> str:
    """Блок «контрагенты» для контекста чата: топ по обороту.

    Даёт ИИ возможность отвечать на вопросы про конкретных клиентов/продавцов
    («сколько принёс клиент #5619», «кому я больше плачу»).
    """
    from bot import ai_engine as ai
    cps = db.get_counterparties(iban=iban, limit=60)
    if not cps:
        return ""
    lines = []
    for c in cps[:max_lines]:
        parts = [f"- {c['description']}: {c['count']} плат."]
        if c["income"] > 0:
            parts.append(f"надходження {ai.fmt(c['income'])} грн")
        if c["expense"] > 0:
            parts.append(f"списання {ai.fmt(c['expense'])} грн")
        lines.append(", ".join(parts))
    hidden = len(cps) - len(lines)
    suffix = f"\n  (…іще {hidden} дрібніших)" if hidden > 0 else ""
    return "  Контрагенти (агреговано за описом):\n    " + "\n    ".join(lines) + suffix + "\n"



def _months_block(txs: list[dict], max_months: int = 12) -> str:
    """Помісячна розбивка: дохід/витрати + топ-3 категорії витрат кожного місяця.

    Закриває питання «порівняй місяці» і «топ трати по місяцях»."""
    from bot import ai_engine as ai
    by_month: dict[str, dict] = {}
    for t in txs:
        ym = (t.get("date") or "")[:7]
        if len(ym) == 7:
            m = by_month.setdefault(ym, {"inc": 0.0, "exp": 0.0, "cat": {}})
            amt = t.get("amount") or 0
            if amt > 0:
                m["inc"] += amt
            else:
                m["exp"] += abs(amt)
                cat = t.get("category") or "інше"
                m["cat"][cat] = m["cat"].get(cat, 0.0) + abs(amt)
    if not by_month:
        return ""
    items = sorted(by_month.items())[-max_months:]
    parts = []
    for ym, v in items:
        top = sorted(v["cat"].items(), key=lambda x: -x[1])[:3]
        top_s = ", ".join(f"{c} {ai.fmt(s)}" for c, s in top) if top else "—"
        parts.append(f"{ym}: +{ai.fmt(v['inc'])}/−{ai.fmt(v['exp'])} (топ: {top_s})")
    return "  По місяцях (дохід/витрати + топ-категорії витрат): " + "; ".join(parts) + "\n"

def get_summary_for_user(chat_id: str) -> str:
    """Текст-сводка по всем счетам пользователя. Для системного промпта ИИ-чата.

    Формат:
        Рахунок UA12••••2345:
          Виручка: 753 691 | Витрати: 2 366 694 | Прибуток: −1 613 003 грн
          Топ-витрати: зарплата (30%), реклама (15%)
          ⚠️ Аномалії: касовий розрив
    """
    chat_id = str(chat_id)
    accounts = db.account_stats(chat_id)
    if not accounts:
        return ""

    from bot.iban import mask
    parts = []
    for a in accounts:
        txs = db.get_transactions(chat_id=chat_id, iban=a["iban"], limit=100000)
        if not txs:
            continue
        # переиспользуем движок метрик
        from bot import ai_engine as ai
        metrics = ai.compute_metrics(txs)
        thresholds = get_alert_thresholds()
        anomalies = ai.detect_anomalies(metrics, thresholds)

        income = ai.fmt(metrics["income"])
        expense = ai.fmt(metrics["expenses"])
        profit = ai.fmt(metrics["profit"])
        line = (
            f"Рахунок {mask(a['iban'])}"
            + (f" ({a['holder']})" if a.get("holder") else "")
            + f":\n"
            f"  Период: {txs[-1]['date']} — {txs[0]['date']}\n"
            f"  Виручка: {income} | Витрати: {expense} | Прибуток: {profit} грн\n"
            f"  Транзакцій: {metrics['tx_count']}\n"
        )
        # помісячна розбивка — для питань «порівняй місяці»
        line += _months_block(txs)
        # контрагенты (для вопросов про конкретных клиентов)
        line += _counterparties_block(a["iban"])
        # топ-3 категорії витрат
        by_cat = [(c, v["expense"]) for c, v in metrics["by_cat"].items() if v["expense"] > 0]
        by_cat.sort(key=lambda x: x[1], reverse=True)
        if by_cat:
            total_exp = metrics["expenses"] or 1
            top3 = ", ".join(f"{c} ({int(v/total_exp*100)}%)" for c, v in by_cat[:3])
            line += f"  Топ-витрати: {top3}\n"
        if anomalies["count"]:
            # краткая версия без markdown-звёздочек и заголовка
            alerts_short = anomalies["message"]
            # убираем заголовок и эмодзи
            for prefix in ["🚨 *Аномалія виявлена ДО того, як потрапить у звіт:*\n\n",
                           "🚨 Аномалія виявлена ДО того, як потрапить у звіт:\n\n"]:
                alerts_short = alerts_short.replace(prefix, "")
            alerts_short = alerts_short.replace("*", "").strip()
            line += f"  ⚠️ Аномалії: {alerts_short[:200]}\n"
        parts.append(line)

    return "\n".join(parts)


# ============================================================
# Bridge к chat_history (память ИИ)
# ============================================================
def get_chat_history(chat_id: str, limit: int = 10, session_id: int | None = None) -> list[dict]:
    return db.get_chat_history(str(chat_id), limit, session_id=session_id)


def add_chat_message(chat_id: str, role: str, content: str, session_id: int | None = None):
    db.add_chat_message(str(chat_id), role, content, session_id=session_id)


def clear_chat_history(chat_id: str, session_id: int | None = None) -> int:
    return db.clear_chat_history(str(chat_id), session_id=session_id)


def list_user_accounts(chat_id: str) -> list[dict]:
    """Счета пользователя с метриками (для /accounts)."""
    return db.account_stats(str(chat_id))


def get_summary_by_iban(iban: str) -> str:
    """Текст-сводка по конкретному IBAN (для веб-чата в дашборде).

    Аналог get_summary_for_user, но фильтрует по IBAN напрямую (без chat_id).
    Используется /api/chat, где пользователь идентифицируется cookie-IBAN.
    """
    if not iban:
        return ""
    txs = db.get_transactions(iban=iban, limit=100000)
    if not txs:
        return ""

    from bot.iban import mask
    from bot import ai_engine as ai

    metrics = ai.compute_metrics(txs)
    thresholds = get_alert_thresholds()
    anomalies = ai.detect_anomalies(metrics, thresholds)

    # holder из accounts (если есть)
    holder = ""
    accts = db.list_accounts()
    for a in accts:
        if a["iban"] == iban:
            holder = a.get("holder") or ""
            break

    income = ai.fmt(metrics["income"])
    expense = ai.fmt(metrics["expenses"])
    profit = ai.fmt(metrics["profit"])
    line = (
        f"Рахунок {mask(iban)}"
        + (f" ({holder})" if holder else "")
        + f":\n"
        f"  Період: {txs[-1]['date']} — {txs[0]['date']}\n"
        f"  Виручка: {income} | Витрати: {expense} | Прибуток: {profit} грн\n"
        f"  Транзакцій: {metrics['tx_count']}\n"
    )
    line += _months_block(txs)
    # контрагенты (для вопросов про конкретных клиентов)
    line += _counterparties_block(iban)
    by_cat = [(c, v["expense"]) for c, v in metrics["by_cat"].items() if v["expense"] > 0]
    by_cat.sort(key=lambda x: x[1], reverse=True)
    if by_cat:
        total_exp = metrics["expenses"] or 1
        top3 = ", ".join(f"{c} ({int(v/total_exp*100)}%)" for c, v in by_cat[:3])
        line += f"  Топ-витрати: {top3}\n"
    if anomalies["count"]:
        alerts_short = anomalies["message"]
        for prefix in ["🚨 *Аномалія виявлена ДО того, як потрапить у звіт:*\n\n",
                       "🚨 Аномалія виявлена ДО того, як потрапить у звіт:\n\n"]:
            alerts_short = alerts_short.replace(prefix, "")
        alerts_short = alerts_short.replace("*", "").strip()
        line += f"  ⚠️ Аномалії: {alerts_short[:200]}\n"
    return line


# ============================================================
# Чтение конфига с фоллбэком на .env
# ============================================================
def get_deepseek_key() -> str:
    """DeepSeek-ключ: БД → .env. БД приоритетнее (админка)."""
    key = db.get_secret("deepseek_key")
    return key or os.getenv("DEEPSEEK_API_KEY", "")


def get_telegram_token() -> str:
    """Telegram-токен: БД → .env."""
    tok = db.get_secret("telegram_token")
    return tok or os.getenv("TELEGRAM_BOT_TOKEN", "")


def get_alert_thresholds() -> dict:
    """Пороги алертов из БД (или дефолт)."""
    return db.get_setting("alert_thresholds", db.DEFAULT_SETTINGS["alert_thresholds"])


def get_categories() -> list[str]:
    """Список категорий из БД (или дефолт)."""
    return db.get_setting("categories", db.DEFAULT_SETTINGS["categories"])


def get_deepseek_model() -> str:
    """Модель DeepSeek из БД (или безопасный дефолт)."""
    from bot import ai_engine
    return db.get_setting("deepseek_model", "") or ai_engine.DEEPSEEK_DEFAULT_MODEL


def set_deepseek_model(model: str):
    """Сохранить выбранную модель в БД."""
    db.set_setting("deepseek_model", model)


def sheets_export_enabled() -> bool:
    """Включён ли экспорт в Google Sheets."""
    return db.get_sheets_config().get("enabled", False)


def auto_export_to_sheets(transactions: list[dict]) -> bool:
    """Автоэкспорт транзакций в Google Sheets (если включён в админке).

    Читает sheet_id + расшифрованные credentials из БД. Возвращает True при успехе,
    False при сбое/отключении. НЕ бросает исключения — не должен валить загрузку.
    """
    try:
        cfg = db.get_sheets_config()
        if not cfg.get("enabled") or not cfg.get("sheet_id"):
            return False
        creds_json = db.get_sheets_credentials()
        if not creds_json:
            _log.warning("Sheets: экспорт включён, но Service Account JSON не задан")
            return False
        import json as _json
        creds_info = _json.loads(creds_json)
        import sheets_writer
        sheets_writer.append_transactions(transactions,
                                          sheet_id=cfg["sheet_id"],
                                          credentials_info=creds_info)
        _log.info("Sheets: експортовано %d транзакцій", len(transactions))

        # обновляем P&L: для КАЖДОГО месяца в транзакциях — отдельная вкладка
        try:
            from datetime import datetime as _dt
            # определяем уникальные месяцы из загруженных транзакций
            months = set()
            for t in transactions:
                try:
                    d = _dt.strptime(str(t.get("date", ""))[:10], "%Y-%m-%d")
                    months.add((d.month, d.year))
                except (ValueError, TypeError):
                    continue

            # P&L-обновление временно отключено (метрики требуют доработки маппинга).
            # Экспорт деталей (append_transactions) уже выполнён выше.
            # TODO: включить после тестирования маппинга категорий → P&L.
            # for month in sorted(months):
            #     ...
        except Exception as pe:
            _log.warning("Sheets P&L: не оновлено (не ламаю експорт): %s", pe)

        return True
    except Exception as e:
        _log.warning("Sheets: экспорт не вдався (не ламаю основну обробку): %s", e)
        return False


# ============================================================
# Доступы-кабинеты по телефону (для привязки Telegram-чата)
# ============================================================
def find_grant_by_telegram(chat_id) -> dict | None:
    """Активный доступ клиента, привязанный к этому Telegram-чату."""
    return db.find_grant_by_telegram(chat_id)


def bind_telegram(chat_id, phone: str) -> dict | None:
    """Привязывает чат к кабинету по телефону (если доступ активен)."""
    return db.bind_telegram(chat_id, phone)


def is_owner_chat(chat_id) -> bool:
    """Чат владельца (привязан командой /owner с паролем админки)."""
    return str(chat_id) in db.owner_telegram_chats()


def add_owner_chat(chat_id) -> bool:
    return db.add_owner_telegram_chat(chat_id)


def normalize_phone(raw: str) -> str:
    return db.normalize_phone(raw)


def mask_phone(phone: str) -> str:
    return db.mask_phone(phone)


GRANT_LEVELS = db.GRANT_LEVELS
