"""Runtime-ссылки на живые объекты процесса (без циклических импортов).

server/app.py пишет сюда ссылку на Telegram Application в lifespan;
dashboard/routes.py и scheduler читают её без импорта server.app.
"""

bot_app = None  # telegram.ext.Application | None
