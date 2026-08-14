"""
Запись результатов в Google Sheets для демо.

Использует gspread + Google Service Account. Credentials можно передать ДВУМЯ способами:
  1. credentials_info (dict) — вставляется из админки (JSON-ключ), приоритетный путь
  2. credentials_path (str) — путь к файлу, фоллбэк для .env (GOOGLE_CREDENTIALS_JSON)

Обе функции деградируют мягко: если Sheets не настроен — бот продолжит работать.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

_log = logging.getLogger("sheets_writer")

DEFAULT_SHEET_ID = "1mQor8lIqxTto-IIHuX4tMWoOHzeeg-KTBiJWMA10jwQ"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetsNotConfigured(Exception):
    """Google Sheets не настроен (нет кредов или gspread не установлен)."""


class SheetsError(Exception):
    """Ошибка записи в Google Sheets (сеть, права, невалидный ключ)."""


def _build_credentials(*, credentials_info: dict | None = None,
                       credentials_path: str | None = None):
    """Создаёт google Credentials из dict (приоритет) или пути к файлу (фоллбэк).

    Лениво импортирует google-auth (тяжелая зависимость).
    """
    try:
        from google.oauth2.service_account import Credentials
    except ImportError as e:
        raise SheetsNotConfigured(
            "google-auth не установлен. Установите: pip install google-auth"
        ) from e

    if credentials_info:
        try:
            return Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
        except (ValueError, KeyError) as e:
            raise SheetsError(f"Невалидный JSON Service Account: {e}") from e

    if credentials_path and os.path.exists(credentials_path):
        try:
            return Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        except (ValueError, KeyError) as e:
            raise SheetsError(f"Не удалось прочитать файл кредов: {e}") from e

    raise SheetsNotConfigured(
        "Google Service Account не налаштований. Додайте JSON-ключ у адмінці "
        "(розділ «Експорт у Google Sheets») або задайте GOOGLE_CREDENTIALS_JSON у .env."
    )


def _open_sheet(*, sheet_id: str, credentials_info: dict | None = None,
                credentials_path: str | None = None):
    """Авторизуется и открывает таблицу по ID. Возвращает (gspread_client, spreadsheet)."""
    try:
        import gspread
    except ImportError as e:
        raise SheetsNotConfigured(
            "gspread не установлен. Установите: pip install gspread"
        ) from e

    creds = _build_credentials(credentials_info=credentials_info,
                               credentials_path=credentials_path)
    client = gspread.authorize(creds)
    try:
        return client, client.open_by_key(sheet_id)
    except Exception as e:
        # gspread может бросить SpreadsheetNotFound или APIError (429 rate limit и т.д.)
        from gspread.exceptions import SpreadsheetNotFound, APIError
        if isinstance(e, SpreadsheetNotFound):
            raise SheetsError(
                f"Таблицю {sheet_id} не знайдено. Перевірте ID і що Service Account має доступ (Поділитися → Редактор)."
            ) from e
        if isinstance(e, APIError):
            raise SheetsError(f"Google API помилка: {e}") from e
        raise


def _ensure_worksheet(spreadsheet, name: str, headers: list[str]):
    """Возвращает вкладку по имени; создаёт с заголовками, если её нет."""
    try:
        ws = spreadsheet.worksheet(name)
    except Exception:
        # вкладки нет → создаём (используем широкую обработку, т.к. gspread ленивый)
        ws = spreadsheet.add_worksheet(title=name, rows="1000", cols=len(headers))
        ws.append_row(headers)
    return ws


def append_transactions(transactions: list[dict], *,
                        sheet_id: str = DEFAULT_SHEET_ID,
                        credentials_info: dict | None = None,
                        credentials_path: Optional[str] = None) -> str:
    """Пишет транзакции в таб «Транзакції». Возвращает URL вкладки.

    transactions: [{date, description, amount, type, category}, ...]
    credentials_info: dict из JSON-ключа (приоритет). Если None — берётся из .env.
    """
    credentials_path = credentials_path or os.getenv("GOOGLE_CREDENTIALS_JSON")
    _client, ss = _open_sheet(sheet_id=sheet_id,
                              credentials_info=credentials_info,
                              credentials_path=credentials_path)

    headers = ["Дата", "Опис", "Сума", "Тип", "Категорія", "Тиждень", "Країна"]
    ws = _ensure_worksheet(ss, "Транзакції", headers)

    rows = []
    for t in transactions:
        rows.append([
            t.get("date", ""), t.get("description", ""), t.get("amount", 0),
            t.get("type", ""), t.get("category", ""), _iso_week(t.get("date", "")),
            t.get("country", ""),
        ])
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={ws.id}"


def append_dashboard(metrics: dict, *,
                     sheet_id: str = DEFAULT_SHEET_ID,
                     credentials_info: dict | None = None,
                     credentials_path: Optional[str] = None) -> str:
    """Пишет рядок метрик в таб «Дашборд». Возвращает URL вкладки.

    metrics: должен содержать ключ 'dashboard_row' (dict) или сам быть dict.
    """
    credentials_path = credentials_path or os.getenv("GOOGLE_CREDENTIALS_JSON")
    _client, ss = _open_sheet(sheet_id=sheet_id,
                              credentials_info=credentials_info,
                              credentials_path=credentials_path)

    row = metrics.get("dashboard_row", metrics)
    headers = list(row.keys())
    ws = _ensure_worksheet(ss, "Дашборд", headers)
    ws.append_row(list(row.values()), value_input_option="USER_ENTERED")

    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={ws.id}"


def _iso_week(date_str: str) -> str:
    """'2026-05-28' → '2026-W22'. Для колонки «Тиждень»."""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
        iso = d.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    except (ValueError, TypeError):
        return "unknown"


# ============================================================
# P&L «Факты и планы» — обновление Fact-ячеек по маппингу
# Один лист = один месяц (копия шаблона «Факты и планы»).
# ============================================================

PL_TEMPLATE = "Факты и планы"  # мастер-шаблон (с планами/формулами)

# Карта: (country, category) → Fact-ячейки.
# Для «продажі» берём income (дохід), для остальных — expense (витрата).
PL_CELL_MAP = {
    "продажі":          ("E3",  "H3",  "income"),    # Доходи
    "реклама":          ("E5",  "H5",  "expense"),   # Витрати на рекламу
    "експерти":         ("E8",  "H8",  "expense"),   # Витрати на експертів
    "зарплата":         ("E11", "H11", "expense"),   # Зарплатний фонд
    "податки":          ("E16", "H16", "expense"),   # Податки
    "інше":             ("E22", "H22", "expense"),   # Інші юридичні витрати
    "підписки":         ("E25", "H25", "expense"),   # Підписки
    "підтримка сайту":  ("E27", "H27", "expense"),   # Підтримка сайту
    "підрядники":       ("E28", "H28", "expense"),   # Підрядники
    "hr":               ("E29", "H29", "expense"),   # HR-витрати
    "повернення":       ("E32", "H32", "expense"),   # Повернення
    "фаундери":         ("E34", "H34", "expense"),   # Зарплата фаундерів
    # оренда → поки не маппимо (немає рядка в шаблоні P&L)
    # переказ → ігнор (внутрішні перекази не доход/витрата)
}

# украинские названия месяцев (для имён вкладок)
_MONTH_NAMES_UA = [
    "", "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]


def month_tab_name(month: tuple[int, int]) -> str:
    """(3, 2026) → «Березень 2026»."""
    m, y = month
    name = _MONTH_NAMES_UA[m] if 1 <= m <= 12 else f"Місяць {m}"
    return f"{name} {y}"


def _get_pl_template(ss):
    """Возвращает worksheet-шаблон «Факты и планы»."""
    try:
        return ss.worksheet(PL_TEMPLATE)
    except Exception:
        return ss.worksheet("Факти і плани")


def ensure_period_pl(ss, tab_name: str):
    """Гарантирует, что вкладка периода (месяц или год) существует.
    Если нет — копирует шаблон «Факты и планы» с новым именем.
    Возвращает worksheet.
    """
    existing = [ws.title for ws in ss.worksheets()]
    if tab_name in existing:
        return ss.worksheet(tab_name)
    template = _get_pl_template(ss)
    new_ws = ss.duplicate_sheet(
        source_sheet_id=template.id,
        new_sheet_name=tab_name,
    )
    _log.info("Создана вкладка P&L: %s", tab_name)
    return new_ws


def ensure_monthly_pl(ss, month: tuple[int, int]):
    """Совместимость со старым API. Использует ensure_period_pl."""
    return ensure_period_pl(ss, month_tab_name(month))
    tab_name = month_tab_name(month)
    existing = [ws.title for ws in ss.worksheets()]

    if tab_name in existing:
        return ss.worksheet(tab_name)

    # копируем шаблон → новая вкладка с именем месяца
    template = _get_pl_template(ss)
    # gspread: duplicate(source_sheet, insert_sheet_index=None, new_sheet_name=None)
    new_ws = ss.duplicate_sheet(
        source_sheet_id=template.id,
        new_sheet_name=tab_name,
    )
    # обновляем B1 (месяц) в новой вкладке
    _client = ss.client
    new_ws.update_acell("B1", _MONTH_NAMES_UA[month[0]] if 1 <= month[0] <= 12 else "")
    _log.info("Создана месячная вкладка P&L: %s", tab_name)
    return new_ws


def read_pl_month_label(*, sheet_id: str, credentials_info: dict | None = None,
                        credentials_path: str | None = None) -> str:
    """Читает B1 шаблона «Факты и планы» — текущий месяц."""
    _client, ss = _open_sheet(sheet_id=sheet_id, credentials_info=credentials_info,
                              credentials_path=credentials_path)
    ws = _get_pl_template(ss)
    return ws.acell("B1").value or ""


def update_pl(pl_data: dict, *, month: tuple[int, int], sheet_id: str,
              credentials_info: dict | None = None,
              credentials_path: str | None = None) -> dict:
    """Обновляет Fact-ячейки P&L в месячной вкладке.

    month: (номер, год) — определяет вкладку (создаёт если нет).
    pl_data: {(country, category): {"income": float, "expense": float}}
    """
    _client, ss = _open_sheet(sheet_id=sheet_id, credentials_info=credentials_info,
                              credentials_path=credentials_path)
    ws = ensure_monthly_pl(ss, month)

    updates = []
    skipped_formulas = 0

    # читаем текущие значения целевых ячеек (для защиты формул)
    target_cells = []
    for _, (ua_cell, pl_cell, _) in PL_CELL_MAP.items():
        target_cells.extend([ua_cell, pl_cell])
    existing = {}
    try:
        for cell in target_cells:
            existing[cell] = ws.acell(cell).value or ""
    except Exception:
        pass

    for cat, (ua_cell, pl_cell, field) in PL_CELL_MAP.items():
        for country, cell in [("UA", ua_cell), ("PL", pl_cell)]:
            key = (country, cat)
            amount = pl_data.get(key, {}).get(field, 0)
            if not amount:
                continue  # нет данных — не трогаем (формулы не ломаем)
            cur = existing.get(cell, "")
            if cur.startswith("="):
                skipped_formulas += 1
                continue
            updates.append((cell, round(amount, 2)))

    updated_cells = []
    if updates:
        batch_data = [{"range": cell, "values": [[val]]} for cell, val in updates]
        ws.batch_update(batch_data, value_input_option="USER_ENTERED")
        updated_cells = [c for c, _ in updates]

    return {
        "updated": len(updated_cells),
        "cells": updated_cells,
        "skipped_formulas": skipped_formulas,
        "tab": month_tab_name(month),
    }


def update_pl_year(pl_data: dict, *, year: int, sheet_id: str,
                   credentials_info: dict | None = None,
                   credentials_path: str | None = None) -> dict:
    """Обновляет годовую вкладку P&L (напр. «2026»). Показує що є, навіть якщо рік не закінчено."""
    tab_name = f"{year}"
    _client, ss = _open_sheet(sheet_id=sheet_id, credentials_info=credentials_info,
                              credentials_path=credentials_path)
    ws = ensure_period_pl(ss, tab_name)
    # обновляем B1 — название года
    ws.update_acell("B1", str(year))

    updates = []
    skipped_formulas = 0
    target_cells = []
    for _, (ua_cell, pl_cell, _) in PL_CELL_MAP.items():
        target_cells.extend([ua_cell, pl_cell])
    existing = {}
    try:
        for cell in target_cells:
            existing[cell] = ws.acell(cell).value or ""
    except Exception:
        pass

    for cat, (ua_cell, pl_cell, field) in PL_CELL_MAP.items():
        for country, cell in [("UA", ua_cell), ("PL", pl_cell)]:
            key = (country, cat)
            amount = pl_data.get(key, {}).get(field, 0)
            if not amount:
                continue
            cur = existing.get(cell, "")
            if cur.startswith("="):
                skipped_formulas += 1
                continue
            updates.append((cell, round(amount, 2)))

    updated_cells = []
    if updates:
        batch_data = [{"range": cell, "values": [[val]]} for cell, val in updates]
        ws.batch_update(batch_data, value_input_option="USER_ENTERED")
        updated_cells = [c for c, _ in updates]

    return {
        "updated": len(updated_cells),
        "cells": updated_cells,
        "skipped_formulas": skipped_formulas,
        "tab": tab_name,
    }
