/* Private agent archive/forget UI. Remote values are always rendered as text. */
(function (root) {
  'use strict';

  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const ACTIVE = new Set(['queued', 'running']);
  const JOB_STATUSES = new Set(['queued', 'running', 'completed', 'failed']);

  function secureRequestId() {
    const shared = root.SwarmAgentMessages?.requestId;
    if (typeof shared === 'function') return shared();
    if (root.crypto?.randomUUID) return root.crypto.randomUUID();
    if (!root.crypto?.getRandomValues) throw Error('Secure randomness is unavailable');
    const bytes = new Uint8Array(16); root.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  function sourcePath(source, path) {
    return source === 'local' ? path : `/api/federation/${encodeURIComponent(source)}/proxy${path}`;
  }

  function text(value, fallback = '') { return typeof value === 'string' ? value : fallback; }
  function validId(value) { return typeof value === 'string' && UUID.test(value); }
  function validJob(value, expected) {
    return !!value && typeof value === 'object' && !Array.isArray(value) && validId(value.id) &&
      validId(value.archive_id) && value.agent_id === expected.agentId && value.id === expected.requestId &&
      value.name === expected.name && JOB_STATUSES.has(value.status);
  }
  function validListing(value) {
    return !!value && typeof value === 'object' && Array.isArray(value.archives) && Array.isArray(value.jobs);
  }
  function node(tag, className = '', value = '') {
    const result = root.document.createElement(tag); result.className = className; result.textContent = value; return result;
  }
  function action(label, className, callback) {
    const result = node('button', className, label); result.type = 'button'; result.addEventListener('click', callback); return result;
  }
  function formatDate(value) {
    if (typeof value !== 'string' || !value) return 'unknown';
    const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
  }
  function formatSize(value) {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 'unknown size';
    if (value < 1024) return `${value} B`;
    if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
    if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
    return `${(value / 1024 ** 3).toFixed(1)} GiB`;
  }

  class AgentArchivesController {
    constructor({query = selector => root.document.querySelector(selector), api,
      currentSwarm = () => 'local', currentGeneration = () => 0, ownerName = source => source === 'local' ? 'Local Swarm' : source,
      onChanged = async () => {}, toast = () => {}, confirm = value => root.confirm(value),
      requestId = secureRequestId, openAgent = () => {}, requestTimeout = 20000,
      setTimer = (callback, delay) => root.setTimeout(callback, delay), clearTimer = timer => root.clearTimeout(timer)} = {}) {
      Object.assign(this, {query, api, currentSwarm, currentGeneration, ownerName, onChanged, toast, confirm,
        requestId, openAgent, requestTimeout, setTimer, clearTimer});
      this.choiceDialog = query('#archive-agent-choice-dialog');
      this.choiceName = query('#archive-agent-name'); this.includeFiles = query('#archive-agent-include-files');
      this.archiveSubmit = query('#archive-agent-submit'); this.forgetOnly = query('#archive-agent-forget-only');
      this.choiceCancel = query('#archive-agent-cancel'); this.openButton = query('#archived-agents-button');
      this.dialog = query('#archives-dialog'); this.owner = query('#archives-owner'); this.listNode = query('#archives-list');
      this.jobsNode = query('#archives-jobs'); this.statusNode = query('#archives-status'); this.errorNode = query('#archives-error');
      this.refreshButton = query('#archives-refresh'); this.closeButton = query('#archives-close');
      this.deleteSelectedButton = query('#archives-delete-selected'); this.selectAll = query('#archives-select-all');
      this.choice = null; this.context = null; this.archives = []; this.jobs = []; this.selection = new Set();
      this.activePosts = new Map(); this.uncertain = new Map(); this.loading = false; this.mutating = false;
      this.token = 0; this.pollTimer = null; this.disposed = false;

      this.archiveSubmit?.addEventListener('click', () => void this.startChoiceArchive());
      this.forgetOnly?.addEventListener('click', () => void this.chooseForgetOnly());
      this.choiceCancel?.addEventListener('click', () => this.cancelChoice());
      this.choiceDialog?.addEventListener('cancel', event => { event.preventDefault(); this.cancelChoice(); });
      this.openButton?.addEventListener('click', () => void this.open());
      this.refreshButton?.addEventListener('click', () => void this.refresh());
      this.closeButton?.addEventListener('click', () => this.close());
      this.dialog?.addEventListener('cancel', event => { event.preventDefault(); if (!this.mutating) this.close(); });
      this.dialog?.addEventListener('close', () => this.closed());
      this.deleteSelectedButton?.addEventListener('click', () => void this.deleteSelected());
      this.selectAll?.addEventListener('change', () => this.selectEveryArchive(!!this.selectAll.checked));
    }

    get busy() { return this.activePosts.size > 0; }
    capture(agent) {
      return {agent: {...agent}, agentId: String(agent?.id || ''), name: text(agent?.name, String(agent?.id || 'agent')),
        status: text(agent?.status), source: this.currentSwarm(), generation: this.currentGeneration()};
    }
    captureOwner() { const source = this.currentSwarm(); return {source, generation: this.currentGeneration(), base: sourcePath(source, '/api/archives')}; }
    sameOwner(context = this.context) {
      return !!context && context.source === this.currentSwarm() && context.generation === this.currentGeneration() &&
        context.base === sourcePath(context.source, '/api/archives');
    }
    choiceCurrent(choice = this.choice) {
      return !!choice && choice.source === this.currentSwarm() && choice.generation === this.currentGeneration();
    }
    active(token = this.token, context = this.context) {
      return !this.disposed && token === this.token && !!this.dialog?.open && this.sameOwner(context);
    }
    setError(message) { if (this.errorNode) { this.errorNode.textContent = text(message); this.errorNode.hidden = !message; } }
    setStatus(message) { if (this.statusNode) this.statusNode.textContent = text(message); }

    choose(agent, forgetCallback) {
      if (!agent?.id || typeof forgetCallback !== 'function' || this.archiveInProgress(agent)) return false;
      this.choice = {...this.capture(agent), forgetCallback};
      if (this.choiceName) this.choiceName.textContent = this.choice.name;
      if (this.includeFiles) { this.includeFiles.checked = false; this.includeFiles.disabled = false; }
      if (this.archiveSubmit) this.archiveSubmit.disabled = false;
      if (this.forgetOnly) this.forgetOnly.disabled = false;
      this.choiceDialog?.showModal();
      return true;
    }
    cancelChoice() { this.choice = null; if (this.choiceDialog?.open) this.choiceDialog.close(); }
    setChoiceBusy(value) {
      if (this.archiveSubmit) this.archiveSubmit.disabled = !!value;
      if (this.forgetOnly) this.forgetOnly.disabled = !!value;
      if (this.choiceCancel) this.choiceCancel.disabled = !!value;
      if (this.includeFiles) this.includeFiles.disabled = !!value;
    }
    async chooseForgetOnly() {
      const selected = this.choice;
      if (!selected || !this.choiceCurrent(selected) || this.activePosts.has(this.archiveKey(selected))) return null;
      this.choice = null; if (this.choiceDialog?.open) this.choiceDialog.close();
      return selected.forgetCallback(selected.agent, selected);
    }
    startChoiceArchive() {
      const selected = this.choice;
      if (!selected) return Promise.resolve(null);
      const includeFiles = !!this.includeFiles?.checked;
      return this.startArchive(selected.agent, includeFiles, selected);
    }

    requestWithTimeout(...args) {
      return new Promise((resolve, reject) => {
        let settled = false, timer;
        const finish = (callback, value) => { if (settled) return; settled = true; if (timer !== undefined) this.clearTimer(timer); callback(value); };
        timer = this.setTimer(() => { const error = Error(`Request timed out after ${this.requestTimeout}ms`); error.timeout = true; finish(reject, error); }, this.requestTimeout);
        Promise.resolve().then(() => this.api(...args)).then(value => finish(resolve, value), error => finish(reject, error));
      });
    }

    archiveKey(context) { return `${context.source}\u0000${context.generation}\u0000${context.agentId}`; }
    archiveInProgress(agent) {
      const id = typeof agent === 'string' ? agent : agent?.id;
      if (!id) return false;
      if (typeof agent === 'object' && ACTIVE.has(agent?.agent_archive?.status)) return true;
      for (const request of this.activePosts.values()) if (request.agentId === id && request.source === this.currentSwarm()) return true;
      return this.jobs.some(job => job?.agent_id === id && ACTIVE.has(job.status));
    }
    async startArchive(agent, includeFiles = false, captured = null) {
      const context = captured || this.capture(agent);
      if (!context.agentId || !validId(context.agentId) || !this.choiceCurrent(context)) return null;
      const key = this.archiveKey(context);
      if (this.activePosts.has(key) || this.uncertain.has(key) || this.archiveInProgress(context.agentId)) {
        const prior = this.uncertain.get(key);
        if (prior) this.toast(`Archive outcome for ${context.name} is uncertain. Inspect request ID ${prior}; do not retry blindly.`, true);
        return prior || null;
      }
      let id;
      try { id = this.requestId(); if (!validId(id)) throw Error('invalid UUID'); }
      catch { this.toast('This browser cannot create a secure request ID. Use a recent browser or HTTPS.', true); return null; }
      const request = {...context, requestId: id, includeFiles: !!includeFiles,
        path: sourcePath(context.source, `/api/agents/${encodeURIComponent(context.agentId)}/archive-and-forget`)};
      this.activePosts.set(key, request); this.setChoiceBusy(true);
      try {
        const value = await this.requestWithTimeout(request.path, 'POST', {include_files: request.includeFiles, request_id: id});
        if (!validJob(value?.job, request)) throw Error('Invalid archive acknowledgement');
        const current = this.choiceCurrent(context);
        if (current) {
          this.jobs = [value.job, ...this.jobs.filter(job => job?.id !== value.job.id)];
          if (this.choice === captured || (!captured && this.choice?.agentId === context.agentId)) {
            this.choice = null; if (this.choiceDialog?.open) this.choiceDialog.close();
          }
          this.toast(`Archive queued for ${context.name}. Job ${id}.`);
          if (this.dialog?.open && this.sameOwner()) { this.render(); this.schedulePoll(); }
        }
        try { void Promise.resolve(this.onChanged({refresh: true, noAutoWake: true})).catch(() => {}); } catch { /* accepted job remains authoritative */ }
        return value.job;
      } catch (error) {
        const uncertain = error?.timeout || !error?.status || error.status >= 500;
        let message = text(error?.message, 'Archive request failed.');
        if (error?.status === 404) message = 'Agent archives are unsupported by this controller.';
        else if (uncertain) { this.uncertain.set(key, id); message += ` Outcome uncertain; do not retry blindly. Request ID ${id}.`; }
        if (this.choiceCurrent(context)) this.toast(message, true);
        return null;
      } finally {
        if (this.activePosts.get(key) === request) this.activePosts.delete(key);
        if (this.choice === captured || (!captured && this.choice?.agentId === context.agentId)) this.setChoiceBusy(false);
      }
    }

    async open() {
      if (this.disposed) return;
      const menu = this.openButton?.closest?.('details'); if (menu) menu.open = false;
      this.token += 1; this.stopPolling(); this.context = this.captureOwner(); this.archives = []; this.jobs = []; this.selection.clear();
      if (this.selectAll) this.selectAll.checked = false;
      if (this.owner) this.owner.textContent = this.ownerName(this.context.source);
      this.setError(''); this.setStatus('Loading archives…'); this.dialog?.showModal();
      await this.refresh();
    }
    close() { if (!this.mutating && this.dialog?.open) this.dialog.close(); }
    closed() { this.token += 1; this.loading = false; this.stopPolling(); }
    sync() {
      if (!this.context || this.sameOwner()) return false;
      this.token += 1; this.stopPolling(); this.loading = false; this.mutating = false;
      this.archives = []; this.jobs = []; this.selection.clear();
      if (this.dialog?.open) {
        this.context = this.captureOwner();
        if (this.owner) this.owner.textContent = this.ownerName(this.context.source);
        this.render(); this.setStatus('Loading archives…'); void this.refresh();
      }
      return true;
    }
    dispose() { this.disposed = true; this.token += 1; this.stopPolling(); this.choice = null; }

    async refresh() {
      if (!this.dialog?.open || this.loading || this.mutating || !this.sameOwner()) return null;
      const context = this.context, token = this.token; this.loading = true; this.updateControls(); this.setError('');
      try {
        const value = await this.api(context.base);
        if (!this.active(token, context)) return null;
        if (!validListing(value)) throw Error('Invalid archive listing');
        this.archives = value.archives.filter(item => item && validId(item.id));
        this.jobs = value.jobs.filter(item => item && validId(item.id));
        const known = new Set(this.archives.map(item => item.id));
        this.selection = new Set([...this.selection].filter(id => known.has(id)));
        this.render(); this.schedulePoll(); return value;
      } catch (error) {
        if (this.active(token, context)) {
          this.setError(error?.status === 404 ? 'Agent archives are unsupported by this controller.' : text(error?.message, 'Could not load archives.'));
          this.setStatus('Archives unavailable.'); this.stopPolling();
        }
        return null;
      } finally { if (this.active(token, context)) { this.loading = false; this.updateControls(); } }
    }

    schedulePoll() {
      this.stopPolling();
      if (!this.dialog?.open || !this.sameOwner() || !this.jobs.some(job => ACTIVE.has(job?.status))) return;
      const token = this.token, context = this.context;
      this.pollTimer = this.setTimer(() => { this.pollTimer = null; if (this.active(token, context)) void this.refresh(); }, 2000);
    }
    stopPolling() { if (this.pollTimer !== null) this.clearTimer(this.pollTimer); this.pollTimer = null; }
    activeForArchive(id) { return this.jobs.some(job => job?.archive_id === id && ACTIVE.has(job.status)); }

    render() {
      this.renderArchives(); this.renderJobs();
      const active = this.jobs.filter(job => ACTIVE.has(job?.status)).length;
      this.setStatus(`${this.archives.length} private archive${this.archives.length === 1 ? '' : 's'}${active ? ` · ${active} job${active === 1 ? '' : 's'} in progress` : ''}.`);
      this.updateControls();
    }
    renderArchives() {
      if (!this.listNode) return;
      this.listNode.replaceChildren();
      if (!this.archives.length) { this.listNode.append(node('p', 'hint', 'No archived agents.')); return; }
      for (const archive of this.archives) {
        const row = node('div', 'archive-row'); row.dataset.archiveId = archive.id;
        const check = node('input'); check.type = 'checkbox'; check.checked = this.selection.has(archive.id); check.disabled = this.mutating;
        check.addEventListener('change', () => { if (check.checked) this.selection.add(archive.id); else this.selection.delete(archive.id); this.updateControls(); });
        const details = node('div', 'archive-details');
        details.append(node('strong', '', text(archive.name, archive.agent_id)),
          node('span', 'hint', `${formatDate(archive.archived_at || archive.created_at)} · ${formatSize(archive.size)} · ${archive.files_included ? 'referenced files included' : 'session + history dependencies / owned uploads'}`));
        if (text(archive.goal)) details.append(node('span', '', archive.goal));
        const access = archive.last_accessed_at ? `Last downloaded ${formatDate(archive.last_accessed_at)}` : 'Never downloaded';
        details.append(node('span', 'hint', `${access}${archive.last_resurrected_at ? ` · Last restored ${formatDate(archive.last_resurrected_at)}` : ''}`));
        const controls = node('div', 'archive-actions');
        const resurrectLabel = archive.last_resurrected_at && archive.restorable !== true ? 'Already restored' : 'Resurrect…';
        const resurrect = action(resurrectLabel, 'small', () => void this.resurrect(archive));
        resurrect.disabled = this.mutating || archive.restorable !== true || archive.missing_zip === true;
        resurrect.title = `Restore from ${this.ownerName(this.context.source)} without waking the agent.`;
        const download = node('a', 'button small', 'Download ZIP');
        download.href = sourcePath(this.context.source, `/api/archives/${encodeURIComponent(archive.id)}/download`);
        download.target = '_blank'; download.rel = 'noopener noreferrer'; download.download = '';
        download.title = `Private ZIP from ${this.ownerName(this.context.source)}: may contain exact session content and secrets. Available only to an authenticated operator.`;
        if (archive.missing_zip === true) { download.removeAttribute?.('href'); download.setAttribute?.('aria-disabled', 'true'); }
        const remove = action('Delete archive…', 'small danger', () => void this.deleteArchives([archive.id]));
        remove.disabled = this.mutating || this.activeForArchive(archive.id);
        remove.title = `Delete only this private archive from ${this.ownerName(this.context.source)}.`;
        controls.append(resurrect, download, remove); row.append(check, details, controls); this.listNode.append(row);
      }
    }
    renderJobs() {
      if (!this.jobsNode) return;
      this.jobsNode.replaceChildren();
      if (!this.jobs.length) { this.jobsNode.append(node('p', 'hint', 'No archive jobs.')); return; }
      for (const job of this.jobs) {
        const row = node('div', `archive-job status-${text(job.status, 'unknown')}`);
        row.append(node('strong', '', `${text(job.name, job.agent_id)} — ${text(job.status, 'unknown')}`), node('span', 'hint', `Job ${job.id}`));
        if (job.error) row.append(node('span', 'error', text(job.error)));
        this.jobsNode.append(row);
      }
    }
    updateControls() {
      const busy = this.loading || this.mutating;
      if (this.refreshButton) this.refreshButton.disabled = busy;
      if (this.closeButton) this.closeButton.disabled = this.mutating;
      if (this.selectAll) {
        this.selectAll.disabled = busy || !this.archives.length;
        this.selectAll.checked = !!this.archives.length && this.archives.every(item => this.selection.has(item.id));
        this.selectAll.indeterminate = this.selection.size > 0 && !this.selectAll.checked;
      }
      if (this.deleteSelectedButton) this.deleteSelectedButton.disabled = busy || this.selection.size === 0 ||
        [...this.selection].some(id => this.activeForArchive(id));
    }
    selectEveryArchive(checked) {
      if (this.loading || this.mutating) return;
      this.selection = checked ? new Set(this.archives.map(item => item.id)) : new Set(); this.renderArchives(); this.updateControls();
    }

    async resurrect(archive) {
      if (this.mutating || !this.dialog?.open || !this.sameOwner() || archive?.restorable !== true || archive.missing_zip === true) return null;
      const warning = `Resurrect ${text(archive.name, archive.agent_id)}?\n\nThis restores the stopped agent with the same ID. Its working folder is unchanged, timers remain disabled, and it is not awakened. The private archive remains available.`;
      const context = this.context, token = this.token;
      if (!this.confirm(warning) || !this.active(token, context)) return null;
      let id; try { id = this.requestId(); if (!validId(id)) throw Error('invalid UUID'); }
      catch { this.setError('A secure request ID could not be generated. Use a recent browser or HTTPS.'); return null; }
      this.mutating = true; this.updateControls(); this.setError('');
      try {
        const result = await this.requestWithTimeout(`${context.base}/${encodeURIComponent(archive.id)}/resurrect`, 'POST', {request_id: id});
        if (!this.active(token, context)) return null;
        if (!result?.agent || result.agent.id !== archive.agent_id || result.agent.auto_resume !== false || result.agent.alive !== false || result.archive?.id !== archive.id)
          throw Error('Invalid resurrection acknowledgement');
        try { await this.onChanged({refresh: true, noAutoWake: true}); } catch { /* acknowledgement remains authoritative */ }
        if (!this.active(token, context)) return result;
        this.openAgent(result.agent, {source: context.source, generation: context.generation, archiveId: archive.id});
        this.toast(`${text(archive.name, archive.agent_id)} restored stopped. The archive was kept.`);
        await this.refreshAfterMutation(token, context); return result;
      } catch (error) {
        if (this.active(token, context)) this.setError(`${text(error?.message, 'Resurrection failed.')} Request ${id} was not retried automatically.`);
        return null;
      } finally { if (this.active(token, context)) { this.mutating = false; this.updateControls(); } }
    }

    async deleteSelected() { return this.deleteArchives([...this.selection]); }
    async deleteArchives(ids) {
      if (this.mutating || !this.dialog?.open || !this.sameOwner()) return false;
      const known = new Map(this.archives.map(item => [item.id, item]));
      const targets = ids.filter((id, index) => validId(id) && known.has(id) && ids.indexOf(id) === index);
      if (!targets.length || targets.some(id => this.activeForArchive(id))) return false;
      const names = targets.map(id => text(known.get(id).name, id));
      const context = this.context, token = this.token;
      if (!this.confirm(`Delete ${targets.length} private archive${targets.length === 1 ? '' : 's'}?\n\n${names.join(', ')}\n\nThis deletes only the archive ZIP and archive record. It does not delete any agent, active session, working folder, or worktree.`) || !this.active(token, context)) return false;
      this.mutating = true; this.updateControls(); this.setError('');
      let completed = 0;
      try {
        for (const id of targets) {
          if (!this.active(token, context)) return false;
          try {
            const result = await this.requestWithTimeout(`${context.base}/${encodeURIComponent(id)}`, 'DELETE');
            if (!this.active(token, context)) return false;
            if (!result || result.id !== id || result.deleted !== true) throw Error('Invalid delete acknowledgement');
            completed += 1; this.selection.delete(id); this.archives = this.archives.filter(item => item.id !== id); this.render();
          } catch (error) {
            if (this.active(token, context)) this.setError(`${text(error?.message, 'Archive deletion failed.')} Stopped after ${completed} of ${targets.length}; refresh before retrying.`);
            return false;
          }
        }
        await this.refreshAfterMutation(token, context); return true;
      } finally { if (this.active(token, context)) { this.mutating = false; this.updateControls(); } }
    }
    async refreshAfterMutation(token, context) {
      if (!this.active(token, context)) return;
      this.mutating = false; this.updateControls(); await this.refresh();
    }
  }

  const api = {AgentArchivesController, sourcePath, validJob, secureRequestId};
  root.SwarmAgentArchives = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
