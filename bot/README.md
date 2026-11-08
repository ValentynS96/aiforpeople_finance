# 📊 AI Фінанси — бот + дашборд (для розробників)

Telegram-бот + веб-дашборд + сторінка налаштувань. Бот читає банківську
виписку, розносить транзакції по категоріях через ШІ (Google Gemini або
DeepSeek — обидва через OpenAI-сумісний API), дашборд показує метрики,
графіки й аномалії в реальному часі.

Без n8n. Один Python-процес. Дані в SQLite.

---

## Архітектура

```
Користувач шле виписку в Telegram або завантажує на сайті
  → ШІ категоризує частинами паралельно (ключ з БД або .env)
  → зберігає в SQLite (db_bridge), ізоляція за owner = номер телефону
  → метрики + аномалії (пороги з налаштувань)
  → відповідь у чат / оновлення дашборда

Дашборд (http://localhost:13500)      Налаштування (/admin, без пароля —
  → KPI + графіки Chart.js              система локальна)
  → фільтри (дата/категорія/рахунок)    → провайдер і модель ШІ
  → чат із ШІ + історія                 → ключі API (зашифровано)
  → перемикач рахунків                  → користувачі, статистика токенів
```

**Один процес**: `uvicorn server.app:app` піднімає і дашборд, і бота
(через lifespan). Бот працює в фоні, сервер обслуговує UI.

---

## Швидкий старт без Docker

```bash
pip install -r bot/requirements.txt
PYTHONPATH=. uvicorn server.app:app --port 13500
```

Ключі можна не тримати в `.env` — усе налаштовується через браузер:
майстер першого запуску сам запитає провайдера (Google/DeepSeek) і ключ.

## `.env` (опційно, для продвинутих)

Див. `bot/.env.example`. Пріоритет: БД (з налаштувань) → `.env`.

## М'яка деградація

- **Немає ключа ШІ** → майстер налаштування при першому вході на сайт
- **Немає Google Service Account** → метрики й алерти працюють, без експорту в Sheets
- **Бот без токена** → дашборд і налаштування працюють

## Структура

```
bot/
  ai_engine.py       провайдери ШІ (Google/DeepSeek) + категоризація частинами
                     + метрики + аномалії + summary + fallback-моделі
  sheets_writer.py   експорт у Google Sheets (gspread)
  db_bridge.py       міст: бот ↔ SQLite (ключі/пороги, транзакції, RAG для чату)
  bot.py             Telegram-обработчик + build_application() для lifespan
dashboard/
  db.py              SQLite: схема, CRUD, фільтри, ізоляція owner
  crypto.py          шифрування секретів (Fernet)
  auth.py            підписані cookie-сесії (IBAN + телефон)
  routes.py          FastAPI: /api/* + /admin/* + /setup
  templates/         dashboard.html, welcome.html, setup.html, admin.html
  static/            style.css, dashboard.js, chat.js
server/
  app.py             FastAPI + lifespan (бот) + StaticFiles + маршрути
  runtime.py         запуск/перезапуск бота без рестарту контейнера
```
