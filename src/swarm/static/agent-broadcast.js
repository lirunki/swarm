/* Durable multi-agent message broadcast UI. All remote text is rendered as text. */
(function (root) {
  'use strict';

  const messageStatuses = new Set(['pending', 'delivering', 'executing', 'failed', 'uncertain']);
  const identity = (sourceId, agentId) => `${sourceId}\u0000${agentId}`;
  const statePath = sourceId => sourceId === 'local'
    ? '/api/state'
    : `/api/federation/${encodeURIComponent(sourceId)}/proxy/api/state`;
  const messagesPath = (sourceId, agentId) => sourceId === 'local'
    ? `/api/agents/${encodeURIComponent(agentId)}/messages`
    : `/api/federation/${encodeURIComponent(sourceId)}/proxy/api/agents/${encodeURIComponent(agentId)}/messages`;

  function defaultCreate(tag, className = '', text = '') {
    const node = root.document.createElement(tag); node.className = className; node.textContent = text; return node;
  }

  function fallbackRequestId() {
    if (root.crypto?.randomUUID) return root.crypto.randomUUID();
    const bytes = new Uint8Array(16); root.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40; bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function requestId() {
    const shared = root.SwarmAgentMessages?.requestId;
    return typeof shared === 'function' ? shared() : fallbackRequestId();
  }

  function validAck(value, target, text) {
    return value?.agent_id === target.agentId && value.message && value.message.id === target.requestId &&
      value.message.text === text && typeof value.message.status === 'string' && messageStatuses.has(value.message.status);
  }

  function normalizeSources(values) {
    const unique = new Map();
    for (const source of values || []) {
      if (!source || typeof source.id !== 'string' || !source.id || source.online === false) continue;
      unique.set(source.id, {id: source.id, name: typeof source.name === 'string' && source.name ? source.name : source.id});
    }
    return [...unique.values()];
  }

  class BroadcastController {
    constructor({query = selector => root.document.querySelector(selector), api, currentSwarm = () => 'local',
      currentGeneration = () => 0, sources = () => [{id: 'local', name: 'Local Swarm'}], onChange = async () => {},
      confirm = text => root.confirm(text), create = defaultCreate, requestTimeout = 20000,
      setTimer = (callback, delay) => root.setTimeout(callback, delay), clearTimer = timer => root.clearTimeout(timer)} = {}) {
      Object.assign(this, {query, api, currentSwarm, currentGeneration, sources, onChange, confirm, create,
        requestTimeout, setTimer, clearTimer});
      this.dialog = query('#broadcast-agents-dialog');
      this.notificationDialog = query('#notifications-dialog');
      this.textarea = query('#notification-broadcast-text');
      this.status = query('#notification-broadcast-status');
      this.scopeControl = query('#broadcast-scope'); this.awakeControl = query('#broadcast-awake'); this.searchControl = query('#broadcast-search');
      this.targetsNode = query('#broadcast-targets'); this.summaryNode = query('#broadcast-summary'); this.errorsNode = query('#broadcast-errors');
      this.sendButton = query('#broadcast-send'); this.resultsNode = query('#broadcast-results'); this.refreshButton = query('#broadcast-refresh');
      this.closeButton = query('#broadcast-close'); this.cancelButton = query('#broadcast-cancel');
      this.session = 0; this.context = null; this.targets = []; this.rows = new Map(); this.checked = new Map(); this.results = [];
      this.loading = false; this.sending = false; this.message = ''; this.failures = [];

      query('#notification-broadcast-open')?.addEventListener('click', () => void this.open());
      this.scopeControl?.addEventListener('change', () => this.renderTargets());
      this.awakeControl?.addEventListener('change', () => this.renderTargets());
      this.searchControl?.addEventListener('input', () => this.renderTargets());
      query('#broadcast-select-all')?.addEventListener('click', () => this.selectShown(true));
      query('#broadcast-select-none')?.addEventListener('click', () => this.selectShown(false));
      this.refreshButton?.addEventListener('click', () => void this.refresh());
      this.sendButton?.addEventListener('click', () => void this.send());
      this.closeButton?.addEventListener('click', () => this.close());
      this.cancelButton?.addEventListener('click', () => this.close());
      this.dialog?.addEventListener('cancel', event => { event.preventDefault(); if (!this.sending) this.close(); });
      this.dialog?.addEventListener('close', () => this.closed());
    }

    setStatus(text) { if (this.status) this.status.textContent = text; }
    setErrors(text) { if (this.errorsNode) this.errorsNode.textContent = text; }
    contextCurrent(context = this.context) {
      return !!context && context.swarm === this.currentSwarm() && context.generation === this.currentGeneration();
    }
    active(session = this.session) { return session === this.session && !!this.dialog?.open && this.contextCurrent(); }

    request(...args) {
      return new Promise((resolve, reject) => {
        let settled = false, timer;
        const finish = (callback, value) => {
          if (settled) return;
          settled = true; if (timer !== undefined) this.clearTimer(timer); callback(value);
        };
        timer = this.setTimer(() => finish(reject, Error(`Request timed out after ${this.requestTimeout}ms`)), this.requestTimeout);
        Promise.resolve().then(() => this.api(...args)).then(
          value => finish(resolve, value), error => finish(reject, error));
      });
    }

    async open() {
      if (this.sending) return;
      const text = String(this.textarea?.value || '').trim();
      if (!text || text.length > 32000) {
        this.setStatus(text.length > 32000 ? 'Message must be at most 32,000 characters.' : 'Enter a message before selecting agents.');
        this.textarea?.reportValidity?.(); return;
      }
      const capturedSources = normalizeSources(this.sources());
      const swarm = this.currentSwarm(), current = capturedSources.find(source => source.id === swarm);
      this.currentUnavailable = !current;
      this.session += 1;
      this.context = {swarm, generation: this.currentGeneration(), sources: capturedSources};
      this.loading = false;
      this.message = text; this.targets = []; this.rows.clear(); this.checked.clear(); this.failures = [];
      if (this.scopeControl) this.scopeControl.value = 'current';
      if (this.awakeControl) this.awakeControl.value = 'all';
      if (this.searchControl) this.searchControl.value = '';
      this.setStatus(''); this.setErrors('');
      if (this.notificationDialog?.open) this.notificationDialog.close();
      this.dialog?.showModal(); this.renderResults();
      await this.refresh();
    }

    closed() {
      this.session += 1;
      this.loading = false;
      // A programmatic close cannot cancel an accepted POST, but send() observes this token before issuing another.
      if (!this.sending) this.setBusy(false);
    }

    close() { if (!this.sending && this.dialog?.open) this.dialog.close(); }

    validState(value) {
      return value && value.online !== false && value.federation_offline !== true && Array.isArray(value.agents) && value.agents.every(agent =>
        agent && typeof agent.id === 'string' && agent.id && (agent.name === undefined || typeof agent.name === 'string') &&
        (agent.goal === undefined || typeof agent.goal === 'string') && (agent.swarm === undefined || typeof agent.swarm === 'string'));
    }

    async refresh() {
      if (!this.dialog?.open || this.loading || this.sending || !this.contextCurrent()) return;
      const context = this.context, session = this.session;
      this.loading = true; this.setBusy(true); this.setErrors('');
      if (this.summaryNode) this.summaryNode.textContent = `Loading ${context.sources.length} Swarm${context.sources.length === 1 ? '' : 's'}…`;
      const loaded = [], failures = [];
      await Promise.all(context.sources.map(async source => {
        try {
          const value = await this.request(statePath(source.id));
          if (!this.validState(value)) throw Error('Invalid or offline state response');
          loaded.push({source, agents: value.agents});
        } catch (error) { failures.push({source, error}); }
      }));
      if (!this.active(session)) return;
      const oldChecked = new Map(this.checked); this.targets = [];
      for (const entry of loaded) for (const agent of entry.agents) {
        const key = identity(entry.source.id, agent.id), managed = agent.managed === true;
        const target = {key, sourceId: entry.source.id, sourceName: entry.source.name, agentId: agent.id,
          name: typeof agent.name === 'string' && agent.name ? agent.name : agent.id,
          goal: typeof agent.goal === 'string' ? agent.goal : '', swarm: typeof agent.swarm === 'string' ? agent.swarm : '',
          awake: !!agent.alive, status: typeof agent.status === 'string' ? agent.status : '', managed};
        this.targets.push(target);
        if (managed) this.checked.set(key, oldChecked.has(key) ? oldChecked.get(key) : true);
      }
      this.failures = failures; this.renderTargets();
      const warnings = [];
      if (this.currentUnavailable) warnings.push('The active Swarm is offline or unavailable. Only this Swarm has no targets; choose All connected Swarms explicitly to select others.');
      if (failures.length) warnings.push(`Could not load ${failures.map(item => item.source.name).join(', ')}. Invalid or offline Swarms were excluded.`);
      this.setErrors(warnings.join('\n'));
      this.loading = false; this.setBusy(false);
    }

    visible(target) {
      const scope = this.scopeControl?.value || 'current';
      if (scope === 'current' && target.sourceId !== this.context?.swarm) return false;
      const awake = this.awakeControl?.value || 'all';
      if (awake === 'awake' && !target.awake) return false;
      if (awake === 'asleep' && target.awake) return false;
      const term = String(this.searchControl?.value || '').trim().toLocaleLowerCase();
      return !term || [target.name, target.agentId, target.goal, target.swarm, target.sourceName].some(value => value.toLocaleLowerCase().includes(term));
    }

    renderTargets() {
      if (!this.targetsNode || !this.context) return;
      this.targetsNode.replaceChildren(); this.rows.clear();
      let shown = 0, observed = 0;
      for (const target of this.targets) {
        if (!this.visible(target)) continue;
        if (!target.managed) { observed += 1; continue; }
        shown += 1;
        const row = this.create('label', 'broadcast-target');
        const checkbox = this.create('input'); checkbox.type = 'checkbox'; checkbox.checked = this.checked.get(target.key) !== false; checkbox.disabled = this.sending;
        checkbox.addEventListener('change', () => { this.checked.set(target.key, !!checkbox.checked); this.renderSummary(); });
        const words = this.create('span', 'broadcast-target-label');
        words.append(this.create('strong', '', target.name), this.create('span', 'hint',
          `${target.agentId} · ${target.sourceName} · ${target.awake ? 'process live' : 'process asleep'}${target.status ? ` · ${target.status}` : ''}`));
        row.append(checkbox, words); this.targetsNode.append(row); this.rows.set(target.key, {target, checkbox, row});
      }
      if (!shown) this.targetsNode.append(this.create('p', 'hint', 'No managed agents match these filters.'));
      if (observed) this.targetsNode.append(this.create('p', 'hint', `${observed} observed agent${observed === 1 ? ' is' : 's are'} not selectable because this controller cannot manage their inbox.`));
      this.renderSummary();
    }

    effectiveTargets() {
      const values = [];
      for (const {target, checkbox} of this.rows.values()) if (!checkbox.disabled && checkbox.checked && this.visible(target)) values.push(target);
      return values;
    }

    renderSummary() {
      if (!this.summaryNode) return;
      const selected = this.effectiveTargets(), shown = this.rows.size;
      this.summaryNode.textContent = `${selected.length} of ${shown} shown managed agents selected. “Awake” means a live process, including dormant or done agents; durable messages may queue even while a live process is dormant or done. Sending never wakes an agent.`;
      if (this.sendButton) this.sendButton.disabled = this.loading || this.sending || selected.length === 0;
    }

    selectShown(checked) {
      if (this.sending) return;
      for (const [key, row] of this.rows) { row.checkbox.checked = checked; this.checked.set(key, checked); }
      this.renderSummary();
    }

    setBusy(value) {
      const controls = [this.scopeControl, this.awakeControl, this.searchControl, this.refreshButton,
        this.query('#broadcast-select-all'), this.query('#broadcast-select-none')];
      for (const control of controls) if (control) control.disabled = !!value;
      if (this.closeButton) this.closeButton.disabled = !!this.sending;
      if (this.cancelButton) this.cancelButton.disabled = !!this.sending;
      for (const row of this.rows.values()) row.checkbox.disabled = !!value;
      if (this.sendButton) {
        this.sendButton.disabled = !!value || this.effectiveTargets().length === 0;
        this.sendButton.textContent = this.sending ? 'Broadcasting…' : 'Broadcast';
      }
    }

    alreadyAccepted(target, text) {
      return this.results.some(result => result.key === target.key && result.text === text && ['queued', 'uncertain'].includes(result.outcome));
    }

    async send() {
      if (!this.dialog?.open || this.loading || this.sending || !this.contextCurrent()) return;
      const text = this.message;
      if (!text || text.length > 32000) { this.setErrors('Message must contain 1 to 32,000 characters.'); return; }
      const visible = this.effectiveTargets();
      const targets = visible.filter(target => !this.alreadyAccepted(target, text));
      if (!targets.length) {
        this.setErrors(visible.length ? 'All shown selections already have queued or uncertain results for this message. Change the message or selection; do not retry blindly.' : 'Select at least one shown agent.');
        return;
      }
      let jobs;
      try { jobs = targets.map(target => ({...target, requestId: requestId()})); }
      catch { this.setErrors('This browser cannot create secure request IDs. Use a recent browser or HTTPS.'); return; }
      const swarmCount = new Set(jobs.map(job => job.sourceId)).size;
      const question = `Broadcast this durable message to ${jobs.length} agent${jobs.length === 1 ? '' : 's'} across ${swarmCount} Swarm${swarmCount === 1 ? '' : 's'}?\n\nMessages are queued in each agent inbox. This does not wake agents; delivery may wait even when a live process is dormant or done.`;
      if (!this.confirm(question)) return;

      const session = this.session, context = this.context, immutableText = text;
      this.sending = true; this.setErrors(''); this.setBusy(true);
      const batch = jobs.map(job => ({...job, text: immutableText, outcome: 'not-sent', detail: 'Not sent.'}));
      // Store request IDs before any POST so interrupted batches remain inspectable.
      this.results = this.results.concat(batch);
      this.renderResults();
      let issued = 0;
      for (const result of batch) {
        if (!this.active(session) || !this.contextCurrent(context)) {
          result.detail = 'Not sent because the dialog closed or the Swarm changed.'; continue;
        }
        issued += 1;
        if (this.summaryNode) this.summaryNode.textContent = `Broadcasting ${issued} of ${batch.length}…`;
        try {
          const value = await this.request(messagesPath(result.sourceId, result.agentId), 'POST', {text: immutableText, request_id: result.requestId});
          if (!validAck(value, result, immutableText)) throw Error('Malformed acknowledgement');
          result.outcome = 'queued'; result.detail = `Accepted with status ${value.message.status}.`;
        } catch (error) {
          if (error?.status >= 400 && error.status < 500) { result.outcome = 'failed'; result.detail = error.message || `Request failed (${error.status}).`; }
          else { result.outcome = 'uncertain'; result.detail = `${error?.message || 'Request failed'}. Do not retry blindly.`; }
        }
        if (this.active(session)) this.renderResults();
      }
      if (issued && this.active(session)) {
        try { await this.onChange({noAutoWake: true}); } catch { /* A dashboard refresh cannot change delivery outcomes. */ }
      }
      this.sending = false;
      if (this.active(session)) { this.setBusy(false); this.renderTargets(); this.renderResults(); }
    }

    renderResults() {
      if (!this.resultsNode) return;
      this.resultsNode.replaceChildren();
      const results = this.results.filter(result => result.text === this.message);
      if (!results.length) return;
      const heading = this.create('p', 'hint', `${results.length} target result${results.length === 1 ? '' : 's'}. Keep request IDs for uncertain outcomes.`);
      this.resultsNode.append(heading);
      for (const result of results) {
        const row = this.create('div', `broadcast-result status-${result.outcome}`);
        row.append(this.create('strong', '', `${result.name} · ${result.sourceName} — ${result.outcome}`),
          this.create('span', 'hint', `Request ID ${result.requestId}`), this.create('span', '', result.detail));
        this.resultsNode.append(row);
      }
    }
  }

  const api = {BroadcastController, statePath, messagesPath, validAck, identity};
  root.SwarmAgentBroadcast = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
