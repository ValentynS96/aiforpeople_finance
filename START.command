#!/usr/bin/env bash
# Запуск AI Фінанси — просто двойной клик по этому файлу (Mac).
cd "$(dirname "$0")"
echo "🚀 Запускаю AI Фінанси... (первая сборка ~5-10 мин)"
docker-compose up -d --build 2>/dev/null || docker compose up -d --build
echo ""
echo "✅ Готово! Открой в браузере: http://localhost:8000"
echo "   Админка: http://localhost:8000/admin (пароль из твоего .env)"
echo ""
echo "Нажми Enter чтобы закрыть окно..."
read
