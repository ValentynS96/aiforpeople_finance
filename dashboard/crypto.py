"""
Шифрование секретов для админки.

Использует Fernet (симметричное шифрование). Мастер-ключ SECRET_KEY задаётся
в .env. Если SECRET_KEY не задан — генерируется и сохраняется автоматически
в data/secret.key (один раз, при первом запуске).

Секреты (DeepSeek/Telegram/Google), которые админ вводит в UI, шифруются
перед записью в SQLite и расшифровываются только при использовании ботом.
"""

import base64
import hashlib
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# Папка для мастер-ключа (рядом с data.db)
_KEY_DIR = Path(__file__).resolve().parent.parent / "data"
_KEY_FILE = _KEY_DIR / "secret.key"


class CryptoError(Exception):
    """Ошибка шифрования/расшифровки."""


def _derive_fernet_key(passphrase: str) -> bytes:
    """Любая строка-пароль → валидный 32-байтный base64 Fernet-ключ (детерминированно).

    Используем PBKDF2-HMAC-SHA256. Salt фиксированный (это не для защиты от
    перебора — для стабильности: тот же SECRET_KEY → тот же ключ → те же данные
    читаемы после рестарта). Случайность salt здесь не нужна: защита от перебора
    обеспечивает длина самого SECRET_KEY.
    """
    salt = b"aiforpeople-dashboard-v1"
    derived = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations=100_000, dklen=32)
    return base64.urlsafe_b64encode(derived)


def _load_or_create_key() -> bytes:
    """Возвращает Fernet-ключ. Приоритет: env SECRET_KEY → файл → создать новый."""
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        # любой пароль → валидный ключ
        return _derive_fernet_key(env_key)

    _KEY_DIR.mkdir(parents=True, exist_ok=True)
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes()

    # нет env и нет файла — генерим случайный (и сохраняем, чтобы не терять данные)
    key = Fernet.generate_key()
    _KEY_FILE.write_bytes(key)
    _KEY_FILE.chmod(0o600)  # только владелец
    return key


def _fernet() -> Fernet:
    return Fernet(_load_or_create_key())


def encrypt(plaintext: str) -> str:
    """Открытый текст → зашифрованная строка (base64). None/пусто → пусто."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Зашифрованная строка → открытый текст. Пусто → пусто."""
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise CryptoError(
            "Не удалось расшифровать секрет. Возможно, изменился SECRET_KEY "
            "или повреждена БД."
        ) from e


def mask(value: str) -> str:
    """Маскирует секрет для показа в UI: sk-abc...xyz"""
    if not value or len(value) <= 8:
        return "•" * 8 if value else ""
    return f"{value[:4]}...{value[-4:]}"
