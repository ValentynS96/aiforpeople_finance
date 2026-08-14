import csv
import logging
import tempfile
from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd

from server.models import ParsedTable

_log = logging.getLogger(__name__)

_converter = None  # DocumentConverter (lazy)


def _get_converter():
  """Ленивая инициализация Docling. Импортируется только при реальном парсинге PDF,
  чтобы сервер (дашборд) стартовал без тяжёлой зависимости docling."""
  global _converter
  if _converter is None:
    try:
      from docling_core.types.doc import TableItem  # noqa: F401
      from docling.datamodel.base_models import InputFormat
      from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as e:
      raise RuntimeError(
        "Docling не установлен. Установите: pip install docling "
        "(нужно только для парсинга PDF через /parse-tool)"
      ) from e
    _log.info("Initializing Docling DocumentConverter (first run, may download models)...")
    _converter = DocumentConverter(
      allowed_formats=[InputFormat.PDF, InputFormat.CSV],
      format_options={
        InputFormat.PDF: PdfFormatOption(),
      },
    )
    _log.info("Docling converter ready")
  return _converter


def parse_csv(content: bytes) -> list[ParsedTable]:
  text = content.decode("utf-8-sig", errors="replace")
  sniffer_sample = text[:8192]
  try:
    dialect = csv.Sniffer().sniff(sniffer_sample)
  except csv.Error:
    dialect = csv.excel

  reader = csv.DictReader(StringIO(text), dialect=dialect)
  headers = reader.fieldnames or []
  rows = []
  for row in reader:
    cleaned = {k.strip(): (v.strip() if v else "") for k, v in row.items() if k}
    if any(cleaned.values()):
      rows.append(cleaned)

  return [ParsedTable(headers=[h.strip() for h in headers if h], rows=rows)]


def _parse_pdf_pymupdf(content: bytes) -> str:
  """Быстрое извлечение текста через PyMuPDF. Для цифровых PDF (не сканов).
  Возвращает текст или пустую строку если PDF — скан (тогда нужен OCR)."""
  try:
    import fitz  # PyMuPDF
  except ImportError:
    return ""
  try:
    doc = fitz.open(stream=content, filetype="pdf")
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return text
  except Exception as e:
    _log.warning("PyMuPDF не смог извлечь текст: %s", e)
    return ""


def parse_pdf(content: bytes, filename: str) -> tuple[list[ParsedTable], str]:
  """Цепочка парсинга PDF:
  1. PyMuPDF (быстро, мгновенно) — для цифровых PDF
  2. Docling (OCR, тяжело) — fallback если PyMuPDF дал пустой/маленький текст (скан)
  """
  # --- ЭТАП 1: PyMuPDF (быстрый) ---
  text = _parse_pdf_pymupdf(content)
  if text and len(text.strip()) > 100:
    # PyMuPDF справился — цифровой PDF, текст есть
    tables = _markdown_tables_to_parsed(text) if "|" in text else []
    _log.info("PDF распарсен через PyMuPDF: %d символов", len(text))
    return tables, text

  # --- ЭТАП 2: Docling (OCR fallback) ---
  # PyMuPDF дал пустой/маленький текст → скорее всего скан, нужен OCR
  _log.info("PyMuPDF дал мало текста (%d симв), пробую Docling OCR...", len(text))
  converter = _get_converter()
  ext = Path(filename).suffix.lower() or ".pdf"

  with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tmp:
    tmp.write(content)
    tmp.flush()
    tmp_path = Path(tmp.name)
    result = converter.convert(tmp_path)

  md = result.document.export_to_markdown()

  tables: list[ParsedTable] = []
  for element, _level in result.document.iterate_items():
    if isinstance(element, TableItem):
      try:
        df: pd.DataFrame = element.export_to_dataframe(doc=result.document)
        if df.empty:
          continue
        headers = [str(h).strip() for h in df.columns.tolist()]
        rows = df.fillna("").astype(str).apply(lambda r: {k: v.strip() for k, v in r.items()}, axis=1).tolist()
        rows = [r for r in rows if any(v for v in r.values())]
        tables.append(ParsedTable(headers=headers, rows=rows))
      except Exception:
        _log.warning("Failed to export table, skipping", exc_info=True)

  if not tables:
    tables = _markdown_tables_to_parsed(md)

  _log.info("PDF распарсен через Docling OCR: %d символов", len(md))
  return tables, md


def _markdown_tables_to_parsed(markdown: str) -> list[ParsedTable]:
  tables: list[ParsedTable] = []
  lines = markdown.split("\n")
  i = 0
  while i < len(lines):
    line = lines[i].strip()
    if "|" in line and i + 1 < len(lines) and "---" in lines[i + 1]:
      headers = [c.strip() for c in line.split("|") if c.strip()]
      i += 2
      rows: list[dict[str, str]] = []
      while i < len(lines) and "|" in lines[i].strip():
        cells = [c.strip() for c in lines[i].strip().split("|") if c.strip()]
        if cells:
          row = dict(zip(headers, cells))
          rows.append(row)
        i += 1
      if rows:
        tables.append(ParsedTable(headers=headers, rows=rows))
    else:
      i += 1
  return tables


def parse(content: bytes, filename: str) -> tuple[list[ParsedTable], str]:
  ext = Path(filename).suffix.lower()

  if ext == ".csv":
    tables = parse_csv(content)
    md = _csv_to_markdown(tables)
    return tables, md

  return parse_pdf(content, filename)


def _csv_to_markdown(tables: list[ParsedTable]) -> str:
  parts: list[str] = []
  for t in tables:
    if not t.headers:
      continue
    header_line = "| " + " | ".join(t.headers) + " |"
    sep_line = "| " + " | ".join("---" for _ in t.headers) + " |"
    data_lines = []
    for row in t.rows:
      vals = [row.get(h, "") for h in t.headers]
      data_lines.append("| " + " | ".join(vals) + " |")
    parts.append(header_line)
    parts.append(sep_line)
    parts.extend(data_lines)
  return "\n".join(parts)
