@echo off
chcp 65001 >nul
REM Запуск AI Фiнанси — просто двойной клик (Windows).
cd /d "%~dp0"
echo Запускаю AI Фiнанси... (первая сборка ~5-10 мин)
docker-compose up -d --build
if errorlevel 1 docker compose up -d --build
echo.
echo Готово! Открой в браузере: http://localhost:8000
echo Адмiнка: http://localhost:8000/admin (пароль з твого .env)
echo.
pause
