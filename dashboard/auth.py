"""
Сесії без паролів (локальна редакція): «адмінки» з паролем більше немає —
стартова сторінка /admin («Налаштування») відкрита, доступ до неї = доступ
до комп'ютера. Тут лишаються лише підписані cookie-сесії:

- IBAN-сесія (поточний рахунок дашборда);
- телефонна сесія (вхід за номером, доступ створюється автоматично).

Безпека cookie:
- Cookie підписаний HMAC-SHA256, містить expiry.
- secret_key: env SECRET_KEY || автогенерация в data/session.key.
"""

import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Request

_log = logging.getLogger("auth")

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


def is_setup_needed() -> bool:
    """True, якщо ще не налаштовано жодного ключа ШІ (Google чи DeepSeek).

    Локальна редакція: майстер першого запуску запитує вибір провайдера й
    API-ключ — це єдине обов'язкове налаштування.
    """
    from dashboard import db as _db
    if _db.secret_is_set("deepseek_key"):
        return False
    return not os.getenv("DEEPSEEK_API_KEY")


def _make_token(expires_at: str, session_id: str) -> str:
    """Подписанный токен: expires|session_id|hmac.

    Разделитель '|' — его нет ни в ISO-timestamp, ни в IBAN, ни в hex-подписи.
    (Точка не подходит: ISO-time содержит '.' в микросекундах.)
    """
    secret = _signing_secret()
    payload = f"{expires_at}|{session_id}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


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
# Телефонная сессия (вход за номером — кабінет створюється автоматично)
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
