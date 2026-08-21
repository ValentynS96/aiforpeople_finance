"""
Авторизация админки: пароль задаётся через setup-мастер (хранится хэшем
в БД) или через ADMIN_PASSWORD в .env (для продвинутых, приоритет env).
Signed-cookie сессия.

Безопасность:
- Пароль сравнивается через hmac.compare_digest (timing-safe).
- Хэш пароля в БД: PBKDF2-HMAC-SHA256, случайная соль.
- Cookie подписан HMAC-SHA256, содержит expiry + session-id (реверсивный).
- secret_key: env SECRET_KEY || автогенерация в data/secret.key (см. crypto).
- samesite=strict. Secure-флаг включается автоматически для https-запросов
  (для localhost/http остаётся без Secure, иначе браузер не примет cookie).
"""

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse

_log = logging.getLogger("auth")

COOKIE_NAME = "admin_session"
SESSION_DAYS = 7

# Файл секрета подписи cookie (рядом с БД): сессии ВЫЖИВАЮТ рестарты контейнера.
_SIGNING_KEY_FILE = Path(__file__).resolve().parent.parent / "data" / "session.key"


def _signing_secret() -> bytes:
    """Секрет для подписи cookie: env SECRET_KEY || файл data/session.key (авто-генерация).

    Раньше был случайный per-process — каждая пересборка контейнера сбрасывала
    все сессии (пользователя «выбивало»). Теперь секрет постоянный.
    """
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key.encode("utf-8")
    try:
        if _SIGNING_KEY_FILE.exists():
            return _SIGNING_KEY_FILE.read_text().strip().encode("utf-8")
        _SIGNING_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32)
        _SIGNING_KEY_FILE.write_text(key)
        _SIGNING_KEY_FILE.chmod(0o600)
        return key.encode("utf-8")
    except OSError as e:
        # недоступная ФС — fallback на per-process (сессии до рестарта)
        _log.warning("session.key недоступен (%s) — сессии сбросятся при рестарте", e)
        return secrets.token_hex(32).encode("utf-8")


def admin_password() -> str:
    """Пароль админки из env (если задан — приоритет). Пусто, если нет."""
    return os.getenv("ADMIN_PASSWORD") or ""


def set_admin_password(password: str):
    """Сохраняет пароль админки в БД как PBKDF2-хэш с солью."""
    from dashboard import db as _db
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), 100_000
    ).hex()
    _db.set_setting("admin_password_hash", {"salt": salt, "hash": digest})


def _check_db_password(password: str) -> bool:
    """Сверяет пароль с хэшем из БД (timing-safe)."""
    from dashboard import db as _db
    record = _db.get_setting("admin_password_hash")
    if not record or not isinstance(record, dict):
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"),
        record["salt"].encode("ascii"), 100_000
    ).hex()
    return hmac.compare_digest(digest, record["hash"])


def is_setup_needed() -> bool:
    """True если первичная настройка не пройдена: пароля нет ни в env, ни в БД."""
    from dashboard import db as _db
    if admin_password():
        return False
    record = _db.get_setting("admin_password_hash")
    return not (record and isinstance(record, dict) and record.get("hash"))


def _make_token(expires_at: str, session_id: str) -> str:
    """Подписанный токен: expires|session_id|hmac.

    Разделитель '|' — его нет ни в ISO-timestamp, ни в IBAN, ни в hex-подписи.
    (Точка не подходит: ISO-time содержит '.' в микросекундах.)
    """
    secret = _signing_secret()
    payload = f"{expires_at}|{session_id}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def _verify_token(token: str) -> bool:
    """Проверяет подпись и срок действия токена."""
    if not token or token.count("|") != 2:
        return False
    expires_at, session_id, sig = token.split("|")
    expected = _make_token(expires_at, session_id)
    if not hmac.compare_digest(token, expected):
        return False
    try:
        exp = datetime.fromisoformat(expires_at)
        return datetime.now(timezone.utc) < exp
    except ValueError:
        return False


def check_password(password: str) -> bool:
    """Проверяет пароль: env ADMIN_PASSWORD (приоритет) → хэш из БД.
    Timing-safe. True если пароль верный."""
    if not password:
        return False
    env_pw = admin_password()
    if env_pw:
        pw_bytes = password.encode("utf-8", errors="surrogatepass")
        expected_bytes = env_pw.encode("utf-8", errors="surrogatepass")
        return hmac.compare_digest(pw_bytes, expected_bytes)
    return _check_db_password(password)


# --- Код підтвердження видачі доступів (другий фактор для /admin/grants) ---
GRANT_CODE_KEY = "grant_code_hash"


def set_grant_code(code: str):
    """Сохраняет код выдачи доступов как PBKDF2-хэш (как пароль админки)."""
    from dashboard import db as _db
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", code.encode("utf-8"), salt.encode("ascii"), 100_000
    ).hex()
    _db.set_setting(GRANT_CODE_KEY, {"salt": salt, "hash": digest})


def grant_code_is_set() -> bool:
    from dashboard import db as _db
    rec = _db.get_setting(GRANT_CODE_KEY)
    return bool(rec and isinstance(rec, dict) and rec.get("hash"))


def check_grant_code(code: str) -> bool:
    """Timing-safe проверка кода выдачи доступов."""
    from dashboard import db as _db
    rec = _db.get_setting(GRANT_CODE_KEY)
    if not rec or not isinstance(rec, dict):
        # код не заданий — видача заборонена (безпечний дефолт)
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", (code or "").encode("utf-8"),
        rec["salt"].encode("ascii"), 100_000
    ).hex()
    return hmac.compare_digest(digest, rec["hash"])


def login(response, password: str) -> bool:
    """Проверяет пароль и ставит cookie. True если пароль верный."""
    if not check_password(password):
        return False
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    session_id = secrets.token_hex(16)
    token = _make_token(expires, session_id)
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="strict",
    )
    return True


def logout(response):
    response.delete_cookie(COOKIE_NAME)


def is_authed(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    return _verify_token(token) if token else False


def require_auth(request: Request):
    """Возвращает RedirectResponse на /admin/login если не авторизован, иначе None."""
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


# ============================================================
# IBAN-сессия (для входа в дашборд по номеру счёта)
# ============================================================
IBAN_COOKIE = "session_iban"


def set_iban_session(response, iban: str):
    """Ставит подписанный cookie с IBAN пользователя (7 дней)."""
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    token = _make_token(expires, iban)
    response.set_cookie(
        IBAN_COOKIE, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
    )


def clear_iban_session(response):
    response.delete_cookie(IBAN_COOKIE)


def get_iban_session(request: Request) -> str:
    """Возвращает IBAN из cookie (верифицируя подпись) или пустую строку."""
    token = request.cookies.get(IBAN_COOKIE)
    if not token:
        return ""
    if token.count("|") != 2:
        return ""
    expires_at, iban, sig = token.split("|")
    expected = _make_token(expires_at, iban)
    if not hmac.compare_digest(token, expected):
        return ""
    try:
        exp = datetime.fromisoformat(expires_at)
        if datetime.now(timezone.utc) >= exp:
            return ""
    except ValueError:
        return ""
    return iban


# ============================================================
# Телефонная сессия (вход по номеру — доступ выдаёт админ)
# ============================================================
PHONE_COOKIE = "phone_session"


def set_phone_session(response, phone: str, level: str, scope: str):
    """Ставит подписанный cookie телефонной сессии: phone:level:scope.

    Сам cookie живёт 7 дней; актуальность доступа (отзыв/истечение)
    дополнительно проверяется по БД на каждом запросе (см. routes._phone_user).
    """
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    token = _make_token(expires, f"{phone}:{level}:{scope}")
    response.set_cookie(
        PHONE_COOKIE, token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
    )


def clear_phone_session(response):
    response.delete_cookie(PHONE_COOKIE)


def get_phone_session(request: Request) -> dict | None:
    """Телефонная сессия из cookie: {phone, level, scope} или None."""
    token = request.cookies.get(PHONE_COOKIE)
    if not token or token.count("|") != 2:
        return None
    expires_at, payload, sig = token.split("|")
    if not hmac.compare_digest(token, _make_token(expires_at, payload)):
        return None
    try:
        exp = datetime.fromisoformat(expires_at)
        if datetime.now(timezone.utc) >= exp:
            return None
    except ValueError:
        return None
    parts = payload.split(":")
    if len(parts) != 3:
        return None
    phone, level, scope = parts
    return {"phone": phone, "level": level, "scope": scope}
