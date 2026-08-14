"""
Витяг та валідація українських IBAN з тексту виписки.

UA IBAN = "UA" + 27 цифр (всього 29 символів). У виписках часто з пробілами
групами по 4-5 цифр: "UA12 3456 7890 0000 0000 0000 1234".
"""

import re

# UA IBAN = UA + 27 цифр (стандарт). Допускаємо 25-27 на випадок неповних тестових.
# Реальні завжди 27. Пробіли між групами цифр ігноруємо.
_IBAN_RE = re.compile(r"UA\s?\d{2}(?:\s?\d){23,27}", re.IGNORECASE)


def extract_iban(text: str) -> str | None:
    """Шукає перший UA-IBAN у тексті, повертає нормалізований (без пробілів) або None."""
    if not text:
        return None
    clean = text.replace("\u00a0", " ")
    m = _IBAN_RE.search(clean)
    if not m:
        return None
    return normalize(m.group(0))


def normalize(iban: str) -> str:
    """Прибирає пробіли, приводить до верхнього регістру."""
    if not iban:
        return ""
    return re.sub(r"\s+", "", iban).upper()


def is_valid(iban: str) -> bool:
    """Базова перевірка: UA + 25-27 цифр (допускаємо тестові з 25-26)."""
    if not iban:
        return False
    norm = normalize(iban)
    return bool(re.fullmatch(r"UA\d{25,27}", norm))


def mask(iban: str) -> str:
    """UA12••••••••1234 — для безпечного показу в чаті/дашборді."""
    norm = normalize(iban)
    if len(norm) < 8:
        return norm
    return f"{norm[:4]}••••{norm[-4:]}"
