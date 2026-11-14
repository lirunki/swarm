/* Local UI. Registry text is always inserted as text, never interpreted as HTML. */
'use strict';
const $ = (selector, root = document) => root.querySelector(selector);
const node = (tag, cls = '', text = '') => { const el = document.createElement(tag); el.className = cls; el.textContent = text; return el; };
const button = (text, cls, action) => { const el = node('button', cls, text); el.type = 'button'; el.addEventListener('click', action); return el; };
const labels = {running: 'Running', waiting: 'Waiting for input', dormant: 'Dormant', idle: 'Idle', done: 'Done'};
let state = {agents: [], errors: {}, health: {}, home: ''};
let activeTab = 'dashboard', browserOpen = false, uiViewStateRestored = false, stateSocket, stateSocketPending = null, reconnectDelay = 1000;
let restartingController = false, restartTimer, activeSwarm = 'local', stateGeneration = 0, presenceSocket, presenceSocketPending = false;
let federationNodes = new Map(), federationRestored = false;
let notificationsInbox = null, pendingNotificationReveal = null, notificationHighlightTimer = null;
let selectedSession = null, sessionData = [], wakeId = null, editId = null;
let selectedGrokSession = null, grokSessionData = [], grokLoadSequence = 0;
let folderBrowserPath = null, folderLoadSequence = 0;
let forgettingAgent = false, metaCompactingId = null, toolInputCompactingId = null;
const toolOutputRepairRequests = new Map();
const terminals = new Map();
const groupedBranches = new Set();
let selectedAgentGroup = null, agentGroupsController = null, groupingModeController = null, historyArchiveController = null, agentLimitsController = null;
let remoteBrowserController = null, browserSettingsController = null;
let browserGroups = null, browserGroupingProjection = null, browserGroupingError = null, browserGroupingOperation = 0, groupingBranchBusy = false, groupingBranchOperation = 0;
const openedAgentGroups = new Set(), agentGroupTabs = new Map();
const cardViews = new SwarmCardViews.CardViews();
const wheelPreferences = SwarmTerminalWheel.preferences;
const wheelSettings = new SwarmTerminalWheel.WheelSettingsController({preferences:wheelPreferences,onError:error=>toast(error.message||String(error),true)});
let toastTimer;
function toast(message, error = false) {
  const el = $('#toast'); el.textContent = message; el.className = `toast${error ? ' error' : ''}`; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, error ? 9000 : 4500);
}
const apiPath = path => activeSwarm === 'local' ? path : `/api/federation/${encodeURIComponent(activeSwarm)}/proxy${path}`;
const stateWSPath = () => activeSwarm === 'local' ? '/ws/state' : `/ws/federation/${encodeURIComponent(activeSwarm)}/state`;
const terminalWSPath = id => activeSwarm === 'local' ? `/ws/terminal/${id}` : `/ws/federation/${encodeURIComponent(activeSwarm)}/terminal/${id}`;
const browserAuth = globalThis.SwarmAuth?.BrowserAuth;
const authenticatedFetch = (path, options) => browserAuth ? browserAuth.fetch(path, options) : fetch(path, options);
const authenticatedSocketURL = path => browserAuth ? browserAuth.socketURL(path) : Promise.resolve(wsURL(path));
function nonJsonResponseError(response) {
  const status = response.status || 'unknown';
  return new Error(`Server error ${status}; check backend logs. Request outcome unknown; check the card before retrying.`);
}
async function localApi(path, method = 'GET', body) {
  const request = typeof authenticatedFetch === 'function' ? authenticatedFetch : fetch;
  const response = await request(path, {method, headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  let value; try { value = await response.json(); } catch { throw nonJsonResponseError(response); }
  if (!response.ok) {
    const error = new Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail || value));
    error.status = response.status; throw error;
  }
  return value;
}
async function api(path, method = 'GET', body) {
  const request = typeof authenticatedFetch === 'function' ? authenticatedFetch : fetch;
  const response = await request(apiPath(path), {method, headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  let value; try { value = await response.json(); } catch { throw nonJsonResponseError(response); }
  if (!response.ok) {
    const error = new Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail || value));
    error.status = response.status; throw error;
  }
  return value;
}
async function browserRequest(path, method = 'GET', body, binary = false) {
  const request = typeof authenticatedFetch === 'function' ? authenticatedFetch : fetch;
  const response = await request(path, {method, headers: body ? {'Content-Type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
  if (!binary) {
    let value; try { value = await response.json(); } catch { throw nonJsonResponseError(response); }
    if (!response.ok) {
      const error = new Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail || value));
      error.status = response.status; error.uncertain = value.uncertain === true; error.requestId = value.request_id; throw error;
    }
    return value;
  }
  if (!response.ok) {
    let value; try { value = await response.json(); } catch { throw nonJsonResponseError(response); }
    const error = new Error(typeof value.detail === 'string' ? value.detail : JSON.stringify(value.detail || value));
    error.status = response.status; error.uncertain = value.uncertain === true; error.requestId = value.request_id; throw error;
  }
  const type = (response.headers.get('content-type') || '').split(';', 1)[0].trim().toLowerCase();
  const declared = Number(response.headers.get('content-length'));
  if (type !== 'image/png') throw Error('Browser screenshot response was not a PNG image');
  if (Number.isFinite(declared) && declared > 8 * 1024 * 1024) throw Error('Browser screenshot exceeds the 8 MiB limit');
  const blob = await response.blob();
  if (blob.type && blob.type !== 'image/png' || blob.size > 8 * 1024 * 1024) throw Error('Browser returned an invalid screenshot');
  return blob;
}
const agentById = id => state.agents.find(a => a.id === id);
const wsURL = path => `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${path}`;
function since(value) {
  const ms = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(ms)) return 'unknown';
  const s = Math.max(0, Math.floor(ms / 1000));
  return s < 60 ? 'just now' : s < 3600 ? `${Math.floor(s / 60)}m ago` : s < 86400 ? `${Math.floor(s / 3600)}h ago` : `${Math.floor(s / 86400)}d ago`;
}
const uiViewStateStorageKey = () => `swarm.ui-view-state.${activeSwarm}`;
const legacyTabStorageKey = () => `swarm.tabs.${activeSwarm}`;
const emptyUIViewState = () => ({terminalTabs: {ids: [], active: 'dashboard'}, browserOpen: false, groupedBranches: [], agentGroupView: {selected: null, opened: []}});
function normalizeUIViewState(value) {
  if (!value || typeof value !== 'object') return emptyUIViewState();
  const tabs = value.terminalTabs && typeof value.terminalTabs === 'object' ? value.terminalTabs : value;
  const ids = Array.isArray(tabs.ids) ? [...new Set(tabs.ids.filter(id => typeof id === 'string' && id).slice(0, 200))] : [];
  const active = typeof tabs.active === 'string' && tabs.active ? tabs.active : 'dashboard';
  const grouped = Array.isArray(value.groupedBranches)
    ? [...new Set(value.groupedBranches.filter(id => typeof id === 'string' && id).slice(0, 1000))]
    : [];
  const groupView = value.agentGroupView && typeof value.agentGroupView === 'object' ? value.agentGroupView : {};
  const opened = Array.isArray(groupView.opened) ? [...new Set(groupView.opened.filter(id => typeof id === 'string' && id).slice(0,100))] : [];
  const savedBrowserOpen = typeof value.browserOpen === 'boolean' ? value.browserOpen : typeof value.browserView?.open === 'boolean' ? value.browserView.open : active === 'browser';
  return {terminalTabs: {ids, active}, browserOpen: savedBrowserOpen || active === 'browser', groupedBranches: grouped, agentGroupView: {selected: typeof groupView.selected === 'string' ? groupView.selected : null, opened}};
}
function readUIViewState() {
  try {
    const current = localStorage.getItem(uiViewStateStorageKey());
    if (current) return normalizeUIViewState(JSON.parse(current));
    const legacy = localStorage.getItem(legacyTabStorageKey());
    return legacy ? normalizeUIViewState(JSON.parse(legacy)) : emptyUIViewState();
  } catch { return emptyUIViewState(); }
}
function rememberUIViewState() {
  try {
    const value = {
      terminalTabs: {ids: [...terminals.keys()], active: activeTab},
      browserOpen,
      groupedBranches: serverManagedBranches() ? readUIViewState().groupedBranches : [...groupedBranches],
      agentGroupView: {selected: selectedAgentGroup, opened: [...openedAgentGroups]},
    };
    localStorage.setItem(uiViewStateStorageKey(), JSON.stringify(value));
    localStorage.removeItem?.(legacyTabStorageKey());
  } catch { /* storage may be disabled */ }
}
function applyState(data) {
  if (!Array.isArray(data.agents)) return;
  const savedView = uiViewStateRestored ? null : readUIViewState();
  if (savedView) {
    groupedBranches.clear();
    for (const id of savedView.groupedBranches) groupedBranches.add(id);
  }
  const groupingAvailable = SwarmAgentGroups.validProjection(data.agent_groups) && !data.federation_offline;
  if (!groupingAvailable) data={...data,agent_groups:state.agent_groups || {revision:0,groups:[],assignments:{}},agent_groups_error:data.agent_groups_error || 'Grouping is unavailable until this Swarm provides a current group state.'};
  else if (!savedView && state.agent_groups && data.agent_groups.revision < state.agent_groups.revision) data={...data,agent_groups:state.agent_groups};
  const previousServerMode=state.grouping_settings?.server_managed;
  if(data.grouping_settings!=null && !SwarmGroupingMode.validSettings(data.grouping_settings)) data={...data,grouping_settings:state.grouping_settings || data.grouping_settings,grouping_settings_error:'Current grouping mode is invalid; edits are disabled.'};
  else if(data.grouping_settings==null && state.grouping_settings && !savedView) data={...data,grouping_settings:state.grouping_settings,grouping_settings_error:'Current grouping mode is unavailable; edits are disabled.'};
  if (!savedView && data.grouping_settings && state.grouping_settings && data.grouping_settings.revision < state.grouping_settings.revision) data={...data,grouping_settings:state.grouping_settings};
  if(!savedView && data.agent_limits && state.agent_limits && data.agent_limits.revision < state.agent_limits.revision) data={...data,agent_limits:state.agent_limits,agent_limits_error:state.agent_limits_error};
  state = data;
  syncGroupingStorage(savedView,previousServerMode);
  if (savedView) {
    browserOpen = savedView.browserOpen;
    selectedAgentGroup = savedView.agentGroupView.selected;
    openedAgentGroups.clear();
    for (const id of savedView.agentGroupView.opened) openedAgentGroups.add(id);
  }
  fleetControl?.applyState(data);
  for (const id of terminals.keys()) if (!agentById(id)) closeTerminal(id);
  render();
  if (savedView) {
    uiViewStateRestored = true;
    for (const id of savedView.terminalTabs.ids) if (agentById(id)) openTerminal(id, false);
    const selected = savedView.terminalTabs.active;
    activate(selected === 'dashboard' || selected === 'browser' || terminals.has(selected) || isAgentGroupTab(selected) ? selected : 'dashboard');
  }
  consumeNotificationReveal();
  notificationsInbox?.refresh();
}
async function refresh() {
  const generation = stateGeneration, before = state;
  try { const data = await api('/api/state'); if (generation === stateGeneration && before === state) applyState(data); }
  catch (error) { if (generation === stateGeneration) toast(error.message, true); }
}
async function connectState() {
  if (stateSocketPending === stateGeneration || (stateSocket && stateSocket.readyState < 2)) return;
  const el = $('#connection'); el.textContent = 'Connecting…'; el.className = 'connection';
  const generation = stateGeneration, path = stateWSPath(); stateSocketPending = generation;
  let url;
  try { url = await authenticatedSocketURL(path); }
  catch (error) {
    if (generation === stateGeneration) { el.textContent = error.name === 'AuthCancelledError' ? 'Sign in required' : 'Connection failed'; el.className = 'connection offline'; }
    return;
  } finally { if (stateSocketPending === generation) stateSocketPending = null; }
  if (generation !== stateGeneration || path !== stateWSPath()) return;
  const socket = new WebSocket(url); stateSocket = socket;
  socket.onopen = () => {
    if (generation !== stateGeneration) { socket.close(); return; }
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
  socket.onclose = (event = {}) => { if (generation !== stateGeneration) return; stateSocket = null;
    if (event.code === 4401 && browserAuth) { browserAuth.invalidate(path); setTimeout(connectState, 1000); return; }
    el.textContent = 'Reconnecting…'; el.className = 'connection offline'; setTimeout(connectState, reconnectDelay); reconnectDelay = Math.min(15000, reconnectDelay * 2); };
  socket.onerror = () => socket.close();
}
function updateRestartCapability() {
  const button = $('#restart-controller');
  const allowed = activeSwarm === 'local' || federationNodes.get(activeSwarm)?.capabilities?.remote_restart;
  button.disabled = !allowed || restartingController;
  const fleet = $('#fleet-control-button');
  if (fleet) { fleet.disabled = !allowed || restartingController; fleet.title = allowed ? 'Master control' : 'Remote fleet restarts require --allow-remote-restart'; }
  button.textContent = restartingController ? '↻ Restarting…' : '↻ Restart active Swarm';
  button.setAttribute('aria-label', restartingController ? 'Restarting backend' : 'Restart backend');
  button.setAttribute('aria-busy', String(restartingController));
  button.title = restartingController ? 'Restarting backend; waiting to reconnect' : allowed ? 'Restart the active Swarm backend controller' : 'This remote Swarm was not launched with --allow-remote-restart';
  const scope = $('#notifications-active-swarm');
  const ownerName=activeSwarm === 'local' ? 'Local Swarm' : federationNodes.get(activeSwarm)?.name || activeSwarm;
  if (scope) scope.textContent = `Active Swarm: ${ownerName}`;
  const settingsOwner=$('#settings-active-owner');if(settingsOwner)settingsOwner.textContent=ownerName;
}
function notificationSources() {
  return [{id: 'local', name: 'Local Swarm'}, ...[...federationNodes.values()]
    .filter(remote => remote.online !== false)
    .map(remote => ({id: remote.id, name: remote.name || remote.id}))];
}
function updateNotificationSources(refreshInbox = true) {
  if (!notificationsInbox) return;
  notificationsInbox.setSources(notificationSources());
  if (refreshInbox) notificationsInbox.refresh();
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
  updateNotificationSources();
  if (!federationRestored) {
    let saved = 'local'; try { saved = localStorage.getItem('swarm.active') || 'local'; } catch { /* storage may be disabled */ }
    if (saved === 'local') federationRestored = true;
    else if (federationNodes.has(saved)) { federationRestored = true; selector.value = saved; setTimeout(() => switchSwarm(saved), 0); }
  }
}
async function loadFederation() {
  try {
    const data = await localApi('/api/federation/nodes'); renderSwarmSelector(data);
    if (!data.enabled || presenceSocketPending || presenceSocket?.readyState < 2) return;
    presenceSocketPending = true;
    let url;
    try { url = await authenticatedSocketURL('/ws/federation/presence'); } finally { presenceSocketPending = false; }
    const socket = new WebSocket(url); presenceSocket = socket;
    socket.onmessage = event => { try { renderSwarmSelector({enabled: true, ...JSON.parse(event.data)}); } catch (error) { console.warn('Invalid federation presence', error); } };
    socket.onclose = (event = {}) => { if (presenceSocket === socket) { presenceSocket = null;
      if (event.code === 4401 && browserAuth) browserAuth.invalidate('/api/state');
      setTimeout(loadFederation, 3000); } };
    socket.onerror = () => socket.close();
  } catch (error) { console.warn('Federation discovery failed', error); }
}
function switchSwarm(nodeId) {
  if (nodeId === activeSwarm) return;
  dashboardControls?.closeAddMenus?.();
  notificationsInbox?.clear();
  if(typeof remoteBrowserController!=='undefined')remoteBrowserController?.deactivate();
  if(uiViewStateRestored) rememberUIViewState();
  for (const id of [...terminals.keys()]) closeTerminal(id, false);
  for (const dialog of document.querySelectorAll('dialog[open]')) dialog.close();
  if (stateSocket) { stateSocket.onclose = null; stateSocket.close(); stateSocket = null; }
  stateGeneration += 1; groupingBranchOperation+=1;browserGroupingOperation+=1;groupingBranchBusy=false;browserGroupingProjection=null;browserGroupingError=null; reconnectDelay = 1000; uiViewStateRestored = false; activeSwarm = nodeId; groupedBranches.clear(); selectedAgentGroup = null; openedAgentGroups.clear();
  for (const tab of agentGroupTabs.values()) tab.wrapper.remove(); agentGroupTabs.clear();
  try { localStorage.setItem('swarm.active', activeSwarm); } catch { /* storage may be disabled */ }
  state = {agents: [], errors: {}, health: {}, home: ''}; dashboardControls.status.reset(); $('#search').value = '';
  $('#swarm-selector').value = activeSwarm;
  $('#swarm-selector').title = $('#swarm-selector').selectedOptions?.[0]?.textContent || 'Active Swarm';
  if(typeof remoteBrowserController!=='undefined')remoteBrowserController?.setContext();
  if(typeof browserSettingsController!=='undefined')browserSettingsController?.setContext();
  activate('dashboard', false); render(); updateRestartCapability(); connectState();
  updateNotificationSources();
}
function revealNotificationAgent({swarmId, agentId}) {
  pendingNotificationReveal = {swarmId, agentId};
  if (swarmId !== activeSwarm) switchSwarm(swarmId);
  else consumeNotificationReveal();
}
function consumeNotificationReveal() {
  const target = pendingNotificationReveal;
  if (!target || target.swarmId !== activeSwarm || !uiViewStateRestored) return;
  const agent = agentById(target.agentId);
  pendingNotificationReveal = null;
  if (!agent) { toast('The notification was removed, but its agent is no longer available.', true); return; }
  if (agent.alive && terminals.has(agent.id)) { activate(agent.id); return; }
  dashboardControls.status.reset(); $('#search').value = '';
  cardViews.setCompact(state.home, agent.id, false);
  openAgentGroup(agentGroupFor(agent.id)); render();
  const card = [...$('#agents').querySelectorAll('.agent-card')].find(value => value.dataset.agentId === agent.id);
  if (!card) { toast('The agent is hidden by unavailable hierarchy data.', true); return; }
  card.scrollIntoView({behavior: 'smooth', block: 'center', inline: 'center'});
  clearTimeout(notificationHighlightTimer); card.classList.add('notification-reveal');
  notificationHighlightTimer = setTimeout(() => card.classList.remove('notification-reveal'), 5000);
}
$('#swarm-selector').addEventListener('change', event => switchSwarm(event.currentTarget.value));
async function restartController() {
  const allowed = activeSwarm === 'local' || federationNodes.get(activeSwarm)?.capabilities?.remote_restart;
  if (!allowed) { toast('Remote restart is disabled on this Swarm.', true); return; }
  if (restartingController || !confirm('Restart the active Swarm backend controller?\n\nAgent processes are owned by tmux and will keep running. The dashboard will reconnect automatically.')) return;
  $('#notifications-dialog')?.close();
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
const provisioningAgent = a => a?.spawn_state === 'provisioning';
const incompleteSetup = a => ['provisioning', 'failed', 'cancelled'].includes(a?.spawn_state);
const restartInProgress = a => a && Object.prototype.hasOwnProperty.call(a,'restart_job_id')
  ? !!a.restart_job_id : !!a?.restart?.job_id && !['completed','cancelled','failed'].includes(a.restart.state);
function creationStage(a) {
  if (typeof a?.creation?.stage === 'string' && a.creation.stage.trim()) return a.creation.stage.trim();
  return {queued: 'Queued', running: 'Setting up…', completed: 'Setup complete', failed: 'Setup failed', cancelled: 'Setup cancelled'}[a?.creation?.state] || (provisioningAgent(a) ? 'Queued' : 'Setup failed');
}
function displayBadge(a) {
  if (restartInProgress(a)) return {state: 'restarting', text: 'Restarting'};
  if (provisioningAgent(a)) return {state: 'provisioning', text: 'Setting up'};
  if (['failed', 'cancelled'].includes(a?.spawn_state)) return {state: 'setup-failed', text: 'Setup failed'};
  return {state: a.status, text: labels[a.status] || a.status};
}
function badge(agent) {
  const display = displayBadge(agent);
  const el = node('span', `badge ${display.state}`); el.append(node('i', 'status-dot'), document.createTextNode(display.text)); return el;
}
function metadata(list, label, value) {
  if (!value) return;
  const dt = node('dt', '', label), dd = node('dd', '', String(value)); dd.title = String(value); list.append(dt, dd);
}
const metaCompactionRunning = a => a?.meta_compaction?.status === 'running';
const toolInputCompactionRunning = a => a?.tool_input_compaction?.status === 'running';
const toolOutputRepairRunning = a => a?.tool_output_repair?.status === 'running';
const toolOutputRepairInFlight = a => {
  const requests = typeof toolOutputRepairRequests === 'undefined' ? null : toolOutputRepairRequests;
  const request = a && requests?.get(a.id);
  return !!request && request.generation === stateGeneration && request.swarm === activeSwarm;
};
const historyArchiveRunning = a => a?.context_archive?.status === 'running';
const historyArchiveInFlight = a => typeof historyArchiveController !== 'undefined' && !!historyArchiveController?.inFlight(a);
const compactionRunning = a => metaCompactionRunning(a) || toolInputCompactionRunning(a) || toolOutputRepairRunning(a) || historyArchiveRunning(a);
const compactionRequestInFlight = a => !!a && (historyArchiveInFlight(a) || metaCompactingId === a.id || toolInputCompactingId === a.id || toolOutputRepairInFlight(a));
const compactionBlocked = a => compactionRunning(a) || compactionRequestInFlight(a);
const persistedManagedPi = a => !!a && a.managed === true && a.backend === 'pi' && !!a.session_file;
const outputRepairStopping = a => !!a && (!!a.stop_pending || !!a.orchestration_stop);
const outputRepairUnavailableReason = a => {
  if (!a) return 'This agent is no longer registered.';
  if (!a.managed || a.backend !== 'pi') return 'Only managed Pi agents can repair oversized tool outputs.';
  if (!a.session_file) return 'A persisted Pi session is required.';
  if (restartInProgress(a)) return 'Fleet restart is active. Wait for it to finish.';
  if (incompleteSetup(a)) return provisioningAgent(a) ? 'Setup is still running.' : 'Setup did not complete.';
  if (a.alive || outputRepairStopping(a)) return 'Stop first. Output repair requires a stopped agent.';
  if (compactionBlocked(a)) return toolOutputRepairRunning(a) || toolOutputRepairInFlight(a)
    ? 'Repairing oversized outputs. The agent will remain stopped.'
    : 'Another saved-session operation is running. Wait for it to finish.';
  return '';
};
function canRevertToolInputs(a) {
  const job = a?.tool_input_compaction;
  return !!(job?.archive && job.source_sha256 && job.replacement_sha256 &&
    (['completed', 'unchanged'].includes(job.status) || (job.status === 'failed' && job.operation === 'revert')));
}
function agentFileCount(agent) {
  return Number.isInteger(agent?.file_count) && agent.file_count >= 0 ? agent.file_count : 0;
}
function agentMessageCount(agent) {
  return Number.isInteger(agent?.message_count) && agent.message_count >= 0 ? agent.message_count : 0;
}
function syncFilesButton(control, agent) {
  const count = agentFileCount(agent);
  control.classList.toggle('has-files', count > 0);
  control.title = agent.files_error || `${count} registered file${count === 1 ? '' : 's'}`;
  control.setAttribute('aria-label', `Files for ${agent?.name || 'agent'}`);
}
function syncMessagesButton(control, agent) {
  const count = agentMessageCount(agent);
  control.hidden = !agent?.managed;
  control.textContent = `✉ ${count}`;
  control.classList.toggle('has-messages', count > 0);
  control.title = agent?.messages_error || `${count} outstanding message${count === 1 ? '' : 's'}`;
  control.setAttribute('aria-label', `Messages for ${agent?.name || 'agent'}: ${count} outstanding`);
}
function openAgentFiles(id) {
  const agent = agentById(id); if (!agent) return;
  const workspace = activeSwarm === 'local' ? 'Local Swarm' : federationNodes.get(activeSwarm)?.name || activeSwarm;
  agentFilesDialog.open(agent, apiPath(`/api/agents/${encodeURIComponent(id)}/files`), workspace);
}
const tmuxMenuItems = [
  {heading: 'Layout'},
  {label: 'Split vertical · side-by-side', action: 'split-horizontal'},
  {label: 'Split horizontal · top/bottom', action: 'split-vertical'},
  {label: 'Reset layout (tiled)', action: 'reset-layout'},
  {label: 'Close current split', action: 'close-pane', title: 'Closes the selected split pane; the main agent pane is protected.'},
  {label: 'Zoom / restore pane', action: 'zoom-pane'},
  {heading: 'Focus pane'},
  {label: 'Focus left pane', action: 'pane-left'},
  {label: 'Focus right pane', action: 'pane-right'},
  {label: 'Focus pane above', action: 'pane-up'},
  {label: 'Focus pane below', action: 'pane-down'},
  {heading: 'Windows'},
  {label: 'Open terminal (new window)', action: 'new-window'},
  {label: 'Next window', action: 'next-window'},
  {label: 'Previous window', action: 'previous-window'},
];
function setTmuxMenuOpen(control, open) {
  control.menu.hidden = !open;
  control.toggle.setAttribute('aria-expanded', String(open));
}
function createTmuxControls(id) {
  const wrapper = node('div', 'terminal-tmux-control');
  const toggle = button('tmux', 'small terminal-tmux-button', () => {
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    for (const t of terminals.values()) if (t.tmuxControl && t.tmuxControl !== control) setTmuxMenuOpen(t.tmuxControl, false);
    setTmuxMenuOpen(control, open);
  });
  const menu = node('div', 'terminal-tmux-menu');
  menu.id = `tmux-menu-${id}`; menu.hidden = true; menu.setAttribute('role', 'group'); menu.setAttribute('aria-label', 'Tmux commands');
  toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-controls', menu.id);
  toggle.title = 'Pane and window commands for this agent tmux session';
  const control = {wrapper, toggle, menu, buttons: []};
  for (const command of tmuxMenuItems) {
    if (command.heading) { menu.append(node('div', 'terminal-tmux-heading', command.heading)); continue; }
    const item = button(command.label, 'terminal-tmux-item', async () => {
      if (item.disabled) return;
      item.disabled = true; item.dataset.busy = 'true';
      try {
        await api(`/api/agents/${encodeURIComponent(id)}/tmux`, 'POST', {action: command.action});
        toast(`${command.label} sent to tmux.`);
      } catch (error) {
        toast(error.status ? error.message : `Tmux action outcome is unknown; inspect the pane layout before retrying. ${error.message}`, true);
      } finally {
        delete item.dataset.busy;
        setTmuxMenuOpen(control, false);
        syncTmuxControls(control, agentById(id));
      }
    });
    item.title = command.title || command.label;
    menu.append(item); control.buttons.push(item);
  }
  wrapper.append(toggle, menu);
  return control;
}
function syncTmuxControls(control, agent) {
  const ready = !!agent?.managed && agent.alive === true && !agent.stop_pending && !agent.orchestration_stop && !restartInProgress(agent) && !compactionBlocked(agent);
  control.toggle.disabled = !ready;
  if (!ready) setTmuxMenuOpen(control, false);
  control.toggle.title = ready ? 'Pane and window commands for this agent tmux session' : 'Tmux controls require a live, managed agent that is not stopping or busy';
  for (const item of control.buttons) item.disabled = !ready || item.disabled && item.dataset.busy === 'true';
}
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('pointerdown', event => {
    for (const t of terminals.values()) if (t.tmuxControl && !t.tmuxControl.wrapper.contains(event.target)) setTmuxMenuOpen(t.tmuxControl, false);
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    for (const t of terminals.values()) if (t.tmuxControl?.toggle.getAttribute('aria-expanded') === 'true') {
      setTmuxMenuOpen(t.tmuxControl, false); t.tmuxControl.toggle.focus(); event.preventDefault();
    }
  });
}
function openAgentMessages(id) {
  const agent = agentById(id); if (!agent?.managed) return;
  const workspace = activeSwarm === 'local' ? 'Local Swarm' : federationNodes.get(activeSwarm)?.name || activeSwarm;
  agentMessagesController.open(agent, apiPath(`/api/agents/${encodeURIComponent(id)}/messages`), workspace);
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
function syncTerminalProduct(terminal, agent) {
  const link = productLink(agent);
  terminal.productSlot.textContent = '';
  terminal.productSlot.hidden = !link;
  if (!link) return;
  const label = typeof agent.product.label === 'string' && agent.product.label.trim() ? agent.product.label.trim() : 'Product';
  const characters = Array.from(label);
  const shortLabel = characters.length > 15 ? `${characters.slice(0, 14).join('')}…` : label;
  link.className = 'terminal-product';
  link.dataset.kind = agent.product.kind;
  link.textContent = `${agent.product.kind === 'file' ? '↓' : '↗'} ${shortLabel}`;
  terminal.productSlot.append(link);
}
function renderCard(a) {
  const card = node('article', 'agent-card'); card.dataset.agentId = a.id;
  const setupStage = (a.creation || incompleteSetup(a)) ? creationStage(a) : '';
  const setupComplete = a.creation?.state === 'completed' || a.spawn_state === 'ready';
  const activityHint = a.activity || 'No activity reported yet';
  const restartStage = restartInProgress(a) ? (a.restart.stage || a.restart.state || 'Restart in progress') : '';
  const stage = restartStage || setupStage;
  const setupHint = setupStage ? `\nSetup: ${setupStage}` : '';
  const restartHint = restartStage ? `\nRestart: ${restartStage}` : '';
  card.title = `${a.name}\n${displayBadge(a).text} · ${a.alive ? 'Process online' : 'Offline'}${setupHint}${restartHint}\nActivity: ${activityHint}`;
  card.classList.toggle('has-process', !!a.alive);
  card.classList.toggle('is-provisioning', provisioningAgent(a));
  const inside = node('div', 'card-inner'), summary = node('header', 'card-summary'), header = node('div', 'card-head');
  const identity = node('div', 'card-identity'), avatar = node('div', 'avatar', a.name.slice(0, 1).toUpperCase());
  const subtitle = node('div', 'card-subtitle', `${a.backend}${a.import_source ? ` ← ${a.import_source.backend}` : ''} · ${a.id.slice(0, 8)}${a.managed ? '' : ' · observed'}`);
  const name = node('h3', '', a.name); name.title = `${a.name}${setupHint}\nActivity: ${activityHint}`;
  identity.append(name, subtitle);
  const details = node('div', 'card-details'); details.id = `card-details-${a.id}`;
  let setupMessages = () => {};
  const toggle = button('', 'card-toggle', () => {
    const compact = !cardViews.isCompact(state.home, a);
    cardViews.setCompact(state.home, a.id, compact);
    SwarmCardViews.applyCardView({card, details, foot, avatar, subtitle, toggle}, compact, a.name);
    terminal.hidden = !compact;
    if (!compact) setupMessages();
  });
  toggle.setAttribute('aria-controls', `${details.id} card-actions-${a.id}`);
  const terminal = button('↗', 'card-terminal', () => openTerminal(a.id));
  terminal.title = `Open terminal for ${a.name}`;
  terminal.setAttribute('aria-label', terminal.title);
  terminal.hidden = true;
  const controls = node('div', 'card-controls'); controls.append(toggle, terminal);
  const statusBadge = badge(a); statusBadge.title = stage ? `${displayBadge(a).text}: ${stage}` : displayBadge(a).text;
  header.append(avatar, identity, statusBadge, controls);
  const goal = node('p', 'goal', a.goal); goal.title = a.goal;
  summary.append(header);
  if (provisioningAgent(a)) {
    const compactStage = node('p', 'creation-stage compact-creation-stage', stage);
    compactStage.title = stage; summary.append(compactStage);
  }
  const product = productLink(a);
  if (product) card.classList.toggle('has-product', true);
  const count = agentFileCount(a), messageCount = agentMessageCount(a);
  if (count || a.files_error || product || a.managed) {
    const library = node('div', 'card-product-files');
    if (product) library.append(product);
    if (count || a.files_error) {
      const files = button(a.files_error ? 'Files !' : `Files ${count}`, 'card-files', () => openAgentFiles(a.id));
      syncFilesButton(files, a);
      files.setAttribute('aria-label', `Files for ${a.name}`);
      library.append(files); card.classList.toggle('has-files', true);
    }
    if (a.managed) {
      const messages = button('', 'card-messages', () => openAgentMessages(a.id));
      syncMessagesButton(messages, a);
      library.append(messages); card.classList.toggle('has-messages', messageCount > 0);
    }
    summary.append(library);
  }
  const activity = node('div', 'activity'); activity.style.borderColor = `var(--${a.status})`;
  activity.id = `card-activity-${a.id}`;
  toggle.setAttribute('aria-describedby', activity.id);
  activity.append(node('span', 'activity-label', 'CURRENT ACTIVITY'), document.createTextNode(activityHint));
  details.append(goal);
  if (restartInProgress(a)) {
    const restart = node('section', 'restart-status');
    restart.append(node('span', 'activity-label', 'RESTART IN PROGRESS'), node('strong', 'restart-status-stage', restartStage));
    if (a.restart.error) restart.append(node('p', 'fleet-target-error', a.restart.error));
    details.append(restart);
  }
  if ((a.creation || incompleteSetup(a)) && !setupComplete) {
    const setup = node('section', `creation-status${['failed', 'cancelled'].includes(a.spawn_state) ? ' failed' : ''}`);
    setup.append(node('span', 'activity-label', 'SETUP'), node('strong', 'creation-status-stage', setupStage));
    if (a.creation?.error) setup.append(node('p', 'creation-error', a.creation.error));
    if (a.creation?.log) {
      const log = node('p', 'creation-log', 'Setup log: '); log.append(node('code', '', a.creation.log)); setup.append(log);
    }
    details.append(setup);
  }
  details.append(activity);
  const messagesSection = node('section', 'expanded-messages');
  setupMessages = () => agentMessagesController.mountExpanded(messagesSection, a, apiPath(`/api/agents/${encodeURIComponent(a.id)}/messages`));
  details.append(messagesSection);
  if (a.activities?.length) {
    const list = node('ul', 'activity-list');
    for (const item of a.activities) {
      const row = node('li'); row.append(node('span', item.status === 'done' ? 'running' : '', item.status === 'done' ? '✓' : item.status === 'running' ? '›' : '○'), document.createTextNode(item.title)); list.append(row);
    } details.append(list);
  }
  const postSetupText = a.wait_reason || (setupComplete ? setupStage : '');
  if (postSetupText) details.append(node('p', 'wait-reason', postSetupText));
  const meta = node('dl', 'metadata');
  metadata(meta, 'Folder', a.folder); metadata(meta, 'Repository', a.repository); metadata(meta, 'Worktree', a.worktree);
  metadata(meta, 'Branch', a.branch); metadata(meta, 'Session', a.session_id || 'Not persisted yet');
  if (a.parent_id) metadata(meta, 'Parent', agentById(a.parent_id)?.name || a.parent_id);
  metadata(meta, 'Process', a.pid ? `PID ${a.pid}` : 'Not running');
  metadata(meta, 'Wake at', a.wake_at ? new Date(a.wake_at).toLocaleString() : '');
  const delivery = a.wake_delivery;
  if (delivery && ['pending', 'delivered'].includes(delivery.state)) {
    metadata(meta, 'Wake delivery', delivery.state === 'pending'
      ? `${a.auto_resume ? 'Pending' : 'Paused'} · ${delivery.attempts} attempt(s)`
      : 'Handed to agent process');
    metadata(meta, 'Wake event', delivery.id);
    if (delivery.state === 'pending') metadata(meta, 'Wake retry', delivery.retry_at ? new Date(delivery.retry_at).toLocaleString() : 'Due');
  }
  metadata(meta, 'Restore', a.auto_resume ? 'Automatic' : 'Manual');
  if (a.import_source) {
    metadata(meta, 'Imported', `${a.import_source.backend} · ${a.import_source.session_id}`);
    metadata(meta, 'Source', a.import_source.session_file);
  }
  const metaDetails = node('details', 'agent-metadata');
  metaDetails.append(node('summary', '', 'Details'), meta);
  details.append(metaDetails);
  if (a.notes) { const notes = node('details', 'notes'); notes.append(node('summary', '', 'Checkpoint'), node('pre', '', a.notes)); details.append(notes); }
  if (a.last_error) details.append(node('div', 'card-error', a.last_error));
  const foot = node('div', 'card-foot'); foot.id = `card-actions-${a.id}`;
  const runtime = node('span', `runtime${a.alive ? ' online' : ''}`, `${a.alive ? '● Online' : '○ Offline'} · ${since(a.updated_at)}`); runtime.title = a.updated_at;
  const actions = node('div', 'actions'), compacting = compactionBlocked(a), setupBlocked = incompleteSetup(a), restarting = restartInProgress(a);
  const edit = button('Edit', 'small', () => editAgent(a.id)); edit.disabled = restarting; if (restarting) edit.title = 'Edit is disabled while restart is active';
  actions.append(button('Terminal ↗', 'small', () => openTerminal(a.id)), edit);
  if (a.managed) {
    const wake = button('Wake', 'small', () => wakeAgent(a.id)); wake.disabled = compacting || setupBlocked || restarting;
    if (restarting) wake.title = 'Restart is in progress';
    else if (setupBlocked) wake.title = provisioningAgent(a) ? 'Setup is still running' : 'Setup did not complete';
    else if (compacting) wake.title = toolOutputRepairRunning(a) || toolOutputRepairInFlight(a) ? 'Oversized tool-output repair is running' : metaCompactionRunning(a) ? 'Meta-compaction is running' : 'Tool input compaction is running';
    actions.append(wake);
    if (a.alive || a.auto_resume || provisioningAgent(a)) actions.append(button('Stop', 'small danger', () => stopAgent(a.id)));
  }
  foot.append(runtime, actions); inside.append(summary, details); card.append(inside, foot);
  const compact = cardViews.isCompact(state.home, a);
  SwarmCardViews.applyCardView({card, details, foot, avatar, subtitle, toggle}, compact, a.name);
  terminal.hidden = !compact;
  if (!compact) setupMessages();
  agentGroupsController?.decorateCard(card, a);
  return card;
}
const browserGroupingKey = () => `swarm.browser-groups.${activeSwarm}.${encodeURIComponent(state.home || '')}`;
function serverManagedGrouping() { return state.grouping_settings?.server_managed !== false; }
function serverManagedBranches() { return state.grouping_settings?.server_managed === true && state.grouping_settings?.initialized === true; }
function groupingRecordsValid() { return !Object.keys(state.errors||{}).some(key=>key.endsWith('.md')); }
function groupingReadOnlyError() { return state.federation_offline ? 'Swarm is offline. Grouping changes are disabled.' : state.agent_groups_error || state.grouping_settings_error || browserGroupingError; }
function syncGroupingStorage(savedView,previousMode) {
  browserGroupingError=null;
  const operation=++browserGroupingOperation;
  if (serverManagedGrouping()) {
    browserGroupingProjection=null;
    if (serverManagedBranches() && SwarmGroupingMode.validSettings(state.grouping_settings)) {
      groupedBranches.clear();for(const id of state.grouping_settings.grouped_branches)groupedBranches.add(id);
    }
  } else {
    browserGroups ||= new SwarmGroupingMode.BrowserGroups();
    const key=browserGroupingKey(),source=activeSwarm,generation=stateGeneration,seed=state.agent_groups,agents=state.agents;
    const options={recordsValid:groupingRecordsValid()};
    try {browserGroupingProjection=browserGroups.readSnapshot(key,seed,agents,options);}
    catch(error) {browserGroupingError=error.message;}
    let locksAvailable=true;
    try {browserGroups.locks();} catch(error) {locksAvailable=false;browserGroupingError=error.message;}
    if(locksAvailable)void (async()=>{
      try {
        await browserGroups.initializeLocked(key,seed,agents,options);
        const loaded=await browserGroups.loadLocked(key,seed,agents,options);
        if(operation!==browserGroupingOperation||source!==activeSwarm||generation!==stateGeneration||key!==browserGroupingKey()||serverManagedGrouping())return;
        if(browserGroupingProjection && loaded.revision<browserGroupingProjection.revision)return;
        browserGroupingProjection=loaded;browserGroupingError=null;render();
      } catch(error) {
        if(operation!==browserGroupingOperation||source!==activeSwarm||generation!==stateGeneration||key!==browserGroupingKey()||serverManagedGrouping())return;
        browserGroupingError=error.message;render();
      }
    })();
    if (!savedView && previousMode!==false) {
      groupedBranches.clear();for(const id of readUIViewState().groupedBranches)groupedBranches.add(id);
    }
  }
  groupingModeController?.setState(state.grouping_settings, state.federation_offline ? 'Owning Swarm is offline.' : state.grouping_settings_error);
}
async function mutateAgentGrouping(path,method,body) {
  if (serverManagedGrouping()) return api(path,method,body);
  if(groupingReadOnlyError())throw Object.assign(Error(groupingReadOnlyError()),{status:409});
  try {return await browserGroups.applyLocked(browserGroupingKey(),body,state.agents,agentGroupProjection(),{recordsValid:groupingRecordsValid()});}
  catch(error){error.status ||= 400;throw error;}
}

async function primeBrowserGroupingSnapshot(source,generation) {
  if(source!==activeSwarm||generation!==stateGeneration)throw Error('Browser grouping source is no longer current');
  if(serverManagedGrouping()) {
    if(state.grouping_settings?.initialized)throw Error('This server was already initialized.');
    const current=agentGroupProjection();
    return {seed:{groups:current.groups,assignments:Object.fromEntries(Object.entries(SwarmAgentGroups.effectiveAssignments(state.agents,current)).filter(([,group])=>group)),grouped_branches:[...groupedBranches].filter(id=>agentById(id))},revision:current.revision};
  }
  browserGroups ||= new SwarmGroupingMode.BrowserGroups();
  const key=browserGroupingKey(),seed=state.agent_groups,agents=state.agents,options={recordsValid:groupingRecordsValid()};
  let current;
  try {current=await browserGroups.loadLocked(key,seed,agents,options);}
  catch {current=browserGroups.readSnapshot(key,seed,agents,options);}
  if(source!==activeSwarm||generation!==stateGeneration||key!==browserGroupingKey()||serverManagedGrouping())throw Error('Browser grouping source is no longer current');
  browserGroupingProjection=current;
  return {seed:{groups:current.groups,assignments:current.assignments,grouped_branches:[...groupedBranches].filter(id=>agentById(id))},revision:current.revision};
}
async function changeBranchGrouping(id,grouped) {
  if(!serverManagedBranches()){rememberUIViewState();return;}
  const previous=state.grouping_settings?.grouped_branches || [],source=activeSwarm,generation=stateGeneration;
  if(groupingBranchBusy || groupingReadOnlyError() || !SwarmGroupingMode.validSettings(state.grouping_settings)){
    groupedBranches.clear();for(const value of previous)groupedBranches.add(value);render();
    toast('Wait for a current, available grouping state before editing branches.',true);return;
  }
  const operation=++groupingBranchOperation;groupingBranchBusy=true;
  const endpoint=source==='local'?'/api/grouping-settings':`/api/federation/${encodeURIComponent(source)}/proxy/api/grouping-settings`;
  let requestId,timer;
  try {
    requestId=SwarmAgentMessages.requestId();
    const request=localApi(endpoint,'POST',{action:'branches',revision:state.grouping_settings.revision,request_id:requestId,grouped_branches:[...groupedBranches]});
    const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('Grouping update timed out.')),20000);});
    const settings=await Promise.race([request,timeout]);
    if(source!==activeSwarm||generation!==stateGeneration||operation!==groupingBranchOperation)return;
    if(!SwarmGroupingMode.validSettings(settings))throw Error('Invalid grouping settings response.');
    if(settings.revision>=(state.grouping_settings?.revision||0)){
      state.grouping_settings=settings;state.agent_groups.revision=Math.max(state.agent_groups.revision,settings.revision);
      groupedBranches.clear();for(const value of settings.grouped_branches)groupedBranches.add(value);
      groupingModeController?.setState(settings);render();
    }
  } catch(error){
    if(source!==activeSwarm||generation!==stateGeneration||operation!==groupingBranchOperation)return;
    groupedBranches.clear();for(const value of state.grouping_settings?.grouped_branches || previous)groupedBranches.add(value);render();
    toast(error.status===409?'Grouping changed on the server. Refreshed before your next edit.':`Grouping update not confirmed. Do not retry blindly. Request ID ${requestId||'unavailable'}. ${error.message}`,true);
    void refresh();
  } finally {clearTimeout(timer);if(operation===groupingBranchOperation)groupingBranchBusy=false;}
}
const agentGroupTabId = id => `agent-group:${id}`;
function agentGroupProjection() { return !serverManagedGrouping() && browserGroupingProjection ? browserGroupingProjection : state.agent_groups || {revision:0, groups:[], assignments:{}}; }
function agentGroupFor(id) { return SwarmAgentGroups.effectiveAssignments(state.agents, agentGroupProjection())[id] || null; }
function isAgentGroupTab(id) { return typeof id === 'string' && id.startsWith('agent-group:') && (agentGroupProjection().groups.some(group => agentGroupTabId(group.id) === id) || (!!state.agent_groups_error && openedAgentGroups.has(id.slice('agent-group:'.length)))); }
function openAgentGroup(id, persist=true) {
  const group = agentGroupProjection().groups.find(group=>group.id===id);
  selectedAgentGroup = group?.id || null;
  if (group) openedAgentGroups.add(group.id);
  agentGroupsController?.clearSelection();
  activate(group ? agentGroupTabId(group.id) : 'dashboard', false);
  render();
  if (persist) rememberUIViewState();
}
function syncAgentGroupTabs() {
  const groups = new Map(agentGroupProjection().groups.map(group=>[group.id,group]));
  if(state.agent_groups_error) for(const id of openedAgentGroups) if(!groups.has(id)) groups.set(id,{id,name:'Group unavailable'});
  if (selectedAgentGroup && !groups.has(selectedAgentGroup) && !state.agent_groups_error) selectedAgentGroup=null;
  for (const [id,entry] of agentGroupTabs) if (!openedAgentGroups.has(id) || !groups.has(id)) { entry.wrapper.remove(); agentGroupTabs.delete(id); openedAgentGroups.delete(id); }
  for (const id of openedAgentGroups) {
    const group=groups.get(id); if (!group) continue;
    if (!agentGroupTabs.has(id)) {
      const wrapper=node('span','tab-group agent-group-tab'), tab=button('','tab',()=>openAgentGroup(id));
      const close=button('×','tab-close',()=>{openedAgentGroups.delete(id); if(selectedAgentGroup===id) openAgentGroup(null); else {syncAgentGroupTabs();rememberUIViewState();}});
      close.setAttribute('aria-label',`Close group dashboard`);wrapper.append(tab,close);wrapper.dataset.groupId=id;
      agentGroupTabs.set(id,{wrapper,tab});
    }
    const entry=agentGroupTabs.get(id);entry.tab.textContent=`▰ ${group.name}`;entry.tab.title=group.name;
    entry.wrapper.classList.toggle('active',activeTab===agentGroupTabId(id));
    entry.tab.setAttribute('aria-current',activeTab===agentGroupTabId(id)?'page':'false');
  }
  // Only the selected folder belongs in the tab strip; retained terminals stay alive.
  for (const [id,entry] of agentGroupTabs) if(id!==selectedAgentGroup) entry.wrapper.remove();
  const dashboard=$('#dashboard-tab'), browserTab=$('#browser-tab'), browserGroup=$('#browser-tab-group');
  const hasBrowserTab=browserTab?.id==='browser-tab', browserNode=browserGroup?.id==='browser-tab-group'?browserGroup:browserTab;
  if(selectedAgentGroup && agentGroupTabs.has(selectedAgentGroup))
    $('#tabs').insertBefore(agentGroupTabs.get(selectedAgentGroup).wrapper,dashboard.nextSibling);
  const assignments=SwarmAgentGroups.effectiveAssignments(state.agents,agentGroupProjection());
  for(const [id,t] of terminals) {t.group.hidden=(assignments[id]||null)!==selectedAgentGroup;$('#tabs').append(t.group);}
  if(hasBrowserTab && browserNode) {
    browserNode.hidden=!browserOpen || selectedAgentGroup!==null || !['dashboard','browser'].includes(activeTab);
    if(activeTab==='browser') $('#tabs').insertBefore(browserNode,dashboard.nextSibling);
    else $('#tabs').append(browserNode);
  }
}
function renderAgentFolders(matching, includeEmpty = true) {
  const container=$('#agent-group-folders');container.replaceChildren();
  if(selectedAgentGroup){container.hidden=true;return;}
  const assignments=SwarmAgentGroups.effectiveAssignments(state.agents,agentGroupProjection());
  for(const group of agentGroupProjection().groups){
    const members=state.agents.filter(agent=>assignments[agent.id]===group.id);
    if(matching && !members.some(agent=>matching.has(agent.id)) && (members.length || !includeEmpty)) continue;
    const folder=node('article','agent-group-folder');folder.dataset.groupId=group.id;
    const open=button('','agent-group-folder-open',()=>openAgentGroup(group.id));
    const icon=node('span','agent-group-folder-icon','▰');icon.setAttribute('aria-hidden','true');
    open.append(icon,node('strong','',group.name),node('span','hint',`${members.length} agent${members.length===1?'':'s'}`));
    open.setAttribute('aria-label',`Open group ${group.name}: ${members.length} agents`);
      folder.append(open);container.append(folder);
  }
  container.hidden=!container.childNodes.length;
}
function render() {
  if(typeof remoteBrowserController!=='undefined')remoteBrowserController?.setContext();
  if(typeof browserSettingsController!=='undefined')browserSettingsController?.setContext();
  agentLimitsController?.setState(state.agent_limits,state.federation_offline?'Owning Swarm is offline.':state.agent_limits_error);
  groupingModeController?.setState(state.grouping_settings,state.federation_offline?'Owning Swarm is offline.':state.grouping_settings_error);
  $('#home-path').textContent = state.home ? `${state.home}/agents` : '~/.swarm/agents';
  $('#agent-count').textContent = state.agents.length;
  const health = $('#health'); health.replaceChildren();
  for (const name of ['tmux', 'pi']) health.append(node('span', `health-pill${state.health[name] ? '' : ' bad'}`, `${state.health[name] ? '●' : '○'} ${name}`));
  agentGroupsController?.setState(state.agents, agentGroupProjection(), groupingReadOnlyError());
  const assignments = SwarmAgentGroups.effectiveAssignments(state.agents,agentGroupProjection());
  const groups=agentGroupProjection().groups;
  if(selectedAgentGroup && !groups.some(group=>group.id===selectedAgentGroup) && !state.agent_groups_error) selectedAgentGroup=null;
  if(isAgentGroupTab(activeTab)===false && activeTab.startsWith('agent-group:')) activate('dashboard',uiViewStateRestored);
  const scopedAgents=state.agents.filter(agent=>(assignments[agent.id]||null)===selectedAgentGroup);
  dashboardControls.update(scopedAgents);
  const groupName=groups.find(group=>group.id===selectedAgentGroup)?.name || (selectedAgentGroup && state.agent_groups_error ? 'Group unavailable' : null);
  groupNameMenu.update({id:selectedAgentGroup,name:groupName,disabled:!!groupingReadOnlyError(),context:`${activeSwarm}:${stateGeneration}`});
  $('#agent-groups-error').textContent=groupingReadOnlyError() || '';$('#agent-groups-error').hidden=!groupingReadOnlyError();
  const query = $('#search').value.trim().toLowerCase();
  const names=new Map(state.agents.map(agent=>[agent.id,agent.name]));
  const groupNames=new Map(groups.map(group=>[group.id,group.name]));
  const needle=SwarmAgentSearch.normalize(query);
  const matchesCard = new Set(state.agents.filter(agent=>dashboardControls.status.matches(agent.status) && (!needle || SwarmAgentSearch.cardText(agent,{parentName:names.get(agent.parent_id),groupName:groupNames.get(assignments[agent.id])}).includes(needle))).map(agent=>agent.id));
  const visible = scopedAgents.filter(agent=>matchesCard.has(agent.id));
  const active = document.activeElement;
  const controlClass = active?.classList.contains('card-toggle') ? 'card-toggle' : active?.classList.contains('card-terminal') ? 'card-terminal' : active?.classList.contains('group-family-select') ? 'group-family-select' : null;
  const focusedControl = active?.classList.contains('org-branch-toggle')
    ? {id: active.getAttribute('data-agent-id'), selector: '.org-branch-toggle'}
    : controlClass ? {id: active.closest('.agent-card')?.dataset.agentId, selector: `.${controlClass}`} : active?.classList.contains('agent-card') ? {id:active.dataset.agentId,selector:'.agent-card'} : null;
  renderAgentFolders(matchesCard, !needle && dashboardControls.status.all);
  const forest = SwarmOrgChart.buildForest(scopedAgents, new Set(visible.map(a => a.id)));
  const chart = $('#agents'), scrollLeft = chart.scrollLeft;
  chart.replaceChildren(SwarmOrgChart.renderForest(forest.roots, renderCard, document, {
    groupedIds: groupedBranches,
    onGroupChange: (id, grouped) => {
      if (grouped) groupedBranches.add(id);
      else groupedBranches.delete(id);
      void changeBranchGrouping(id,grouped);
    },
  }));
  chart.hidden = !forest.shownCount;
  chart.scrollLeft = scrollLeft;
  $('#hierarchy-summary').textContent = forest.ancestorCount
    ? `${forest.matchedCount} ${forest.matchedCount === 1 ? 'match' : 'matches'} · ${forest.ancestorCount} dimmed ancestor${forest.ancestorCount === 1 ? '' : 's'} kept for context`
    : `${forest.shownCount} agent${forest.shownCount === 1 ? '' : 's'} · ${forest.roots.length} root${forest.roots.length === 1 ? '' : 's'}`;
  if (forest.issuesCount) $('#hierarchy-summary').textContent += ` · ${forest.issuesCount} missing or invalid parent links`;
  $('#hierarchy-summary').classList.toggle('sr-only', !forest.ancestorCount && !forest.issuesCount);
  if (focusedControl) {
    const replacement = focusedControl.selector === '.org-branch-toggle'
      ? [...chart.querySelectorAll(focusedControl.selector)].find(control => control.getAttribute('data-agent-id') === focusedControl.id)
      : [...chart.querySelectorAll('.agent-card')].find(card => card.dataset.agentId === focusedControl.id);
    if (replacement) (['.org-branch-toggle','.agent-card'].includes(focusedControl.selector) ? replacement : $(focusedControl.selector, replacement))?.focus({preventScroll: true});
  }
  const empty = $('#empty'); empty.hidden = visible.length > 0 || !$('#agent-group-folders').hidden;
  $('h2', empty).textContent = state.agents.length ? 'No agents match this view.' : 'A little quiet in the swarm.';
  $('#empty-create').hidden = state.agents.length > 0;
  const errors = Object.entries(state.errors || {}); $('#registry-errors').hidden = !errors.length;
  $('#error-list').replaceChildren(...errors.map(([file, message]) => node('pre', '', `${file}: ${message}`)));
  for (const terminal of terminals.values()) syncTerminal(terminal);
  if (terminals.has(activeTab) && (assignments[activeTab]||null)!==selectedAgentGroup) activate(selectedAgentGroup?agentGroupTabId(selectedAgentGroup):'dashboard',uiViewStateRestored);
  syncAgentGroupTabs();
  if ($('#edit-dialog').open) updateEditAvailability();
  if ($('#wake-dialog').open) updateWakeAvailability();
  agentFilesDialog.sync(agentById(agentFilesDialog.agentId));
  agentMessagesController.sync(agentById(agentMessagesController.agentId));
}
function activate(id, persist = true) {
  const previousGroup=selectedAgentGroup;
  if (id && terminals.has(id)) {selectedAgentGroup=agentGroupFor(id);if(selectedAgentGroup)openedAgentGroups.add(selectedAgentGroup);}
  else if(isAgentGroupTab(id)) {selectedAgentGroup=id.slice('agent-group:'.length);openedAgentGroups.add(selectedAgentGroup);}
  else {selectedAgentGroup=null;}
  if(id==='browser')browserOpen=true;
  activeTab = id === 'browser' ? 'browser' : id && (terminals.has(id) || isAgentGroupTab(id)) ? id : 'dashboard';
  groupNameMenu.dropdown.close();
  const dashboardVisible=activeTab==='dashboard'||isAgentGroupTab(activeTab), browserVisible=activeTab==='browser';
  $('#dashboard').hidden = !dashboardVisible; $('#terminals').hidden = dashboardVisible || browserVisible;
  const browserPanel=$('#browser-panel'),browserTab=$('#browser-tab'),browserGroup=$('#browser-tab-group');
  if(browserPanel?.id==='browser-panel')browserPanel.hidden=!browserVisible;
  if(browserTab?.id==='browser-tab'){browserTab.classList.toggle('active',browserVisible);browserTab.setAttribute('aria-current',browserVisible?'page':'false');}
  if(browserGroup?.id==='browser-tab-group')browserGroup.classList.toggle('active',browserVisible);
  $('#dashboard-tab').classList.toggle('active', activeTab === 'dashboard');
  $('#dashboard-tab').setAttribute('aria-current', activeTab === 'dashboard' ? 'page' : 'false');
  if(browserVisible && typeof remoteBrowserController!=='undefined' && remoteBrowserController && !remoteBrowserController.active)
    void remoteBrowserController.activate();
  else if(!browserVisible && typeof remoteBrowserController!=='undefined')remoteBrowserController?.deactivate();
  for (const [key, t] of terminals) {
    t.pane.hidden = key !== activeTab; t.tab.classList.toggle('active', key === activeTab); t.group.classList.toggle('active', key === activeTab);
    t.tab.setAttribute('aria-current', key === activeTab ? 'page' : 'false');
    t.mobileKeys?.update();
    if (key === activeTab) { initTerminal(t); requestAnimationFrame(() => { t.fit?.fit(); if (!t.copyText?.active) t.term?.focus(); }); }
  }
  syncAgentGroupTabs();
  if(previousGroup!==selectedAgentGroup) render();
  if (persist) rememberUIViewState();
}
function openTerminal(id, focus = true) {
  if (!agentById(id)) return;
  if (!terminals.has(id)) {
    const pane = node('div', 'terminal-pane'); pane.hidden = true;
    const header = node('div', 'terminal-header');
    const title = node('strong'), info = node('span', 'hint terminal-info'), connected = node('span', 'hint terminal-connection', 'Detached');
    const copyButton = node('button', 'small terminal-copy-button', 'Copy text'); copyButton.type = 'button'; copyButton.hidden = true; copyButton.dataset.agentId = id;
    const filesButton = button('Files', 'small terminal-files', () => openAgentFiles(id));
    const productSlot = node('span', 'terminal-product-slot'); productSlot.hidden = true;
    const messagesButton = button('✉ 0', 'small terminal-messages', () => openAgentMessages(id));
    const tmuxControl = createTmuxControls(id);
    const reconnectButton = button('↻ Reconnect', 'small terminal-reconnect', () => { const t = terminals.get(id); disconnectTerminal(t); attachTerminal(t); });
    header.append(title, info, connected, copyButton, productSlot, filesButton, messagesButton, tmuxControl.wrapper, reconnectButton);
    const surface = node('div', 'terminal-surface'), screen = node('div', 'terminal-screen'), overlay = node('div', 'terminal-overlay');
    surface.append(screen, overlay); pane.append(header, surface); $('#terminals').append(pane);
    const group = node('span', 'tab-group'), tab = button('', 'tab', () => activate(id));
    const tabLabel = node('span', 'tab-label'); tab.append(node('span', '', '›_'), tabLabel);
    group.append(tab, button('×', 'tab-close', () => closeTerminal(id))); $('.tab-close', group).setAttribute('aria-label', 'Close terminal tab'); $('#tabs').append(group);
    const t = {id, pane, title, info, connected, screen, overlay, group, tab, tabLabel, copyButton, filesButton, messagesButton, productSlot, tmuxControl, reconnectButton, term: null, fit: null, ws: null, timer: null, closed: false};
    terminals.set(id, t); syncTerminal(t);
  }
  if (focus) {activate(id);render();} else {syncAgentGroupTabs();rememberUIViewState();}
}
function initTerminal(t) {
  if (t.term) return;
  if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') { toast('Terminal assets are missing. See README vendor instructions.', true); return; }
  t.term = new Terminal({cursorBlink: true, fontSize: 13, fontFamily: 'ui-monospace,Consolas,monospace', scrollback: 10000, theme: {background: '#0d1015', foreground: '#e8ebf1', cursor: '#ffa16a'}});
  t.fit = new FitAddon.FitAddon(); t.term.loadAddon(t.fit); t.term.open(t.screen); t.fit.fit();
  t.term.onData(data => {
    if (t.copyText?.active || t.ws?.readyState !== WebSocket.OPEN) return;
    const wheelData=t.wheelSpeed?.transform(data) ?? data;
    t.ws.send(JSON.stringify({type:'input',data:t.mobileKeys?.transform(wheelData) ?? wheelData}));
  });
  t.term.onResize(({cols, rows}) => { if (t.ws?.readyState === WebSocket.OPEN) t.ws.send(JSON.stringify({type: 'resize', cols, rows})); });
  t.wheelSpeed = new SwarmTerminalWheel.TerminalWheelMultiplier({
    element:t.screen,term:t.term,multiplier:()=>wheelPreferences.value,
    canScale:()=>!t.closed && !t.pane.hidden && !t.copyText?.active && !$('dialog[open]') && t.ws?.readyState===WebSocket.OPEN,
  });
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
    isActive: () => !t.closed && !t.pane.hidden && !t.copyText?.active && !$('dialog[open]') && terminalAttachable(agentById(t.id)),
    ready: () => t.ws?.readyState === WebSocket.OPEN,
    send: data => t.term.input(data, true),
    onKeyboardHidden: () => {
      if (t.closed || t.pane.hidden || t.copyText?.active || $('dialog[open]') || t.ws?.readyState !== WebSocket.OPEN || !terminalAttachable(agentById(t.id))) return;
      t.fit?.fit(); disconnectTerminal(t); attachTerminal(t);
    },
  });
  t.observer = new ResizeObserver(() => { if (!t.pane.hidden) t.fit?.fit(); }); t.observer.observe(t.screen);
  attachTerminal(t);
}
function overlay(t, title, message, actionText, action) {
  t.overlay.hidden = false; t.overlay.replaceChildren(node('h2', '', title), node('p', '', message));
  if (action) t.overlay.append(button(actionText, 'primary', action));
}
function terminalAttachable(a) {
  return !!a && (a.alive === true || (provisioningAgent(a) && a.creation?.state !== 'queued'));
}
function syncTerminal(t) {
  const a = agentById(t.id); if (!a) return;
  syncFilesButton(t.filesButton, a);
  syncTerminalProduct(t, a);
  syncMessagesButton(t.messagesButton, a);
  syncTmuxControls(t.tmuxControl, a);
  t.mobileKeys?.update();
  t.title.textContent = a.name; t.title.title = a.name; t.tabLabel.textContent = a.name;
  t.reconnectButton.setAttribute('aria-label', `Reconnect terminal for ${a.name}`);
  t.group.dataset.status = a.status;
  t.tab.title = `${a.name} · ${labels[a.status] || a.status}`;
  t.tab.setAttribute('aria-label', t.tab.title);
  const stage = incompleteSetup(a) ? creationStage(a) : '';
  t.info.textContent = `${stage || labels[a.status]} · ${a.session_id || 'session pending'} · ${a.folder}`;
  t.info.title = t.info.textContent;
  const compacting = compactionBlocked(a);
  if (provisioningAgent(a) && a.creation?.state === 'queued') {
    overlay(t, 'Setup is queued.', 'Setup has not started yet. This terminal will attach automatically when setup begins.');
  } else if (terminalAttachable(a)) {
    if (t.term && (!t.ws || t.ws.readyState > 1) && !t.timer) attachTerminal(t);
  } else if (['failed', 'cancelled'].includes(a.spawn_state)) {
    const message = [stage, a.creation?.error, a.creation?.log ? `Setup log: ${a.creation.log}` : ''].filter(Boolean).join('\n');
    overlay(t, a.spawn_state === 'cancelled' ? 'Setup was cancelled.' : 'Setup failed.', message || 'The agent setup process did not complete.');
  } else if (!a.alive) {
    const restarting = restartInProgress(a);
    overlay(t, restarting ? 'Restart in progress.' : compacting ? 'Session compaction is running.' : 'This agent is offline.', restarting ? (a.restart.stage || 'Waiting for the restart service. No wake is needed.') : compacting ? 'The saved session is being updated. It stays stopped afterward; wake it explicitly when the job completes.' : 'Opening a terminal only attaches to a process. Wake the agent explicitly to continue its saved session.', a.managed && !compacting && !restarting ? 'Wake agent ↗' : '', a.managed && !compacting && !restarting ? () => wakeAgent(t.id) : null);
  }
}
function attachTerminal(t) { void attachTerminalAuthenticated(t); }
async function attachTerminalAuthenticated(t) {
  if (t.closed || t.connecting || !t.term || t.ws?.readyState < 2) return;
  const agent = agentById(t.id);
  if (!terminalAttachable(agent)) { syncTerminal(t); return; }
  clearTimeout(t.timer); t.timer = null; t.connecting = true;
  const generation = stateGeneration;
  overlay(t, provisioningAgent(agent) ? 'Attaching setup terminal…' : 'Attaching terminal…', provisioningAgent(agent) ? 'Setup continues independently of this tab.' : 'The agent keeps running independently of this tab.');
  const path = `${terminalWSPath(t.id)}?cols=${t.term.cols}&rows=${t.term.rows}`;
  let url;
  try { url = await authenticatedSocketURL(path); }
  catch (error) {
    if (!t.closed && generation === stateGeneration) overlay(t, 'Sign in required.', error.message, 'Sign in / reconnect', () => attachTerminal(t));
    return;
  } finally { t.connecting = false; }
  if (t.closed || generation !== stateGeneration || !terminals.has(t.id)) return;
  const ws = new WebSocket(url); ws.binaryType = 'arraybuffer'; t.ws = ws;
  ws.onopen = () => { if (t.ws !== ws || t.closed || generation !== stateGeneration) { ws.close(); return; } t.overlay.hidden = true; t.connected.textContent = '● Attached'; t.term.reset(); t.fit.fit(); t.mobileKeys?.update(); ws.send(JSON.stringify({type: 'resize', cols: t.term.cols, rows: t.term.rows})); };
  ws.onmessage = event => { if (t.ws === ws && event.data instanceof ArrayBuffer) t.term.write(new Uint8Array(event.data)); };
  ws.onerror = () => ws.close();
  ws.onclose = (event = {}) => {
    if (t.closed || t.ws !== ws || generation !== stateGeneration) return;
    if (event.code === 4401 && browserAuth) { browserAuth.invalidate(path); setTimeout(connectState, 1000); return; }
    t.connected.textContent = '○ Detached'; t.mobileKeys?.update();
    if (!terminalAttachable(agentById(t.id))) { syncTerminal(t); return; }
    overlay(t, 'Terminal disconnected.', 'Reattaching in a few seconds. The agent or setup process is not stopped by a tab disconnect.', 'Reconnect', () => { disconnectTerminal(t); attachTerminal(t); });
    t.timer = setTimeout(() => { t.timer = null; attachTerminal(t); }, 4000);
  };
}
function disconnectTerminal(t) {
  clearTimeout(t.timer); t.timer = null;
  if (t.ws) { const ws = t.ws; t.ws = null; ws.onclose = null; ws.close(); }
  t.mobileKeys?.update();
}
function closeTerminal(id, persist = true) {
  const t = terminals.get(id); if (!t) return;
  t.closed = true; t.mobileKeys?.dispose(); t.copyText?.dispose(); t.touchScroll?.dispose(); t.wheelSpeed?.dispose(); disconnectTerminal(t); t.observer?.disconnect(); t.term?.dispose(); t.pane.remove(); t.group.remove(); terminals.delete(id);
  // Deliberately no stop API call: only the attachment is disposable.
  if (activeTab === id) {activate(selectedAgentGroup ? agentGroupTabId(selectedAgentGroup) : 'dashboard', persist);render();}
  else if (persist) rememberUIViewState();
}
function showDialog(id) { const dlg = $(id); $('.form-error', dlg).textContent = ''; dlg.showModal(); }
function editAgent(id) {
  const a = agentById(id); if (!a) return;
  if (restartInProgress(a)) { toast('Wait for the fleet restart to finish before editing this agent.', true); return; }
  editId = id;
  const form = $('#edit-form'); form.elements.name.value = a.name; form.elements.goal.value = a.goal; form.elements.status.value = a.status; form.elements.auto_resume.checked = a.auto_resume;
  form.elements.meta_model.value = ''; form.elements.overlap_tokens.value = '10000'; form.elements.chunk_count.value = '2'; $('#meta-compact-controls').hidden = true; $('#meta-compact-toggle').textContent = 'Meta-compact…';
  $('#revert-allow-history-loss').checked = false;
  updateEditAvailability(); showDialog('#edit-dialog');
}
function updateForgetAvailability() {
  const a = agentById(editId);
  const active = a?.alive && a.status === 'running', compacting = compactionBlocked(a);
  const restarting = restartInProgress(a);
  $('#forget-agent').disabled = forgettingAgent || !a || active || compacting || restarting;
  $('#forget-explanation').textContent = !a ? 'This agent is no longer registered.' : restarting
    ? 'Forget is disabled while fleet restart is active. Stop remains available on the card.' : compacting
    ? 'Forget is disabled while session compaction is running.'
    : active ? 'This agent is actively running. Stop it before forgetting it.'
    : 'Forget stops any remaining Swarm-owned process and removes the card. Saved session history stays in its current location.';
}
function updateEditAvailability() {
  updateForgetAvailability();
  const a = agentById(editId), job = a?.meta_compaction, toolJob = a?.tool_input_compaction, outputJob = a?.tool_output_repair;
  const running = metaCompactionRunning(a), toolRunning = toolInputCompactionRunning(a), outputRunning = toolOutputRepairRunning(a);
  const starting = metaCompactingId === editId, toolStarting = toolInputCompactingId === editId, outputStarting = toolOutputRepairInFlight(a);
  const restarting = restartInProgress(a);
  const historyRunning=historyArchiveRunning(a),historyStarting=historyArchiveInFlight(a);
  const blocked = running || toolRunning || outputRunning || starting || toolStarting || outputStarting || historyRunning || historyStarting || restarting;
  const eligible = !!a && a.managed === true && a.backend === 'pi' && !!a.session_file && !a.alive && !outputRepairStopping(a) && !incompleteSetup(a);
  let availability = 'Available while this managed Pi agent is stopped.';
  if (!a) availability = 'This agent is no longer registered.';
  else if (restarting) availability = 'Fleet restart is active. Editing and compaction are disabled.';
  else if (historyRunning || historyStarting) availability = 'Archiving older context. It will remain stopped when complete.';
  else if (running) availability = 'Compacting the saved session. It will remain stopped when complete.';
  else if (toolRunning || toolStarting) availability = 'Tool input compaction is running. It will remain stopped when complete.';
  else if (outputRunning || outputStarting) availability = 'Oversized tool-output repair is running. It will remain stopped when complete.';
  else if (!a.managed || a.backend !== 'pi') availability = 'Only managed Pi agents can be meta-compacted.';
  else if (!a.session_file) availability = 'A persisted Pi session is required.';
  else if (a.alive) availability = 'Stop this agent before meta-compacting its session.';
  $('#meta-compact-availability').textContent = availability;
  for (const field of ['name', 'goal', 'status', 'auto_resume']) $('#edit-form').elements[field].disabled = restarting;
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
  else if (restarting) toolAvailability = 'Fleet restart is active. Editing and compaction are disabled.';
  else if(historyRunning || historyStarting)toolAvailability='Older-context archival is running. Wait for it to finish.';
  else if (running || starting) toolAvailability = 'Meta-compaction is running. Wait for it to finish.';
  else if (outputRunning || outputStarting) toolAvailability = 'Oversized tool-output repair is running. Wait for it to finish.';
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
  historyArchiveController?.update(a);
  const outputReason = outputRepairUnavailableReason(a);
  $('#tool-output-repair-availability').textContent = outputReason || 'Available while this managed Pi agent is stopped.';
  $('#repair-tool-outputs').disabled = !!outputReason;
  $('#repair-tool-outputs').title = outputReason || 'Repair oversized tool outputs in this stopped session';
  const outputStatus = $('#tool-output-repair-status'); outputStatus.hidden = !outputJob;
  if (outputJob) {
    $('#tool-output-repair-state').textContent = outputJob.status || 'unknown';
    const hasOutputResult = ['completed', 'unchanged'].includes(outputJob.status) || outputJob.outputs_repaired != null;
    $('#tool-output-repair-result').hidden = !hasOutputResult;
    $('#tool-output-repair-count').textContent = String(outputJob.outputs_repaired ?? 0);
    $('#tool-output-repair-original-bytes').textContent = String(outputJob.original_bytes ?? 0);
    $('#tool-output-repair-preview-bytes').textContent = String(outputJob.preview_bytes ?? 0);
    $('#tool-output-repair-error').hidden = !outputJob.error; $('#tool-output-repair-error').textContent = outputJob.error || '';
    $('#tool-output-repair-archive-row').hidden = !outputJob.archive; $('#tool-output-repair-archive').textContent = outputJob.archive || '';
    $('#tool-output-repair-directory-row').hidden = !outputJob.directory; $('#tool-output-repair-directory').textContent = outputJob.directory || '';
  }
}
async function startMetaCompaction() {
  const id = editId, a = agentById(id), form = $('#edit-form');
  if (!a || metaCompactingId || toolInputCompactingId || historyArchiveInFlight(a) || compactionRunning(a) || (typeof toolOutputRepairRequests !== 'undefined' && toolOutputRepairInFlight(a)) || !a.managed || a.backend !== 'pi' || !a.session_file || a.alive) { updateEditAvailability(); return; }
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
  if (!a || metaCompactingId || toolInputCompactingId || historyArchiveInFlight(a) || compactionRunning(a) || (typeof toolOutputRepairRequests !== 'undefined' && toolOutputRepairInFlight(a)) || !a.managed || a.backend !== 'pi' || !a.session_file || a.alive) { updateEditAvailability(); return; }
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
  if (!a || metaCompactingId || toolInputCompactingId || historyArchiveInFlight(a) || compactionRunning(a) || (typeof toolOutputRepairRequests !== 'undefined' && toolOutputRepairInFlight(a)) || !canRevertToolInputs(a) || !a.managed || a.backend !== 'pi' || !a.session_file || a.alive) { updateEditAvailability(); return; }
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
function outputRepairSummary(report) {
  if (report.status === 'failed') return report.error || 'Oversized tool-output repair failed.';
  if (report.status === 'unchanged') return 'No oversized tool outputs needed repair.';
  const count = Number.isInteger(report.outputs_repaired) ? report.outputs_repaired : 0;
  const original = Number.isFinite(report.original_bytes) ? report.original_bytes : 0;
  const preview = Number.isFinite(report.preview_bytes) ? report.preview_bytes : 0;
  return `Repaired ${count} oversized tool output${count === 1 ? '' : 's'} (${original} original bytes → ${preview} preview bytes).`;
}
async function repairToolOutputs(agentId = editId) {
  const id = agentId, a = agentById(id), reason = outputRepairUnavailableReason(a);
  if (reason) { if (editId === id && $('#edit-dialog').open) updateEditAvailability(); return; }
  const message = `Repair oversized tool outputs for ${a.name}?\n\nThis addresses Copilot's single tool-output string limit (10,485,760 characters). The full original session is archived. Active output text larger than 1 MiB is elided to an approximately 32 KiB preview with lookup references. No model calls are made. Incomplete or raw history is retained, and images and tool inputs are unchanged.\n\nThe original remains archived and the agent stays stopped until you manually Wake it. This is not overall conversation compaction; other context overflow errors may still need Meta-compact.\n\nSession:\n${a.session_file}`;
  if (!confirm(message)) return;
  const context = {generation: stateGeneration, swarm: activeSwarm};
  const requestPath = apiPath(`/api/agents/${encodeURIComponent(id)}/repair-tool-outputs`);
  const request = {...context, path: requestPath};
  toolOutputRepairRequests.set(id, request);
  if (editId === id && $('#edit-dialog').open) $('.form-error', $('#edit-form')).textContent = '';
  render();
  try {
    const report = await localApi(requestPath, 'POST', {});
    if (context.generation !== stateGeneration || context.swarm !== activeSwarm) return;
    const current = agentById(id); if (current) current.tool_output_repair = report;
    toast(outputRepairSummary(report), report.status === 'failed' || !!report.error);
    render();
    await refresh();
  } catch (error) {
    if (context.generation !== stateGeneration || context.swarm !== activeSwarm) return;
    let message = error.message;
    if (error.status === 404) message = 'The selected controller does not support oversized tool-output repair yet. Update that controller and try again.';
    else if (!error.status && !/outcome unknown/i.test(message)) message = `${message} Request outcome unknown; check the persisted repair report before retrying.`;
    if (editId === id && $('#edit-dialog').open) $('.form-error', $('#edit-form')).textContent = message;
    else toast(message, true);
  } finally {
    if (toolOutputRepairRequests.get(id) === request) toolOutputRepairRequests.delete(id);
    if (context.generation === stateGeneration && context.swarm === activeSwarm) render();
  }
}
async function forgetAgent() {
  const id = editId, a = agentById(id);
  if (!a || forgettingAgent) return;
  if ((a.alive && a.status === 'running') || compactionBlocked(a) || restartInProgress(a)) { updateForgetAvailability(); return; }
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
$('#repair-tool-outputs').addEventListener('click', () => repairToolOutputs(editId));
$('#compact-tool-inputs').addEventListener('click', compactToolInputs);
$('#revert-tool-input-compaction').addEventListener('click', revertToolInputs);
$('#meta-compact-toggle').addEventListener('click', () => {
  const controls = $('#meta-compact-controls'); controls.hidden = !controls.hidden;
  $('#meta-compact-toggle').textContent = controls.hidden ? 'Meta-compact…' : 'Hide options';
  updateEditAvailability();
});
$('#start-meta-compact').addEventListener('click', startMetaCompaction);
function updateWakeAvailability() {
  const a = agentById(wakeId), blocked = compactionBlocked(a) || incompleteSetup(a) || restartInProgress(a);
  $('[type=submit]', $('#wake-form')).disabled = blocked;
}
function wakeAgent(id) {
  const a = agentById(id); if (!a) return;
  if (incompleteSetup(a)) { toast(provisioningAgent(a) ? 'Wait for setup to finish before waking this agent.' : 'This agent cannot wake because setup did not complete.', true); return; }
  if (restartInProgress(a)) { toast('Restart is already in progress; no Wake is needed.', true); return; }
  if (compactionBlocked(a)) { toast('Wait for session compaction to finish before waking this agent.', true); return; }
  wakeId = id; $('#wake-form').reset(); $('#wake-title').textContent = `Wake ${a.name || 'agent'}`; updateWakeAvailability(); showDialog('#wake-dialog');
}
async function stopAgent(id) {
  const agent = agentById(id), restartNote = restartInProgress(agent) ? ' The active fleet restart is cancelled for this agent.' : '';
  if (!confirm(`Stop ${agent?.name}? This stops the process and disables automatic restore.${restartNote} Its goal and session are retained.`)) return;
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
    // Capture the selected workspace before awaiting: a late remote response must never mutate a newly selected Swarm.
    const requestGeneration = stateGeneration, requestSwarm = activeSwarm;
    const agent = await api('/api/agents', 'POST', data);
    if (requestGeneration !== stateGeneration || requestSwarm !== activeSwarm) return;
    $('#create-dialog').close(); form.reset(); dashboardControls.status.reset(); $('#search').value = ''; activate('dashboard');
    const current = agentById(agent.id);
    const agents = state.agents.filter(existing => existing.id !== agent.id); agents.push(current || agent);
    applyState({...state, agents});
    toast('Agent created; setup is running in its terminal.');
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
$('#import-grok-button').addEventListener('click', () => { dashboardControls.closeAddMenus(); $('#create-dialog')?.close(); $('#import-grok-form').reset(); $('#grok-session-search').value = ''; $('#grok-roots').textContent = ''; showDialog('#import-grok-dialog'); loadGrokSessions(); });
$('#refresh-grok-sessions').addEventListener('click', loadGrokSessions);
$('#grok-session-search').addEventListener('input', renderGrokSessions);
const fleetControl = new SwarmFleetControl.FleetControl({
  apiPath,
  api: localApi,
  currentSwarm: () => activeSwarm,
  currentGeneration: () => stateGeneration,
  canMutate: () => activeSwarm === 'local' || !!federationNodes.get(activeSwarm)?.capabilities?.remote_restart,
  onChange: refresh,
});
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
const agentMessagesController = new SwarmAgentMessages.MessagesController({api: localApi, onChange: refresh});
const dashboardControls = new SwarmDashboardControls.DashboardControls({onStatusChange: render});
const groupNameMenu = new SwarmDashboardControls.GroupNameMenu({
  onRename:id=>agentGroupsController?.renameGroup(id),onDelete:id=>agentGroupsController?.deleteGroup(id),
});
const relaySettingsDialog = new SwarmRelaySettings.RelaySettingsDialog({
  api: localApi,
  onOpen:()=>{wheelSettings.open();updateRestartCapability();if($('#browser-settings-section')?.open)void browserSettingsController?.openSettings();},
  onChange: async value => {
    if (!value.enabled && activeSwarm !== 'local') switchSwarm('local');
    await loadFederation();
  },
});
for (const el of document.querySelectorAll('[data-close]')) el.addEventListener('click', () => el.closest('dialog').close());
const createDialog = () => {
  dashboardControls.closeAddMenus();
  const form = $('#create-form'); form.reset(); loadAgentFolderOptions(); updateWorktreeCreateFields();
  $('#model-selection-hint').textContent = 'Reading models from the server Pi configuration…';
  showDialog('#create-dialog'); loadModelOptions();
};
$('#create-button').addEventListener('click', createDialog); $('#empty-create').addEventListener('click', createDialog);
$('#open-browser-button').addEventListener('click', () => { dashboardControls.closeAddMenus(); agentGroupsController?.clearSelection(); activate('browser'); render(); });
$('#adopt-button').addEventListener('click', () => { dashboardControls.closeAddMenus(); $('#create-dialog')?.close(); $('#adopt-form').reset(); $('#session-search').value = ''; showDialog('#adopt-dialog'); loadSessions(); });
$('#refresh-sessions').addEventListener('click', loadSessions); $('#session-search').addEventListener('input', renderSessions);
$('#dashboard-tab').addEventListener('click', () => {agentGroupsController?.clearSelection();activate('dashboard');});
$('#browser-tab')?.addEventListener('click', () => {agentGroupsController?.clearSelection();activate('browser');});
$('#browser-tab-close')?.addEventListener('click', () => {
  browserOpen=false;
  remoteBrowserController?.deactivate();
  if(activeTab==='browser')activate('dashboard',false);else syncAgentGroupTabs();
  rememberUIViewState();
});
$('#browser-settings-section')?.addEventListener('toggle', event => {if(event.currentTarget.open)void browserSettingsController?.openSettings();});
$('#search').addEventListener('input', render);
document.addEventListener('keydown', event => { if (event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName) && (activeTab === 'dashboard' || isAgentGroupTab(activeTab)) && !document.querySelector('dialog[open]')) { event.preventDefault(); dashboardControls.status.dropdown.open(); $('#search').focus(); } });
function clearUnauthorizedWorkspace(nodeId) {
  dashboardControls?.closeAddMenus?.();
  notificationsInbox?.clear();
  if(typeof remoteBrowserController!=='undefined')remoteBrowserController?.deactivate();
  pendingNotificationReveal = null;
  if (nodeId !== 'local' && nodeId !== activeSwarm) return;
  if(uiViewStateRestored) rememberUIViewState();
  for (const id of [...terminals.keys()]) closeTerminal(id, false);
  for (const dialog of document.querySelectorAll('dialog[open]')) if (dialog.id !== 'auth-dialog') dialog.close();
  if (stateSocket) { stateSocket.onclose = null; stateSocket.close(); stateSocket = null; }
  if (nodeId === 'local' && presenceSocket) { presenceSocket.onclose = null; presenceSocket.close(); presenceSocket = null; }
  stateGeneration += 1; groupingBranchOperation+=1;browserGroupingOperation+=1;groupingBranchBusy=false;browserGroupingProjection=null;browserGroupingError=null; uiViewStateRestored = false; groupedBranches.clear(); state = {agents: [], errors: {}, health: {}, home: ''};
  dashboardControls.status.reset(); $('#search').value = ''; activate('dashboard', false); render();
  $('#connection').textContent = 'Sign in required'; $('#connection').className = 'connection offline';
}
notificationsInbox = new SwarmNotifications.NotificationsInbox({
  fetch: authenticatedFetch,
  onOpenAgent: revealNotificationAgent,
});
updateNotificationSources(false);
notificationsInbox.start();
agentGroupsController = new SwarmAgentGroups.AgentGroupsController({
  api:mutateAgentGrouping, currentSwarm:()=>activeSwarm,currentGeneration:()=>stateGeneration,toast:message=>toast(message,true),
  onOpenGroup:id=>openAgentGroup(id),
  onChanged:async(projection,options)=>{
    if(options?.refresh){await refresh();return;}
    if(projection && projection.revision >= (agentGroupProjection()?.revision||0)){
      if(serverManagedGrouping()){state.agent_groups=projection;if(state.grouping_settings)state.grouping_settings.revision=projection.revision;}
      else {browserGroupingOperation+=1;browserGroupingProjection=projection;}
      state.agent_groups_error=null;agentGroupsController.clearSelection();render();rememberUIViewState();
    }
  },
});
groupingModeController = new SwarmGroupingMode.GroupingModeController({
  api:localApi,currentSwarm:()=>activeSwarm,currentGeneration:()=>stateGeneration,
  ownerName:source=>source==='local'?'Local Swarm':federationNodes.get(source)?.name||source,
  projection:()=>agentGroupProjection(),branches:()=>[...groupedBranches].filter(id=>agentById(id)),primeSnapshot:primeBrowserGroupingSnapshot,onError:message=>toast(message,true),
  onChanged:async(settings,metadata)=>{
    if(metadata.source!==activeSwarm||metadata.generation!==stateGeneration)return;
    if(metadata.refreshBrowser){
      browserGroupingOperation+=1;
      try {await primeBrowserGroupingSnapshot(metadata.source,metadata.generation);browserGroupingError=null;render();}
      catch(error){browserGroupingError=error.message;render();}
      return;
    }
    await refresh();
  },
});
agentLimitsController = new SwarmAgentLimits.AgentLimitsController({
  api:localApi,currentSwarm:()=>activeSwarm,currentGeneration:()=>stateGeneration,
  ownerName:source=>source==='local'?'Local Swarm':federationNodes.get(source)?.name||source,
  onError:message=>toast(message,true),
  onChanged:async(projection,metadata)=>{
    if(metadata.source!==activeSwarm||metadata.generation!==stateGeneration)return;
    if(projection && (!state.agent_limits || projection.revision>=state.agent_limits.revision))state.agent_limits=projection;
    await refresh();
  },
});
historyArchiveController = new SwarmHistoryArchive.HistoryArchiveController({
  api:localApi,currentSwarm:()=>activeSwarm,currentGeneration:()=>stateGeneration,agent:()=>agentById(editId),toast,
  additionalReason:agent=>agent && (metaCompactingId===agent.id || toolInputCompactingId===agent.id || toolOutputRepairInFlight(agent)) ? 'Another session maintenance request is in flight. Wait for its persisted outcome.' : '',
  onBusyChange:()=>render(),
  onChanged:async(report)=>{const agent=state.agents.find(agent=>agent.session_file===report.source&&(!agent.session_id||agent.session_id===report.session_id));if(agent)agent.context_archive=report;render();await refresh();},
});
const browserOwnerName = source => source === 'local' ? 'Local Swarm' : federationNodes.get(source)?.name || source;
if (globalThis.SwarmRemoteBrowser) {
  remoteBrowserController = new SwarmRemoteBrowser.RemoteBrowserController({
    request: browserRequest, currentSwarm: () => activeSwarm, currentGeneration: () => stateGeneration,
    ownerName: browserOwnerName, toast,
    onChanged: async status => browserSettingsController?.setContext(status),
  });
  browserSettingsController = new SwarmRemoteBrowser.BrowserSettingsController({
    request: browserRequest, currentSwarm: () => activeSwarm, currentGeneration: () => stateGeneration,
    ownerName: browserOwnerName, toast,
    onChanged: async (registry,context) => {
      if(context.source!==activeSwarm||context.generation!==stateGeneration)return;
      remoteBrowserController?.updateInstances(registry);
    },
  });
}
const agentBroadcast = new SwarmAgentBroadcast.BroadcastController({
  api: localApi,
  currentSwarm: () => activeSwarm,
  currentGeneration: () => stateGeneration,
  sources: notificationSources,
  onChange: () => { void refresh(); },
});
globalThis.addEventListener?.('storage',event=>{
  if(event.key==='swarm.terminal-wheel-multiplier'||event.key===null)wheelSettings.reload();
  if(!uiViewStateRestored || serverManagedGrouping())return;
  if(event.key===browserGroupingKey() || event.key===null){syncGroupingStorage(null,false);render();}
  else if(event.key===uiViewStateStorageKey()){
    groupedBranches.clear();for(const id of readUIViewState().groupedBranches)if(agentById(id))groupedBranches.add(id);render();
  }
});
globalThis.addEventListener?.('swarm-auth-unauthorized', event => clearUnauthorizedWorkspace(event.detail?.node || 'local'));
globalThis.addEventListener?.('swarm-auth-retry', () => { notificationsInbox.clear(); updateNotificationSources(); loadFederation(); refresh(); connectState(); });
globalThis.addEventListener?.('beforeunload', () => remoteBrowserController?.dispose());
loadFederation(); refresh(); connectState();
