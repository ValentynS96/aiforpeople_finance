"""
Планировщик автоматизаций (APScheduler): ежемесячный P&L-отчёт в Telegram.

Расписание: 1-го числа каждого месяца в 09:00 — отчёт за прошедший месяц
по каждому счёту (accounts: chat_id ↔ iban). Если транзакций за месяц нет —
пропускаем счёт молча.

Ручной запуск (для демо/админки): send_monthly_report(bot_app) шлёт отчёт
за последний ЗАКРЫТЫЙ месяц всем счетам с данными немедленно.

Бот передаётся снаружи (server/app.py lifespan) — планировщик не знает,
как строить Application.
"""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

_log = logging.getLogger("scheduler")

_scheduler: AsyncIOScheduler | None = None

# Автоматичний щомісячний звіт тимчасово вимкнено. True — увімкнути знову.
MONTHLY_REPORT_ENABLED = False


def previous_month(now: datetime | None = None) -> str:
    """'YYYY-MM' прошедшего месяца (15.08 → '2026-07', 01.01 → предыдущий год)."""
    now = now or datetime.now(timezone.utc)
    y, m = now.year, now.month
    m -= 1
    if m == 0:
        m, y = 12, y - 1
    return f"{y:04d}-{m:02d}"


def build_monthly_report(iban: str, month: str) -> str | None:
    """Собирает текст P&L-отчёта по счёту за месяц. None если данных нет."""
    from dashboard import db as dbmod

    txs = dbmod.get_transactions(iban=iban, month=month, limit=100000)
    if not txs:
        return None

    from bot import ai_engine as ai, db_bridge
    metrics = ai.compute_metrics(txs)
    thresholds = db_bridge.get_alert_thresholds()
    anomalies = ai.detect_anomalies(metrics, thresholds)

    from bot.iban import mask
    text = (
        f"📊 *Місячний звіт P&L — {month}*\n"
        f"🏦 Рахунок: `{mask(iban)}`\n\n"
        f"{ai.build_summary(metrics, anomalies)}"
    )
    return text


async def send_monthly_report(bot_app) -> dict:
    """Шлёт отчёт за прошедший месяц всем счетам с данными.

    Возвращает {'sent': N, 'skipped': M} для UI/логов.
    Одна ошибка доставки не валит остальные.
    """
    from dashboard import db as dbmod
    from telegram.constants import ParseMode

    month = previous_month()
    sent = skipped = failed = 0
    for acc in dbmod.list_accounts():
        try:
            text = build_monthly_report(acc["iban"], month)
            if not text:
                skipped += 1
                continue
            await bot_app.bot.send_message(
                chat_id=acc["chat_id"], text=text, parse_mode=ParseMode.MARKDOWN,
            )
            sent += 1
        except Exception as e:
            failed += 1
            _log.error("звіт %s (chat=%s): %s", acc["iban"], acc["chat_id"], e)
    _log.info("місячні звіти %s: надіслано=%d, пропущено=%d, помилок=%d",
              month, sent, skipped, failed)
    return {"sent": sent, "skipped": skipped, "failed": failed, "month": month}


def start_scheduler(bot_app) -> AsyncIOScheduler | None:
    """Стартует планировщик (1-е число, 09:00). None если APScheduler недоступен."""
    global _scheduler
    if not MONTHLY_REPORT_ENABLED:
        _log.info("📅 Щомісячний звіт вимкнено (MONTHLY_REPORT_ENABLED = False)")
        return None
    try:
        _scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
        _scheduler.add_job(
            send_monthly_report,
            trigger=CronTrigger(day=1, hour=9, minute=0),
            args=[bot_app],
            id="monthly_report",
            name="Щомісячний P&L у Telegram",
            replace_existing=True,
        )
        _scheduler.start()
        _log.info("📅 Планувальник запущено: місячний звіт — 1-го числа, 09:00 (Київ)")
        return _scheduler
    except Exception as e:
        _log.warning("⚠️ Планувальник не запущено: %s", e)
        return None


def stop_scheduler():
    global _scheduler
    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None


def next_run_info() -> str:
    """Когда следующий запуск (для показа в админке)."""
    if not _scheduler:
        return "не запущено"
    job = _scheduler.get_job("monthly_report")
    if not job or not job.next_run_time:
        return "невідомо"
    return job.next_run_time.strftime("%d.%m.%Y %H:%M")
