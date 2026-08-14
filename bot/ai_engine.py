"""
AI-движок для демо «AI замість фінансиста».

Бизнес-логика: категоризация выписок через DeepSeek, метрики, аномалии, чат.
Протестировано на 250 транзакциях.

Поток:
    text (сира виписка)
      → categorize()  : DeepSeek розносить по категоріях
      → compute_metrics() : виручка / витрати / прибуток / маржа / топ
      → detect_anomalies() : 4 правила проактивних алертів
      → build_summary() : markdown-повідомлення в чат

Усі функції чистые (без побічных эффектов) — легко тестировать.
"""

import asyncio
import json
import re
from datetime import datetime

import requests

# --- Канонічні 8 категорій (узгоджено з лендингом + старим воркфлоу) ---
CATEGORIES = [
    "продажі", "реклама", "експерти", "зарплата", "оренда",
    "податки", "підписки", "підрядники", "hr", "підтримка сайту",
    "повернення", "фаундери", "переказ", "інше",
]

# Приклади описів з виписок для кожної категорії (підвищує точність AI)
CATEGORY_EXAMPLES = {
    "продажі": "Оплата від клієнта, інкасація, надходження на рахунок, оплата за послуги/товари",
    "реклама": "Google Ads, Meta Ads, Facebook Ads, таргетована реклама, промо, Boost",
    "експерти": "Консультація експерта, аутсорс розробки, послуги спеціаліста, аудит, юрист-експерт",
    "зарплата": "Зарплата, премія, аванс, беніфіти, заробітна плата, виплата працівнику",
    "оренда": "Оренда офісу, коворкінг, оренда приміщення, аренда складу",
    "податки": "ЄСВ, ПДВ, податок на прибуток, єдиний податок, ПФУ, пенсійний фонд",
    "підписки": "AWS, Google Workspace, Notion, Zoom, Slack, CRM, Jira, Figma, GitHub, VPS",
    "підрядники": "Підрядник, фрілансер, виконавець робіт, оплата за проект",
    "hr": "Рекрутинг, курси, навчання, Divine, LinkedIn Premium, батрак, підбір персоналу",
    "підтримка сайту": "Хостинг, домен, SSL сертифікат, підтримка сайта, Cloudflare",
    "повернення": "Повернення клієнту, refund, повернення коштів, скасування оплати",
    "фаундери": "Зарплата фаундера, дивіденди засновнику, виплата партнеру",
    "переказ": "Внутрішній переказ, поповнення рахунку, перевод між рахунками, SWIFT",
    "інше": "Банківські комісії, нотаріус, юридичні послуги, штрафи, інші витрати",
}

SYSTEM_PROMPT = """Ти — AI-асистент для аналізу банківських виписок. Проаналізуй текст виписки та розкатегоризуй КОЖНУ транзакцію.

КАТЕГОРІЇ (лише ці 8):
- продажі (дохід від клієнтів, платежі за товари/послуги)
- реклама (Google Ads, Meta Ads, інша реклама)
- зарплата (виплати працівникам)
- оренда (оренда офісу, приміщення)
- податки (податкові платежі, ЄСВ, ПДВ)
- підписки (SaaS, програми, інтернет, зв'язок)
- переказ (внутрішні перекази, поповнення)
- інше (якщо не впевнений)

Поверни результат ЯВНО як JSON з ключем "transactions":
{ "transactions": [{ "date": "YYYY-MM-DD", "description": "Опис", "amount": 15000.00, "type": "дохід", "category": "продажі" }] }

ПРАВИЛА:
- Дохід = позитивна сума, витрата = негативна
- Якщо не впевнений — категорія "інше"
- Поверни ТІЛЬКИ JSON без додаткового тексту
- Усі суми в грн"""

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODELS_URL = "https://api.deepseek.com/models"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"  # безопасный дефолт; реальная — из БД/админки


class AIError(Exception):
    """Помилка категоризації/парсингу — ловиться в боті й відправляється юзеру."""


def build_system_prompt(categories: list[str] | None = None) -> str:
    """Строит системный промпт с заданным списком категорий.

    По умолчанию — канонические 8. Позволяет админке менять категории,
    не трогая код. «інше» всегда добавляется как fallback.
    """
    cats = categories or CATEGORIES
    # гарантуємо fallback «інше»
    if "інше" not in cats:
        cats = cats + ["інше"]
    cat_list = "\n".join(
        f"- {c}" + (f" (приклади: {CATEGORY_EXAMPLES.get(c, '')})" if c in CATEGORY_EXAMPLES else "")
        for c in cats
    )
    return f"""Ти — AI-асистент для аналізу банківських виписок. Проаналізуй текст виписки та розкатегоризуй КОЖНУ транзакцію.

КАТЕГОРІЇ (лише ці, список через дефіс):
{cat_list}

Поверни результат ЯВНО як JSON з ключем "transactions":
{{ "transactions": [{{ "date": "YYYY-MM-DD", "description": "Опис", "amount": 15000.00, "type": "дохід", "category": "продажі", "country": "UA" }}] }}

ПРАВИЛА:
- Дохід = позитивна сума, витрата = негативна
- Якщо не впевнений — категорія "інше"
- "country": "UA" (Україна) або "PL" (Польща) — країна банківського рахунку / операції за описом
- Поверни ТІЛЬКИ JSON без додаткового тексту
- Усі суми в грн"""


# ============================================================
# 1. Категоризація через DeepSeek
# ============================================================
def categorize(text: str, api_key: str, *, timeout: int = 90,
               categories: list[str] | None = None,
               model: str = "") -> list[dict]:
    """Сира виписка → масив транзакцій з категоріями.

    categories: список категорий (по умолчанию канонические 8). Передавайте из БД,
    чтобы изменения в админке влияли на категоризацию.
    model: модель DeepSeek (по умолчанию DEEPSEEK_DEFAULT_MODEL; реальная — из БД).
    Повертає список [{date, description, amount, type, category}].
    Кидає AIError при помилці мережі/AI/парсингу.
    """
    if not api_key:
        raise AIError("DEEPSEEK_API_KEY не налаштований.")

    system_prompt = build_system_prompt(categories)
    payload = {
        "model": model or DEEPSEEK_DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text[:15000]},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise AIError(f"DeepSeek API недоступний: {e}") from e

    content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")

    # Парсинг JSON (DeepSeek іноді обгортає в текст)
    parsed = _extract_json(content)
    if parsed is None:
        raise AIError(f"AI повернув не JSON: {content[:200]}")

    txs = parsed.get("transactions", [])
    if not txs:
        raise AIError("AI не знайшов транзакцій у виписці.")

    # Нормалізація кожної транзакції
    normalized = []
    for t in txs:
        try:
            amount = float(t.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0.0
        normalized.append({
            "date": str(t.get("date", datetime.now().strftime("%Y-%m-%d")))[:10],
            "description": str(t.get("description", ""))[:100],
            "amount": amount,
            "type": t.get("type", "дохід" if amount >= 0 else "витрата"),
            "category": t.get("category", "інше") or "інше",
            "country": str(t.get("country", "")).upper() or "UA",  # UA/PL, дефолт UA
        })
    return normalized


async def categorize_async(text: str, api_key: str, *, timeout: int = 90,
                           categories: list[str] | None = None,
                           model: str = "") -> list[dict]:
    """Async-обёртка над categorize(): не блокирует event loop."""
    return await asyncio.to_thread(categorize, text, api_key,
                                   timeout=timeout, categories=categories, model=model)


def _extract_json(content: str) -> dict | None:
    """Витягує JSON-об'єкт з відповіді (навіть якщо є зайвий текст)."""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", content)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ============================================================
# 1б. ИИ-чат (multi-turn) — ответы на вопросы по финансовым данным
# ============================================================
def build_chat_system_prompt(account_summary: str, relevant: str = "") -> str:
    """Строит системный промпт для чата с подставленной сводкой по счетам.

    account_summary — текст-сводка по всем IBAN пользователя (готовит db_bridge).
    relevant — найденные по вопросу транзакции (RAG через FTS5), могут быть пустыми.
    """
    relevant_block = ""
    if relevant:
        relevant_block = f"""
РЕЛЕВАНТНІ ТРАНЗАКЦІЇ ДО ПИТАННЯ КОРИСТУВАЧА (найдені пошуком — точні дані):
{relevant}
"""
    return f"""Ти — AI-фінансовий радник для українського малого бізнесу.

ДАНІ КОРИСТУВАЧА (агреговані по рахунках):
{account_summary or '(дані поки що відсутні — порадь завантажити виписку)'}
{relevant_block}
ПРАВИЛА:
- Відповідай коротко (2-5 речень), українською
- Спирайся ТІЛЬКИ на ці дані, не вигадуй цифри
- Якщо в «РЕЛЕВАНТНІ ТРАНЗАКЦІЇ» є відповідь на питання — використовуй саме її,
  це точний результат пошуку по транзакціях
- Якщо даних недостатньо — чесно скажи й порадь завантажити виписку
- Числа форматуй з пробілами як роздільниками тисяч: 1 234 567 грн
- При витратах вказуй категорію та суму"""


def chat(messages: list[dict], api_key: str, *, timeout: int = 60,
         model: str = "") -> str:
    """Multi-turn чат с DeepSeek. messages = [{role, content}, ...].

    model: модель DeepSeek (по умолчанию DEEPSEEK_DEFAULT_MODEL; реальная — из БД).
    Возвращает текст ответа ассистента. НЕ использует json_object — свободный текст.
    """
    if not api_key:
        raise AIError("DEEPSEEK_API_KEY не налаштований.")

    payload = {
        "model": model or DEEPSEEK_DEFAULT_MODEL,
        "messages": messages,
        "temperature": 0.3,  # чуть выше, чем для категоризации — естественнее ответы
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise AIError(f"DeepSeek API недоступний: {e}") from e

    return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()


async def chat_async(messages: list[dict], api_key: str, *, timeout: int = 60,
                     model: str = "") -> str:
    """Async-обёртка над chat(): не блокирует event loop."""
    return await asyncio.to_thread(chat, messages, api_key, timeout=timeout, model=model)


def list_models(api_key: str, *, timeout: int = 15) -> list[str]:
    """Список актуальних моделей з DeepSeek API. Повертає ['deepseek-v4-flash', ...].

    Используется админкой для заполнения dropdown актуальными именами моделей.
    Кидає AIError при помилці.
    """
    if not api_key:
        raise AIError("DEEPSEEK_API_KEY не налаштований.")
    try:
        resp = requests.get(DEEPSEEK_MODELS_URL,
                            headers={"Authorization": f"Bearer {api_key}"},
                            timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise AIError(f"DeepSeek API недоступний: {e}") from e
    data = resp.json().get("data", [])
    return [m["id"] for m in data if "id" in m]


# ============================================================
# 2. Метрики (для дашборду)
# ============================================================
def compute_metrics(transactions: list[dict]) -> dict:
    """Транзакції → згорнуті метрики. Дзеркало JS_COMPUTE_METRICS."""
    income = sum(t["amount"] for t in transactions if t["amount"] > 0)
    expenses = sum(abs(t["amount"]) for t in transactions if t["amount"] < 0)
    profit = income - expenses
    margin = round((profit / income) * 100) if income > 0 else 0

    by_cat: dict[str, dict] = {}
    for t in transactions:
        cat = t["category"] or "інше"
        if cat not in by_cat:
            by_cat[cat] = {"income": 0.0, "expense": 0.0, "count": 0}
        if t["amount"] > 0:
            by_cat[cat]["income"] += t["amount"]
        else:
            by_cat[cat]["expense"] += abs(t["amount"])
        by_cat[cat]["count"] += 1

    top_cat = None
    expense_cats = [(c, v) for c, v in by_cat.items() if v["expense"] > 0]
    if expense_cats:
        top_cat = max(expense_cats, key=lambda x: x[1]["expense"])

    return {
        "income": income,
        "expenses": expenses,
        "profit": profit,
        "margin": margin,
        "top_category": f"{top_cat[0]} ({fmt(top_cat[1]['expense'])} грн)" if top_cat else "—",
        "tx_count": len(transactions),
        "by_cat": by_cat,
        # рядок для дашборду
        "dashboard_row": {
            "Дата оновлення": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "Виручка": round(income, 2),
            "Витрати": round(expenses, 2),
            "Прибуток": round(profit, 2),
            "Маржа %": margin,
            "Топ-категорія": f"{top_cat[0]}" if top_cat else "—",
            "Кількість транзакцій": len(transactions),
        },
    }


# ============================================================
# 2б. Агрегация по (категорія × країна) — для P&L
# ============================================================
def compute_by_cat_and_country(transactions: list[dict]) -> dict:
    """Транзакції → {country: {category: {"income": float, "expense": float}}}.

    Не ломает существующий compute_metrics (у него плоский by_cat).
    Используется для обновления P&L Fact-ячеек по странам.
    """
    result: dict[str, dict[str, dict]] = {}
    for t in transactions:
        country = (t.get("country") or "UA").upper()
        cat = t.get("category") or "інше"
        amt = t.get("amount", 0)
        if country not in result:
            result[country] = {}
        if cat not in result[country]:
            result[country][cat] = {"income": 0.0, "expense": 0.0}
        if amt >= 0:
            result[country][cat]["income"] += amt
        else:
            result[country][cat]["expense"] += abs(amt)
    return result


# ============================================================
# 3. Аномалії — 4 правила проактивних алертів
# ============================================================
def detect_anomalies(metrics: dict, thresholds: dict | None = None) -> dict:
    """Метрики → список алертів + markdown-повідомлення.

    thresholds (опц.): пороги правил. Если None — дефолты (захардкожено ниже).
    Формат (соответствует DEFAULT_SETTINGS в dashboard/db.py):
        {
          "cash_gap_enabled": True,      # правило 1 включено
          "category_share_pct": 30,      # правило 2: категория ≥ X% витрат
          "taxes_multiplier": 3,         # правило 3: податки ≥ X× среднего
          "salary_income_pct": 80,       # правило 4: зарплата ≥ X% виручки
        }
    """
    t = thresholds or {}
    by_cat = metrics["by_cat"]
    income = metrics["income"]
    expenses = metrics["expenses"]
    profit = metrics["profit"]
    alerts = []

    cat_share_pct = t.get("category_share_pct", 30)
    taxes_mult = t.get("taxes_multiplier", 3)
    salary_pct = t.get("salary_income_pct", 80)

    # 1. Касовий розрив
    if t.get("cash_gap_enabled", True) and profit < 0:
        alerts.append(f"🔴 *Касовий розрив:* витрати перевищили дохід на {fmt(abs(profit))} грн")

    # 2. Перевитрата категорії (≥ cat_share_pct % витрат)
    for cat, v in by_cat.items():
        if v["expense"] > 0:
            share = (v["expense"] / expenses) * 100 if expenses else 0
            if share >= cat_share_pct:
                alerts.append(f"⚠️ *{cat}* з'їдає {round(share)}% витрат ({fmt(v['expense'])} грн)")

    # 3. Податки ≥ taxes_mult × середньої категорії
    if by_cat.get("податки", {}).get("expense", 0) > 0:
        expense_cats = [v for v in by_cat.values() if v["expense"] > 0]
        if len(expense_cats) > 1:
            avg = expenses / len(expense_cats)
            ratio = by_cat["податки"]["expense"] / avg
            if ratio >= taxes_mult:
                alerts.append(f"⚠️ *Податки* у {ratio:.1f}× вище за норму ({fmt(by_cat['податки']['expense'])} грн)")

    # 4. Зарплата ≥ salary_pct % доходу
    if by_cat.get("зарплата", {}).get("expense", 0) > 0 and income > 0:
        ratio = (by_cat["зарплата"]["expense"] / income) * 100
        if ratio >= salary_pct:
            alerts.append(f"⚠️ *Зарплати* = {round(ratio)}% виручки — бізнес працює на персонал")

    return {
        "count": len(alerts),
        "message": (
            f"🚨 *Аномалія виявлена ДО того, як потрапить у звіт:*\n\n" + "\n".join(alerts)
            if alerts else ""
        ),
    }


# ============================================================
# 4. Підсумок у чат
# ============================================================
def build_summary(metrics: dict, anomalies: dict) -> str:
    """Формує фінальне markdown-повідомлення. Дзеркало JS_BUILD_SUMMARY."""
    a = anomalies["message"]
    return (
        "📊 *Аналіз виписки завершено!*\n\n"
        f"🟢 Дохід: {fmt(metrics['income'])} грн\n"
        f"🔴 Витрати: {fmt(metrics['expenses'])} грн\n"
        f"💰 Прибуток: {fmt(metrics['profit'])} грн\n"
        f"📈 Маржа: {metrics['margin']}%\n"
        f"📦 {metrics['tx_count']} транзакцій оброблено\n\n"
        f"{'🏆 Топ-витрата: ' + metrics['top_category'] + chr(10) + chr(10) if metrics['top_category'] != '—' else ''}"
        f"{a + chr(10) + chr(10) if a else ''}"
        "Детальніше — у дашборді: http://localhost:8000 📊"
    )


# ============================================================
# Утиліти
# ============================================================
def fmt(n: float) -> str:
    """Число → укр. формат: 1 234 567 (округлено, без копійок). NaN/Inf → '—'."""
    import math
    if n is None or math.isnan(n) or math.isinf(n):
        return "—"
    sign = "-" if n < 0 else ""
    return sign + f"{abs(n):,.0f}".replace(",", " ")
