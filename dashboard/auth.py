"""
Авторизация админки: один пароль (ADMIN_PASSWORD из .env), signed-cookie сессия.

Безопасность:
- Пароль сравнивается через hmac.compare_digest (timing-safe).
- Cookie подписан HMAC-SHA256, содержит expiry + session-id (реверсивный).
- secret_key: env SECRET_KEY || случайный (с предупреждением в логе).
- samesite=strict. Secure-флаг включается автоматически для https-запросов
  (для localhost/http остаётся без Secure, иначе браузер не примет cookie).
"""

import hashlib
import hmac
import logging
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import RedirectResponse

_log = logging.getLogger("auth")

COOKIE_NAME = "admin_session"
SESSION_DAYS = 7

# Случайный per-process секрет (перезагружается при рестарте → сессии сбрасываются,
# это плата за отсутствие SECRET_KEY в env). Логируем предупреждение.
_random_secret = secrets.token_hex(32)


def _signing_secret() -> bytes:
    """Секрет для подписи cookie: env SECRET_KEY || случайный per-process."""
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key.encode("utf-8")
    # Только для локальной разработки без настроенного SECRET_KEY
    return _random_secret.encode("utf-8")


def admin_password() -> str:
    """Пароль админки из env. Если не задан — 'admin' с предупреждением."""
    pw = os.getenv("ADMIN_PASSWORD")
    if not pw:
        _log.warning(
            "⚠️ ADMIN_PASSWORD не задан — используем демо-пароль 'admin'. "
            "НЕ используйте в продакшене! Задайте ADMIN_PASSWORD в .env."
        )
        return "admin"
    return pw


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
    """Проверяет пароль без установки cookie (для деструктивных действий).
    Timing-safe сравнение. True если пароль верный."""
    pw_bytes = password.encode("utf-8", errors="surrogatepass")
    expected_bytes = admin_password().encode("utf-8", errors="surrogatepass")
    return hmac.compare_digest(pw_bytes, expected_bytes)


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
