/* ============================================================
   Дашборд: метрики, графики, таблица, загрузка выписки
   IBAN определяется cookie-сессией (не dropdown) — фильтр убран.
   ============================================================ */
const PALETTE = ['#6366f1','#8b5cf6','#a855f7','#22d3ee','#10b981','#f59e0b','#ef4444','#ec4899'];
const fmt = n => new Intl.NumberFormat('uk-UA').format(Math.round(n));
const fmtMoney = n => fmt(n) + ' грн';

let charts = {};

document.addEventListener('DOMContentLoaded', () => {
  loadPeriods();
  loadMetrics();
  loadTransactions();
  setupUpload();
  setupTxSearch();

  document.getElementById('applyFilters').addEventListener('click', () => {
    loadMetrics(); loadTransactions();
  });
  document.getElementById('resetFilters').addEventListener('click', () => {
    document.getElementById('fFrom').value = '';
    document.getElementById('fTo').value = '';
    document.getElementById('fCategory').value = 'all';
    selectPeriod('all');  // сброс периода тоже
    loadMetrics(); loadTransactions();
  });
});

function filterParams() {
  const p = new URLSearchParams();
  const from = document.getElementById('fFrom').value;
  const to = document.getElementById('fTo').value;
  const cat = document.getElementById('fCategory').value;
  if (from) p.set('date_from', from);
  if (to) p.set('date_to', to);
  if (cat) p.set('category', cat);
  // период (месяц/год) — из переключателя
  const monthSel = document.getElementById('periodMonth');
  const yearSel = document.getElementById('periodYear');
  if (monthSel && monthSel.value) p.set('month', monthSel.value);
  if (yearSel && yearSel.value) p.set('year', yearSel.value);
  // полнотекстовый поиск
  const search = document.getElementById('txSearch');
  if (search && search.value.trim()) p.set('q', search.value.trim());
  return p.toString();
}

// ============================================================
// Переключатель периодов (Месяц / Год / Всё время)
// ============================================================
const MONTH_NAMES_UA = ['','Січень','Лютий','Березень','Квітень','Травень','Червень',
  'Липень','Серпень','Вересень','Жовтень','Листопад','Грудень'];

async function loadPeriods() {
  try {
    const r = await fetch('/api/periods');
    const data = await r.json();
    const mSel = document.getElementById('periodMonth');
    const ySel = document.getElementById('periodYear');
    // месяцы
    (data.months || []).forEach(ym => {
      const [y, m] = ym.split('-');
      const opt = document.createElement('option');
      opt.value = ym;
      opt.textContent = `${MONTH_NAMES_UA[parseInt(m)] || m} ${y}`;
      mSel.appendChild(opt);
    });
    // годы
    (data.years || []).forEach(y => {
      const opt = document.createElement('option');
      opt.value = y;
      opt.textContent = y;
      ySel.appendChild(opt);
    });
  } catch (e) { console.error('loadPeriods', e); }
}

function selectPeriod(type) {
  const allBtn = document.getElementById('periodAll');
  const mSel = document.getElementById('periodMonth');
  const ySel = document.getElementById('periodYear');
  // визуальный active
  allBtn.classList.toggle('active', type === 'all');
  if (type === 'all') {
    mSel.value = ''; ySel.value = '';
  } else if (type === 'month') {
    ySel.value = ''; allBtn.classList.remove('active');
    if (!mSel.value) { mSel.value = ''; return; }  // выбран placeholder
  } else if (type === 'year') {
    mSel.value = ''; allBtn.classList.remove('active');
    if (!ySel.value) { ySel.value = ''; return; }
  }
  loadMetrics();
  loadTransactions();
}

async function loadMetrics() {
  try {
    const r = await fetch('/api/metrics?' + filterParams());
    const m = await r.json();
    renderKPIs(m);
    renderAlerts(m.alerts || []);
    renderCategoryChart(m.by_category || []);
    renderWeeklyChart(m.weekly || []);
    // пустое состояние
    const empty = document.getElementById('emptyState');
    if (m.tx_count === 0) {
      empty.style.display = 'block';
    } else {
      empty.style.display = 'none';
    }
  } catch (e) { console.error('loadMetrics', e); }
}

function renderKPIs(m) {
  document.getElementById('kpiIncome').textContent = fmtMoney(m.income);
  document.getElementById('kpiExpense').textContent = fmtMoney(m.expenses);
  document.getElementById('kpiProfit').textContent = fmtMoney(m.profit);
  document.getElementById('kpiMargin').textContent = m.margin + '%';
  document.getElementById('kpiTxCount').textContent = m.tx_count;
  document.getElementById('kpiTopCat').textContent = m.top_category && m.top_category !== '—'
    ? '🏆 ' + m.top_category : '';
}

function renderAlerts(alerts) {
  const panel = document.getElementById('alertPanel');
  const list = document.getElementById('alertList');
  if (!alerts.length) { panel.classList.add('empty'); return; }
  panel.classList.remove('empty');
  list.innerHTML = alerts.map(a => `<div class="alert-item">${a}</div>`).join('');
}

function renderCategoryChart(cats) {
  const ctx = document.getElementById('catChart');
  if (charts.cat) charts.cat.destroy();
  if (!cats.length) return;
  charts.cat = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: cats.map(c => c.category),
      datasets: [{ data: cats.map(c => c.amount), backgroundColor: PALETTE, borderWidth: 0 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: '62%',
      plugins: {
        legend: { position: 'right', labels: { color: '#cbd5e1', font: { size: 12 }, padding: 12 } },
        tooltip: { callbacks: { label: c => ` ${c.label}: ${fmtMoney(c.raw)}` } },
      },
    },
  });
}

function renderWeeklyChart(weekly) {
  const ctx = document.getElementById('weeklyChart');
  if (charts.weekly) charts.weekly.destroy();
  if (!weekly.length) return;
  charts.weekly = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: weekly.map(w => w.week),
      datasets: [
        { label: 'Надходження', data: weekly.map(w => w.income), backgroundColor: '#10b981', borderRadius: 6 },
        { label: 'Витрати', data: weekly.map(w => w.expense), backgroundColor: '#ef4444', borderRadius: 6 },
        { type: 'line', label: 'Прибуток', data: weekly.map(w => w.profit),
          borderColor: '#22d3ee', backgroundColor: 'transparent', borderWidth: 2, tension: 0.3, pointRadius: 3 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#cbd5e1', font: { size: 12 } } },
        tooltip: { callbacks: { label: c => ` ${c.dataset.label}: ${fmtMoney(c.raw)}` } },
      },
      scales: {
        x: { ticks: { color: '#64748b' }, grid: { display: false } },
        y: { ticks: { color: '#64748b', callback: v => fmt(v) }, grid: { color: 'rgba(255,255,255,0.04)' } },
      },
    },
  });
}

async function loadTransactions() {
  try {
    const p = new URLSearchParams(filterParams());
    p.set('limit', 100);
    const r = await fetch('/api/transactions?' + p.toString());
    const txs = await r.json();
    const tbody = document.getElementById('txBody');
    if (!txs.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;padding:40px;color:var(--text-muted)">Транзакцій поки що немає</td></tr>';
      return;
    }
    tbody.innerHTML = txs.map(t => {
      const cls = t.amount >= 0 ? 'amount-in' : 'amount-out';
      const sign = t.amount >= 0 ? '+' : '−';
      return `<tr>
        <td>${t.date}</td>
        <td>${esc(t.description)}</td>
        <td><span class="cat-chip">${esc(t.category)}</span></td>
        <td class="num ${cls}">${sign}${fmt(Math.abs(t.amount))}</td>
        <td>${esc(t.type)}</td>
      </tr>`;
    }).join('');
  } catch (e) { console.error('loadTransactions', e); }
}

// ============================================================
// Загрузка выписки (drag&drop + click)
// ============================================================
function setupUpload() {
  const zone = document.getElementById('uploadZone');
  const input = document.getElementById('fileInput');

  zone.addEventListener('click', () => input.click());
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault(); zone.classList.remove('dragover');
    if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
  });
  input.addEventListener('change', () => {
    if (input.files.length) uploadFile(input.files[0]);
    input.value = '';
  });
}

async function uploadFile(file) {
  const status = document.getElementById('uploadStatus');
  status.className = '';
  status.innerHTML = `
    <div class="upload-progress">
      <div class="progress-step" id="step-parse"><span class="step-icon">⏳</span> Розпізнання файлу</div>
      <div class="progress-step" id="step-ai"><span class="step-icon">⏳</span> AI категоризація (1–3 хв)</div>
      <div class="progress-step" id="step-save"><span class="step-icon">⏳</span> Збереження</div>
      <div class="progress-step" id="step-export"><span class="step-icon">⏳</span> Експорт у Google</div>
    </div>
    <div class="progress-msg" id="progressMsg">Починаю обробку...</div>
  `;

  const fd = new FormData();
  fd.append('file', file);

  try {
    const resp = await fetch('/api/upload', { method: 'POST', body: fd });
    // читаем SSE-стрим вручную
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // парсим SSE-сообщения (разделитель \n\n)
      const events = buffer.split('\n\n');
      buffer = events.pop(); // последний (возможно неполный)
      for (const evt of events) {
        const lines = evt.split('\n');
        let eventType = 'message', dataStr = '';
        for (const line of lines) {
          if (line.startsWith('event: ')) eventType = line.slice(7);
          else if (line.startsWith('data: ')) dataStr += line.slice(6);
        }
        if (!dataStr) continue;
        try {
          const data = JSON.parse(dataStr);
          handleUploadEvent(eventType, data, status);
        } catch (e) { /* игнорим */ }
      }
    }
  } catch (e) {
    status.className = 'err';
    status.innerHTML = '❌ Не вдалося зв\'язатись з сервером.';
  }
}

function handleUploadEvent(eventType, data, status) {
  const msg = document.getElementById('progressMsg');

  if (eventType === 'step') {
    // обновляем визуал шага
    const el = document.getElementById('step-' + data.step);
    if (el) {
      el.className = 'progress-step ' + data.status;  // active / done
      const icon = el.querySelector('.step-icon');
      if (data.status === 'done') icon.textContent = '✅';
      else if (data.status === 'active') icon.textContent = '🔄';
    }
    if (msg) msg.textContent = data.message;
  } else if (eventType === 'error') {
    status.className = 'err';
    status.innerHTML = '❌ ' + (data.error || 'Помилка');
  } else if (eventType === 'done') {
    const parts = [`✅ Додано <strong>${data.tx_count}</strong> транзакцій`];
    parts.push(`Усього на рахунку: <strong>${data.total_tx}</strong>`);
    if (data.sheets_exported) parts.push('· 📋 Експортовано в Google');
    status.className = 'ok';
    status.innerHTML = parts.join(' ');
    // перезагружаем метрики
    loadMetrics();
    loadTransactions();
  }
}

function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

// ============================================================
// Полнотекстовый поиск по транзакциям (FTS5 на бэкенде)
// ============================================================
function setupTxSearch() {
  const input = document.getElementById('txSearch');
  const clearBtn = document.getElementById('txSearchClear');
  if (!input) return;
  let timer = null;
  input.addEventListener('input', () => {
    clearBtn.style.display = input.value.trim() ? 'inline-flex' : 'none';
    clearTimeout(timer);
    timer = setTimeout(() => { loadTransactions(); }, 400);
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); loadTransactions(); }
  });
  clearBtn.addEventListener('click', () => {
    input.value = '';
    clearBtn.style.display = 'none';
    loadTransactions();
    input.focus();
  });
}

// ============================================================
// Переключение вкладок: Дашборд / Історія чату
// ============================================================
function switchTab(tab) {
  const dash = document.getElementById('dashboardView');
  const hist = document.getElementById('historyView');
  const btnD = document.getElementById('tabDashboard');
  const btnH = document.getElementById('tabHistory');
  if (tab === 'history') {
    dash.style.display = 'none';
    hist.style.display = 'block';
    btnD.classList.remove('active');
    btnH.classList.add('active');
    if (typeof loadSessions === 'function') loadSessions();
  } else {
    dash.style.display = 'grid';
    hist.style.display = 'none';
    btnH.classList.remove('active');  // фикс бага: раньше добавлял active к btnH
    btnD.classList.add('active');
    loadMetrics(); loadTransactions();
  }
}

async function loadChatHistory() {
  // загружаем сессии (функция в chat.js)
  if (typeof loadSessions === 'function') {
    loadSessions();
  }
}

async function clearChatHistory() {
  if (!currentSessionId) return;
  if (!confirm('Очистити повідомлення цієї бесіди?')) return;
  try {
    await fetch('/api/chat/history?session_id=' + currentSessionId, { method: 'DELETE' });
    if (currentSessionId) selectSession(currentSessionId, document.getElementById('currentSessionTitle').textContent);
  } catch (e) { /* ignore */ }
}
