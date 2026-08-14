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
