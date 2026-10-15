/* Local UI. Registry text is always inserted as text, never interpreted as HTML. */
'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const node = (tag, cls = '', text = '') => { const el = document.createElement(tag); el.className = cls; el.textContent = text; return el; };
const button = (text, cls, action) => { const el = node('button', cls, text); el.type = 'button'; el.addEventListener('click', action); return el; };
const labels = {running: 'Running', waiting: 'Waiting for input', dormant: 'Dormant', idle: 'Idle', done: 'Done'};
let state = {agents: [], errors: {}, health: {}, home: ''};
let filter = 'all', activeTab = 'dashboard', restored = false, stateSocket, reconnectDelay = 1000;
let selectedSession = null, sessionData = [], wakeId = null, editId = null;
let selectedGrokSession = null, grokSessionData = [], grokLoadSequence = 0;
let forgettingAgent = false;
const terminals = new Map();
const cardViews = new SwarmCardViews.CardViews();
let toastTimer;
function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.className = `toast${error ? ' error' : ''}`; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, error ? 9000 : 4500);
}
async function api(path, method = 'GET', body) {
  const response = await fetch(path, {method, headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  let value; try { value = await response.json(); } catch { throw new Error(`Unexpected response (${response.status})`); }
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail || value));
  return value;
}
const agentById = id => state.agents.find(a => a.id === id);
const wsURL = path => `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`;
function since(value) {
  const ms = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(ms)) return 'unknown';
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? 'just now' : s < 3600 ? `${Math.floor(s / 60)}m ago` : s < 86400 ? `${Math.floor(s / 3600)}h ago` : `${Math.floor(s / 86400)}d ago`;
}
function remember() { try { localStorage.setItem('swarm.tabs', JSON.stringify({ids: [...terminals.keys()], active: activeTab})); } catch { /* storage may be disabled */ } }
function applyState(data) {
  if (!Array.isArray(data.agents)) return;
  state = data;
  for (const id of terminals.keys()) if (!agentById(id)) closeTerminal(id);
  render();
  if (!restored) {
    restored = true;
    try {
      const saved = JSON.parse(localStorage.getItem('swarm.tabs') || '{}');
      for (const id of saved.ids || []) if (agentById(id)) openTerminal(id, false);
      activate(saved.active === 'dashboard' || terminals.has(saved.active) ? saved.active : 'dashboard');
    } catch { /* invalid persisted tabs are ignored */ }
  }
}
async function refresh() { try { applyState(await api('/api/state')); } catch (error) { toast(error.message, true); } }
function connectState() {
  if (stateSocket && stateSocket.readyState < 2) return;
  const el = $('#connection'); el.textContent = 'Connecting…'; el.className = 'connection';
  const socket = new WebSocket(wsURL('/ws/state')); stateSocket = socket;
  socket.onopen = () => { reconnectDelay = 1000; el.textContent = '● Live'; el.className = 'connection live'; refresh(); };
  socket.onmessage = event => { try { applyState(JSON.parse(event.data)); } catch (error) { console.warn('Invalid state update', error); } };
  socket.onclose = () => { el.textContent = 'Reconnecting…'; el.className = 'connection offline'; setTimeout(connectState, reconnectDelay); reconnectDelay = Math.min(15000, reconnectDelay * 2); };
  socket.onerror = () => socket.close();
}
function badge(status) {
  const el = node('span', `badge ${status}`); el.append(node('i', 'status-dot'), document.createTextNode(labels[status] || status)); return el;
}
function metadata(list, label, value) {
  if (!value) return;
  const dt = node('dt', '', label), dd = node('dd', '', String(value)); dd.title = String(value); list.append(dt, dd);
}
function renderCard(a) {
  const card = node('article', 'agent-card'); card.dataset.agentId = a.id;
  const inside = node('div', 'card-inner'), summary = node('header', 'card-summary'), header = node('div', 'card-head');
  const identity = node('div', 'card-identity'), avatar = node('div', 'avatar', a.name.slice(0, 1).toUpperCase());
  const subtitle = node('div', 'card-subtitle', `${a.backend}${a.import_source ? ` ← ${a.import_source.backend}` : ''} · ${a.id.slice(0, 8)}${a.managed ? '' : ' · observed'}`);
  const name = node('h3', '', a.name); name.title = a.name;
  identity.append(name, subtitle);
  const details = node('div', 'card-details'); details.id = `card-details-${a.id}`;
  const toggle = button('', 'card-toggle', () => {
    const compact = !cardViews.isCompact(state.home, a);
    cardViews.setCompact(state.home, a.id, compact);
    SwarmCardViews.applyCardView({card, details, foot, avatar, subtitle, toggle}, compact, a.name);
  });
  toggle.setAttribute('aria-controls', `${details.id} card-actions-${a.id}`);
  header.append(avatar, identity, badge(a.status), toggle);
  const goal = node('p', 'goal', a.goal); goal.title = a.goal;
  summary.append(header, goal);
  const activity = node('div', 'activity'); activity.style.borderColor = `var(--${a.status})`;
  activity.append(node('span', 'activity-label', 'CURRENT ACTIVITY'), document.createTextNode(a.activity || 'No activity reported yet'));
  details.append(activity);
  if (a.activities?.length) {
    const list = node('ul', 'activity-list');
    for (const item of a.activities) {
      const row = node('li'); row.append(node('span', item.status === 'done' ? 'running' : '', item.status === 'done' ? '✓' : item.status === 'running' ? '›' : '○'), document.createTextNode(item.title)); list.append(row);
    } details.append(list);
  }
  if (a.wait_reason) details.append(node('p', 'wait-reason', a.wait_reason));
  const meta = node('dl', 'metadata');
  metadata(meta, 'Folder', a.folder); metadata(meta, 'Repository', a.repository); metadata(meta, 'Worktree', a.worktree);
  metadata(meta, 'Branch', a.branch); metadata(meta, 'Session', a.session_id || 'Not persisted yet');
  metadata(meta, 'Process', a.pid ? `PID ${a.pid}` : 'Not running');
  metadata(meta, 'Wake at', a.wake_at ? new Date(a.wake_at).toLocaleString() : '');
  metadata(meta, 'Restore', a.auto_resume ? 'Automatic' : 'Manual');
  if (a.import_source) {
    metadata(meta, 'Imported', `${a.import_source.backend} · ${a.import_source.session_id}`);
    metadata(meta, 'Source', a.import_source.session_file);
  }
  details.append(meta);
  if (a.notes) { const notes = node('details', 'notes'); notes.append(node('summary', '', 'Checkpoint'), node('pre', '', a.notes)); details.append(notes); }
  if (a.last_error) details.append(node('div', 'card-error', a.last_error));
  const foot = node('div', 'card-foot'); foot.id = `card-actions-${a.id}`;
  const runtime = node('span', `runtime${a.alive ? ' online' : ''}`, `${a.alive ? '● Online' : '○ Offline'} · ${since(a.updated_at)}`); runtime.title = a.updated_at;
  const actions = node('div', 'actions');
  actions.append(button('Terminal ↗', 'small', () => openTerminal(a.id)), button('Edit', 'small', () => editAgent(a.id)));
  if (a.managed) {
    actions.append(button('Wake', 'small', () => wakeAgent(a.id)));
    if (a.alive || a.auto_resume) actions.append(button('Stop', 'small danger', () => stopAgent(a.id)));
  }
  foot.append(runtime, actions); inside.append(summary, details); card.append(inside, foot);
  SwarmCardViews.applyCardView({card, details, foot, avatar, subtitle, toggle}, cardViews.isCompact(state.home, a), a.name);
  return card;
}
function render() {
  $('#home-path').textContent = state.home ? `${state.home}/agents` : '~/.swarm/agents';
  $('#agent-count').textContent = state.agents.length;
  const health = $('#health'); health.replaceChildren();
  for (const name of ['tmux', 'pi']) health.append(node('span', `health-pill${state.health[name] ? '' : ' bad'}`, `${state.health[name] ? '●' : '○'} ${name}`));
  const summary = $('#summary'); summary.replaceChildren();
  for (const status of ['all', ...Object.keys(labels)]) {
    const count = status === 'all' ? state.agents.length : state.agents.filter(a => a.status === status).length;
    const stat = button('', `stat${filter === status ? ' active' : ''}`, () => { filter = status; render(); });
    stat.setAttribute('aria-pressed', String(filter === status));
    const title = node('span', 'stat-title'); title.append(node('i', `status-dot ${status}`), document.createTextNode(status === 'all' ? 'All agents' : labels[status]));
    stat.append(title, node('span', `stat-value ${status}`, String(count))); summary.append(stat);
  }
  const query = $('#search').value.trim().toLowerCase();
  const visible = state.agents.filter(a => (filter === 'all' || a.status === filter) && [a.name, a.goal, a.activity, a.folder, a.branch].join(' ').toLowerCase().includes(query));
  const focusedToggle = document.activeElement?.classList.contains('card-toggle') ? document.activeElement.closest('.agent-card')?.dataset.agentId : null;
  $('#agents').replaceChildren(...visible.map(renderCard));
  if (focusedToggle) {
    const replacement = [...$('#agents').children].find(card => card.dataset.agentId === focusedToggle);
    if (replacement) $('.card-toggle', replacement)?.focus({preventScroll: true});
  }
  const empty = $('#empty'); empty.hidden = visible.length > 0;
  $('h2', empty).textContent = state.agents.length ? 'No agents match this view.' : 'A little quiet in the swarm.';
  $('#empty-create').hidden = state.agents.length > 0;
  const errors = Object.entries(state.errors || {}); $('#registry-errors').hidden = !errors.length;
  $('#error-list').replaceChildren(...errors.map(([file, message]) => node('pre', '', `${file}: ${message}`)));
  for (const terminal of terminals.values()) syncTerminal(terminal);
  if ($('#edit-dialog').open) updateForgetAvailability();
}
function activate(id) {
  activeTab = id && terminals.has(id) ? id : 'dashboard';
  $('#dashboard').hidden = activeTab !== 'dashboard'; $('#terminals').hidden = activeTab === 'dashboard';
  $('#dashboard-tab').classList.toggle('active', activeTab === 'dashboard');
  for (const [key, t] of terminals) {
    t.pane.hidden = key !== activeTab; t.tab.classList.toggle('active', key === activeTab); t.group.classList.toggle('active', key === activeTab);
    if (key === activeTab) { initTerminal(t); requestAnimationFrame(() => { t.fit?.fit(); t.term?.focus(); }); }
  } remember();
}
function openTerminal(id, focus = true) {
  if (!agentById(id)) return;
  if (!terminals.has(id)) {
    const pane = node('div', 'terminal-pane'); pane.hidden = true;
    const header = node('div', 'terminal-header');
    const title = node('strong'), info = node('span', 'hint'), connected = node('span', 'hint', 'Detached');
    header.append(title, info, connected, button('↻ Reconnect', 'small', () => { const t = terminals.get(id); disconnectTerminal(t); attachTerminal(t); }), button('Wake', 'small', () => wakeAgent(id)), button('Close tab', 'small', () => closeTerminal(id)));
    const surface = node('div', 'terminal-surface'), screen = node('div', 'terminal-screen'), overlay = node('div', 'terminal-overlay');
    surface.append(screen, overlay); pane.append(header, surface); $('#terminals').append(pane);
    const group = node('span', 'tab-group'), tab = button('', 'tab', () => activate(id));
    const tabLabel = node('span', 'tab-label'); tab.append(node('span', '', '›_'), tabLabel);
    group.append(tab, button('×', 'tab-close', () => closeTerminal(id))); $('.tab-close', group).setAttribute('aria-label', 'Close terminal tab'); $('#tabs').append(group);
    const t = {id, pane, title, info, connected, screen, overlay, group, tab, tabLabel, term: null, fit: null, ws: null, timer: null, closed: false};
    terminals.set(id, t); syncTerminal(t);
  }
  if (focus) activate(id); else remember();
}
function initTerminal(t) {
  if (t.term) return;
  if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') { toast('Terminal assets are missing. See README vendor instructions.', true); return; }
  t.term = new Terminal({cursorBlink: true, fontSize: 13, fontFamily: 'ui-monospace,Consolas,monospace', scrollback: 10000, theme: {background: '#0d1015', foreground: '#e8ebf1', cursor: '#ffa16a'}});
  t.fit = new FitAddon.FitAddon(); t.term.loadAddon(t.fit); t.term.open(t.screen); t.fit.fit();
  t.term.onData(data => { if (t.ws?.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify({type: 'input', data})); });
  t.term.onResize(({cols, rows}) => { if (t.ws?.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify({type: 'resize', cols, rows})); });
  t.observer = new ResizeObserver(() => { if (!t.pane.hidden) t.fit?.fit(); }); t.observer.observe(t.screen);
  attachTerminal(t);
}
function overlay(t, title, message, actionText, action) {
  t.overlay.hidden = false; t.overlay.replaceChildren(node('h2', '', title), node('p', '', message));
  if (action) t.overlay.append(button(actionText, 'primary', action));
}
function syncTerminal(t) {
  const a = agentById(t.id); if (!a) return;
  t.title.textContent = a.name; t.tabLabel.textContent = a.name;
  t.info.textContent = `${labels[a.status]} · ${a.session_id || 'session pending'} · ${a.folder}`;
  if (!a.alive) {
    overlay(t, 'This agent is offline.', 'Opening a terminal only attaches to a process. Wake the agent explicitly to continue its saved session.', a.managed ? 'Wake agent ↗' : '', a.managed ? () => wakeAgent(t.id) : null);
  } else if (t.term && (!t.ws || t.ws.readyState > 1) && !t.timer) attachTerminal(t);
}
function attachTerminal(t) {
  if (t.closed || !t.term || t.ws?.readyState < 2) return;
  if (!agentById(t.id)?.alive) { syncTerminal(t); return; }
  clearTimeout(t.timer); t.timer = null;
  overlay(t, 'Attaching terminal…', 'The agent keeps running independently of this tab.');
  const ws = new WebSocket(wsURL(`/ws/terminal/${t.id}?cols=${t.term.cols}&rows=${t.term.rows}`)); ws.binaryType = 'arraybuffer'; t.ws = ws;
  ws.onopen = () => { if (t.ws !== ws || t.closed) { ws.close(); return; } t.overlay.hidden = true; t.connected.textContent = '● Attached'; t.term.reset(); t.fit.fit(); ws.send(JSON.stringify({type: 'resize', cols: t.term.cols, rows: t.term.rows})); };
  ws.onmessage = event => { if (t.ws === ws && event.data instanceof ArrayBuffer) t.term.write(new Uint8Array(event.data)); };
  ws.onerror = () => ws.close();
  ws.onclose = () => {
    if (t.closed || t.ws !== ws) return;
    t.connected.textContent = '○ Detached';
    if (!agentById(t.id)?.alive) { syncTerminal(t); return; }
    overlay(t, 'Terminal disconnected.', 'Reattaching in a few seconds. The agent is not stopped by a tab disconnect.', 'Reconnect', () => { disconnectTerminal(t); attachTerminal(t); });
    t.timer = setTimeout(() => { t.timer = null; attachTerminal(t); }, 4000);
  };
}
function disconnectTerminal(t) {
  clearTimeout(t.timer); t.timer = null;
  if (t.ws) { const ws = t.ws; t.ws = null; ws.onclose = null; ws.close(); }
}
function closeTerminal(id) {
  const t = terminals.get(id); if (!t) return;
  t.closed = true; disconnectTerminal(t); t.observer?.disconnect(); t.term?.dispose(); t.pane.remove(); t.group.remove(); terminals.delete(id);
  // Deliberately no stop API call: only the attachment is disposable.
  if (activeTab === id) activate('dashboard'); remember();
}
function showDialog(id) { const dlg = $(id); $('.form-error', dlg).textContent = ''; dlg.showModal(); }
function editAgent(id) {
  const a = agentById(id); if (!a) return; editId = id;
  const form = $('#edit-form'); form.elements.name.value = a.name; form.elements.goal.value = a.goal; form.elements.status.value = a.status; form.elements.auto_resume.checked = a.auto_resume;
  updateForgetAvailability(); showDialog('#edit-dialog');
}
function updateForgetAvailability() {
  const a = agentById(editId);
  const active = a?.alive && a.status === 'running';
  $('#forget-agent').disabled = forgettingAgent || !a || active;
  $('#forget-explanation').textContent = !a ? 'This agent is no longer registered.' : active
    ? 'This agent is actively running. Stop it before forgetting it.'
    : 'Forget stops any remaining Swarm-owned process and removes the card. Saved session history stays in its current location.';
}
async function forgetAgent() {
  const id = editId, a = agentById(id);
  if (!a || forgettingAgent) return;
  if (a.alive && a.status === 'running') { updateForgetAvailability(); return; }
  const history = a.session_file ? `Saved session history will be kept at:\n${a.session_file}` : 'Any saved session history will be kept.';
  if (!confirm(`Forget ${a.name}? This stops any remaining Swarm-owned process and removes the dashboard card.\n\n${history}`)) return;
  const form = $('#edit-form'); $('.form-error', form).textContent = '';
  forgettingAgent = true; updateForgetAvailability(); $('[type=submit]', form).disabled = true;
  try {
    await api(`/api/agents/${id}`, 'DELETE');
    cardViews.forget(state.home, id); closeTerminal(id);
    if (editId === id) $('#edit-dialog').close();
    applyState({...state, agents: state.agents.filter(agent => agent.id !== id)});
    toast('Agent forgotten. Saved session history was kept.'); await refresh();
  } catch (error) {
    if (editId === id) $('.form-error', form).textContent = error.message;
    else toast(error.message, true);
  }
  finally { forgettingAgent = false; updateForgetAvailability(); $('[type=submit]', form).disabled = false; }
}
$('#forget-agent').addEventListener('click', forgetAgent);
function wakeAgent(id) {
  wakeId = id; $('#wake-form').reset(); $('#wake-title').textContent = `Wake ${agentById(id)?.name || 'agent'}`; showDialog('#wake-dialog');
}
async function stopAgent(id) {
  if (!confirm(`Stop ${agentById(id)?.name}? This stops the process and disables automatic restore. Its goal and session are retained.`)) return;
  try { await api(`/api/agents/${id}/stop`, 'POST'); toast('Agent stopped. Saved session retained.'); await refresh(); } catch (e) { toast(e.message, true); }
}
async function submit(form, action) {
  const submitButton = $('[type=submit]', form); submitButton.disabled = true; $('.form-error', form).textContent = '';
  try { await action(); } catch (error) { $('.form-error', form).textContent = error.message; } finally { submitButton.disabled = false; }
}
$('#create-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  submit(form, async () => {
    const data = Object.fromEntries(new FormData(form)); data.create_worktree = form.elements.create_worktree.checked;
    if (!data.model) delete data.model;
    if (data.create_worktree && (!data.repository || !data.branch)) throw new Error('A local repository and a new branch are required for a worktree.');
    const agent = await api('/api/agents', 'POST', data); $('#create-dialog').close(); form.reset();
    toast(agent.last_error ? `Agent registered but could not start: ${agent.last_error}` : 'Agent launched.', !!agent.last_error); await refresh();
  });
});
$('#wake-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  submit(form, async () => { const a = await api(`/api/agents/${wakeId}/wake`, 'POST', {message: form.elements.message.value}); $('#wake-dialog').close(); toast(a.last_error || 'Resume instructions sent.', !!a.last_error); await refresh(); });
});
$('#edit-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  submit(form, async () => { await api(`/api/agents/${editId}`, 'PATCH', {name: form.elements.name.value, goal: form.elements.goal.value, status: form.elements.status.value, auto_resume: form.elements.auto_resume.checked}); $('#edit-dialog').close(); toast('Agent record updated.'); await refresh(); });
});
async function loadSessions() {
  selectedSession = null; $('#session-list').replaceChildren(node('p', 'session-message', 'Reading saved pi sessions…'));
  try { const data = await api('/api/pi/sessions'); sessionData = data.sessions; renderSessions(); if (Object.keys(data.errors || {}).length) $('.form-error', $('#adopt-form')).textContent = `${Object.keys(data.errors).length} unreadable session file(s): ${Object.values(data.errors)[0]}`; }
  catch (e) { $('#session-list').replaceChildren(node('p', 'session-message', e.message)); }
}
function renderSessions() {
  const query = $('#session-search').value.toLowerCase();
  const list = $('#session-list'); list.replaceChildren();
  const visible = sessionData.filter(s => [s.name, s.goal, s.folder, s.session_id].join(' ').toLowerCase().includes(query));
  for (const s of visible) {
    const row = node('label', `session-choice${s.available ? '' : ' blocked'}${selectedSession === s.session_file ? ' selected' : ''}`);
    const radio = document.createElement('input'); radio.type = 'radio'; radio.name = 'selected_session'; radio.disabled = !s.available; radio.checked = selectedSession === s.session_file;
    radio.addEventListener('change', () => { selectedSession = s.session_file; const form = $('#adopt-form'); form.elements.name.value = s.name; form.elements.goal.value = s.goal; renderSessions(); });
    const details = node('div'); details.append(node('span', 'session-name', s.name), node('span', 'session-info', s.folder), node('span', 'session-info', `${s.session_id} · ${since(s.updated_at)}`));
    if (s.blocked_reason) details.append(node('span', 'session-reason', s.blocked_reason)); row.append(radio, details); list.append(row);
  }
  if (!visible.length) list.append(node('p', 'session-message', 'No saved sessions found. Pi saves persistent sessions under ~/.pi/agent/sessions by default; on WSL, Swarm also checks /mnt/c/Users/<user>/.pi/agent/sessions.'));
}
$('#adopt-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  submit(form, async () => {
    if (!selectedSession) throw new Error('Choose an available, inactive session first.');
    await api('/api/agents/adopt', 'POST', {session_file: selectedSession, name: form.elements.name.value, goal: form.elements.goal.value});
    $('#adopt-dialog').close(); toast('Session adopted. It will not run until you wake it.'); await refresh();
  });
});
async function loadGrokSessions() {
  const sequence = ++grokLoadSequence;
  selectedGrokSession = null; grokSessionData = [];
  $('#grok-import-preview').hidden = true;
  $('#grok-session-list').replaceChildren(node('p', 'session-message', 'Reading saved Grok Build sessions…'));
  $('.form-error', $('#import-grok-form')).textContent = '';
  try {
    const data = await api('/api/grok/sessions');
    if (sequence !== grokLoadSequence) return;
    grokSessionData = data.sessions;
    $('#grok-roots').textContent = `Session roots: ${(data.roots || []).join(' · ')}`;
    renderGrokSessions();
    if (Object.keys(data.errors || {}).length) $('.form-error', $('#import-grok-form')).textContent = `${Object.keys(data.errors).length} unreadable Grok session(s): ${Object.values(data.errors)[0]}`;
  } catch (error) {
    if (sequence === grokLoadSequence) $('#grok-session-list').replaceChildren(node('p', 'session-message', error.message));
  }
}
function renderGrokSessions() {
  const query = $('#grok-session-search').value.trim().toLowerCase();
  const visible = grokSessionData.filter(s => [s.name, s.goal, s.original_folder, s.folder, s.session_id].join(' ').toLowerCase().includes(query));
  const list = $('#grok-session-list'); list.replaceChildren();
  for (const session of visible) {
    const row = node('label', `session-choice${session.available ? '' : ' blocked'}${selectedGrokSession === session.session_file ? ' selected' : ''}`);
    const radio = document.createElement('input'); radio.type = 'radio'; radio.name = 'selected_grok_session'; radio.disabled = !session.available; radio.checked = selectedGrokSession === session.session_file;
    radio.addEventListener('change', () => {
      selectedGrokSession = session.session_file;
      const form = $('#import-grok-form'); form.elements.name.value = session.name; form.elements.goal.value = session.goal; form.elements.folder.value = session.folder;
      const preview = $('#grok-import-preview'); preview.hidden = false;
      preview.replaceChildren(node('p', '', `${session.num_messages} source records · ${session.original_folder}`), ...session.warnings.map(warning => node('p', 'hint', warning)));
      renderGrokSessions();
    });
    const details = node('div'); details.append(node('span', 'session-name', session.name), node('span', 'session-info', session.original_folder), node('span', 'session-info', `${session.session_id} · ${session.num_messages} records · ${since(session.updated_at)}`));
    if (session.blocked_reason) details.append(node('span', 'session-reason', session.blocked_reason));
    if (!session.folder_exists) details.append(node('span', 'session-reason', 'Choose an existing working folder before importing.'));
    row.append(radio, details); list.append(row);
  }
  if (!visible.length) list.append(node('p', 'session-message', grokSessionData.length ? 'No Grok sessions match your search.' : 'No saved Grok Build sessions found. Use --grok-sessions-dir to configure a different root.'));
}
$('#import-grok-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  submit(form, async () => {
    if (!selectedGrokSession) throw new Error('Choose an available Grok session first.');
    await api('/api/agents/import/grok', 'POST', {session_file: selectedGrokSession, name: form.elements.name.value, goal: form.elements.goal.value, folder: form.elements.folder.value});
    $('#import-grok-dialog').close(); filter = 'all'; $('#search').value = ''; activate('dashboard');
    toast('Grok history converted and adopted as pi. Wake it when you are ready.'); await refresh();
  });
});
$('#import-grok-button').addEventListener('click', () => { $('#import-grok-form').reset(); $('#grok-session-search').value = ''; $('#grok-roots').textContent = ''; showDialog('#import-grok-dialog'); loadGrokSessions(); });
$('#refresh-grok-sessions').addEventListener('click', loadGrokSessions);
$('#grok-session-search').addEventListener('input', renderGrokSessions);
for (const el of document.querySelectorAll('[data-close]')) el.addEventListener('click', () => el.closest('dialog').close());
const createDialog = () => showDialog('#create-dialog');
$('#create-button').addEventListener('click', createDialog); $('#empty-create').addEventListener('click', createDialog);
$('#adopt-button').addEventListener('click', () => { $('#adopt-form').reset(); $('#session-search').value = ''; showDialog('#adopt-dialog'); loadSessions(); });
$('#refresh-sessions').addEventListener('click', loadSessions); $('#session-search').addEventListener('input', renderSessions);
$('#dashboard-tab').addEventListener('click', () => activate('dashboard')); $('#search').addEventListener('input', render);
document.addEventListener('keydown', event => { if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) && activeTab === 'dashboard' && !document.querySelector('dialog[open]')) { event.preventDefault(); $('#search').focus(); } });
refresh(); connectState();
