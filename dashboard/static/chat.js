/* ============================================================
   Чат: мини-чат (дашборд) + ChatGPT-style сессии (вкладка «Історія»)
   ============================================================ */

// --- Текущая выбранная беседа (для обеих панелей) ---
let currentSessionId = null;

// --- Мини-чат на дашборде (правая колонка) ---
const chatMessages = document.getElementById('chatMessages');
const chatForm = document.getElementById('chatForm');
const chatInput = document.getElementById('chatInput');

if (chatForm) {
  chatForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = chatInput.value.trim();
    if (!msg) return;
    chatInput.value = '';  // чистим сразу — не ждём ответа ИИ
    await sendChatMessage(msg, 'mini');
  });
}

// чіпи швидких питань — для обох панелей (міні-чат + історія)
document.querySelectorAll('.suggestion-chip').forEach(chip => {
  chip.addEventListener('click', async () => {
    const isHistory = chip.closest('#historyView') !== null;
    await sendChatMessage(chip.dataset.q, isHistory ? 'history' : 'mini');
  });
});

// --- «ШІ думає»: анімовані крапки + обертові фрази ---
const THINKING_PHRASES = [
  'Аналізую ваші транзакції…',
  'Звертаюсь до виписки…',
  'Порівнюю категорії…',
  'Рахую суми…',
  'Майже готово…',
];
function makeThinking(container) {
  const div = document.createElement('div');
  div.className = 'chat-msg assistant thinking';
  div.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>' +
                  '<span class="typing-text"></span>';
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  let i = 0;
  const label = div.querySelector('.typing-text');
  label.textContent = THINKING_PHRASES[0];
  div._timer = setInterval(() => {
    i = (i + 1) % THINKING_PHRASES.length;
    label.textContent = THINKING_PHRASES[i];
  }, 2200);
  return div;
}
function stopThinking(el) {
  if (el && el._timer) clearInterval(el._timer);
  if (el) el.remove();
}

// --- Отправка сообщения (общая для мини-чата и истории) ---
async function sendChatMessage(message, source = 'mini') {
  const container = source === 'history'
    ? document.getElementById('historyMessages')
    : chatMessages;
  if (!container) return;

  appendMsg('user', message, container);
  const typing = makeThinking(container);

  try {
    const body = { message };
    if (currentSessionId) body.session_id = currentSessionId;
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    stopThinking(typing);
    if (!r.ok) {
      appendMsg('error', data.error || 'Помилка', container);
    } else {
      appendMsg('assistant', data.reply, container);
      // если создалась новая сессия — запоминаем
      if (data.session_id && !currentSessionId) {
        currentSessionId = data.session_id;
      }
      // обновляем сайдбар сессий (мог измениться заголовок)
      if (source === 'history') loadSessions();
    }
  } catch (e) {
    stopThinking(typing);
    appendMsg('error', 'Не вдалося зв\'язатись з сервером.', container);
  }
}

// Отправка из вкладки «Історія чату»
async function sendHistoryMessage() {
  const input = document.getElementById('historyChatInput');
  if (!input) return;
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  // если нет выбранной сессии — создаём новую
  if (!currentSessionId) {
    await newSession();
  }
  await sendChatMessage(msg, 'history');
}

function appendMsg(role, text, container) {
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  if (role === 'assistant') {
    // мини-markdown: **жирні цифри** + переноси рядків (спочатку екрануємо HTML)
    const safe = text
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    div.innerHTML = safe
      .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
      .replace(/\n/g, '<br>');
  } else {
    div.textContent = text;
  }
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

// ============================================================
// ChatGPT-style сессии (вкладка «Історія чату»)
// ============================================================
async function loadSessions() {
  const list = document.getElementById('sessionList');
  if (!list) return;
  try {
    const r = await fetch('/api/chat/sessions');
    const data = await r.json();
    if (!data.sessions || data.sessions.length === 0) {
      list.innerHTML = '<div class="session-empty">Поки що немає бесід.<br>Створіть нову ↑</div>';
      return;
    }
    list.innerHTML = data.sessions.map(s => `
      <div class="session-item ${s.id === currentSessionId ? 'active' : ''}" data-id="${s.id}">
        <span class="session-title" onclick="selectSession(${s.id}, '${escAttr(s.title)}')">${esc(s.title)}</span>
        <button class="session-del" onclick="deleteSession(${s.id})" title="Видалити">🗑</button>
      </div>
    `).join('');
  } catch (e) {
    list.innerHTML = '<div class="session-empty">Помилка завантаження</div>';
  }
}

async function newSession() {
  try {
    const r = await fetch('/api/chat/sessions', { method: 'POST' });
    const data = await r.json();
    currentSessionId = data.id;
    document.getElementById('historyMessages').innerHTML =
      '<div class="empty-state"><div class="icon">💬</div>Нова бесіда. Напишіть перше повідомлення...</div>';
    document.getElementById('currentSessionTitle').textContent = data.title;
    document.getElementById('clearSessionBtn').style.display = 'inline-flex';
    document.getElementById('historyChatInput').focus();
    loadSessions();
  } catch (e) { /* ignore */ }
}

async function selectSession(sessionId, title) {
  currentSessionId = sessionId;
  document.getElementById('currentSessionTitle').textContent = title;
  document.getElementById('clearSessionBtn').style.display = 'inline-flex';
  // подгружаем сообщения этой беседы
  try {
    const r = await fetch('/api/chat/history?session_id=' + sessionId);
    const data = await r.json();
    const container = document.getElementById('historyMessages');
    if (!data.messages || data.messages.length === 0) {
      container.innerHTML = '<div class="empty-state"><div class="icon">💬</div>Ця бесіда порожня</div>';
    } else {
      container.innerHTML = data.messages.map(m => {
        const label = m.role === 'user' ? '🙋' : '🤖';
        return `<div class="chat-msg ${m.role}">${label} ${esc(m.content)}</div>`;
      }).join('');
      container.scrollTop = container.scrollHeight;
    }
    // обновляем active в сайдбаре
    document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
    const active = document.querySelector(`.session-item[data-id="${sessionId}"]`);
    if (active) active.classList.add('active');
  } catch (e) { /* ignore */ }
}

async function deleteSession(sessionId) {
  if (!confirm('Видалити бесіду разом з повідомленнями?')) return;
  try {
    await fetch(`/api/chat/sessions/${sessionId}`, { method: 'DELETE' });
    if (currentSessionId === sessionId) {
      currentSessionId = null;
      document.getElementById('historyMessages').innerHTML =
        '<div class="empty-state"><div class="icon">💬</div>Оберіть бесіду зліва або створіть нову</div>';
      document.getElementById('currentSessionTitle').textContent = 'Оберіть бесіду';
      document.getElementById('clearSessionBtn').style.display = 'none';
    }
    loadSessions();
  } catch (e) { /* ignore */ }
}

// утилиты
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
function escAttr(s) { return (s || '').replace(/'/g, "\\'"); }

// ============================================================
// Полнотекстовый поиск по сообщениям всех бесед (FTS5)
// ============================================================
let chatSearchTimer = null;

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('chatSearch');
  if (!input) return;
  input.addEventListener('input', () => {
    clearTimeout(chatSearchTimer);
    const q = input.value.trim();
    if (!q) { loadSessions(); return; }  // пусто — обратно список сессий
    chatSearchTimer = setTimeout(() => searchChatMessages(q), 350);
  });
});

async function searchChatMessages(q) {
  const list = document.getElementById('sessionList');
  if (!list) return;
  list.innerHTML = '<div class="session-empty">🔍 Шукаю...</div>';
  try {
    const r = await fetch('/api/chat/search?q=' + encodeURIComponent(q));
    const data = await r.json();
    if (!r.ok) {
      list.innerHTML = '<div class="session-empty">Помилка пошуку</div>';
      return;
    }
    const results = data.results || [];
    if (!results.length) {
      list.innerHTML = '<div class="session-empty">Нічого не знайдено 🤷</div>';
      return;
    }
    list.innerHTML = results.map(m => {
      const icon = m.role === 'user' ? '🙋' : '🤖';
      const title = m.session_title ? esc(m.session_title) : 'Бесіда';
      const canOpen = m.session_id
        ? `onclick="selectSession(${m.session_id}, '${escAttr(title)}')"` : '';
      return `
        <div class="search-result" ${canOpen} title="Відкрити бесіду">
          <div class="search-result-title">${icon} ${title}</div>
          <div class="search-result-excerpt">${esc(m.content.slice(0, 120))}${m.content.length > 120 ? '…' : ''}</div>
        </div>`;
    }).join('');
  } catch (e) {
    list.innerHTML = '<div class="session-empty">Помилка пошуку</div>';
  }
}
