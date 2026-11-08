import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path

os.environ["DOCLING_DEVICE"] = "cpu"
os.environ["DOCLING_NUM_THREADS"] = "8"

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from server.models import HealthResponse, ParseResponse
from server.parser import parse
from dashboard import db as dashboard_db
from dashboard.routes import router as dashboard_router

logging.basicConfig(
  level=logging.INFO,
  format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# httpx логирует полный URL запроса, а в URL Telegram API лежит токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)
_log = logging.getLogger(__name__)

# Состояние бота (стартует в lifespan); ссылка дублируется в server.runtime
# чтобы dashboard/routes.py мог слать сообщения без циклического импорта.
_bot_app = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Старт/стоп: инициализируем БД, запускаем бота и планировщик в фоне."""
    global _bot_app, _bot_task
    dashboard_db.init_db()
    _log.info("SQLite инициализирована: %s", dashboard_db.DB_PATH)

    # Telegram-бот (если токен уже задан — в БД или .env).
    # Если зададут позже через setup-мастер/админку — бот поднимется через
    # runtime.restart_bot() без перезапуска контейнера.
    try:
        import sys
        repo = str(Path(__file__).resolve().parent.parent)
        if repo not in sys.path:
            sys.path.insert(0, repo)
        from server import runtime
        await runtime.start_bot()
        _bot_app = runtime.bot_app
    except Exception as e:
        _log.warning("⚠️ Бот не запущен: %s (сервер продолжит работу)", e)

    yield

    # Корректная остановка планировщика и бота
    try:
        from server import runtime
        from server.scheduler import stop_scheduler
        stop_scheduler()
        await runtime.stop_bot()
    except Exception:
        pass


app = FastAPI(
  title="AI замість фінансиста",
  version="1.0.0",
  description="Дашборд + админка + Telegram-бот для анализа банковских выписок",
  lifespan=lifespan,
)

# Статика и шаблоны дашборда
_STATIC_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
app.include_router(dashboard_router)


# Локальна редакція: без паролів. Сторінка /admin («Налаштування») відкрита —
# система працює на комп'ютері користувача, доступ до неї = доступ до комп'ютера.

ALLOWED_EXTENSIONS = {".pdf", ".csv"}

INDEX_HTML = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Bank Statement Parser</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }
  .container { max-width: 1200px; margin: 0 auto; padding: 2rem; }
  h1 { font-size: 1.5rem; margin-bottom: 0.5rem; color: #f1f5f9; }
  .subtitle { color: #94a3b8; margin-bottom: 2rem; font-size: 0.9rem; }
  .upload-zone {
    border: 2px dashed #334155; border-radius: 12px; padding: 3rem; text-align: center;
    background: #1e293b; cursor: pointer; transition: all 0.2s;
  }
  .upload-zone:hover, .upload-zone.dragover { border-color: #3b82f6; background: #1e293b; }
  .upload-zone input { display: none; }
  .upload-icon { font-size: 2.5rem; margin-bottom: 1rem; }
  .upload-text { color: #94a3b8; }
  .upload-text strong { color: #3b82f6; }
  .file-name { margin-top: 0.75rem; color: #22c55e; font-size: 0.85rem; display: none; }
  button {
    margin-top: 1.5rem; padding: 0.75rem 2rem; background: #3b82f6; color: white;
    border: none; border-radius: 8px; font-size: 1rem; cursor: pointer; font-weight: 500;
  }
  button:hover { background: #2563eb; }
  button:disabled { background: #475569; cursor: not-allowed; }
  .loading { display: none; text-align: center; margin-top: 2rem; }
  .spinner { border: 3px solid #334155; border-top: 3px solid #3b82f6; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin: 0 auto 1rem; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .error { display: none; background: #451a1a; border: 1px solid #7f1d1d; border-radius: 8px; padding: 1rem; margin-top: 2rem; color: #fca5a5; }
  .result { display: none; margin-top: 2rem; }
  .stats {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1rem;
    margin-bottom: 2rem;
  }
  .stat-card { background: #1e293b; border-radius: 8px; padding: 1rem; text-align: center; }
  .stat-value { font-size: 1.75rem; font-weight: 700; color: #3b82f6; }
  .stat-label { font-size: 0.8rem; color: #94a3b8; margin-top: 0.25rem; }
  .tab-bar { display: flex; gap: 0; margin-bottom: 1rem; }
  .tab {
    padding: 0.6rem 1.2rem; background: #1e293b; border: 1px solid #334155;
    cursor: pointer; color: #94a3b8; font-size: 0.85rem; transition: all 0.2s;
  }
  .tab:first-child { border-radius: 8px 0 0 8px; }
  .tab:last-child { border-radius: 0 8px 8px 0; }
  .tab.active { background: #3b82f6; color: white; border-color: #3b82f6; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .table-wrap { overflow-x: auto; border-radius: 8px; border: 1px solid #334155; }
  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  th { background: #1e293b; padding: 0.6rem 0.75rem; text-align: left; color: #94a3b8; font-weight: 600; position: sticky; top: 0; white-space: nowrap; }
  td { padding: 0.5rem 0.75rem; border-top: 1px solid #1e293b; white-space: nowrap; max-width: 200px; overflow: hidden; text-overflow: ellipsis; }
  tr:hover td { background: #1e293b; }
  .markdown-box {
    background: #1e293b; border-radius: 8px; padding: 1.5rem; border: 1px solid #334155;
    white-space: pre-wrap; font-family: 'SF Mono', 'Fira Code', monospace; font-size: 0.8rem;
    max-height: 600px; overflow: auto; line-height: 1.6;
  }
  .table-scroll { max-height: 500px; overflow-y: auto; }
  .new-parse-btn { display: none; margin-top: 1rem; padding: 0.5rem 1.5rem; background: transparent; border: 1px solid #334155; color: #94a3b8; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }
  .new-parse-btn:hover { border-color: #3b82f6; color: #3b82f6; }
</style>
</head>
<body>
<div class="container">
  <h1>Bank Statement Parser</h1>
  <p class="subtitle">Завантажте банківську виписку (PDF або CSV) для парсингу через Docling</p>

  <div id="uploadSection">
    <div class="upload-zone" id="dropZone">
      <div class="upload-icon">📄</div>
      <div class="upload-text">Перетягніть файл сюди або <strong>натисніть для вибору</strong></div>
      <div class="upload-text" style="margin-top:0.5rem;font-size:0.8rem">PDF, CSV — до 50MB</div>
      <div class="file-name" id="fileName"></div>
      <input type="file" id="fileInput" accept=".pdf,.csv">
    </div>
    <button id="parseBtn" disabled>Запарсити</button>
    <button class="new-parse-btn" id="newBtn">← Новий файл</button>
  </div>

  <div class="loading" id="loading">
    <div class="spinner"></div>
    <div>Парсинг документу...</div>
  </div>

  <div class="error" id="error"></div>

  <div class="result" id="result">
    <div class="stats" id="stats"></div>
    <div class="tab-bar">
      <div class="tab active" data-tab="table">Таблиця</div>
      <div class="tab" data-tab="json">JSON</div>
      <div class="tab" data-tab="markdown">Markdown</div>
    </div>
    <div class="tab-content active" id="tab-table">
      <div id="tableContent"></div>
    </div>
    <div class="tab-content" id="tab-json">
      <div class="markdown-wrap">
        <pre class="markdown-box" id="jsonContent"></pre>
      </div>
    </div>
    <div class="tab-content" id="tab-markdown">
      <pre class="markdown-box" id="markdownContent"></pre>
    </div>
  </div>
</div>

<script>
const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const parseBtn = document.getElementById('parseBtn');
const newBtn = document.getElementById('newBtn');
const fileName = document.getElementById('fileName');
const loading = document.getElementById('loading');
const error = document.getElementById('error');
const result = document.getElementById('result');

let selectedFile = null;

dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('dragover'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', e => {
  e.preventDefault(); dropZone.classList.remove('dragover');
  if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => { if (fileInput.files.length) selectFile(fileInput.files[0]); });

function selectFile(f) {
  const ext = f.name.split('.').pop().toLowerCase();
  if (!['pdf','csv'].includes(ext)) { alert('Тільки PDF або CSV'); return; }
  selectedFile = f;
  fileName.textContent = f.name + ' (' + (f.size/1024).toFixed(1) + ' KB)';
  fileName.style.display = 'block';
  parseBtn.disabled = false;
}

newBtn.addEventListener('click', () => {
  result.style.display = 'none';
  error.style.display = 'none';
  newBtn.style.display = 'none';
  parseBtn.style.display = 'inline-block';
  fileName.style.display = 'none';
  parseBtn.disabled = true;
  fileInput.value = '';
  selectedFile = null;
});

parseBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  parseBtn.disabled = true;
  loading.style.display = 'block';
  error.style.display = 'none';
  result.style.display = 'none';

  const fd = new FormData();
  fd.append('file', selectedFile);

  try {
    const resp = await fetch('/parse', { method: 'POST', body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || 'Unknown error');
    showResult(data);
  } catch (e) {
    error.textContent = e.message;
    error.style.display = 'block';
    parseBtn.disabled = false;
  } finally {
    loading.style.display = 'none';
  }
});

function showResult(data) {
  parseBtn.style.display = 'none';
  newBtn.style.display = 'inline-block';

  document.getElementById('stats').innerHTML = `
    <div class="stat-card"><div class="stat-value">${data.tables.length}</div><div class="stat-label">Таблиць</div></div>
    <div class="stat-card"><div class="stat-value">${data.row_count}</div><div class="stat-label">Рядків</div></div>
    <div class="stat-card"><div class="stat-value">${data.file_type.toUpperCase()}</div><div class="stat-label">Тип файлу</div></div>
    <div class="stat-card"><div class="stat-value">${data.filename.length > 25 ? data.filename.slice(0,25)+'...' : data.filename}</div><div class="stat-label">Файл</div></div>
  `;

  let html = '';
  data.tables.forEach((t, idx) => {
    if (data.tables.length > 1) html += `<h3 style="margin:1rem 0 0.5rem;color:#94a3b8">Таблиця ${idx+1}</h3>`;
    html += '<div class="table-wrap"><div class="table-scroll"><table><thead><tr>';
    t.headers.forEach(h => html += `<th>${esc(h)}</th>`);
    html += '</tr></thead><tbody>';
    t.rows.forEach(row => {
      html += '<tr>';
      t.headers.forEach(h => html += `<td title="${esc(row[h]||'')}">${esc(row[h]||'')}</td>`);
      html += '</tr>';
    });
    html += '</tbody></table></div></div>';
  });
  document.getElementById('tableContent').innerHTML = html;
  document.getElementById('jsonContent').textContent = JSON.stringify(data, null, 2);
  document.getElementById('markdownContent').textContent = data.markdown;
  result.style.display = 'block';
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  });
});
</script>
</body>
</html>"""


@app.get("/parse-tool", response_class=HTMLResponse)
async def parse_tool():
  """Старый Docling-парсер PDF/CSV (инструмент, не основной дашборд)."""
  return INDEX_HTML


@app.get("/health", response_model=HealthResponse)
async def health():
  return HealthResponse(status="ok", docling_ready=True)


@app.post("/parse", response_model=ParseResponse)
async def parse_document(file: UploadFile = File(...)):
  filename = file.filename or "unknown"
  ext = Path(filename).suffix.lower()

  if ext not in ALLOWED_EXTENSIONS:
    raise HTTPException(
      status_code=400,
      detail=f"Unsupported file type: {ext}. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
    )

  content = await file.read()
  if not content:
    raise HTTPException(status_code=400, detail="Empty file")

  _log.info(f"Parsing {filename} ({len(content)} bytes)")

  try:
    tables, markdown = parse(content, filename)
  except Exception as e:
    _log.error(f"Failed to parse {filename}: {e}", exc_info=True)
    raise HTTPException(
      status_code=422,
      detail="Не вдалося розібрати файл. Перевірте формат (PDF/CSV).",
    ) from e

  row_count = sum(len(t.rows) for t in tables)
  file_type = ext.lstrip(".")

  _log.info(f"Parsed {filename}: {len(tables)} table(s), {row_count} row(s)")

  return ParseResponse(
    filename=filename,
    file_type=file_type,
    tables=tables,
    markdown=markdown,
    row_count=row_count,
  )
