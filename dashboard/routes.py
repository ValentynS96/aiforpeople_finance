"""
FastAPI-маршруты дашборда и админки.

Подключается в server/app.py через `app.include_router(router)`.

Маршруты:
  GET  /                    → дашборд (HTML)
  GET  /api/metrics         → KPI + графики (с фильтрами ?from=&to=&category=)
  GET  /api/transactions    → таблица транзакций (с фильтрами + пагинация)
  GET  /api/categories      → список категорий (для фильтра)

  GET  /admin               → админка (4 раздела, за паролем)
  GET  /admin/login         → форма входа
  POST /admin/login         → проверка пароля
  GET  /admin/logout        → выход
  POST /admin/thresholds    → сохранить пороги алертов
  POST /admin/categories    → сохранить категории
  POST /admin/secrets       → сохранить ключи (шифруются)
  POST /admin/sheets        → конфиг экспорта в Google Sheets
  POST /admin/export-sheets → запустить экспорт текущих данных в Sheets
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from dashboard import auth, db

_log = logging.getLogger("dashboard")

router = APIRouter()
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ============================================================
# Вспомогательное: агрегация метрик из транзакций (как ai_engine, но из БД)
# ============================================================
def _aggregate(transactions: list[dict]) -> dict:
    """Считает KPI и данные для графиков из списка транзакций (из БД)."""
    if not transactions:
        return {
            "income": 0, "expenses": 0, "profit": 0, "margin": 0, "tx_count": 0,
            "by_category": [], "weekly": [], "top_category": "—",
        }

    income = sum(t["amount"] for t in transactions if t["amount"] > 0)
    expenses = sum(abs(t["amount"]) for t in transactions if t["amount"] < 0)
    profit = income - expenses
    margin = round((profit / income) * 100) if income > 0 else 0

    # по категориям (для donut)
    by_cat: dict[str, float] = {}
    for t in transactions:
        cat = t["category"] or "інше"
        amt = abs(t["amount"]) if t["amount"] < 0 else 0
        by_cat[cat] = by_cat.get(cat, 0) + amt
    cat_list = sorted(
        [{"category": k, "amount": round(v, 2)} for k, v in by_cat.items() if v > 0],
        key=lambda x: x["amount"], reverse=True,
    )
    top = cat_list[0]["category"] if cat_list else "—"

    # по неделям (для line + bar)
    weekly: dict[str, dict] = {}
    for t in transactions:
        wk = t.get("week") or _iso_week(t.get("date", ""))
        if wk not in weekly:
            weekly[wk] = {"income": 0.0, "expense": 0.0}
        if t["amount"] > 0:
            weekly[wk]["income"] += t["amount"]
        else:
            weekly[wk]["expense"] += abs(t["amount"])
    weekly_list = [
        {"week": k, "income": round(v["income"], 2),
         "expense": round(v["expense"], 2), "profit": round(v["income"] - v["expense"], 2)}
        for k, v in sorted(weekly.items())
    ]

    return {
        "income": round(income, 2),
        "expenses": round(expenses, 2),
        "profit": round(profit, 2),
        "margin": margin,
        "tx_count": len(transactions),
        "top_category": top,
        "by_category": cat_list,
        "weekly": weekly_list,
    }


def _iso_week(date_str: str) -> str:
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    except (ValueError, TypeError):
        return "unknown"


def _detect_anomalies(metrics: dict) -> list[str]:
    """Лёгкая версия детектора для дашборда (без markdown)."""
    thresholds = db.get_setting("alert_thresholds", {})
    t = thresholds or {}
    alerts = []
    inc, exp, prof = metrics["income"], metrics["expenses"], metrics["profit"]

    if t.get("cash_gap_enabled", True) and prof < 0:
        alerts.append(f"🔴 Касовий розрив: витрати перевищили дохід на {int(abs(prof)):,} грн".replace(",", " "))

    cat_pct = t.get("category_share_pct", 30)
    for c in metrics["by_category"]:
        share = (c["amount"] / exp * 100) if exp else 0
        if share >= cat_pct:
            alerts.append(f"⚠️ {c['category']} з'їдає {round(share)}% витрат")

    return alerts


# ============================================================
# Вход по IBAN + дашборд
# ============================================================
@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Если IBAN-сессии нет → welcome. Иначе → дашборд."""
    iban = auth.get_iban_session(request)
    if not iban:
        return templates.TemplateResponse(request, "welcome.html", {})
    cats = db.get_setting("categories", [])
    from bot.iban import mask
    return templates.TemplateResponse(request, "dashboard.html", {
        "categories": cats,
        "stats": db.stats(),
        "iban_mask": mask(iban),
        "iban": iban,
    })


@router.post("/login-iban")
async def login_iban(request: Request, iban: str = Form(...)):
    """Устанавливает IBAN-сессию и редиректит в дашборд."""
    from bot.iban import normalize, is_valid
    norm = normalize(iban)
    if not is_valid(norm):
        return templates.TemplateResponse(request, "welcome.html", {
            "error": "Невірний формат IBAN. Має бути UA + 27 цифр.",
        }, status_code=400)
    resp = RedirectResponse("/", status_code=303)
    auth.set_iban_session(resp, norm)
    return resp


@router.get("/logout-iban")
async def logout_iban():
    resp = RedirectResponse("/", status_code=303)
    auth.clear_iban_session(resp)
    return resp


@router.get("/api/session")
async def api_session(request: Request):
    """Текущий IBAN + сводка для шапки дашборда."""
    iban = auth.get_iban_session(request)
    if not iban:
        return {"iban": "", "has_data": False}
    txs = db.get_transactions(iban=iban, limit=100000)
    from bot.iban import mask
    return {
        "iban": iban,
        "iban_mask": mask(iban),
        "has_data": len(txs) > 0,
        "tx_count": len(txs),
    }


# ============================================================
# Веб-чат (ИИ в дашборде)
# ============================================================
# ============================================================
# Chat sessions (беседы как в ChatGPT)
# ============================================================
@router.get("/api/chat/sessions")
async def api_chat_sessions(request: Request):
    """Список бесед пользователя."""
    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    sessions = db.list_chat_sessions(iban)
    return {"sessions": sessions}


@router.post("/api/chat/sessions")
async def api_chat_session_create(request: Request):
    """Создать новую беседу."""
    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    sid = db.create_chat_session(iban)
    return {"id": sid, "title": "Нова бесіда"}


@router.patch("/api/chat/sessions/{session_id}")
async def api_chat_session_rename(request: Request, session_id: int):
    """Переименовать беседу."""
    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    import json as _json
    body = await request.body()
    try:
        data = _json.loads(body)
        title = (data.get("title") or "").strip()[:100]
    except (ValueError, KeyError):
        return JSONResponse({"error": "Невірний запит"}, status_code=400)
    if not title:
        return JSONResponse({"error": "Назва порожня"}, status_code=400)
    ok = db.rename_chat_session(session_id, title)
    return {"ok": ok}


@router.delete("/api/chat/sessions/{session_id}")
async def api_chat_session_delete(request: Request, session_id: int):
    """Удалить беседу + её сообщения."""
    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    ok = db.delete_chat_session(session_id, iban)
    return {"ok": ok}


@router.post("/api/chat")
async def api_chat(request: Request):
    """Чат с ИИ по данным текущего IBAN (из cookie). session_id привязывает к беседе."""
    import json as _json
    from bot import ai_engine as ai, db_bridge

    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась. Увійдіть заново."}, status_code=401)

    body = await request.body()
    try:
        data = _json.loads(body)
        message = (data.get("message") or "").strip()
        session_id = data.get("session_id")  # может быть None
    except (ValueError, KeyError):
        return JSONResponse({"error": "Невірний запит"}, status_code=400)

    if not message or len(message) > 2000:
        return JSONResponse({"error": "Повідомлення порожнє або занадто довге"}, status_code=400)

    # если session_id не задан — создаём новую беседу автоматически
    if not session_id:
        session_id = db.create_chat_session(iban)

    api_key = db_bridge.get_deepseek_key()
    if not api_key:
        return JSONResponse({"error": "AI не налаштований (DeepSeek ключ). Зверніться до адміністратора."}, status_code=503)

    # сохраняем вопрос + история конкретной беседы
    db.add_chat_message(iban, "user", message, session_id=session_id)
    db.update_session_title_if_first(session_id, message)
    history = db.get_chat_history(iban, limit=10, session_id=session_id)
    summary = db_bridge.get_summary_by_iban(iban)
    system_prompt = ai.build_chat_system_prompt(summary)

    messages = [{"role": "system", "content": system_prompt}] + history
    try:
        model = db_bridge.get_deepseek_model()
        reply = await ai.chat_async(messages, api_key, model=model)
    except ai.AIError as e:
        _log.error("веб-чат IBAN=%s: %s", iban, e)
        return JSONResponse({"error": "AI тимчасово недоступний. Спробуйте пізніше."}, status_code=503)

    db.add_chat_message(iban, "assistant", reply, session_id=session_id)
    db.touch_session(session_id)
    return {"reply": reply, "session_id": session_id}


@router.get("/api/chat/history")
async def api_chat_history(request: Request, session_id: int | None = None):
    """История чата. Если session_id — конкретная беседа."""
    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    history = db.get_chat_history(iban, limit=100, session_id=session_id)
    return {"messages": history, "count": len(history)}


@router.delete("/api/chat/history")
async def api_chat_history_clear(request: Request, session_id: int | None = None):
    """Очистка истории чата. Если session_id — только этой беседы."""
    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    n = db.clear_chat_history(iban, session_id=session_id)
    return {"cleared": n}


# ============================================================
# Загрузка выписки (PDF/CSV) из дашборда
# ============================================================
@router.post("/api/upload")
async def api_upload(request: Request):
    """Загрузка выписки PDF/CSV из дашборда. SSE-стриминг живых статусов.

    Возвращает text/event-stream: каждый этап шлёт {step, status, message, ...}.
    Финальный event содержит результат (ok + метрики).
    """
    import json as _json

    # Читаем форму ДО создания StreamingResponse (тело запроса доступно только здесь)
    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась. Увійдіть заново."}, status_code=401)

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return JSONResponse({"error": "Файл не отримано"}, status_code=400)

    content = await upload.read()
    filename = upload.filename or "statement"
    ext = Path(filename).suffix.lower()
    if ext not in {".pdf", ".csv", ".txt"}:
        return JSONResponse({"error": "Підтримуються лише PDF, CSV або TXT"}, status_code=400)
    if len(content) > 20 * 1024 * 1024:
        return JSONResponse({"error": "Файл занадто великий (макс. 20 МБ)"}, status_code=400)

    # заранее подгружаем тяжёлые импорты (до стрима, чтобы не падало внутри)
    from bot import ai_engine as ai, db_bridge
    from bot.iban import extract_iban, mask

    async def stream():
        def send(event_type, data):
            return f"event: {event_type}\ndata: {_json.dumps(data, ensure_ascii=False)}\n\n"

        # --- ЭТАП 1: Парсинг ---
        yield send("step", {"step": "parse", "status": "active",
                            "message": f"📄 Розпізнаю файл: {filename}..."})
        try:
            from server.parser import parse
            _tables, markdown = await asyncio.to_thread(parse, content, filename)
        except RuntimeError:
            yield send("error", {"error": "Парсинг PDF недоступний. Спробуйте CSV."})
            return
        except Exception as e:
            _log.error("парсинг %s: %s", filename, e)
            yield send("error", {"error": "Не вдалося розібрати файл. Перевірте формат."})
            return

        if not markdown or len(markdown.strip()) < 50:
            yield send("error", {"error": "Файл порожній або текст не розпізнано"})
            return

        yield send("step", {"step": "parse", "status": "done",
                            "message": f"✅ Розпізнано текст виписки"})

        # --- ЭТАП 2: IBAN проверка ---
        file_iban = extract_iban(markdown)
        if file_iban and file_iban != iban:
            yield send("error", {"error": f"IBAN у файлі ({mask(file_iban)}) не збігається з вашим рахунком ({mask(iban)})"})
            return

        # --- ЭТАП 3: AI категоризация ---
        yield send("step", {"step": "ai", "status": "active",
                            "message": "🧠 AI розносить транзакції по категоріях... (10-30 сек)"})

        api_key = db_bridge.get_deepseek_key()
        if not api_key:
            yield send("error", {"error": "AI не налаштований (DeepSeek ключ). Зверніться до адміністратора."})
            return

        try:
            categories = db_bridge.get_categories()
            model = db_bridge.get_deepseek_model()
            transactions = await ai.categorize_async(markdown, api_key,
                                                     categories=categories, model=model)
        except ai.AIError as e:
            _log.error("категоризация %s: %s", filename, e)
            yield send("error", {"error": "AI не зміг розпізнати транзакції. Спробуйте інший формат."})
            return

        yield send("step", {"step": "ai", "status": "done",
                            "message": f"✅ AI розібрав {len(transactions)} транзакцій"})

        # --- ЭТАП 4: Сохранение ---
        yield send("step", {"step": "save", "status": "active",
                            "message": "💾 Зберігаю в базу даних..."})
        db_bridge.save_result(transactions, chat_id=iban, username="(web)", iban=iban)
        yield send("step", {"step": "save", "status": "done",
                            "message": "✅ Збережено в дашборд"})

        # --- ЭТАП 5: Google Sheets экспорт ---
        yield send("step", {"step": "export", "status": "active",
                            "message": "📋 Експортую в Google Таблицю..."})
        sheets_ok = db_bridge.auto_export_to_sheets(transactions)
        yield send("step", {"step": "export", "status": "done",
                            "message": "✅ Експортовано" if sheets_ok else "⚠️ Експорт пропущено"})

        # --- ЭТАП 6: Метрики + финал ---
        all_tx = db.get_transactions(iban=iban, limit=100000)
        metrics = ai.compute_metrics(all_tx)
        thresholds = db_bridge.get_alert_thresholds()
        anomalies = ai.detect_anomalies(metrics, thresholds)

        yield send("done", {
            "ok": True,
            "tx_count": len(transactions),
            "total_tx": len(all_tx),
            "income": round(metrics["income"], 2),
            "expenses": round(metrics["expenses"], 2),
            "profit": round(metrics["profit"], 2),
            "anomalies": anomalies["count"],
            "sheets_exported": sheets_ok,
        })

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/admin/send-report")
async def admin_send_report(request: Request):
    """Ручной запуск ежемесячного P&L-отчёта (для демо). Требует админ-сессию."""
    redir = auth.require_auth(request)
    if redir:
        return redir
    from server import runtime
    if not runtime.bot_app:
        return JSONResponse({"ok": False, "error": "Telegram-бот не запущено (немає токена)"}, status_code=400)
    from server.scheduler import send_monthly_report
    result = await send_monthly_report(runtime.bot_app)
    return JSONResponse({"ok": True, **result})


# ============================================================
# Удаление данных по IBAN (подтверждение паролем админа)
# ============================================================
@router.post("/api/delete-data")
async def api_delete_data(request: Request):
    """Удаляет все данные указанного IBAN. Требует админ-сессию + пароль.

    Тело: {"password": "...", "iban": "UA..."}.
    Доступ: только из админки (auth.require_auth).
    """
    import json as _json
    redir = auth.require_auth(request)
    if redir:
        return JSONResponse({"error": "Потрібна авторизація адміністратора"}, status_code=401)

    body = await request.body()
    try:
        data = _json.loads(body)
        password = data.get("password", "")
        iban = (data.get("iban") or "").strip()
    except (ValueError, KeyError):
        return JSONResponse({"error": "Невірний запит"}, status_code=400)

    if not iban:
        return JSONResponse({"error": "Не вказано IBAN"}, status_code=400)

    if not auth.check_password(password):
        return JSONResponse({"error": "Невірний пароль"}, status_code=403)

    deleted = db.delete_iban_data(iban)
    _log.warning("Видалено дані IBAN=%s: %s", iban, deleted)
    return {"ok": True, "deleted": deleted}


# ============================================================
# Модель DeepSeek (выбор в админке, список тянется через API)
# ============================================================
@router.get("/api/deepseek/models")
async def api_deepseek_models(request: Request):
    """Возвращает актуальный список моделей с DeepSeek + текущую выбранную."""
    redir = auth.require_auth(request)
    if redir:
        return redir
    from bot import ai_engine as ai, db_bridge
    api_key = db_bridge.get_deepseek_key()
    if not api_key:
        return JSONResponse({"error": "DeepSeek ключ не налаштований"}, status_code=400)
    try:
        models = await asyncio.to_thread(ai.list_models, api_key)
    except ai.AIError as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return {
        "models": models,
        "current": db_bridge.get_deepseek_model(),
    }


@router.post("/admin/model")
async def admin_save_model(request: Request, model: str = Form(...)):
    """Сохраняет выбранную модель DeepSeek."""
    redir = auth.require_auth(request)
    if redir:
        return redir
    from bot import db_bridge
    db_bridge.set_deepseek_model(model.strip())
    return RedirectResponse("/admin#model", status_code=303)


@router.get("/api/metrics")
async def api_metrics(request: Request, date_from: str = "", date_to: str = "",
                      category: str = "all", month: str = "", year: str = ""):
    """KPI + графики. Автофильтр по IBAN + период (месяц/год)."""
    iban = auth.get_iban_session(request) or "all"
    txs = db.get_transactions(date_from=date_from, date_to=date_to,
                              category=category, iban=iban,
                              month=month, year=year, limit=10000)
    metrics = _aggregate(txs)
    metrics["alerts"] = _detect_anomalies(metrics)
    return metrics


@router.get("/api/transactions")
async def api_transactions(request: Request, date_from: str = "", date_to: str = "",
                           category: str = "all", month: str = "", year: str = "",
                           q: str = "", limit: int = 100):
    """Таблица транзакций. Автофильтр по IBAN + период + полнотекстовый поиск (q)."""
    iban = auth.get_iban_session(request) or "all"
    if q.strip():
        # FTS5-поиск; категория/период дополнительно фильтруем в Python (мало строк)
        txs = db.search_transactions(q, iban=iban, limit=500)
        if category and category != "all":
            txs = [t for t in txs if t.get("category") == category]
        if month:
            txs = [t for t in txs if (t.get("date") or "").startswith(month)]
        if year:
            txs = [t for t in txs if (t.get("date") or "").startswith(year)]
        return txs[:limit]
    return db.get_transactions(date_from=date_from, date_to=date_to,
                               category=category, iban=iban,
                               month=month, year=year, limit=limit)


@router.get("/api/chat/search")
async def api_chat_search(request: Request, q: str = ""):
    """Полнотекстовый поиск по сообщениям чата текущего пользователя."""
    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    if not q.strip():
        return {"results": [], "count": 0}
    results = db.search_chat(q, chat_id=iban)
    return {"results": results, "count": len(results)}


@router.get("/api/periods")
async def api_periods(request: Request):
    """Доступные месяцы и годы для переключателя периодов."""
    iban = auth.get_iban_session(request) or "all"
    return db.get_periods(iban=iban)


@router.get("/api/categories")
async def api_categories():
    return {"categories": db.get_setting("categories", [])}


@router.get("/api/accounts")
async def api_accounts():
    """Список счетов с метриками — для dropdown фильтра IBAN на дашборде."""
    from bot.iban import mask
    accounts = db.account_stats()
    result = []
    for a in accounts:
        result.append({
            "iban": a["iban"],
            "iban_mask": mask(a["iban"]),
            "holder": a.get("holder") or "",
            "tx_count": a["tx_count"],
            "income": round(a["income"], 2),
            "expense": round(a["expense"], 2),
        })
    return {"accounts": result}


# ============================================================
# Админка
# ============================================================
@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse(request, "admin_login.html", {"error": ""})


@router.post("/admin/login")
async def admin_login_submit(request: Request, password: str = Form(...)):
    resp = RedirectResponse("/admin", status_code=303)
    if auth.login(resp, password):
        return resp
    return templates.TemplateResponse(request, "admin_login.html", {
        "error": "Невірний пароль",
    }, status_code=401)


@router.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    auth.logout(resp)
    return resp


def _secret_status() -> dict:
    """Статусы секретов (без раскрытия значений) — для админки."""
    return {
        "deepseek_key": db.secret_is_set("deepseek_key"),
        "telegram_token": db.secret_is_set("telegram_token"),
    }


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    redir = auth.require_auth(request)
    if redir:
        return redir

    thresholds = db.get_setting("alert_thresholds", {})
    categories = db.get_setting("categories", [])
    sheets = db.get_sheets_config()
    sessions = db.list_sessions(limit=50)

    # когда следующий запуск ежемесячного отчёта
    try:
        from server.scheduler import next_run_info
        next_report_run = next_run_info()
    except Exception:
        next_report_run = "невідомо"

    return templates.TemplateResponse(request, "admin.html", {
        "thresholds": thresholds,
        "categories": categories,
        "sheets": sheets,
        "sessions": sessions,
        "secret_status": _secret_status(),
        "stats": db.stats(),
        "next_report_run": next_report_run,
    })


@router.post("/admin/thresholds")
async def admin_save_thresholds(request: Request,
                                cash_gap_enabled: bool = Form(False),
                                category_share_pct: float = Form(30),
                                taxes_multiplier: float = Form(3),
                                salary_income_pct: float = Form(80)):
    redir = auth.require_auth(request)
    if redir:
        return redir
    db.set_setting("alert_thresholds", {
        "cash_gap_enabled": cash_gap_enabled,
        "category_share_pct": category_share_pct,
        "taxes_multiplier": taxes_multiplier,
        "salary_income_pct": salary_income_pct,
    })
    resp = RedirectResponse("/admin#thresholds", status_code=303)
    return resp


@router.post("/admin/categories")
async def admin_save_categories(request: Request, categories: str = Form(...)):
    redir = auth.require_auth(request)
    if redir:
        return redir
    # парсим: разделитель — новая строка
    cats = [c.strip() for c in categories.split("\n") if c.strip()]
    db.set_setting("categories", cats)
    return RedirectResponse("/admin#categories", status_code=303)


@router.post("/admin/secrets")
async def admin_save_secrets(request: Request,
                             deepseek_key: str = Form(""),
                             telegram_token: str = Form("")):
    redir = auth.require_auth(request)
    if redir:
        return redir
    # сохраняем только непустые поля (пустое = не менять)
    if deepseek_key.strip():
        db.set_secret("deepseek_key", deepseek_key.strip())
    if telegram_token.strip():
        db.set_secret("telegram_token", telegram_token.strip())
    return RedirectResponse("/admin#secrets", status_code=303)


@router.post("/admin/sheets")
async def admin_save_sheets(request: Request,
                            sheet_id: str = Form(""),
                            enabled: bool = Form(False),
                            google_credentials: str = Form("")):
    redir = auth.require_auth(request)
    if redir:
        return redir
    # валидация JSON-ключа, если поле заполнено
    creds = google_credentials.strip()
    if creds:
        import json as _json
        try:
            _json.loads(creds)  # проверяем, что это валидный JSON
        except ValueError:
            return templates.TemplateResponse(request, "admin.html", {
                "thresholds": db.get_setting("alert_thresholds", {}),
                "categories": db.get_setting("categories", []),
                "sheets": {**db.get_sheets_config(), "sheet_id": sheet_id},
                "sessions": db.list_sessions(limit=50),
                "secret_status": _secret_status(),
                "stats": db.stats(),
                "sheets_error": "Невалідний JSON. Скопіюйте увесь вміст файлу ключа Service Account.",
            }, status_code=400)
    db.set_sheets_config(sheet_id.strip(), enabled, credentials_json=creds)
    return RedirectResponse("/admin#sheets", status_code=303)


@router.post("/admin/export-sheets")
async def admin_export_sheets(request: Request):
    """Экспорт текущих транзакций в Google Sheets (для кураторов)."""
    redir = auth.require_auth(request)
    if redir:
        return redir
    cfg = db.get_sheets_config()
    if not cfg.get("sheet_id"):
        return JSONResponse({"ok": False, "error": "Спочатку введіть ID таблиці"}, status_code=400)
    if not cfg.get("has_credentials"):
        return JSONResponse({"ok": False, "error": "Спочатку додайте JSON-ключ Service Account"}, status_code=400)
    from bot import sheets_writer
    try:
        import json as _json
        creds_info = _json.loads(db.get_sheets_credentials())
        txs = db.get_transactions(limit=100000)
        rows = [{"date": t["date"], "description": t["description"],
                 "amount": t["amount"], "type": t["type"], "category": t["category"]}
                for t in txs]
        url = sheets_writer.append_transactions(rows, sheet_id=cfg["sheet_id"],
                                                credentials_info=creds_info)
        return JSONResponse({"ok": True, "url": url, "rows": len(rows)})
    except sheets_writer.SheetsError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        _log.error("Экспорт в Sheets не удался: %s", e)
        return JSONResponse(
            {"ok": False, "error": "Помилка експорту. Перевірте GOOGLE_CREDENTIALS_JSON та ID таблиці в логах сервера."},
            status_code=500,
        )


@router.post("/admin/update-pl")
async def admin_update_pl(request: Request):
    """Ручное обновление P&L — для каждого месяца своя вкладка."""
    redir = auth.require_auth(request)
    if redir:
        return redir
    cfg = db.get_sheets_config()
    if not cfg.get("sheet_id"):
        return JSONResponse({"ok": False, "error": "Спочатку введіть ID таблиці"}, status_code=400)
    if not cfg.get("has_credentials"):
        return JSONResponse({"ok": False, "error": "Спочатку додайте JSON-ключ Service Account"}, status_code=400)
    from bot import sheets_writer
    try:
        import json as _json
        creds_info = _json.loads(db.get_sheets_credentials())
        # определяем все месяцы из БД
        import sqlite3 as _sql
        conn = _sql.connect(str(db.DB_PATH))
        rows = conn.execute(
            "SELECT DISTINCT substr(date,1,7) as ym FROM transactions "
            "WHERE date IS NOT NULL AND length(date) >= 7 ORDER BY ym"
        ).fetchall()
        conn.close()
        months = []
        for r in rows:
            ym = r[0]
            try:
                y, m = int(ym[:4]), int(ym[5:7])
                months.append((m, y))
            except (ValueError, IndexError):
                continue

        results = []
        total_updated = 0
        for month in months:
            pl_data = db.get_pl_data(month)
            if not pl_data:
                continue
            result = sheets_writer.update_pl(pl_data, month=month,
                                             sheet_id=cfg["sheet_id"],
                                             credentials_info=creds_info)
            total_updated += result["updated"]
            results.append({"tab": result["tab"], "updated": result["updated"]})
            import time as _t; _t.sleep(2)  # Google API rate limit

        # --- годовые вкладки ---
        years = sorted({y for _, y in months})
        for year in years:
            pl_data_y = db.get_pl_data_year(year)
            if not pl_data_y:
                continue
            import time as _t; _t.sleep(2)
            result_y = sheets_writer.update_pl_year(pl_data_y, year=year,
                                                    sheet_id=cfg["sheet_id"],
                                                    credentials_info=creds_info)
            total_updated += result_y["updated"]
            results.append({"tab": result_y["tab"], "updated": result_y["updated"]})

        return JSONResponse({
            "ok": True,
            "periods": len(results),
            "tabs": [r["tab"] for r in results],
            "updated": total_updated,
        })
    except sheets_writer.SheetsError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        _log.error("P&L обновление не удалось: %s", e)
        return JSONResponse({"ok": False, "error": "Помилка оновлення P&L. Деталі в логах."}, status_code=500)
