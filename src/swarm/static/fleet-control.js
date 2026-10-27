/* Master control for explicitly restarting live managed Pi processes. */
(function (root) {
  'use strict';
  const ACTIVE = new Set(['running', 'attention']);
  const TERMINAL = new Set(['completed', 'cancelled']);
  function element(tag, className = '', text = '') {
    const value = document.createElement(tag); value.className = className; value.textContent = text; return value;
  }
  function actionButton(text, className, action) {
    const value = element('button', className, text); value.type = 'button'; value.addEventListener('click', action); return value;
  }
  function requestId() {
    if (root.crypto?.randomUUID) return root.crypto.randomUUID();
    const bytes = new Uint8Array(16); root.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function isActiveRestart(agent) {
    const restart = agent?.restart;
    if (agent && Object.prototype.hasOwnProperty.call(agent,'restart_job_id')) return !!agent.restart_job_id;
    return !!restart?.job_id && !TERMINAL.has(restart.state) && restart.state !== 'failed';
  }
  class FleetControl {
    constructor({query = selector => document.querySelector(selector), apiPath, api, currentSwarm, currentGeneration,
      onChange = async () => {}, canMutate = () => true, confirm = text => root.confirm(text), create = element, setTimer = root.setTimeout.bind(root), clearTimer = root.clearTimeout.bind(root)}) {
      Object.assign(this, {query, apiPath, api, currentSwarm, currentGeneration, onChange, canMutate, confirm, create, setTimer, clearTimer});
      this.dialog = query('#fleet-restart-dialog'); this.button = query('#fleet-control-button'); this.preview = null; this.job = null;
      this.operation = 0; this.pollTimer = null; this.context = null; this.busy = false;
      this.button.addEventListener('click', () => this.open());
      query('#fleet-preview-refresh').addEventListener('click', () => this.loadPreview());
      query('#fleet-restart-start').addEventListener('click', () => this.start());
      query('#fleet-job-cancel').addEventListener('click', () => this.cancelJob());
      this.dialog.addEventListener('close', () => { this.operation += 1; this.stopPolling(); });
    }
    capture() {
      return {swarm: this.currentSwarm(), generation: this.currentGeneration(), base: this.apiPath('/api/fleet/restarts')};
    }
    valid(context, operation = this.operation) {
      return this.dialog.open && operation === this.operation && context.swarm === this.currentSwarm() && context.generation === this.currentGeneration()
        && context.base === this.apiPath('/api/fleet/restarts');
    }
    setError(message) { this.query('#fleet-restart-error').textContent = message || ''; }
    setBusy(value) {
      this.busy = value;
      this.query('#fleet-preview-refresh').disabled = value;
      this.query('#fleet-restart-start').disabled = value || !this.canMutate() || !this.preview?.targets?.length;
      this.renderJob();
    }
    async open() {
      this.query('#notifications-dialog')?.close();
      this.operation += 1; this.stopPolling(); this.preview = null; this.job = null; this.context = this.capture();
      for (const id of ['#fleet-skipped-section', '#fleet-job-section']) {
        const details = this.query(id); details.open = false; details.hidden = true;
      }
      this.query('#fleet-restart-workspace').textContent = this.currentSwarm() === 'local' ? 'Local Swarm' : this.currentSwarm();
      this.query('#fleet-preview-targets').replaceChildren(this.create('p', 'fleet-empty', 'Loading restart preview…'));
      this.query('#fleet-preview-skipped').replaceChildren(); this.setError(''); this.dialog.showModal();
      this.query('#fleet-restart-cancel').focus();
      await this.loadJobs();
      if (this.valid(this.context)) await this.loadPreview();
    }
    missingController(error) {
      return error?.status === 404 ? 'Fleet restart control is unavailable on this controller. Restart or update that Swarm controller, then try again.' : error.message;
    }
    async loadJobs() {
      const context = this.context, operation = this.operation;
      try {
        const result = await this.api(context.base);
        if (!this.valid(context, operation)) return;
        const jobs = Array.isArray(result.jobs) ? result.jobs : [];
        this.job = jobs.find(job => ACTIVE.has(job.status)) || jobs[0] || null;
        this.renderJob(); this.schedulePoll();
      } catch (error) { if (this.valid(context, operation)) this.setError(this.missingController(error)); }
    }
    async loadPreview() {
      const context = this.context, operation = this.operation; if (!context || !this.valid(context, operation) || this.busy) return;
      this.setBusy(true); this.setError('');
      try {
        const result = await this.api(`${context.base}/preview`);
        if (!this.valid(context, operation)) return;
        this.preview = {targets: Array.isArray(result.targets) ? result.targets : [], skipped: Array.isArray(result.skipped) ? result.skipped : [], home: result.home || ''};
        this.renderPreview();
      } catch (error) { if (this.valid(context, operation)) this.setError(this.missingController(error)); }
      finally { if (this.valid(context, operation)) this.setBusy(false); }
    }
    renderPreview() {
      const targets = this.query('#fleet-preview-targets'), skipped = this.query('#fleet-preview-skipped'); targets.replaceChildren(); skipped.replaceChildren();
      for (const target of this.preview?.targets || []) {
        const row = this.create('label', 'fleet-preview-row');
        const check = this.create('input'); check.type = 'checkbox'; check.checked = true; check.dataset.agentId = target.id;
        const text = this.create('span'); text.append(this.create('strong', '', target.name || target.id), this.create('small', '', `${target.status || 'unknown'} · ${target.transport === 'native' ? 'native graceful restart' : 'legacy /quit required'}`));
        row.append(check, text); targets.append(row);
      }
      if (!this.preview?.targets?.length) targets.append(this.create('p', 'fleet-empty', 'No eligible live managed Pi agents in this Swarm. Stopped agents will not be launched.'));
      for (const item of this.preview?.skipped || []) {
        const row = this.create('li'); row.append(this.create('strong', '', item.name || item.id), document.createTextNode(` — ${item.reason || 'not eligible'}`)); skipped.append(row);
      }
      this.query('#fleet-skipped-section').hidden = !this.preview?.skipped?.length;
      this.query('#fleet-skipped-count').textContent = String(this.preview?.skipped?.length || 0);
      this.query('#fleet-preview-home').textContent = this.preview?.home || '';
      this.query('#fleet-restart-start').disabled = this.busy || !this.canMutate() || !this.preview?.targets?.length;
    }
    selectedTargets() {
      const selected = new Set([...this.query('#fleet-preview-targets').querySelectorAll('input[type="checkbox"]:checked')].map(input => input.dataset.agentId));
      return (this.preview?.targets || []).filter(target => selected.has(target.id)).map(target => ({id: target.id, token: target.token}));
    }
    async start() {
      const context = this.context, operation = this.operation, targets = this.selectedTargets();
      if (!this.canMutate()) { this.setError('Remote fleet restarts require --allow-remote-restart on that controller.'); return; }
      if (!context || !this.valid(context, operation) || this.busy || !targets.length) { if (!targets.length) this.setError('Select at least one agent.'); return; }
      const names = (this.preview.targets || []).filter(item => targets.some(target => target.id === item.id)).map(item => item.name || item.id);
      if (!this.confirm(`Restart ${names.length} live agent process${names.length === 1 ? '' : 'es'} in ${context.swarm === 'local' ? 'Local Swarm' : context.swarm}?\n\nAgents: ${names.join(', ')}\n\nThis first requests a graceful native idle handshake. It sends no work prompt and never auto-forces on timeout. In-progress work or tool calls may delay graceful exit. Unsent terminal drafts may be lost when Pi exits. Existing timers and queued mail may resume previously authorized work. Legacy and force actions require separate warnings.`)) return;
      let id;
      try { id = requestId(); } catch { this.setError('A secure request ID could not be generated. Use a recent browser or HTTPS.'); return; }
      this.setBusy(true); this.setError('');
      try {
        const result = await this.api(context.base, 'POST', {request_id: id, targets, timeout_seconds: 300, confirmed: true});
        if (!this.valid(context, operation)) return;
        this.job = result.job; this.preview = null; this.renderPreview(); this.renderJob(); this.schedulePoll(); await this.onChange();
      } catch (error) {
        if (this.valid(context, operation)) this.setError(error?.status === 409
          ? `The process snapshot changed or is no longer valid. Refresh the preview, review the new names, and confirm again.\n${error.message}`
          : `Restart request ${id} failed; its outcome may be uncertain. Do not submit it again blindly. Refresh jobs and preview before deciding whether to retry.\n${this.missingController(error)}`);
      } finally { if (this.valid(context, operation)) this.setBusy(false); }
    }
    applyState(data) {
      if (!this.dialog.open || !Array.isArray(data?.restart_jobs)) return;
      const active = data.restart_jobs.find(job => job.id === this.job?.id) || data.restart_jobs.find(job => ACTIVE.has(job.status));
      if (active) { this.job = active; this.renderJob(); this.schedulePoll(); }
    }
    renderJob() {
      const section = this.query('#fleet-job-section'), list = this.query('#fleet-job-targets'); section.hidden = !this.job; list.replaceChildren();
      this.query('#fleet-restart-cancel').textContent = this.job ? 'Close' : 'Cancel';
      if (!this.job) return;
      this.query('#fleet-job-status').textContent = this.job.status || 'unknown';
      const counts = this.job.counts || {}; this.query('#fleet-job-counts').textContent = Object.entries(counts).map(([name, count]) => `${name} ${count}`).join(' · ');
      for (const target of this.job.targets || []) {
        const row = this.create('li', 'fleet-job-row'), info = this.create('div', 'fleet-job-info');
        info.append(this.create('strong', '', target.name || target.id), this.create('span', `fleet-target-state ${target.state || ''}`, target.state || 'unknown'), this.create('small', '', target.stage || 'Waiting'));
        if (target.error) info.append(this.create('p', 'fleet-target-error', target.error));
        const actions = this.create('div', 'fleet-job-actions');
        if (target.can_legacy) actions.append(actionButton('Legacy /quit…', 'small', () => this.agentAction('legacy', target)));
        if (target.can_force) actions.append(actionButton('Force restart…', 'small danger', () => this.agentAction('force', target)));
        for (const control of actions.children) control.disabled = this.busy || !this.canMutate();
        row.append(info, actions); list.append(row);
      }
      const cancellable = (this.job.targets || []).filter(target => target.can_cancel).map(target => target.id);
      const cancel = this.query('#fleet-job-cancel'); cancel.disabled = this.busy || !this.canMutate() || !cancellable.length; cancel.hidden = TERMINAL.has(this.job.status);
    }
    async agentAction(kind, target) {
      if (this.busy || !this.canMutate()) return;
      const warning = kind === 'legacy'
        ? `Legacy /quit ${target.name || target.id}?\n\nWARNING: This types /quit into the current terminal editor and presses Enter. Swarm cannot verify the editor is empty: an existing draft may be submitted instead of quitting. Check the editor is empty first. It may interrupt work or a tool call. No automatic work prompt is sent after relaunch.`
        : `Force restart ${target.name || target.id}?\n\nWARNING: This kills the Swarm-owned process and relaunches it promptlessly. In-progress work or tool calls may be interrupted; external effects cannot be rolled back. Already queued mail may be released.`;
      if (!this.confirm(warning)) return;
      await this.mutate(kind, [target.id]);
    }
    async cancelJob() {
      const ids = (this.job?.targets || []).filter(target => target.can_cancel).map(target => target.id);
      if (ids.length) await this.mutate('cancel', ids);
    }
    async mutate(kind, ids) {
      const context = this.context, operation = this.operation, jobId = this.job?.id;
      if (!context || !jobId || !this.valid(context, operation) || this.busy || !this.canMutate()) return;
      this.setBusy(true); this.setError('');
      try {
        const result = await this.api(`${context.base}/${encodeURIComponent(jobId)}/${kind}`, 'POST', {confirmed: true, agent_ids: ids});
        if (!this.valid(context, operation) || this.job?.id !== jobId) return;
        this.job = result.job; this.renderJob(); this.schedulePoll(); await this.onChange();
      } catch (error) {
        if (this.valid(context, operation)) this.setError(error?.status === 409 && kind === 'cancel'
          ? `Cancellation did not complete. Exit may already be committed, or cleanup needs attention. Refresh the job. ${error.message}`
          : `${kind === 'cancel' ? 'Cancel' : 'Restart action'} request failed; its outcome may be uncertain. Refresh the job before retrying.\n${this.missingController(error)}`);
      } finally { if (this.valid(context, operation)) this.setBusy(false); }
    }
    schedulePoll() {
      this.stopPolling();
      if (!this.dialog.open || !ACTIVE.has(this.job?.status)) return;
      const context = this.context, operation = this.operation, id = this.job.id;
      this.pollTimer = this.setTimer(async () => {
        this.pollTimer = null;
        try {
          const result = await this.api(`${context.base}/${encodeURIComponent(id)}`);
          if (!this.valid(context, operation) || this.job?.id !== id) return;
          this.job = result.job; this.renderJob(); await this.onChange(); this.schedulePoll();
        } catch (error) { if (this.valid(context, operation)) { this.setError(this.missingController(error)); this.schedulePoll(); } }
      }, 5000);
    }
    stopPolling() { if (this.pollTimer != null) this.clearTimer(this.pollTimer); this.pollTimer = null; }
  }
  const exports = {FleetControl, isActiveRestart, requestId};
  if (typeof module !== 'undefined' && module.exports) module.exports = exports;
  else root.SwarmFleetControl = exports;
})(typeof globalThis !== 'undefined' ? globalThis : this);
