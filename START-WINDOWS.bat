@echo off
chcp 65001 >nul
title AI Фінанси — запуск
cd /d "%~dp0"

REM ============================================================
REM  AI Фінанси — запуск подвійним кліком (Windows)
REM  Перший запуск: збирає контейнер ~5-10 хв. Далі — ~10 секунд.
REM ============================================================
set PORT=13500
set URL=http://localhost:%PORT%

docker --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo ❌ Docker не встановлено.
  echo    1. Встанови Docker Desktop: https://www.docker.com/products/docker-desktop/
  echo    2. Запусти Docker Desktop
  echo    3. Запусти цей файл ще раз
  echo.
  pause
  exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
  echo.
  echo ⏳ Docker Desktop ще запускається. Дочекайся його запуску й відкрий цей файл ще раз.
  echo.
  pause
  exit /b 1
)

echo 🚀 Запускаю AI Фінанси... (перший раз збирається ~5-10 хв)
docker compose up -d --build
if errorlevel 1 (
  docker-compose up -d --build
)

echo ⏳ Чекаю на сервер...
timeout /t 10 /nobreak >nul

set /a tries=0
:waitloop
curl -s -o nul "%URL%/health" 2>nul
if not errorlevel 1 goto ready
set /a tries+=1
if %tries% geq 60 goto slow
timeout /t 2 /nobreak >nul
goto waitloop

:ready
echo.
echo ✅ Готово! Відкриваю браузер: %URL%
start "" "%URL%"
echo.
echo Наступного разу: подвійний клік по цьому файлу — і система підніметься.
timeout /t 3 /nobreak >nul
exit /b 0

:slow
echo.
echo ⚠️ Сервер довго не відповідає. Подивіться лог: docker logs ai-finansy
echo.
pause
