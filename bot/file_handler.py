"""
Приём PDF/CSV файлов выписок от Telegram и подготовка к категоризации.

Поток:
    Telegram document → download_as_bytearray → server.parser.parse()
    → markdown (полный текст) → iban.extract_iban()
    → returns (text_for_ai, iban, filename)

PDF парсится через Docling (с OCR), CSV — через stdlib csv.
Возвращает текст для AI + IBAN. Не вызывает AI — это делает бот.
"""

import logging
import sys
from pathlib import Path

from bot.iban import extract_iban

_repo = str(Path(__file__).resolve().parent.parent)
if _repo not in sys.path:
    sys.path.insert(0, _repo)

_log = logging.getLogger("file_handler")

ALLOWED_EXT = {".pdf", ".csv", ".txt"}
MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 МБ (лимит Telegram getFile)


class FileParseError(Exception):
    """Ошибка парсинга файла — ловится в боте, показывается юзеру."""


async def download_document(update) -> tuple[bytes, str]:
    """Скачивает документ из Telegram в память. Возвращает (content, filename).

    Лимит 20 МБ (Telegram bot API). Большие файлы требуют local Bot API server.
    """
    doc = update.message.document
    if not doc:
        raise FileParseError("Це не файл.")

    filename = doc.file_name or "statement"
    ext = Path(filename).suffix.lower()

    if ext not in ALLOWED_EXT:
        raise FileParseError(
            f"Підтримуються лише PDF, CSV або TXT. Отримав: {ext or '(без розширення)'}"
        )

    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        raise FileParseError(
            f"Файл занадто великий ({doc.file_size // 1024 // 1024} МБ). Максимум 20 МБ."
        )

    try:
        tg_file = await doc.get_file()
        raw = await tg_file.download_as_bytearray()
        return bytes(raw), filename
    except Exception as e:
        raise FileParseError(f"Не вдалося завантажити файл: {e}") from e


def parse_to_text(content: bytes, filename: str) -> str:
    """Парсит файл в текст через server.parser. Возвращает markdown/текст.

    CSV — без тяжёлых зависимостей. PDF — через Docling (lazy import).
    """
    from server.parser import parse

    try:
        _tables, markdown = parse(content, filename)
    except RuntimeError as e:
        # Docling не установлен
        raise FileParseError(
            "Парсинг PDF недоступний (Docling не встановлений). "
            "Надішліть CSV або скопіюйте текст виписки."
        ) from e
    except Exception as e:
        raise FileParseError(f"Не вдалося розібрати файл: {e}") from e

    if not markdown or len(markdown.strip()) < 50:
        raise FileParseError(
            "Файл порожній або не містить розпізнаного тексту. "
            "Якщо це скан — переконайтесь, що якість достатня."
        )

    return markdown


def extract_account_info(text: str) -> dict:
    """Извлекает IBAN из текста выписки. Возвращает {iban, iban_mask}.

    IBAN может быть None — тогда бот попросит ввести вручную.
    """
    iban = extract_iban(text)
    from bot.iban import mask
    return {
        "iban": iban,
        "iban_mask": mask(iban) if iban else None,
    }
