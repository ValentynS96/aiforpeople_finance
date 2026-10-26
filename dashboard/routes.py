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
    """Умная главная:
    аноним → лендинг с входом по телефону;
    клиент без выписок → онбординг с загрузкой (данные могли прийти из Telegram);
    все остальные → дашборд."""
    if auth.is_setup_needed():
        return RedirectResponse("/setup", status_code=303)
    iban = auth.get_iban_session(request)
    grant = _phone_user(request)
    if not iban and auth.is_authed(request):
        iban = "all"          # адмін без IBAN-сесії бачить усі рахунки
    if not iban and not grant:
        return templates.TemplateResponse(request, "welcome.html", {
            "iban_login_allowed": bool(db.get_setting("allow_iban_login", False)),
        })
    # клієнт уперше в системі (виписки ще немає, зокрема з Telegram) → онбординг
    if grant and not db.list_accounts(chat_id=grant["phone"]):
        return templates.TemplateResponse(request, "welcome.html", {
            "who": grant["label"] or db.mask_phone(grant["phone"]),
            "show_upload": grant["level"] == "full",
        })
    cats = db.get_setting("categories", [])
    from bot.iban import mask
    return templates.TemplateResponse(request, "dashboard.html", {
        "categories": cats,
        "stats": db.stats(),
        "iban_mask": "всі рахунки" if iban == "all" else mask(iban),
        "iban": iban,
    })


@router.post("/login-iban")
async def login_iban(request: Request, iban: str = Form(...)):
    """Устанавливает IBAN-сессию и редиректит в дашборд.

    По умолчанию ВЫКЛЮЧЕН (данные клиентов нельзя открывать по чужому IBAN).
    Включается в админке; админ может входить всегда."""
    from bot.iban import normalize, is_valid
    if not auth.is_authed(request) and not db.get_setting("allow_iban_login", False):
        return templates.TemplateResponse(request, "welcome.html", {
            "error": "Вхід за IBAN вимкнено. Увійдіть за номером телефону.",
        }, status_code=403)
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
    auth.clear_phone_session(resp)
    auth.logout(resp)   # повний вихід: і з адмінської сесії також
    return resp


# ============================================================
# Вход по номеру телефона (доступ выдаёт админ, без SMS)
# ============================================================
def _phone_user(request: Request) -> dict | None:
    """Телефонная сессия + повторная проверка доступа в БД.

    None — если сессии нет ИЛИ доступ отозван/истёк (тогда пользователь
    фактически разлогинен, даже пока живёт cookie).
    """
    sess = auth.get_phone_session(request)
    if not sess:
        return None
    grant = db.find_active_grant(sess["phone"])
    if not grant:
        return None
    return grant


def _client_single_iban(grant: dict) -> str:
    """Єдиний дозволений IBAN клієнта без мультирахунку (перший за часом)."""
    accs = db.list_accounts(chat_id=grant["phone"])
    if not accs:
        return ""
    return sorted(accs, key=lambda a: a.get("first_seen") or "")[0]["iban"]


def _data_scope(request: Request) -> tuple[str, str]:
    """(iban-фильтр, chat_id-фильтр) для дата-эндпоинтов с учётом прав.

    Клиент, вошедший по телефону: chat_id = его телефон → через JOIN accounts
    видит ТОЛЬКО свои счета (даже если подменит cookie на чужой IBAN).
    Без мультирахунку — форс першого рахунку (спільний дашборд заборонений).
    Админ/владелец (IBAN-сессия): как задано — «all» или конкретный IBAN.
    """
    iban = auth.get_iban_session(request) or "all"
    grant = _phone_user(request)
    if grant:
        if grant.get("multi_account"):
            return iban, grant["phone"]
        single = _client_single_iban(grant)
        return (single or iban), grant["phone"]
    return iban, ""


@router.post("/login-phone")
async def login_phone(request: Request, phone: str = Form(...)):
    """Вход по номеру телефона: доступ должен быть выдан админом и не истечь."""
    norm = db.normalize_phone(phone)
    grant = db.find_active_grant(norm) if norm else None
    if not grant:
        # единое сообщение — не раскрываем, есть ли номер в системе
        return templates.TemplateResponse(request, "welcome.html", {
            "phone_error": "Доступ для цього номера не знайдено або строк минув. "
                           "Зверніться до адміністратора.",
        }, status_code=401)

    resp = RedirectResponse("/", status_code=303)
    auth.set_phone_session(resp, grant["phone"], grant["level"], "all")
    # без мультирахунку — одразу фіксуюємо перший (єдиний видимий) рахунок
    if grant.get("multi_account"):
        auth.set_iban_session(resp, "all")
    else:
        auth.set_iban_session(resp, _client_single_iban(grant) or "all")
    db.touch_grant_login(grant["id"])
    db.log_usage(grant["label"] or db.mask_phone(grant["phone"]),
                 "вхід за телефоном", source="web",
                 details=f"{db.mask_phone(grant['phone'])} · "
                         f"{db.GRANT_LEVELS.get(grant['level'], grant['level'])}")
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
        "iban_mask": "всі рахунки" if iban == "all" else mask(iban),
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
    _, ws = _data_scope(request)
    key = ws or auth.get_iban_session(request)
    if not key:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    sessions = db.list_chat_sessions(key)
    return {"sessions": sessions}


@router.post("/api/chat/sessions")
async def api_chat_session_create(request: Request):
    """Создать новую беседу."""
    _, ws = _data_scope(request)
    key = ws or auth.get_iban_session(request)
    if not key:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    sid = db.create_chat_session(key)
    return {"id": sid, "title": "Нова бесіда"}


@router.patch("/api/chat/sessions/{session_id}")
async def api_chat_session_rename(request: Request, session_id: int):
    """Переименовать беседу."""
    _, ws = _data_scope(request)
    key = ws or auth.get_iban_session(request)
    if not key:
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
    _, ws = _data_scope(request)
    key = ws or auth.get_iban_session(request)
    if not key:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    ok = db.delete_chat_session(session_id, key)
    return {"ok": ok}


@router.post("/api/chat")
async def api_chat(request: Request):
    """Чат с ИИ по данным текущего IBAN (из cookie). session_id привязывает к беседе."""
    import json as _json
    from bot import ai_engine as ai, db_bridge

    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась. Увійдіть заново."}, status_code=401)

    # доступ по телефону: чат вимагає рівень «перегляд + чат» або вище
    pu = _phone_user(request)
    if auth.get_phone_session(request) and not pu:
        return JSONResponse({"error": "Доступ припинено. Зверніться до адміністратора."}, status_code=401)
    if pu and pu["level"] not in ("chat", "full"):
        return JSONResponse({"error": "Чат із ШІ у вашому доступі вимкнено (лише перегляд)."}, status_code=403)

    # клиент по телефону → его личный кабинет (ключ = телефон); иначе IBAN
    _, ws = _data_scope(request)
    key = ws or iban

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
        session_id = db.create_chat_session(key)

    api_key = db_bridge.get_deepseek_key()
    if not api_key:
        return JSONResponse({"error": "AI не налаштований (DeepSeek ключ). Зверніться до адміністратора."}, status_code=503)

    # сохраняем вопрос + история конкретной беседы
    db.add_chat_message(key, "user", message, session_id=session_id)
    db.update_session_title_if_first(session_id, message)
    history = db.get_chat_history(key, limit=10, session_id=session_id)
    summary = (db_bridge.get_summary_for_user(ws)
               if ws else db_bridge.get_summary_by_iban(iban))
    # RAG: ищем транзакции по словам вопроса (FTS5); клиент — только свои счета
    if ws:
        relevant = db_bridge.find_relevant(message, chat_id=ws)
    else:
        relevant = db_bridge.find_relevant(message, iban=iban)
    system_prompt = ai.build_chat_system_prompt(summary, relevant=relevant)

    messages = [{"role": "system", "content": system_prompt}] + history
    from bot.iban import mask
    user = ((pu["label"] or db.mask_phone(pu["phone"])) if pu
            else f"web · {mask(iban)}")
    meta: dict = {}
    try:
        model = db_bridge.get_deepseek_model()
        reply = await ai.chat_async(messages, api_key, model=model, usage_meta=meta)
    except ai.AIError as e:
        _log.error("веб-чат IBAN=%s: %s", iban, e)
        db.log_usage(user, "чат (помилка)", source="web", ok=False,
                     details=f"{message[:80]} · {str(e)[:120]}")
        return JSONResponse({"error": "AI тимчасово недоступний. Спробуйте пізніше."}, status_code=503)

    db.log_usage(user, "чат", source="web", model=meta.get("model", ""),
                 api_calls=meta.get("api_calls", 0),
                 prompt_tokens=meta.get("prompt_tokens", 0),
                 completion_tokens=meta.get("completion_tokens", 0),
                 cached_tokens=meta.get("cached_tokens", 0),
                 seconds=meta.get("seconds", 0.0),
                 details=f"питання: {message[:80]}")

    db.add_chat_message(key, "assistant", reply, session_id=session_id)
    db.touch_session(session_id)
    return {"reply": reply, "session_id": session_id}


@router.get("/api/chat/history")
async def api_chat_history(request: Request, session_id: int | None = None):
    """История чата. Если session_id — конкретная беседа."""
    _, ws = _data_scope(request)
    key = ws or auth.get_iban_session(request)
    if not key:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    history = db.get_chat_history(key, limit=100, session_id=session_id)
    return {"messages": history, "count": len(history)}


@router.delete("/api/chat/history")
async def api_chat_history_clear(request: Request, session_id: int | None = None):
    """Очистка истории чата. Если session_id — только этой беседы."""
    _, ws = _data_scope(request)
    key = ws or auth.get_iban_session(request)
    if not key:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    n = db.clear_chat_history(key, session_id=session_id)
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

    # доступ по телефону: полный уровень — обязателен для загрузок
    pu = _phone_user(request)
    if auth.get_phone_session(request) and not pu:
        return JSONResponse({"error": "Доступ припинено. Зверніться до адміністратора."}, status_code=401)
    if pu and pu["level"] != "full":
        return JSONResponse({"error": "Завантаження виписок доступне лише з повним доступом."}, status_code=403)

    # Читаем форму ДО создания StreamingResponse (тело запроса доступно только здесь)
    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась. Увійдіть заново."}, status_code=401)

    # кабинет: клиент по телефону → его телефон; иначе IBAN-сессия (владелец)
    _, ws = _data_scope(request)
    ws_id = ws or iban

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
        # Пропускається: режим «всі рахунки» та клієнти з мультирахунком
        # (у них інший IBAN у файлі = додати рахунок, а не помилка).
        file_iban = extract_iban(markdown)
        multi_ok = bool(pu and pu.get("multi_account"))
        if file_iban and iban != "all" and not multi_ok and file_iban != iban:
            yield send("error", {"error": f"IBAN у файлі ({mask(file_iban)}) не збігається з вашим рахунком ({mask(iban)})"})
            return
        target_iban = file_iban or (iban if iban != "all" else "")

        # --- ЭТАП 3: AI категоризация ---
        yield send("step", {"step": "ai", "status": "active",
                            "message": "🧠 AI розносить транзакції по категоріях... (зазвичай 1–3 хвилини)"})

        api_key = db_bridge.get_deepseek_key()
        if not api_key:
            yield send("error", {"error": "AI не налаштований (DeepSeek ключ). Зверніться до адміністратора."})
            return

        try:
            categories = db_bridge.get_categories()
            model = db_bridge.get_deepseek_model()

            # категоризація йде частинами — показуємо прогрес у стрімі
            box = {"done": 0, "total": 0}
            meta: dict = {}
            task = asyncio.create_task(ai.categorize_async(
                markdown, api_key, categories=categories, model=model,
                progress_cb=lambda done, total: box.update(done=done, total=total),
                usage_meta=meta))
            while not task.done():
                await asyncio.sleep(1)
                if box["total"] > 1 and box["done"]:
                    yield send("step", {"step": "ai", "status": "active",
                                        "message": f"🧠 AI розносить транзакції: "
                                                   f"оброблено {box['done']} з {box['total']} частин..."})
            transactions = await task
        except ai.AIError as e:
            _log.error("категоризация %s: %s", filename, e)
            yield send("error", {"error": _human_ai_error(str(e))})
            return

        yield send("step", {"step": "ai", "status": "done",
                            "message": f"✅ AI розібрав {len(transactions)} транзакцій"})

        from bot.iban import mask as _mask_iban
        ws_label = ((pu["label"] or db.mask_phone(pu["phone"])) if pu
                    else f"web · {_mask_iban(target_iban or iban)}")
        db.log_usage(ws_label, "загрузка виписки", source="web",
                     model=meta.get("model", ""), api_calls=meta.get("api_calls", 0),
                     prompt_tokens=meta.get("prompt_tokens", 0),
                     completion_tokens=meta.get("completion_tokens", 0),
                     cached_tokens=meta.get("cached_tokens", 0),
                     seconds=meta.get("seconds", 0.0),
                     details=f"{filename}: {len(transactions)} транзакцій")

        # --- ЭТАП 4: Сохранение (в кабинет пользователя) ---
        yield send("step", {"step": "save", "status": "active",
                            "message": "💾 Зберігаю в базу даних..."})
        res = db_bridge.save_result(transactions, chat_id=ws_id,
                                    username=ws_label, iban=target_iban) or {}
        inserted = res.get("inserted", len(transactions))
        skipped = res.get("skipped", 0)
        yield send("step", {"step": "save", "status": "done",
                            "message": "✅ Збережено в дашборд"})

        # --- ЭТАП 5: Google Sheets экспорт ---
        yield send("step", {"step": "export", "status": "active",
                            "message": "📋 Експортую в Google Таблицю..."})
        sheets_ok = db_bridge.auto_export_to_sheets(transactions)
        yield send("step", {"step": "export", "status": "done",
                            "message": "✅ Експортовано" if sheets_ok else "⚠️ Експорт пропущено"})

        # --- ЭТАП 6: Метрики + финал ---
        all_tx = db.get_transactions(iban=target_iban or "all", chat_id=ws, limit=100000)
        metrics = ai.compute_metrics(all_tx)
        thresholds = db_bridge.get_alert_thresholds()
        anomalies = ai.detect_anomalies(metrics, thresholds)

        dup_note = f" · {skipped} дублікатів пропущено" if skipped else ""
        yield send("done", {
            "ok": True,
            "tx_count": inserted,
            "total_tx": len(all_tx),
            "income": round(metrics["income"], 2),
            "expenses": round(metrics["expenses"], 2),
            "profit": round(metrics["profit"], 2),
            "anomalies": anomalies["count"],
            "sheets_exported": sheets_ok,
        })

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/api/first-upload")
async def api_first_upload(request: Request):
    """ПЕРВАЯ загрузка виписки — без IBAN-сессии (новичок с welcome-страницы).

    Запускает ФОНОВУЮ задачу и сразу возвращает job_id. Обработка не умирает
    при закрытии/перезагрузке страницы — статус смотрим через /api/job/{id}.
    """
    # доступ по телефону: полный уровень — обязателен для загрузок
    pu = _phone_user(request)
    if auth.get_phone_session(request) and not pu:
        return JSONResponse({"error": "Доступ припинено. Зверніться до адміністратора."}, status_code=401)
    if pu and pu["level"] != "full":
        return JSONResponse({"error": "Завантаження виписок доступне лише з повним доступом."}, status_code=403)
    # анонимам загрузка запрещена: сначала войдите по телефону (или админ)
    if not pu and not auth.is_authed(request) and not auth.get_iban_session(request):
        return JSONResponse({"error": "Спочатку увійдіть за номером телефону."}, status_code=401)

    # кабинет: клиент → телефон, админ → 'admin'
    ws_id = (pu["phone"] if pu
             else ("admin" if auth.is_authed(request) else auth.get_iban_session(request)))
    ws_label = ((pu["label"] or db.mask_phone(pu["phone"])) if pu
                else ("адмін" if ws_id == "admin" else "web"))

    import uuid as _uuid

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

    job_id = _uuid.uuid4().hex[:12]
    _jobs[job_id] = {"status": "running", "step": "parse", "message": f"📄 Розпізнаю файл: {filename}..."}
    _gc_jobs()
    asyncio.create_task(_process_first_upload(job_id, content, filename,
                                              ws_id=ws_id, ws_label=ws_label))
    return {"job_id": job_id}


# Фоновые задачи первой загрузки: id → {status, step, message, ...}
_jobs: dict[str, dict] = {}


def _gc_jobs(max_jobs: int = 50):
    """Держим последние N задач, старые удаляем."""
    while len(_jobs) > max_jobs:
        _jobs.pop(next(iter(_jobs)))


async def _process_first_upload(job_id: str, content: bytes, filename: str,
                                ws_id: str = "", ws_label: str = ""):
    """Фоновая обработка первой выписки: парсинг → IBAN → AI → сохранение.
    Обновляет _jobs[job_id] на каждом шаге. Переживает перезагрузку страницы.

    ws_id — кабинет (телефон клиента / 'admin' / IBAN владельца),
    ws_label — человекочитаемое имя для журнала статистики."""
    from bot import ai_engine as ai, db_bridge
    from bot.iban import extract_iban, mask

    def update(**kw):
        _jobs[job_id].update(kw)

    try:
        from server.parser import parse
        _tables, markdown = await asyncio.to_thread(parse, content, filename)
        if not markdown or len(markdown.strip()) < 50:
            update(status="error", error="Файл порожній або текст не розпізнано")
            return

        iban = extract_iban(markdown)
        if not iban:
            update(status="error", error="У файлі не знайдено IBAN рахунку. "
                                         "Спробуйте виписку з реквізитами або введіть IBAN вручну нижче.")
            return
        update(step="ai", status="running",
               message=f"✅ Файл розпізнано · 🏦 Рахунок: {mask(iban)} · "
                       f"🧠 AI розносить транзакції (зазвичай 1–3 хвилини)...")

        api_key = db_bridge.get_deepseek_key()
        if not api_key:
            update(status="error", error="AI не налаштований (DeepSeek ключ). Зверніться до адміністратора.")
            return
        categories = db_bridge.get_categories()
        model = db_bridge.get_deepseek_model()

        def ai_progress(done: int, total: int):
            if total > 1:
                update(step="ai", status="running",
                       message=f"✅ Файл розпізнано · 🏦 Рахунок: {mask(iban)} · "
                               f"🧠 AI розносить транзакції: оброблено {done} з {total} частин...")

        meta: dict = {}
        transactions = await ai.categorize_async(markdown, api_key,
                                                 categories=categories, model=model,
                                                 progress_cb=ai_progress, usage_meta=meta)
        update(step="ai", message=f"✅ AI розібрав {len(transactions)} транзакцій")
        db.log_usage(ws_label or "web", "загрузка виписки", source="web",
                     model=meta.get("model", ""), api_calls=meta.get("api_calls", 0),
                     prompt_tokens=meta.get("prompt_tokens", 0),
                     completion_tokens=meta.get("completion_tokens", 0),
                     cached_tokens=meta.get("cached_tokens", 0),
                     seconds=meta.get("seconds", 0.0),
                     details=f"{filename}: {len(transactions)} транзакцій")

        update(step="save", status="running", message="💾 Зберігаю...")
        res = db_bridge.save_result(transactions, chat_id=ws_id or iban,
                                    username=ws_label or "(web)", iban=iban) or {}
        inserted = res.get("inserted", len(transactions))
        skipped = res.get("skipped", 0)
        db_bridge.auto_export_to_sheets(transactions)

        done_msg = f"✅ Готово! {inserted} нових транзакцій"
        if skipped:
            done_msg += f" ({skipped} дублікатів пропущено)"
        update(status="done", step="save", iban=iban,
               tx_count=inserted, message=done_msg)
        _log.info("first-upload %s: готово, iban=%s, tx=%d", job_id, mask(iban), len(transactions))
    except ai.AIError as e:
        _log.error("first-upload %s: AI помилка: %s", job_id, e)
        update(status="error", error=_human_ai_error(str(e)))
    except Exception as e:
        _log.error("first-upload %s: %s", job_id, e, exc_info=True)
        update(status="error", error="Не вдалося обробити файл. Спробуйте ще раз.")


def _human_ai_error(err_text: str) -> str:
    """Переводит коды ошибок DeepSeek в понятные подсказки."""
    if "401" in err_text or "Unauthorized" in err_text:
        return ("Невірний DeepSeek API-ключ (401). Скопіюйте справжній ключ з "
                "platform.deepseek.com → API Keys та оновіть його в адмінці (Ключі API).")
    if "402" in err_text:
        return ("Недостатньо коштів на DeepSeek (402). Поповніть баланс на "
                "platform.deepseek.com (2–5 доларів вистачить надовго).")
    if "429" in err_text:
        return "Перевищено ліміт запитів DeepSeek (429). Зачекайте хвилину та спробуйте ще раз."
    if "timeout" in err_text.lower() or "timed out" in err_text.lower():
        return "AI відповів занадто довго. Спробуйте ще раз — або файл замалий для розпізнавання."
    return "AI не зміг розпізнати транзакції. Спробуйте інший формат файлу."


@router.get("/api/job/{job_id}")
async def api_job_status(job_id: str):
    """Статус фоновой задачи (poll с welcome-страницы)."""
    job = _jobs.get(job_id)
    if not job:
        return JSONResponse({"status": "unknown", "error": "Задачу не знайдено"}, status_code=404)
    return job


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
    db.log_usage("адмін", "видалено дані рахунку", source="admin",
                 details=f"{iban}: {deleted}")
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


@router.post("/api/deepseek/test-key")
async def api_deepseek_test_key(request: Request):
    """Проверка DeepSeek ключа. Если в теле передан key — проверяем ЕГО
    (введённый в форму, до сохранения); иначе — сохранённый в БД."""
    redir = auth.require_auth(request)
    if redir:
        return JSONResponse({"ok": False, "error": "Потрібна авторизація"}, status_code=401)
    import json as _json
    from bot import ai_engine as ai, db_bridge

    body = await request.body()
    key = ""
    try:
        data = _json.loads(body)
        key = (data.get("key") or "").strip()
    except (ValueError, AttributeError):
        pass
    if not key:
        key = db_bridge.get_deepseek_key()
        if not key:
            return JSONResponse({"ok": False, "error": "Ключ не заданий (ні в полі, ні в БД)"})
    if " " in key:
        return JSONResponse({"ok": False,
                             "error": "Ключ містить пробіл — здається, при копіюванні прилип зайвий текст"})
    try:
        models = await asyncio.to_thread(ai.list_models, key)
        return {"ok": True, "models": len(models), "current": db_bridge.get_deepseek_model()}
    except ai.AIError as e:
        return JSONResponse({"ok": False, "error": _human_ai_error(str(e))})


@router.post("/admin/model")
async def admin_save_model(request: Request, model: str = Form(...)):
    """Сохраняет выбранную модель DeepSeek."""
    redir = auth.require_auth(request)
    if redir:
        return redir
    from bot import db_bridge
    db_bridge.set_deepseek_model(model.strip())
    db.log_usage("адмін", "змінено модель AI", source="admin",
                 details=model.strip())
    return RedirectResponse("/admin#model", status_code=303)


# ============================================================
# Доступы по телефону (выдача/отзыв/продление)
# ============================================================
@router.post("/admin/grants")
async def admin_create_grant(request: Request, phone: str = Form(...),
                             label: str = Form(""), level: str = Form("view"),
                             days: str = Form("30"),
                             multi_account: bool = Form(False),
                             code: str = Form("")):
    """Выдаёт доступ-кабинет по номеру телефона. Повторная выдача = обновление.

    Требует второй фактор — код подтверждения выдачи (даже при админ-сессии)."""
    redir = auth.require_auth(request)
    if redir:
        return redir

    # код видачі — обов'язковий другий фактор
    if not auth.grant_code_is_set():
        db.log_usage("адмін", "попытка выдачи без заданного кода", source="admin",
                     ok=False, details=f"{db.mask_phone(db.normalize_phone(phone) or phone[:15])}")
        return _admin_back(False, "Помилка: код видачі не налаштований. Зверніться до власника системи.")
    if not auth.check_grant_code(code):
        db.log_usage("адмін", "НЕВІРНИЙ КОД ВИДАЧІ", source="admin", ok=False,
                     details=f"номер: {db.mask_phone(db.normalize_phone(phone) or phone[:15])}")
        return _admin_back(False, "Помилка: невірний код підтвердження видачі (спробу зафіксовано)")

    norm = db.normalize_phone(phone)
    if not norm:
        db.log_usage("адмін", "помилка видачі доступу", source="admin", ok=False,
                     details=f"некоректний номер: {phone[:30]}")
        return _admin_back(False, f"Помилка: некоректний номер «{phone[:30]}»")
    if level not in db.GRANT_LEVELS:
        level = "view"
    days_n = None if days in ("", "0", "none") else max(1, min(365, int(days)))
    back = "/access" if getattr(admin_create_grant, "_back_access", False) else "/admin"

    db.create_grant(norm, label.strip()[:60], level, "all", days_n,
                    multi_account=multi_account)
    db.log_usage("адмін", "видано доступ", source="admin",
                 details=f"{label.strip() or db.mask_phone(norm)} · {db.mask_phone(norm)} · "
                         f"{db.GRANT_LEVELS[level]} · "
                         f"{'без строку' if days_n is None else str(days_n) + ' дн.'} · "
                         f"{'мультирахунок' if multi_account else 'один рахунок'}")
    return _admin_back(True, f"Доступ видано: {label.strip() or db.mask_phone(norm)} "
                             f"({db.GRANT_LEVELS[level]}, "
                             f"{'без строку' if days_n is None else str(days_n) + ' дн.'}, "
                             f"{'🏦 всі рахунки' if multi_account else '🏦 один рахунок'})")


def _admin_back(ok: bool, msg: str, anchor: str = "access",
                page: str = "/admin") -> RedirectResponse:
    """Редирект назад (админка или /access) с уведомлением о результате."""
    from urllib.parse import quote
    url = f"{page}?ok={1 if ok else 0}&msg={quote(msg)}"
    if page == "/admin":
        url += f"#{anchor}"
    return RedirectResponse(url, status_code=303)


@router.post("/admin/provider")
async def admin_set_provider(request: Request, provider: str = Form(...)):
    """Перемикає провайдера ШІ (google = Gemini API / deepseek)."""
    redir = auth.require_auth(request)
    if redir:
        return redir
    from bot import db_bridge
    from bot.ai_engine import PROVIDERS
    provider = provider.lower()
    if provider not in PROVIDERS:
        return _admin_back(False, f"Помилка: невідомий провайдер «{provider[:20]}»", anchor="secrets")
    db_bridge.set_ai_provider(provider)
    db.log_usage("адмін", "змінено провайдера ШІ", source="admin",
                 details=f"{provider} · модель за замовчуванням: {PROVIDERS[provider]['default_model']}")
    return _admin_back(True, f"Провайдера змінено: {provider}. "
                             f"Модель: {PROVIDERS[provider]['default_model']}. "
                             f"Не забудьте ключ відповідного провайдера нижче.", anchor="secrets")


@router.post("/admin/grants/{gid}/level")
async def admin_grant_level(request: Request, gid: int, level: str = Form(...)):
    """Зміна рівня прав уже виданого доступу."""
    redir = auth.require_auth(request)
    if redir:
        return redir
    if level not in db.GRANT_LEVELS:
        db.log_usage("адмін", "помилка зміни рівня", source="admin",
                     details=f"grant #{gid}: невідомий рівень «{level[:20]}»")
        return _admin_back(False, f"Помилка: невідомий рівень «{level}»")
    ok = db.set_grant_level(gid, level)
    db.log_usage("адмін", "змінено рівень доступу" if ok else "помилка зміни рівня",
                 source="admin",
                 details=f"grant #{gid} → {db.GRANT_LEVELS[level]}")
    if ok:
        return _admin_back(True, f"Права доступу #{gid} змінено: {db.GRANT_LEVELS[level]}")
    return _admin_back(False, f"Помилка: доступ #{gid} не знайдено")


@router.post("/admin/grants/{gid}/multi")
async def admin_grant_multi(request: Request, gid: int, on: bool = Form(False)):
    """Вмикає/вимикає мультирахунок (спільний дашборд по всіх рахунках)."""
    redir = auth.require_auth(request)
    if redir:
        return redir
    ok = db.set_grant_multi(gid, on)
    db.log_usage("адмін", "мультирахунок " + ("увімкнено" if on else "вимкнено")
                 if ok else "помилка перемикання мультирахунку",
                 source="admin", details=f"grant #{gid}")
    if ok:
        return _admin_back(True, f"Мультирахунок для доступу #{gid} "
                                 f"{'увімкнено' if on else 'вимкнено'}")
    return _admin_back(False, f"Помилка: доступ #{gid} не знайдено")


@router.post("/admin/grants/{gid}/revoke")
async def admin_revoke_grant(request: Request, gid: int):
    redir = auth.require_auth(request)
    if redir:
        return redir
    ok = db.revoke_grant(gid)
    db.log_usage("адмін", "доступ відкликано" if ok else "помилка відкликання",
                 source="admin", details=f"grant #{gid}")
    if ok:
        return _admin_back(True, f"Доступ #{gid} відкликано")
    return _admin_back(False, f"Помилка: доступ #{gid} не знайдено")


@router.post("/admin/grants/{gid}/extend")
async def admin_extend_grant(request: Request, gid: int, days: int = 30):
    redir = auth.require_auth(request)
    if redir:
        return redir
    days = max(1, min(365, days))
    ok = db.extend_grant(gid, days)
    db.log_usage("адмін", "доступ продовжено" if ok else "помилка продовження",
                 source="admin", details=f"grant #{gid} +{days} дн.")
    if ok:
        return _admin_back(True, f"Доступ #{gid} продовжено на {days} дн.")
    return _admin_back(False, f"Помилка: доступ #{gid} не знайдено")


@router.post("/admin/iban-login-toggle")
async def admin_iban_login_toggle(request: Request, enabled: bool = Form(False)):
    """Переключає вхід за IBAN без доступу (за замовчуванням вимкнений)."""
    redir = auth.require_auth(request)
    if redir:
        return redir
    db.set_setting("allow_iban_login", bool(enabled))
    db.log_usage("адмін", "перемикач входу за IBAN", source="admin",
                 details="увімкнено" if enabled else "вимкнено")
    return _admin_back(enabled, "Вхід за IBAN увімкнено" if enabled else "Вхід за IBAN вимкнено")


@router.get("/api/metrics")
async def api_metrics(request: Request, date_from: str = "", date_to: str = "",
                      category: str = "all", month: str = "", year: str = ""):
    """KPI + графики. Автофильтр по IBAN + период (месяц/год)."""
    if auth.get_phone_session(request) and not _phone_user(request):
        return JSONResponse({"error": "Доступ припинено"}, status_code=401)
    iban, ws = _data_scope(request)
    txs = db.get_transactions(date_from=date_from, date_to=date_to,
                              category=category, iban=iban, chat_id=ws,
                              month=month, year=year, limit=10000)
    metrics = _aggregate(txs)
    metrics["alerts"] = _detect_anomalies(metrics)
    return metrics


@router.get("/api/transactions")
async def api_transactions(request: Request, date_from: str = "", date_to: str = "",
                           category: str = "all", month: str = "", year: str = "",
                           q: str = "", limit: int = 100):
    """Таблица транзакций. Автофильтр по IBAN + период + полнотекстовый поиск (q)."""
    iban, ws = _data_scope(request)
    # доп. фильтр «только свои счета» нужен в режиме агрегата («all»)
    my_ibans = ({a["iban"] for a in db.list_accounts(chat_id=ws)}
                if ws and iban == "all" else None)
    if q.strip():
        # FTS5-поиск; категория/период дополнительно фильтруем в Python (мало строк)
        txs = db.search_transactions(q, iban=iban, limit=500)
        if my_ibans is not None:
            txs = [t for t in txs if t.get("iban") in my_ibans]
        if category and category != "all":
            txs = [t for t in txs if t.get("category") == category]
        if month:
            txs = [t for t in txs if (t.get("date") or "").startswith(month)]
        if year:
            txs = [t for t in txs if (t.get("date") or "").startswith(year)]
        return txs[:limit]
    return db.get_transactions(date_from=date_from, date_to=date_to,
                               category=category, iban=iban, chat_id=ws,
                               month=month, year=year, limit=limit)


@router.get("/api/chat/search")
async def api_chat_search(request: Request, q: str = ""):
    """Полнотекстовый поиск по сообщениям чата текущего пользователя."""
    iban = auth.get_iban_session(request)
    if not iban:
        return JSONResponse({"error": "Сесія закінчилась"}, status_code=401)
    if not q.strip():
        return {"results": [], "count": 0}
    _, ws = _data_scope(request)
    results = db.search_chat(q, chat_id=ws or iban)
    return {"results": results, "count": len(results)}


@router.get("/api/periods")
async def api_periods(request: Request):
    """Доступные месяцы и годы для переключателя периодов."""
    iban, ws = _data_scope(request)
    return db.get_periods(iban=iban, chat_id=ws)


@router.get("/api/categories")
async def api_categories():
    return {"categories": db.get_setting("categories", [])}


@router.get("/api/accounts")
async def api_accounts(request: Request):
    """Список счетов с метриками — для перемикача рахунків на дашборді.

    Клиент видит только свои; без мультирахунку — лише один (перший) рахунок."""
    from bot.iban import mask
    grant = _phone_user(request)
    if grant:
        accounts = db.account_stats(chat_id=grant["phone"])
        if not grant.get("multi_account"):
            single = _client_single_iban(grant)
            accounts = [a for a in accounts if a["iban"] == single]
    else:
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


@router.get("/api/set-account")
async def api_set_account(request: Request, iban: str):
    """Перемикає рахунок дашборда (cookie) — з перевіркою прав доступу."""
    if iban != "all":
        from bot.iban import normalize, is_valid
        iban = normalize(iban)
        if not is_valid(iban):
            return JSONResponse({"ok": False, "error": "Невірний IBAN"}, status_code=400)
    grant = _phone_user(request)
    if grant:
        if iban == "all":
            if not grant.get("multi_account"):
                return JSONResponse({"ok": False,
                                     "error": "Мультирахунок вимкнено у вашому доступі"}, status_code=403)
        else:
            my = {a["iban"] for a in db.list_accounts(chat_id=grant["phone"])}
            if iban not in my:
                return JSONResponse({"ok": False, "error": "Це не ваш рахунок"}, status_code=403)
    resp = JSONResponse({"ok": True})
    auth.set_iban_session(resp, iban)
    return resp


@router.get("/api/attach-iban")
async def api_attach_iban(request: Request, iban: str):
    """Ставит IBAN-сессию по параметру (после первой загрузки виписки).
    Вызывается JS-ом после успешного /api/first-upload."""
    from bot.iban import normalize, is_valid
    if (not auth.is_authed(request) and not _phone_user(request)
            and not db.get_setting("allow_iban_login", False)):
        return JSONResponse({"ok": False, "error": "Вхід за IBAN вимкнено"}, status_code=403)
    norm = normalize(iban)
    if not is_valid(norm):
        return JSONResponse({"ok": False, "error": "Невірний IBAN"}, status_code=400)
    resp = JSONResponse({"ok": True})
    auth.set_iban_session(resp, norm)
    return resp


# ============================================================
# Первичная настройка (setup-мастер: пароль + ключи через UI)
# ============================================================
@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Мастер первого запуска. Если уже настроено — редирект в админку."""
    if not auth.is_setup_needed():
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": ""})


@router.post("/setup")
async def setup_submit(request: Request,
                       password: str = Form(...),
                       password2: str = Form(...),
                       deepseek_key: str = Form(""),
                       telegram_token: str = Form("")):
    """Сохраняет пароль (хэш в БД) + ключи (Fernet). Рестартит бота с токеном."""
    if not auth.is_setup_needed():
        return RedirectResponse("/admin", status_code=303)

    if password != password2:
        return templates.TemplateResponse(request, "setup.html", {
            "error": "Паролі не співпадають",
        }, status_code=400)
    if len(password) < 4:
        return templates.TemplateResponse(request, "setup.html", {
            "error": "Пароль має бути мінімум 4 символи",
        }, status_code=400)

    auth.set_admin_password(password)
    ds = deepseek_key.strip()
    if ds:
        # формат настоящего ключа DeepSeek: sk- + 32 hex-символа (35 всего)
        import re as _re
        if not _re.fullmatch(r"sk-[0-9a-f]{32}", ds):
            return templates.TemplateResponse(request, "setup.html", {
                "error": "DeepSeek ключ виглядає невірно. Справжній ключ: «sk-» + 32 символи "
                         "(приклад: sk-0be3f1a2c3d4e5f67890abcdef1234567). "
                         "Скопіюйте його повністю з platform.deepseek.com → API Keys.",
            }, status_code=400)
        db.set_secret("deepseek_key", ds)

    # бот стартует сразу, если токен ввели (иначе — позже через админку)
    if telegram_token.strip():
        db.set_secret("telegram_token", telegram_token.strip())
        from server import runtime
        await runtime.restart_bot()

    _log.info("✅ Первинне налаштування завершено")

    # сразу логиним и ведём на стартовую страницу (загрузка первой виписки)
    resp = RedirectResponse("/", status_code=303)
    auth.login(resp, password)
    return resp


# ============================================================
# Админка
# ============================================================
@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if auth.is_setup_needed():
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "admin_login.html", {"error": ""})


@router.post("/admin/login")
async def admin_login_submit(request: Request, password: str = Form(...),
                             next: str = Form("/admin")):
    # захист від відкритих редиректів — тільки внутрішні шляхи
    nxt = next if next.startswith("/") and not next.startswith("//") else "/admin"
    resp = RedirectResponse(nxt, status_code=303)
    ok = auth.login(resp, password)
    db.log_usage("адмін", "вхід в адмінку", source="admin", ok=ok,
                 details="пароль прийнято" if ok else "невірний пароль")
    if ok:
        return resp
    return templates.TemplateResponse(request, "admin_login.html", {
        "error": "Невірний пароль",
    }, status_code=401)


# ============================================================
# Швидка сторінка «Доступи» — тільки видача, без важкої адмінки
# ============================================================
@router.get("/access", response_class=HTMLResponse)
async def access_page(request: Request):
    """Легкий інструмент видачі доступів (для власника після вебінару)."""
    if not auth.is_authed(request):
        return templates.TemplateResponse(request, "access.html", {
            "need_login": True, "grants": [], "notice": "", "notice_ok": True,
            "grant_levels": db.GRANT_LEVELS, "counts": {},
        })
    grants = db.list_grants()
    active = [g for g in grants if g["status"] == "active"]
    return templates.TemplateResponse(request, "access.html", {
        "need_login": False,
        "grants": grants[:15],
        "grant_levels": db.GRANT_LEVELS,
        "notice": request.query_params.get("msg", ""),
        "notice_ok": request.query_params.get("ok", "1") == "1",
        "counts": {
            "active": len(active),
            "tg": sum(1 for g in grants if g["telegram_chat_id"]),
            "total": len(grants),
        },
    })


@router.get("/admin/logout")
async def admin_logout():
    # вихід → на головну сторінку системи (передбачувано для користувача)
    resp = RedirectResponse("/", status_code=303)
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
        "usage_totals": db.usage_totals(),
        "usage_by_user": db.usage_by_user(),
        "usage_recent": db.usage_recent(50),
        "grants": db.list_grants(),
        "grant_levels": db.GRANT_LEVELS,
        "accounts": db.account_stats(),
        "allow_iban_login": bool(db.get_setting("allow_iban_login", False)),
        "ai_provider": (db.get_setting("ai_provider") or "deepseek").lower(),
        "ai_provider_label": {"google": "Google Gemini API", "deepseek": "DeepSeek API"}
                             .get((db.get_setting("ai_provider") or "deepseek").lower(), "DeepSeek API"),
        "notice": request.query_params.get("msg", ""),
        "notice_ok": request.query_params.get("ok", "1") == "1",
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
        ds = deepseek_key.strip()
        # защита от прилипшего текста при копировании (напр. "... I DeepSeek")
        if " " in ds:
            return templates.TemplateResponse(request, "admin.html", {
                "thresholds": db.get_setting("alert_thresholds", {}),
                "categories": db.get_setting("categories", []),
                "sheets": db.get_sheets_config(),
                "sessions": db.list_sessions(limit=50),
                "secret_status": _secret_status(),
                "stats": db.stats(),
                "next_report_run": "—",
                "sheets_error": "DeepSeek ключ містить пробіл — здається, до нього прилип зайвий "
                                "текст при копіюванні. Вставте чистий ключ (sk- + 32 символи).",
            }, status_code=400)
        db.set_secret("deepseek_key", ds)
    if telegram_token.strip():
        db.set_secret("telegram_token", telegram_token.strip())
        # перезапуск бота с новым токеном — в фоне, не блокируем ответ
        import asyncio as _a
        from server import runtime
        _a.get_event_loop().create_task(runtime.restart_bot())
    db.log_usage("адмін", "оновлено ключі API", source="admin",
                 details=", ".join(filter(None, [
                     "DeepSeek" if deepseek_key.strip() else "",
                     "Telegram" if telegram_token.strip() else ""])) or "—")
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
