/* Local UI. Registry text is always inserted as text, never interpreted as HTML. */
'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const node = (tag, cls = '', text = '') => { const el = document.createElement(tag); el.className = cls; el.textContent = text; return el; };
const button = (text, cls, action) => { const el = node('button', cls, text); el.type = 'button'; el.addEventListener('click', action); return el; };
const labels = {running: 'Running', waiting: 'Waiting for input', dormant: 'Dormant', idle: 'Idle', done: 'Done'};
let state = {agents: [], errors: {}, health: {}, home: ''};
let activeTab = 'dashboard', restored = false, stateSocket, reconnectDelay = 1000;
let restartingController = false, restartTimer, activeSwarm = 'local', stateGeneration = 0, presenceSocket;
let federationNodes = new Map(), federationRestored = false;
let selectedSession = null, sessionData = [], wakeId = null, editId = null;
let selectedGrokSession = null, grokSessionData = [], grokLoadSequence = 0;
let folderBrowserPath = null, folderLoadSequence = 0;
let forgettingAgent = false, metaCompactingId = null, toolInputCompactingId = null;
const terminals = new Map();
const cardViews = new SwarmCardViews.CardViews();
let toastTimer;
function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.className = `toast${error ? ' error' : ''}`; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, error ? 9000 : 4500);
}
const apiPath = path => activeSwarm === 'local' ? path : `/api/federation/${encodeURIComponent(activeSwarm)}/proxy${path}`;
const stateWSPath = () => activeSwarm === 'local' ? '/ws/state' : `/ws/federation/${encodeURIComponent(activeSwarm)}/state`;
const terminalWSPath = id => activeSwarm === 'local' ? `/ws/terminal/${id}` : `/ws/federation/${encodeURIComponent(activeSwarm)}/terminal/${id}`;
async function localApi(path, method = 'GET', body) {
  const response = await fetch(path, {method, headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  let value; try { value = await response.json(); } catch { throw new Error(`Unexpected response (${response.status})`); }
  if (!response.ok) {
    const error = new Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail || value));
    error.status = response.status; throw error;
  }
  return value;
}
async function api(path, method = 'GET', body) {
  const response = await fetch(apiPath(path), {method, headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
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
const tabStorageKey = () => `swarm.tabs.${activeSwarm}`;
function remember() { try { localStorage.setItem(tabStorageKey(), JSON.stringify({ids: [...terminals.keys()], active: activeTab})); } catch { /* storage may be disabled */ } }
function applyState(data) {
  if (!Array.isArray(data.agents)) return;
  state = data;
  for (const id of terminals.keys()) if (!agentById(id)) closeTerminal(id);
  render();
  if (!restored) {
    restored = true;
    try {
      const saved = JSON.parse(localStorage.getItem(tabStorageKey()) || '{}');
      for (const id of saved.ids || []) if (agentById(id)) openTerminal(id, false);
      activate(saved.active === 'dashboard' || terminals.has(saved.active) ? saved.active : 'dashboard');
    } catch { /* invalid persisted tabs are ignored */ }
  }
}
async function refresh() {
  const generation = stateGeneration;
  try { const data = await api('/api/state'); if (generation === stateGeneration) applyState(data); }
  catch (error) { if (generation === stateGeneration) toast(error.message, true); }
}
function connectState() {
  if (stateSocket && stateSocket.readyState < 2) return;
  const el = $('#connection'); el.textContent = 'Connecting…'; el.className = 'connection';
  const generation = stateGeneration;
  const socket = new WebSocket(wsURL(stateWSPath())); stateSocket = socket;
  socket.onopen = () => {
    reconnectDelay = 1000; el.textContent = activeSwarm === 'local' ? '● Live' : 'Connected'; el.className = activeSwarm === 'local' ? 'connection live' : 'connection'; refresh();
    if (restartingController) {
      restartingController = false; clearTimeout(restartTimer);
      updateRestartCapability();
      toast('Swarm backend controller restarted. Agents kept running.');
    }
  };
  socket.onmessage = event => {
    if (generation !== stateGeneration) return;
    try {
      const data = JSON.parse(event.data); applyState(data);
      el.textContent = data.federation_offline ? 'Remote offline' : '● Live';
      el.className = data.federation_offline ? 'connection offline' : 'connection live';
    } catch (error) { console.warn('Invalid state update', error); }
  };
  socket.onclose = () => { if (generation !== stateGeneration) return; el.textContent = 'Reconnecting…'; el.className = 'connection offline'; setTimeout(connectState, reconnectDelay); reconnectDelay = Math.min(15000, reconnectDelay * 2); };
  socket.onerror = () => socket.close();
}
function updateRestartCapability() {
  const button = $('#restart-controller');
  const allowed = activeSwarm === 'local' || federationNodes.get(activeSwarm)?.capabilities?.remote_restart;
  button.disabled = !allowed || restartingController;
  button.textContent = '↻';
  button.setAttribute('aria-label', restartingController ? 'Restarting backend' : 'Restart backend');
  button.setAttribute('aria-busy', String(restartingController));
  button.title = restartingController ? 'Restarting backend; waiting to reconnect' : allowed ? 'Restart the active Swarm backend controller' : 'This remote Swarm was not launched with --allow-remote-restart';
}
function renderSwarmSelector(data) {
  const incoming = new Map((data.nodes || []).map(item => [item.id, item]));
  if (activeSwarm !== 'local' && !incoming.has(activeSwarm) && federationNodes.has(activeSwarm)) {
    incoming.set(activeSwarm, {...federationNodes.get(activeSwarm), online: false});
  }
  federationNodes = incoming;
  const selector = $('#swarm-selector'), previous = activeSwarm;
  selector.replaceChildren();
  const local = document.createElement('option'); local.value = 'local'; local.textContent = 'Local'; selector.append(local);
  for (const remote of federationNodes.values()) {
    const option = document.createElement('option'); option.value = remote.id;
    option.textContent = `${remote.name} (${remote.id})${remote.online === false ? ' — offline' : ''}`;
    selector.append(option);
  }
  selector.value = [...selector.options].some(option => option.value === previous) ? previous : 'local';
  selector.title = selector.selectedOptions?.[0]?.textContent || 'Active Swarm';
  $('#swarm-switcher').hidden = !data.enabled && !federationNodes.size;
  if (activeSwarm !== 'local' && federationNodes.get(activeSwarm)?.online === false) {
    $('#connection').textContent = 'Remote offline'; $('#connection').className = 'connection offline';
  } else if (restartingController && activeSwarm !== 'local') {
    restartingController = false; clearTimeout(restartTimer);
    toast('Remote Swarm backend controller restarted. Agents kept running.');
  }
  updateRestartCapability();
  if (!federationRestored) {
    let saved = 'local'; try { saved = localStorage.getItem('swarm.active') || 'local'; } catch { /* storage may be disabled */ }
    if (saved === 'local') federationRestored = true;
    else if (federationNodes.has(saved)) { federationRestored = true; selector.value = saved; setTimeout(() => switchSwarm(saved), 0); }
  }
}
async function loadFederation() {
  try {
    const data = await localApi('/api/federation/nodes'); renderSwarmSelector(data);
    if (!data.enabled || presenceSocket?.readyState < 2) return;
    const socket = new WebSocket(wsURL('/ws/federation/presence')); presenceSocket = socket;
    socket.onmessage = event => { try { renderSwarmSelector({enabled: true, ...JSON.parse(event.data)}); } catch (error) { console.warn('Invalid federation presence', error); } };
    socket.onclose = () => { if (presenceSocket === socket) { presenceSocket = null; setTimeout(loadFederation, 3000); } };
    socket.onerror = () => socket.close();
  } catch (error) { console.warn('Federation discovery failed', error); }
}
function switchSwarm(nodeId) {
  if (nodeId === activeSwarm) return;
  for (const id of [...terminals.keys()]) closeTerminal(id);
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  if (stateSocket) { stateSocket.onclose = null; stateSocket.close(); stateSocket = null; }
  stateGeneration += 1; reconnectDelay = 1000; restored = false; activeSwarm = nodeId;
  try { localStorage.setItem('swarm.active', activeSwarm); } catch { /* storage may be disabled */ }
  state = {agents: [], errors: {}, health: {}, home: ''}; dashboardControls.status.reset(); $('#search').value = '';
  $('#swarm-selector').value = activeSwarm;
  $('#swarm-selector').title = $('#swarm-selector').selectedOptions?.[0]?.textContent || 'Active Swarm';
  activate('dashboard'); render(); updateRestartCapability(); connectState();
}
$('#swarm-selector').addEventListener('change', event => switchSwarm(event.currentTarget.value));
async function restartController() {
  const allowed = activeSwarm === 'local' || federationNodes.get(activeSwarm)?.capabilities?.remote_restart;
  if (!allowed) { toast('Remote restart is disabled on this Swarm.', true); return; }
  if (restartingController || !confirm('Restart the active Swarm backend controller?\n\nAgent processes are owned by tmux and will keep running. The dashboard will reconnect automatically.')) return;
  restartingController = true; updateRestartCapability();
  try {
    await api('/api/controller/restart', 'POST');
    toast('Backend restart requested. Waiting to reconnect…');
    restartTimer = setTimeout(() => {
      if (!restartingController) return;
      restartingController = false; updateRestartCapability();
      toast('The backend did not reconnect within 20 seconds.', true);
    }, 20000);
  } catch (error) {
    restartingController = false; updateRestartCapability();
    toast(error.message, true);
  }
}
$('#restart-controller').addEventListener('click', restartController);
function badge(status) {
  const el = node('span', `badge ${status}`); el.append(node('i', 'status-dot'), document.createTextNode(labels[status] || status)); return el;
}
function metadata(list, label, value) {
  if (!value) return;
  const dt = node('dt', '', label), dd = node('dd', '', String(value)); dd.title = String(value); list.append(dt, dd);
}
const metaCompactionRunning = a => a?.meta_compaction?.status === 'running';
const toolInputCompactionRunning = a => a?.tool_input_compaction?.status === 'running';
const compactionRunning = a => metaCompactionRunning(a) || toolInputCompactionRunning(a);
const compactionRequestInFlight = a => !!a && (metaCompactingId === a.id || toolInputCompactingId === a.id);
const compactionBlocked = a => compactionRunning(a) || compactionRequestInFlight(a);
function canRevertToolInputs(a) {
  const job = a?.tool_input_compaction;
  return !!(job?.archive && job.source_sha256 && job.replacement_sha256 &&
    (['completed', 'unchanged'].includes(job.status) || (job.status === 'failed' && job.operation === 'revert')));
}
function openAgentFiles(id) {
  const agent = agentById(id); if (!agent) return;
  const workspace = activeSwarm === 'local' ? 'Local Swarm' : federationNodes.get(activeSwarm)?.name || activeSwarm;
  agentFilesDialog.open(agent, apiPath(`/api/agents/${encodeURIComponent(id)}/files`), workspace);
}
function productLink(a) {
  const product = a.product;
  if (!product || !['link', 'file'].includes(product.kind)) return null;
  const label = typeof product.label === 'string' && product.label.trim() ? product.label : 'Product';
  const link = node('a', 'product-link', `${product.kind === 'file' ? '↓' : '↗'} ${label}`);
  if (product.kind === 'link') {
    try {
      const url = new URL(product.url);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return null;
      link.href = url.href;
    } catch { return null; }
    link.target = '_blank'; link.rel = 'noopener noreferrer';
    link.title = `Open latest product: ${label}`;
  } else {
    if (typeof product.id !== 'string' || !product.id) return null;
    link.href = apiPath(`/api/agents/${encodeURIComponent(a.id)}/products/${encodeURIComponent(product.id)}/download`);
    link.download = product.filename || 'product';
    link.title = `Download latest product: ${label}`;
  }
  link.setAttribute('aria-label', link.title);
  return link;
}
function renderCard(a) {
  const card = node('article', 'agent-card'); card.dataset.agentId = a.id;
  const activityHint = a.activity || 'No activity reported yet';
  card.title = `${a.name}\n${labels[a.status] || a.status} · ${a.alive ? 'Process online' : 'Offline'}\nActivity: ${activityHint}`;
  card.classList.toggle('has-process', !!a.alive);
  const inside = node('div', 'card-inner'), summary = node('header', 'card-summary'), header = node('div', 'card-head');
  const identity = node('div', 'card-identity'), avatar = node('div', 'avatar', a.name.slice(0, 1).toUpperCase());
  const subtitle = node('div', 'card-subtitle', `${a.backend}${a.import_source ? ` ← ${a.import_source.backend}` : ''} · ${a.id.slice(0, 8)}${a.managed ? '' : ' · observed'}`);
  const name = node('h3', '', a.name); name.title = `${a.name}\nActivity: ${activityHint}`;
  identity.append(name, subtitle);
  const details = node('div', 'card-details'); details.id = `card-details-${a.id}`;
  const toggle = button('', 'card-toggle', () => {
    const compact = !cardViews.isCompact(state.home, a);
    cardViews.setCompact(state.home, a.id, compact);
    SwarmCardViews.applyCardView({card, details, foot, avatar, subtitle, toggle}, compact, a.name);
    terminal.hidden = !compact;
  });
  toggle.setAttribute('aria-controls', `${details.id} card-actions-${a.id}`);
  const terminal = button('↗', 'card-terminal', () => openTerminal(a.id));
  terminal.title = `Open terminal for ${a.name}`;
  terminal.setAttribute('aria-label', terminal.title);
  terminal.hidden = true;
  const controls = node('div', 'card-controls'); controls.append(toggle, terminal);
  const statusBadge = badge(a.status); statusBadge.title = labels[a.status] || a.status;
  header.append(avatar, identity, statusBadge, controls);
  const goal = node('p', 'goal', a.goal); goal.title = a.goal;
  summary.append(header);
  const product = productLink(a);
  if (product) card.classList.toggle('has-product', true);
  const count = Number.isInteger(a.file_count) && a.file_count >= 0 ? a.file_count : 0;
  if (count || a.files_error) {
    const library = node('div', 'card-product-files');
    const files = button(a.files_error ? 'Files !' : `Files ${count}`, 'card-files', () => openAgentFiles(a.id));
    files.title = a.files_error || `${count} registered file${count === 1 ? '' : 's'}`;
    files.setAttribute('aria-label', `Files for ${a.name}`);
    if (product) library.append(product);
    library.append(files); summary.append(library); card.classList.toggle('has-files', true);
  } else if (product) summary.append(product);
  const activity = node('div', 'activity'); activity.style.borderColor = `var(--${a.status})`;
  activity.id = `card-activity-${a.id}`;
  toggle.setAttribute('aria-describedby', activity.id);
  activity.append(node('span', 'activity-label', 'CURRENT ACTIVITY'), document.createTextNode(activityHint));
  details.append(goal, activity);
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
  if (a.parent_id) metadata(meta, 'Parent', agentById(a.parent_id)?.name || a.parent_id);
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
  const actions = node('div', 'actions'), compacting = compactionBlocked(a);
  actions.append(button('Terminal ↗', 'small', () => openTerminal(a.id)), button('Edit', 'small', () => editAgent(a.id)));
  if (a.managed) {
    const wake = button('Wake', 'small', () => wakeAgent(a.id)); wake.disabled = compacting; if (compacting) wake.title = metaCompactionRunning(a) ? 'Meta-compaction is running' : 'Tool input compaction is running'; actions.append(wake);
    if (a.alive || a.auto_resume) actions.append(button('Stop', 'small danger', () => stopAgent(a.id)));
  }
  foot.append(runtime, actions); inside.append(summary, details); card.append(inside, foot);
  const compact = cardViews.isCompact(state.home, a);
  SwarmCardViews.applyCardView({card, details, foot, avatar, subtitle, toggle}, compact, a.name);
  terminal.hidden = !compact;
  return card;
}
function render() {
  $('#home-path').textContent = state.home ? `${state.home}/agents` : '~/.swarm/agents';
  $('#agent-count').textContent = state.agents.length;
  const health = $('#health'); health.replaceChildren();
  for (const name of ['tmux', 'pi']) health.append(node('span', `health-pill${state.health[name] ? '' : ' bad'}`, `${state.health[name] ? '●' : '○'} ${name}`));
  dashboardControls.update(state.agents);
  const query = $('#search').value.trim().toLowerCase();
  const visible = state.agents.filter(a => dashboardControls.status.matches(a.status) && [a.name, a.goal, a.activity, a.folder, a.branch].join(' ').toLowerCase().includes(query));
  const active = document.activeElement;
  const controlClass = active?.classList.contains('card-toggle') ? 'card-toggle' : active?.classList.contains('card-terminal') ? 'card-terminal' : null;
  const focusedControl = controlClass ? {id: active.closest('.agent-card')?.dataset.agentId, selector: `.${controlClass}`} : null;
  const forest = SwarmOrgChart.buildForest(state.agents, new Set(visible.map(a => a.id)));
  const chart = $('#agents'), scrollLeft = chart.scrollLeft;
  chart.replaceChildren(SwarmOrgChart.renderForest(forest.roots, renderCard));
  chart.hidden = !forest.shownCount;
  chart.scrollLeft = scrollLeft;
  $('#hierarchy-summary').textContent = forest.ancestorCount
    ? `${forest.matchedCount} ${forest.matchedCount === 1 ? 'match' : 'matches'} · ${forest.ancestorCount} dimmed ancestor${forest.ancestorCount === 1 ? '' : 's'} kept for context`
    : `${forest.shownCount} agent${forest.shownCount === 1 ? '' : 's'} · ${forest.roots.length} root${forest.roots.length === 1 ? '' : 's'}`;
  if (forest.issuesCount) $('#hierarchy-summary').textContent += ` · ${forest.issuesCount} missing or invalid parent links`;
  $('#hierarchy-summary').classList.toggle('sr-only', !forest.ancestorCount && !forest.issuesCount);
  $('#collapse-all').disabled = !state.agents.length;
  if (focusedControl) {
    const replacement = [...chart.querySelectorAll('.agent-card')].find(card => card.dataset.agentId === focusedControl.id);
    if (replacement) $(focusedControl.selector, replacement)?.focus({preventScroll: true});
  }
  const empty = $('#empty'); empty.hidden = visible.length > 0;
  $('h2', empty).textContent = state.agents.length ? 'No agents match this view.' : 'A little quiet in the swarm.';
  $('#empty-create').hidden = state.agents.length > 0;
  const errors = Object.entries(state.errors || {}); $('#registry-errors').hidden = !errors.length;
  $('#error-list').replaceChildren(...errors.map(([file, message]) => node('pre', '', `${file}: ${message}`)));
  for (const terminal of terminals.values()) syncTerminal(terminal);
  if ($('#edit-dialog').open) updateEditAvailability();
  if ($('#wake-dialog').open) updateWakeAvailability();
  agentFilesDialog.sync(agentById(agentFilesDialog.agentId));
}
$('#collapse-all').addEventListener('click', () => {
  cardViews.setAllCompact(state.home, state.agents, true);
  render();
});
function activate(id) {
  activeTab = id && terminals.has(id) ? id : 'dashboard';
  $('#dashboard').hidden = activeTab !== 'dashboard'; $('#terminals').hidden = activeTab === 'dashboard';
  $('#dashboard-tab').classList.toggle('active', activeTab === 'dashboard');
  for (const [key, t] of terminals) {
    t.pane.hidden = key !== activeTab; t.tab.classList.toggle('active', key === activeTab); t.group.classList.toggle('active', key === activeTab);
    t.mobileKeys?.update();
    if (key === activeTab) { initTerminal(t); requestAnimationFrame(() => { t.fit?.fit(); if (!t.copyText?.active) t.term?.focus(); }); }
  } remember();
}
function openTerminal(id, focus = true) {
  if (!agentById(id)) return;
  if (!terminals.has(id)) {
    const pane = node('div', 'terminal-pane'); pane.hidden = true;
    const header = node('div', 'terminal-header');
    const title = node('strong'), info = node('span', 'hint terminal-info'), connected = node('span', 'hint terminal-connection', 'Detached');
    const copyButton = node('button', 'small terminal-copy-button', 'Copy text'); copyButton.type = 'button'; copyButton.hidden = true; copyButton.dataset.agentId = id;
    header.append(title, info, connected, copyButton, button('Files', 'small terminal-files', () => openAgentFiles(id)), button('↻ Reconnect', 'small', () => { const t = terminals.get(id); disconnectTerminal(t); attachTerminal(t); }));
    const surface = node('div', 'terminal-surface'), screen = node('div', 'terminal-screen'), overlay = node('div', 'terminal-overlay');
    surface.append(screen, overlay); pane.append(header, surface); $('#terminals').append(pane);
    const group = node('span', 'tab-group'), tab = button('', 'tab', () => activate(id));
    const tabLabel = node('span', 'tab-label'); tab.append(node('span', '', '›_'), tabLabel);
    group.append(tab, button('×', 'tab-close', () => closeTerminal(id))); $('.tab-close', group).setAttribute('aria-label', 'Close terminal tab'); $('#tabs').append(group);
    const t = {id, pane, title, info, connected, screen, overlay, group, tab, tabLabel, copyButton, term: null, fit: null, ws: null, timer: null, closed: false};
    terminals.set(id, t); syncTerminal(t);
  }
  if (focus) activate(id); else remember();
}
function initTerminal(t) {
  if (t.term) return;
  if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') { toast('Terminal assets are missing. See README vendor instructions.', true); return; }
  t.term = new Terminal({cursorBlink: true, fontSize: 13, fontFamily: 'ui-monospace,Consolas,monospace', scrollback: 10000, theme: {background: '#0d1015', foreground: '#e8ebf1', cursor: '#ffa16a'}});
  t.fit = new FitAddon.FitAddon(); t.term.loadAddon(t.fit); t.term.open(t.screen); t.fit.fit();
  t.term.onData(data => { if (!t.copyText?.active && t.ws?.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify({type: 'input', data: t.mobileKeys?.transform(data) ?? data})); });
  t.term.onResize(({cols, rows}) => { if (t.ws?.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify({type: 'resize', cols, rows})); });
  t.touchScroll = new SwarmTerminalTouch.TerminalTouchScroll({
    element: t.screen,
    canScroll: () => !t.closed && !t.copyText?.active && t.ws?.readyState === WebSocket.OPEN && SwarmTerminalTouch.supportsScroll(t.term),
    wheel: (direction, point) => SwarmTerminalTouch.scrollTerminal(t.term, direction, point),
    onGesture: () => t.term.blur(),
  });
  t.copyText = new SwarmTerminalCopy.TerminalCopy({term: t.term, screen: t.screen, button: t.copyButton,
    onOpen: () => { t.touchScroll.reset(); t.term.blur(); t.mobileKeys?.update(); },
    onClose: focus => { if (focus && !t.closed && !t.pane.hidden) t.copyButton.focus({preventScroll: true}); },
  });
  t.mobileKeys = new SwarmTerminalKeys.TerminalKeys({term: t.term, pane: t.pane,
    isActive: () => !t.closed && !t.pane.hidden && !t.copyText?.active && !!agentById(t.id)?.alive,
    ready: () => t.ws?.readyState === WebSocket.OPEN,
    send: data => t.term.input(data, true),
  });
  t.observer = new ResizeObserver(() => { if (!t.pane.hidden) t.fit?.fit(); }); t.observer.observe(t.screen);
  attachTerminal(t);
}
function overlay(t, title, message, actionText, action) {
  t.overlay.hidden = false; t.overlay.replaceChildren(node('h2', '', title), node('p', '', message));
  if (action) t.overlay.append(button(actionText, 'primary', action));
}
function syncTerminal(t) {
  const a = agentById(t.id); if (!a) return;
  t.mobileKeys?.update();
  t.title.textContent = a.name; t.title.title = a.name; t.tabLabel.textContent = a.name;
  t.info.textContent = `${labels[a.status]} · ${a.session_id || 'session pending'} · ${a.folder}`;
  t.info.title = t.info.textContent;
  const compacting = compactionBlocked(a);
  if (!a.alive) {
    overlay(t, compacting ? 'Session compaction is running.' : 'This agent is offline.', compacting ? 'The saved session is being updated. It stays stopped afterward; wake it explicitly when the job completes.' : 'Opening a terminal only attaches to a process. Wake the agent explicitly to continue its saved session.', a.managed && !compacting ? 'Wake agent ↗' : '', a.managed && !compacting ? () => wakeAgent(t.id) : null);
  } else if (t.term && (!t.ws || t.ws.readyState > 1) && !t.timer) attachTerminal(t);
}
function attachTerminal(t) {
  if (t.closed || !t.term || t.ws?.readyState < 2) return;
  if (!agentById(t.id)?.alive) { syncTerminal(t); return; }
  clearTimeout(t.timer); t.timer = null;
  overlay(t, 'Attaching terminal…', 'The agent keeps running independently of this tab.');
  const ws = new WebSocket(wsURL(`${terminalWSPath(t.id)}?cols=${t.term.cols}&rows=${t.term.rows}`)); ws.binaryType = 'arraybuffer'; t.ws = ws;
  ws.onopen = () => { if (t.ws !== ws || t.closed) { ws.close(); return; } t.overlay.hidden = true; t.connected.textContent = '● Attached'; t.term.reset(); t.fit.fit(); t.mobileKeys?.update(); ws.send(JSON.stringify({type: 'resize', cols: t.term.cols, rows: t.term.rows})); };
  ws.onmessage = event => { if (t.ws === ws && event.data instanceof ArrayBuffer) t.term.write(new Uint8Array(event.data)); };
  ws.onerror = () => ws.close();
  ws.onclose = () => {
    if (t.closed || t.ws !== ws) return;
    t.connected.textContent = '○ Detached'; t.mobileKeys?.update();
    if (!agentById(t.id)?.alive) { syncTerminal(t); return; }
    overlay(t, 'Terminal disconnected.', 'Reattaching in a few seconds. The agent is not stopped by a tab disconnect.', 'Reconnect', () => { disconnectTerminal(t); attachTerminal(t); });
    t.timer = setTimeout(() => { t.timer = null; attachTerminal(t); }, 4000);
  };
}
function disconnectTerminal(t) {
  clearTimeout(t.timer); t.timer = null;
  if (t.ws) { const ws = t.ws; t.ws = null; ws.onclose = null; ws.close(); }
  t.mobileKeys?.update();
}
function closeTerminal(id) {
  const t = terminals.get(id); if (!t) return;
  t.closed = true; t.mobileKeys?.dispose(); t.copyText?.dispose(); t.touchScroll?.dispose(); disconnectTerminal(t); t.observer?.disconnect(); t.term?.dispose(); t.pane.remove(); t.group.remove(); terminals.delete(id);
  // Deliberately no stop API call: only the attachment is disposable.
  if (activeTab === id) activate('dashboard'); remember();
}
function showDialog(id) { const dlg = $(id); $('.form-error', dlg).textContent = ''; dlg.showModal(); }
function editAgent(id) {
  const a = agentById(id); if (!a) return; editId = id;
  const form = $('#edit-form'); form.elements.name.value = a.name; form.elements.goal.value = a.goal; form.elements.status.value = a.status; form.elements.auto_resume.checked = a.auto_resume;
  form.elements.meta_model.value = ''; form.elements.overlap_tokens.value = '10000'; form.elements.chunk_count.value = '2'; $('#meta-compact-controls').hidden = true; $('#meta-compact-toggle').textContent = 'Meta-compact…';
  $('#revert-allow-history-loss').checked = false;
  updateEditAvailability(); showDialog('#edit-dialog');
}
function updateForgetAvailability() {
  const a = agentById(editId);
  const active = a?.alive && a.status === 'running', compacting = compactionBlocked(a);
  $('#forget-agent').disabled = forgettingAgent || !a || active || compacting;
  $('#forget-explanation').textContent = !a ? 'This agent is no longer registered.' : compacting
    ? 'Forget is disabled while session compaction is running.'
    : active ? 'This agent is actively running. Stop it before forgetting it.'
    : 'Forget stops any remaining Swarm-owned process and removes the card. Saved session history stays in its current location.';
}
function updateEditAvailability() {
  updateForgetAvailability();
  const a = agentById(editId), job = a?.meta_compaction, toolJob = a?.tool_input_compaction;
  const running = metaCompactionRunning(a), toolRunning = toolInputCompactionRunning(a), starting = metaCompactingId === editId, toolStarting = toolInputCompactingId === editId;
  const blocked = running || toolRunning || starting || toolStarting;
  const eligible = !!a && a.managed === true && a.backend === 'pi' && !!a.session_file && !a.alive;
  let availability = 'Available while this managed Pi agent is stopped.';
  if (!a) availability = 'This agent is no longer registered.';
  else if (running) availability = 'Compacting the saved session. It will remain stopped when complete.';
  else if (toolRunning || toolStarting) availability = 'Tool input compaction is running. It will remain stopped when complete.';
  else if (!a.managed || a.backend !== 'pi') availability = 'Only managed Pi agents can be meta-compacted.';
  else if (!a.session_file) availability = 'A persisted Pi session is required.';
  else if (a.alive) availability = 'Stop this agent before meta-compacting its session.';
  $('#meta-compact-availability').textContent = availability;
  $('#meta-compact-toggle').disabled = !eligible || blocked;
  $('#start-meta-compact').disabled = !eligible || blocked;
  const optionsHidden = $('#meta-compact-controls').hidden;
  $('#edit-form').elements.meta_model.disabled = blocked || optionsHidden;
  $('#edit-form').elements.overlap_tokens.disabled = blocked || optionsHidden;
  $('#edit-form').elements.chunk_count.disabled = blocked || optionsHidden;
  $('#save-agent').disabled = blocked;
  const status = $('#meta-compact-status'); status.hidden = !job;
  if (job) {
    $('#meta-compact-state').textContent = job.status || 'unknown';
    $('#meta-compact-stage').textContent = job.stage ? `· ${job.stage}` : '';
    $('#meta-compact-error').hidden = !job.error; $('#meta-compact-error').textContent = job.error || '';
    $('#meta-compact-archive-row').hidden = !job.archive; $('#meta-compact-archive').textContent = job.archive || '';
    $('#meta-compact-directory-row').hidden = !job.directory; $('#meta-compact-directory').textContent = job.directory || '';
  }
  let toolAvailability = 'Available while this managed Pi agent is stopped.';
  if (!a) toolAvailability = 'This agent is no longer registered.';
  else if (running || starting) toolAvailability = 'Meta-compaction is running. Wait for it to finish.';
  else if (toolRunning || toolStarting) toolAvailability = 'Updating the saved tool-input session. It will remain stopped when complete.';
  else if (!a.managed || a.backend !== 'pi') toolAvailability = 'Only managed Pi agents can compact tool inputs.';
  else if (!a.session_file) toolAvailability = 'A persisted Pi session is required.';
  else if (a.alive) toolAvailability = 'Stop this agent before compacting tool inputs.';
  $('#tool-input-compact-availability').textContent = toolAvailability;
  $('#compact-tool-inputs').disabled = !eligible || blocked;
  $('#revert-tool-input-compaction').disabled = !eligible || blocked || !canRevertToolInputs(a);
  $('#revert-allow-history-loss').disabled = !eligible || blocked || !canRevertToolInputs(a);
  $('#tool-input-revert-hint').textContent = toolJob?.status === 'reverted'
    ? 'Original session restored. Both the original archive and compacted backup are retained.'
    : !canRevertToolInputs(a) ? 'No tool-input compaction is available to revert.'
    : 'Stop the agent first. Revert refuses newer history unless you explicitly check the history-loss option.';
  const toolStatus = $('#tool-input-compact-status'); toolStatus.hidden = !toolJob;
  if (toolJob) {
    $('#tool-input-compact-state').textContent = toolJob.status || 'unknown';
    const hasResult = toolJob.status !== 'reverted' && (toolJob.status === 'completed' || toolJob.status === 'unchanged' || toolJob.calls_compacted != null || toolJob.estimated_tokens_saved != null);
    $('#tool-input-compact-result').hidden = !hasResult;
    $('#tool-input-compact-count').textContent = String(toolJob.calls_compacted ?? 0);
    $('#tool-input-compact-savings').textContent = String(toolJob.estimated_tokens_saved ?? 0);
    $('#tool-input-compact-error').hidden = !toolJob.error; $('#tool-input-compact-error').textContent = toolJob.error || '';
    $('#tool-input-compact-archive-row').hidden = !toolJob.archive; $('#tool-input-compact-archive').textContent = toolJob.archive || '';
    $('#tool-input-compact-directory-row').hidden = !toolJob.directory; $('#tool-input-compact-directory').textContent = toolJob.directory || '';
    $('#tool-input-revert-backup-row').hidden = !toolJob.compacted_backup; $('#tool-input-revert-backup').textContent = toolJob.compacted_backup || '';
  }
}
async function startMetaCompaction() {
  const id = editId, a = agentById(id), form = $('#edit-form');
  if (!a || metaCompactingId || toolInputCompactingId || compactionRunning(a) || !a.managed || a.backend !== 'pi' || !a.session_file || a.alive) { updateEditAvailability(); return; }
  const overlapInput = form.elements.overlap_tokens, chunkInput = form.elements.chunk_count;
  if (!overlapInput.reportValidity() || !chunkInput.reportValidity()) return;
  const overlapTokens = Number(overlapInput.value), chunkCount = Number(chunkInput.value), model = form.elements.meta_model.value.trim();
  if (!Number.isInteger(chunkCount) || chunkCount < 2 || chunkCount > 16) { chunkInput.setCustomValidity('Use a whole number from 2 to 16.'); chunkInput.reportValidity(); chunkInput.setCustomValidity(''); return; }
  if (!Number.isInteger(overlapTokens) || overlapTokens < 0 || overlapTokens > 100000) { overlapInput.setCustomValidity('Use a whole number from 0 to 100000.'); overlapInput.reportValidity(); overlapInput.setCustomValidity(''); return; }
  const modelDescription = model || 'the agent/session model (an explicit model is required if none is saved)';
  if (!confirm(`Meta-compact ${a.name}?\n\nThis summarizes ${chunkCount} chunks separately, then merges them (${chunkCount + 1} Pi stages) using ${modelDescription}. Provider retries may add requests and costs. Summaries are lossy. Each adjacent pair shares approximately ${overlapTokens.toLocaleString()} tokens of overlap (capped for short inputs). Stop any external Pi using this session too.\n\nAfter success, Swarm archives and replaces this tracked file:\n${a.session_file}\n\nThe agent stays stopped until you explicitly Wake it.`)) return;
  $('.form-error', form).textContent = ''; metaCompactingId = id; updateEditAvailability();
  try {
    const job = await api(`/api/agents/${id}/meta-compact`, 'POST', {model: model || null, overlap_tokens: overlapTokens, chunk_count: chunkCount});
    const current = agentById(id); if (current) current.meta_compaction = job;
    const message = job.status === 'failed' ? (job.error || 'Meta-compaction failed.') : job.status === 'completed' ? 'Meta-compaction completed.' : 'Meta-compaction started.';
    toast(message, job.status === 'failed');
    render(); await refresh();
  } catch (error) { if (editId === id) $('.form-error', form).textContent = error.message; else toast(error.message, true); }
  finally { if (metaCompactingId === id) metaCompactingId = null; if ($('#edit-dialog').open) updateEditAvailability(); }
}
async function compactToolInputs() {
  const id = editId, a = agentById(id), form = $('#edit-form');
  if (!a || metaCompactingId || toolInputCompactingId || compactionRunning(a) || !a.managed || a.backend !== 'pi' || !a.session_file || a.alive) { updateEditAvailability(); return; }
  if (!confirm(`Compact completed tool inputs for ${a.name}?\n\nNo model calls are made. Long completed tool inputs are replaced by 100-character previews and toolrun IDs. Tool output contents are preserved. Completed tool exchanges become inert history and cannot execute as tool calls.\n\nThe full original session is archived, and the active branch is retained. This agent stays stopped until you explicitly Wake it.\n\nSession:\n${a.session_file}`)) return;
  $('.form-error', form).textContent = ''; toolInputCompactingId = id; updateEditAvailability();
  try {
    const report = await api(`/api/agents/${id}/compact-tool-inputs`, 'POST', {});
    const current = agentById(id); if (current) current.tool_input_compaction = report;
    const count = report.calls_compacted ?? 0, savings = report.estimated_tokens_saved ?? 0;
    toast(report.status === 'unchanged' ? 'Tool inputs were already compact.' : `Compacted ${count} tool inputs; estimated ${savings} tokens saved.`, !!report.error);
    render(); await refresh();
  } catch (error) { if (editId === id) $('.form-error', form).textContent = error.message; else toast(error.message, true); }
  finally { if (toolInputCompactingId === id) toolInputCompactingId = null; if ($('#edit-dialog').open) updateEditAvailability(); }
}
async function revertToolInputs() {
  const id = editId, a = agentById(id), form = $('#edit-form');
  if (!a || metaCompactingId || toolInputCompactingId || compactionRunning(a) || !canRevertToolInputs(a) || !a.managed || a.backend !== 'pi' || !a.session_file || a.alive) { updateEditAvailability(); return; }
  const allowHistoryLoss = $('#revert-allow-history-loss').checked;
  const historyWarning = allowHistoryLoss
    ? 'WARNING: You enabled history loss. Newer history and other changes will be discarded from the active session and retained only in the backup.'
    : 'Revert is refused if newer history or other changes exist, so they will not be discarded.';
  if (!confirm(`Revert tool-input compaction for ${a.name}?\n\nThis restores the exact original session from:\n${a.tool_input_compaction.archive}\n\nThe entire current session is backed up first. ${historyWarning} The original large inputs and context-size problem may return. No model calls are made. Stop any external Pi using this session too.\n\nThe agent stays stopped until you explicitly Wake it.`)) return;
  $('.form-error', form).textContent = ''; toolInputCompactingId = id; updateEditAvailability();
  try {
    const report = await api(`/api/agents/${id}/revert-tool-input-compaction`, 'POST', {allow_history_loss: allowHistoryLoss});
    const current = agentById(id); if (current) current.tool_input_compaction = report;
    const restoredMessage = report.history_discarded
      ? 'Original restored. Newer history was removed from the active session and kept in the backup.'
      : 'Original session restored. Current-session backup retained.';
    toast(report.status === 'reverted' ? restoredMessage : (report.error || 'Revert failed.'), report.status !== 'reverted');
    render(); await refresh();
  } catch (error) { if (editId === id) $('.form-error', form).textContent = error.message; else toast(error.message, true); }
  finally { if (toolInputCompactingId === id) toolInputCompactingId = null; if ($('#edit-dialog').open) updateEditAvailability(); }
}
async function forgetAgent() {
  const id = editId, a = agentById(id);
  if (!a || forgettingAgent) return;
  if ((a.alive && a.status === 'running') || compactionBlocked(a)) { updateForgetAvailability(); return; }
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
  finally { forgettingAgent = false; updateForgetAvailability(); $('[type=submit]', form).disabled = compactionBlocked(agentById(editId)); }
}
$('#forget-agent').addEventListener('click', forgetAgent);
$('#compact-tool-inputs').addEventListener('click', compactToolInputs);
$('#revert-tool-input-compaction').addEventListener('click', revertToolInputs);
$('#meta-compact-toggle').addEventListener('click', () => {
  const controls = $('#meta-compact-controls'); controls.hidden = !controls.hidden;
  $('#meta-compact-toggle').textContent = controls.hidden ? 'Meta-compact…' : 'Hide options';
  updateEditAvailability();
});
$('#start-meta-compact').addEventListener('click', startMetaCompaction);
function updateWakeAvailability() {
  const running = compactionBlocked(agentById(wakeId));
  $('[type=submit]', $('#wake-form')).disabled = running;
}
function wakeAgent(id) {
  const a = agentById(id); if (!a) return;
  if (compactionBlocked(a)) { toast('Wait for session compaction to finish before waking this agent.', true); return; }
  wakeId = id; $('#wake-form').reset(); $('#wake-title').textContent = `Wake ${a.name || 'agent'}`; updateWakeAvailability(); showDialog('#wake-dialog');
}
async function stopAgent(id) {
  if (!confirm(`Stop ${agentById(id)?.name}? This stops the process and disables automatic restore. Its goal and session are retained.`)) return;
  try { await api(`/api/agents/${id}/stop`, 'POST'); toast('Agent stopped. Saved session retained.'); await refresh(); } catch (e) { toast(e.message, true); }
}
async function submit(form, action) {
  const submitButton = $('[type=submit]', form); submitButton.disabled = true; $('.form-error', form).textContent = '';
  try { await action(); } catch (error) { $('.form-error', form).textContent = error.message; } finally {
    submitButton.disabled = false;
    if (form.id === 'edit-form') updateEditAvailability(); else if (form.id === 'wake-form') updateWakeAvailability();
  }
}
function addSelectOption(select, value, text = value) {
  const existing = [...select.options].find(option => option.value === value);
  if (existing) { existing.textContent = text; return existing; }
  const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option); return option;
}
function loadAgentFolderOptions(selected = '') {
  const select = $('#create-form').elements.folder;
  select.replaceChildren(); addSelectOption(select, '', state.agents.length ? 'Select an agent folder…' : 'Browse for a folder…');
  const folders = [...new Set(state.agents.map(agent => agent.folder).filter(Boolean))].sort((a, b) => a.localeCompare(b));
  for (const folder of folders) addSelectOption(select, folder);
  if (selected) addSelectOption(select, selected).selected = true;
}
async function loadModelOptions() {
  const select = $('#create-form').elements.model, hint = $('#model-selection-hint');
  select.disabled = true; select.replaceChildren(); addSelectOption(select, '', 'Loading Pi models…');
  try {
    const data = await api('/api/config/models'); select.replaceChildren();
    addSelectOption(select, '', data.default ? `Pi default — ${data.default}` : 'Pi default');
    for (const model of data.models || []) {
      const details = [model.context && `${model.context} context`, model.thinking === 'yes' && 'thinking', model.images === 'yes' && 'images'].filter(Boolean).join(' · ');
      addSelectOption(select, model.id, details ? `${model.id} — ${details}` : model.id);
    }
    hint.textContent = data.error ? `Pi model discovery warning: ${data.error}` : `${(data.models || []).length} models available from the server's Pi configuration.`;
  } catch (error) {
    select.replaceChildren(); addSelectOption(select, '', 'Pi default'); hint.textContent = `Could not list Pi models: ${error.message}`;
  } finally { select.disabled = false; }
}
function siblingWorktreePath(source, branch) {
  const clean = source.replace(/\/+$/, ''), slash = clean.lastIndexOf('/'), parent = slash >= 0 ? clean.slice(0, slash + 1) : '', name = slash >= 0 ? clean.slice(slash + 1) : clean;
  const safeBranch = branch.replace(/[\\/]/g, '_');
  return clean && safeBranch ? `${parent}${name}_${safeBranch}` : '';
}
function updateWorktreeCreateFields() {
  const form = $('#create-form'), enabled = form.elements.create_worktree.checked, fields = $('#worktree-create-fields');
  fields.hidden = !enabled; form.elements.branch.required = enabled;
  const destination = siblingWorktreePath(form.elements.folder.value, form.elements.branch.value.trim());
  $('#worktree-destination').textContent = destination || 'Choose a folder and branch';
  $('#folder-selection-hint').textContent = enabled
    ? 'Select the existing Git worktree to branch from. The destination is computed below.'
    : 'Choose a folder used by an existing agent, or browse folders visible to the Swarm server.';
}
async function loadFolderBrowser(path) {
  const sequence = ++folderLoadSequence, message = $('#folder-browser-message'); message.textContent = '';
  $('#folder-browser-list').replaceChildren(node('p', 'session-message', 'Reading server folders…'));
  try {
    const data = await api(`/api/folders${path ? `?path=${encodeURIComponent(path)}` : ''}`);
    if (sequence !== folderLoadSequence) return;
    folderBrowserPath = data.path; $('#folder-browser-path').textContent = data.path;
    $('#folder-parent').disabled = !data.parent; $('#folder-parent').dataset.path = data.parent || '';
    $('#select-folder').textContent = data.git ? 'Select this Git folder' : 'Select this folder';
    const list = $('#folder-browser-list'); list.replaceChildren();
    for (const directory of data.directories) {
      const entry = button(directory.name, 'folder-entry', () => loadFolderBrowser(directory.path));
      entry.setAttribute('role', 'option'); entry.title = directory.path; list.append(entry);
    }
    if (!data.directories.length) list.append(node('p', 'session-message', 'No readable subfolders.'));
    if (data.truncated) message.textContent = 'Only the first 2,000 folders are shown.';
  } catch (error) {
    if (sequence !== folderLoadSequence) return;
    folderBrowserPath = null; $('#folder-browser-list').replaceChildren(); message.textContent = error.message;
  }
}
function openFolderBrowser() {
  const selected = $('#create-form').elements.folder.value;
  showDialog('#folder-dialog'); loadFolderBrowser(selected || null);
}
$('#browse-folder').addEventListener('click', openFolderBrowser);
$('#folder-parent').addEventListener('click', event => loadFolderBrowser(event.currentTarget.dataset.path));
$('#select-folder').addEventListener('click', () => {
  if (!folderBrowserPath) return;
  const select = $('#create-form').elements.folder; addSelectOption(select, folderBrowserPath).selected = true;
  $('#folder-dialog').close(); updateWorktreeCreateFields();
});
$('#create-form').elements.folder.addEventListener('change', updateWorktreeCreateFields);
$('#create-form').elements.create_worktree.addEventListener('change', updateWorktreeCreateFields);
$('#create-form').elements.branch.addEventListener('input', updateWorktreeCreateFields);
$('#create-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  submit(form, async () => {
    const data = Object.fromEntries(new FormData(form));
    data.create_worktree = form.elements.create_worktree.checked;
    data.automatic_worktree_path = data.create_worktree;
    if (!data.model) delete data.model;
    if (!data.create_worktree) delete data.branch;
    if (data.create_worktree && (!data.folder || !data.branch.trim())) throw new Error('Choose a Git folder and enter a new branch.');
    const agent = await api('/api/agents', 'POST', data); $('#create-dialog').close(); form.reset();
    toast(agent.last_error ? `Agent registered but could not start: ${agent.last_error}` : 'Agent launched.', !!agent.last_error); await refresh();
  });
});
$('#wake-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  if (compactionBlocked(agentById(wakeId))) { updateWakeAvailability(); return; }
  submit(form, async () => {
    const message = form.elements.message.value;
    const a = await api(`/api/agents/${wakeId}/wake`, 'POST', {message});
    $('#wake-dialog').close();
    toast(a.last_error || (message ? 'Resume instructions sent.' : 'Session resumed without a prompt.'), !!a.last_error);
    await refresh();
  });
});
$('#edit-form').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget;
  if (compactionBlocked(agentById(editId))) { updateEditAvailability(); return; }
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
    $('#import-grok-dialog').close(); dashboardControls.status.reset(); $('#search').value = ''; activate('dashboard');
    toast('Grok history converted and adopted as pi. Wake it when you are ready.'); await refresh();
  });
});
$('#import-grok-button').addEventListener('click', () => { dashboardControls.adoption.close(true); $('#import-grok-form').reset(); $('#grok-session-search').value = ''; $('#grok-roots').textContent = ''; showDialog('#import-grok-dialog'); loadGrokSessions(); });
$('#refresh-grok-sessions').addEventListener('click', loadGrokSessions);
$('#grok-session-search').addEventListener('input', renderGrokSessions);
const agentFilesDialog = new SwarmAgentFiles.AgentFilesDialog({
  api: localApi,
  onChange: refresh,
  onInsert: (id, base) => {
    if (!agentById(id) || apiPath(`/api/agents/${encodeURIComponent(id)}/files`) !== base) return;
    $('#files-dialog').close(); openTerminal(id);
    terminals.get(id)?.mobileKeys?.clearModifiers();
    toast('File reference inserted. Not submitted.');
  },
});
const dashboardControls = new SwarmDashboardControls.DashboardControls({onStatusChange: render});
const relaySettingsDialog = new SwarmRelaySettings.RelaySettingsDialog({
  api: localApi,
  onChange: async value => {
    if (!value.enabled && activeSwarm !== 'local') switchSwarm('local');
    await loadFederation();
  },
});
for (const el of document.querySelectorAll('[data-close]')) el.addEventListener('click', () => el.closest('dialog').close());
const createDialog = () => {
  const form = $('#create-form'); form.reset(); loadAgentFolderOptions(); updateWorktreeCreateFields();
  $('#model-selection-hint').textContent = 'Reading models from the server Pi configuration…';
  showDialog('#create-dialog'); loadModelOptions();
};
$('#create-button').addEventListener('click', createDialog); $('#empty-create').addEventListener('click', createDialog);
$('#adopt-button').addEventListener('click', () => { dashboardControls.adoption.close(true); $('#adopt-form').reset(); $('#session-search').value = ''; showDialog('#adopt-dialog'); loadSessions(); });
$('#refresh-sessions').addEventListener('click', loadSessions); $('#session-search').addEventListener('input', renderSessions);
$('#dashboard-tab').addEventListener('click', () => activate('dashboard')); $('#search').addEventListener('input', render);
document.addEventListener('keydown', event => { if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) && activeTab === 'dashboard' && !document.querySelector('dialog[open]')) { event.preventDefault(); $('#search').focus(); } });
loadFederation(); refresh(); connectState();
