"""Runtime-управление Telegram-ботом и планировщиком (без циклических импортов).

start_bot/stop_bot/restart_bot вызываются:
  - из server/app.py lifespan (при старте/остановке сервера),
  - из dashboard/routes.py (после ввода токена через setup-мастер или админку —
    бот поднимается с новым токеном без перезапуска контейнера).
"""

import logging

_log = logging.getLogger("runtime")

bot_app = None  # telegram.ext.Application | None


async def start_bot() -> bool:
    """Собирает и запускает бота (токен из БД с fallback .env).
    True если запущен. Обновляет runtime.bot_app."""
    global bot_app
    from bot.bot import build_application
    try:
        app = build_application()
        if not app:
            _log.info("бот не запущен: токен не заданий (БД/ env порожні)")
            return False
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        bot_app = app
        _log.info("🤖 Telegram-бот запущений")
        _sync_scheduler()
        return True
    except Exception as e:
        _log.warning("⚠️ Не вдалося запустити бота: %s", e)
        bot_app = None
        return False


async def stop_bot():
    """Аккуратно останавливает бота (если запущен)."""
    global bot_app
    if not bot_app:
        return
    try:
        await bot_app.updater.stop()
        await bot_app.stop()
        await bot_app.shutdown()
    except Exception as e:
        _log.debug("stop_bot: %s", e)
    bot_app = None


async def restart_bot() -> bool:
    """Перезапуск с актуальным токеном из БД (после сохранения через UI)."""
    await stop_bot()
    return await start_bot()


def _sync_scheduler():
    """Привязывает планировщик месячных отчётов к текущему боту
    (или останавливает, если бота нет)."""
    try:
        from server.scheduler import start_scheduler, stop_scheduler
        stop_scheduler()
        if bot_app:
            start_scheduler(bot_app)
    except Exception as e:
        _log.debug("scheduler sync: %s", e)
