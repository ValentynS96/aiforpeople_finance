# 💼 AI Фінанси — установка за 3 кроки

## Крок 1. Встанови Docker Desktop (один раз)

Скачай з [docker.com](https://www.docker.com/products/docker-desktop/),
встанови та **запусти** (кит у треї має бути активним).

## Крок 2. Заполни `.env`

Скопіюй приклад конфігу:

```bash
cp bot/.env.example .env
```

Відкрий файл **`.env`** у корні текстовим редактором (Блокнот / TextEdit)
та заполни 4 рядки:

```env
ADMIN_PASSWORD=придумай_пароль_адмінки
SECRET_KEY=будь-яка_випадкова_строка
TELEGRAM_BOT_TOKEN=токен_від_BotFather
DEEPSEEK_API_KEY=ключ_з_platform.deepseek.com
```

**Де взяти ключі:**

| Ключ | Де отримати |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram → [@BotFather](https://t.me/BotFather) → `/newbot` → скопіюй токен `123456:AAE...` |
| `DEEPSEEK_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com/) → API Keys → Create key → `sk-...` (поповни баланс на $2–5) |
| `ADMIN_PASSWORD` | Придумай сам — це пароль входу в http://localhost:8000/admin |
| `SECRET_KEY` | Будь-яка випадкова строка (набери 15+ символів) |

## Крок 3. Запусти

**Найпростіше — подвійний клік:**
- 🖱 Mac → файл `START.command`
- 🖱 Windows → файл `START-WINDOWS.bat`

**Або однією командою в терміналі** (у папці проєкту):

```bash
docker-compose up -d --build
```

Перша збірка триває 5–10 хвилин. Коли закінчиться —

| Адреса | Що це |
|---|---|
| http://localhost:8000 | 📊 Дашборд |
| http://localhost:8000/admin | ⚙️ Адмінка |
| твій бот у Telegram | 🤖 Надішли йому PDF/CSV виписку |

## Перевірка що все працює

1. У Telegram надішли боту `/start`
2. Надішли будь-яку PDF або CSV виписку
3. Через 10–30 сек бот надішле зведення 📊
4. Відкрий http://localhost:8000 → введи IBAN рахунку → дашборд готовий

## Часті питання

**Зупинити / запустити:**
```bash
docker-compose stop    # зупинити
docker-compose start   # запустити
```

**Де мої дані?** У файлі `data/data.db` (створиться після першого запуску). Бекап = скопіювати цей файл.

**Бот не відповідає?** Перевір що Docker Desktop запущений, потім: `docker-compose logs -f`

**Повна інструкція** по всіх функціях — у [README-ПОВНИЙ.md](README-ПОВНИЙ.md).

---
⚖️ Використання — лише в особистих цілях. Перепродаж заборонено (див. LICENSE.md).
