#!/usr/bin/env bash
# ============================================================
#  AI Фінанси — запуск подвійним кліком (Mac)
#  Перший запуск: збирає контейнер ~5–10 хв. Далі — ~10 секунд.
# ============================================================
cd "$(dirname "$0")"
PORT=13500
URL="http://localhost:$PORT"

# Перевірка Docker
if ! command -v docker >/dev/null 2>&1; then
  echo ""
  echo "❌ Docker не встановлено."
  echo "   1. Встанови Docker Desktop: https://www.docker.com/products/docker-desktop/"
  echo "   2. Запусти Docker Desktop (іконка кита в меню зверху)"
  echo "   3. Запусти цей файл ще раз"
  echo ""
  echo "Натисни Enter щоб закрити..."
  read
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo ""
  echo "⏳ Docker Desktop ще запускається. Дочекайся іконки кита й запусти цей файл ще раз."
  echo ""
  echo "Натисни Enter щоб закрити..."
  read
  exit 1
fi

echo "🚀 Запускаю AI Фінанси... (перший раз збирається ~5–10 хв)"
docker-compose up -d --build 2>/dev/null || docker compose up -d --build

echo -n "⏳ Чекаю на сервер"
for i in $(seq 1 120); do
  if curl -s -o /dev/null "$URL/health" 2>/dev/null; then
    echo ""
    echo "✅ Готово! Відкриваю браузер: $URL"
    open "$URL"
    echo ""
    echo "Наступного разу: подвійний клік по цьому файлу — і система підніметься."
    sleep 2
    exit 0
  fi
  echo -n "."
  sleep 2
done
echo ""
echo "⚠️ Сертер довго не відповідає. Подивіться лог: docker logs ai-finansy"
echo ""
echo "Натисни Enter щоб закрити..."
read
