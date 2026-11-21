/* Bulk archive/forget UI for a frozen selection. Remote values are rendered as text. */
(function (root) {
  'use strict';

  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const ACTIVE = new Set(['queued', 'running']);
  const ARCHIVE_ACK = new Set(['queued', 'running', 'completed']);

  function text(value, fallback = '') { return typeof value === 'string' ? value : fallback; }
  function validId(value) { return typeof value === 'string' && UUID.test(value); }
  function sourcePath(source, path) {
    return source === 'local' ? path : `/api/federation/${encodeURIComponent(source)}/proxy${path}`;
  }
  function requestId() {
    const create = root.SwarmAgentArchives?.secureRequestId;
    if (typeof create !== 'function') throw Error('Secure request IDs are unavailable');
    const value = create();
    if (!validId(value)) throw Error('Invalid secure request ID');
    return value;
  }
  function element(tag, className, value) {
    const node = root.document.createElement(tag); node.className = className || ''; node.textContent = value || ''; return node;
  }
  function restartActive(agent) {
    if (Object.prototype.hasOwnProperty.call(agent || {}, 'restart_job_id')) return !!agent.restart_job_id;
    return !!agent?.restart?.job_id && !['completed', 'cancelled', 'failed'].includes(agent.restart.state);
  }
  function busyReason(agent) {
    if (!agent || agent.missing === true) return 'No longer registered';
    if (agent.alive === true) return 'Stop this agent first';
    if (agent.stop_pending || agent.orchestration_stop) return 'Agent stop is still pending';
    if (agent.spawn_state != null && agent.spawn_state !== 'ready') return 'Setup is incomplete';
    if (restartActive(agent)) return 'Fleet restart is in progress';
    if (ACTIVE.has(agent?.agent_archive?.status)) return 'Archive job is already in progress';
    const maintenance = [agent.meta_compaction, agent.tool_input_compaction, agent.tool_output_repair, agent.context_archive];
    if (maintenance.some(job => job?.status === 'running')) return 'Saved-session maintenance is in progress';
    return '';
  }
  function archiveReason(agent) {
    const busy = busyReason(agent); if (busy) return busy;
    if (agent.managed !== true || agent.backend !== 'pi' || !text(agent.session_file)) return 'Archive requires a managed Pi session';
    return '';
  }
  function snapshotAgent(agent) {
    const result = {...agent};
    for (const key of ['agent_archive', 'meta_compaction', 'tool_input_compaction', 'tool_output_repair', 'context_archive', 'restart'])
      if (agent?.[key] && typeof agent[key] === 'object') result[key] = {...agent[key]};
    return result;
  }
  function folder(agent) {
    for (const key of ['working_folder', 'worktree_path', 'workdir', 'cwd', 'folder'])
      if (agent?.[key] != null) return agent[key];
    return null;
  }
  function sameIdentity(captured, current, kind) {
    if (!current || current.id !== captured.id || current.session_file !== captured.session_file || current.session_id !== captured.session_id) return false;
    const before = folder(captured), now = folder(current);
    return kind !== 'forget' || before == null || before === now;
  }

  class BulkAgentArchiveController {
    constructor({query = selector => root.document.querySelector(selector), api,
      currentSwarm = () => 'local', currentGeneration = () => 0, agents = () => [], selectedIDs = () => [],
      onChanged = async () => {}, confirm = value => root.confirm(value), toast = () => {}, requestTimeout = 20000,
      setTimer = (callback, delay) => root.setTimeout(callback, delay), clearTimer = timer => root.clearTimeout(timer)} = {}) {
      Object.assign(this, {query, api, currentSwarm, currentGeneration, agents, selectedIDs, onChanged, confirm, toast,
        requestTimeout, setTimer, clearTimer});
      this.openButton = query('#group-bulk-forget'); this.dialog = query('#bulk-forget-dialog');
      this.summary = query('#bulk-forget-summary'); this.targetsNode = query('#bulk-forget-targets');
      this.includeFiles = query('#bulk-forget-include-files'); this.archiveButton = query('#bulk-forget-archive');
      this.forgetButton = query('#bulk-forget-only'); this.cancelButton = query('#bulk-forget-cancel');
      this.resultsNode = query('#bulk-forget-results'); this.errorNode = query('#bulk-forget-error');
      this.context = null; this.busy = false; this.completed = false; this.token = 0;
      this.receipts = new Map();

      this.openButton?.addEventListener('click', () => this.open());
      this.archiveButton?.addEventListener('click', () => void this.run('archive'));
      this.forgetButton?.addEventListener('click', () => void this.run('forget'));
      this.cancelButton?.addEventListener('click', () => this.close());
      this.dialog?.addEventListener('cancel', event => { event.preventDefault(); if (!this.busy) this.close(); });
      this.dialog?.addEventListener('close', () => this.closed());
    }

    current(context = this.context) {
      return !!context && context.source === this.currentSwarm() && context.generation === this.currentGeneration();
    }
    active(token, context) {
      return token === this.token && this.context === context && !!this.dialog?.open && this.current(context);
    }
    setError(message) {
      if (!this.errorNode) return;
      this.errorNode.textContent = text(message); this.errorNode.hidden = !message;
    }
    selectedAgents() {
      const currentAgents = this.agents();
      const records = new Map((Array.isArray(currentAgents) ? currentAgents : []).filter(agent => validId(agent?.id)).map(agent => [agent.id, agent]));
      let values; try { values = [...this.selectedIDs()]; } catch { values = []; }
      return [...new Set(values)].filter(validId).sort().map(id => records.has(id) ? snapshotAgent(records.get(id)) : {id, name: id, missing: true});
    }
    open() {
      if (this.busy) return false;
      const source = this.currentSwarm(), generation = this.currentGeneration(), targets = this.selectedAgents();
      if (source !== this.currentSwarm() || generation !== this.currentGeneration()) return false;
      if (!targets.length) { this.toast('Select at least one registered agent family first.', true); return false; }
      this.token += 1;
      this.context = {source, generation, targets};
      this.completed = false; this.setError(''); this.resultsNode?.replaceChildren();
      if (this.includeFiles) { this.includeFiles.checked = false; this.includeFiles.disabled = false; }
      const archivable = targets.filter(agent => !archiveReason(agent)).length;
      const forgettable = targets.filter(agent => !busyReason(agent)).length;
      if (this.summary) this.summary.textContent = `${targets.length} selected agent${targets.length === 1 ? '' : 's'} · ${archivable} can be archived · ${forgettable} can be forgotten.`;
      if (this.targetsNode) {
        this.targetsNode.replaceChildren();
        for (const agent of targets) {
          const reason = archiveReason(agent);
          this.targetsNode.append(element('li', '', `${text(agent.name, agent.id)} — ${reason || 'ready to archive'}`));
        }
      }
      this.updateControls(); this.dialog?.showModal(); return true;
    }
    invalidate() { this.token += 1; this.busy = false; this.completed = false; }
    close() {
      this.invalidate(); this.context = null;
      if (this.dialog?.open) this.dialog.close();
      this.updateControls();
    }
    closed() {
      if (!this.context && !this.busy) return;
      this.invalidate(); this.context = null; this.updateControls();
    }
    sync() {
      if (!this.context || this.current(this.context)) { this.updateControls(); return false; }
      this.invalidate(); this.context = null;
      if (this.dialog?.open) this.dialog.close();
      this.updateControls(); return true;
    }
    updateControls() {
      const targets = this.context?.targets || [];
      const archivable = targets.some(agent => !archiveReason(agent));
      const forgettable = targets.some(agent => !busyReason(agent));
      if (this.archiveButton) this.archiveButton.disabled = this.busy || this.completed || !archivable;
      if (this.forgetButton) this.forgetButton.disabled = this.busy || this.completed || !forgettable;
      if (this.cancelButton) this.cancelButton.disabled = this.busy;
      if (this.includeFiles) this.includeFiles.disabled = this.busy || this.completed || !archivable;
    }
    receiptKey(context, agent, kind, includeFiles) {
      return JSON.stringify([context.source, agent.id, agent.session_file ?? null, agent.session_id ?? null, kind, kind === 'archive' && !!includeFiles]);
    }
    operationTargets(kind, context, includeFiles) {
      return context.targets.filter(agent => !(kind === 'archive' ? archiveReason(agent) : busyReason(agent)) &&
        !this.receipts.has(this.receiptKey(context, agent, kind, includeFiles)));
    }
    confirmation(kind, targets, skipped, includeFiles) {
      const names = targets.map(agent => text(agent.name, agent.id)).join(', ');
      const frozen = 'This list is frozen. Newly discovered children or other family members will not be included.';
      if (kind === 'archive') return `Archive and forget ${targets.length} selected agent${targets.length === 1 ? '' : 's'}?\n\n${names}\n\n${frozen}\n\nPrivate ZIP jobs will be queued${includeFiles ? ' with registered files and product snapshots' : ''}. Forgetting occurs only after each archive verifies successfully. No agent will be stopped automatically.${skipped ? ` ${skipped} ineligible or previously submitted selected agent${skipped === 1 ? '' : 's'} will be left unchanged.` : ''}`;
      return `Forget ${targets.length} selected agent${targets.length === 1 ? '' : 's'} without archives?\n\n${names}\n\n${frozen}\n\nOnly stopped registrations are eligible; this action does not send a stop request. Removing a registration may allow a remaining Swarm process to stop. Saved sessions, working folders and worktrees are kept. This cannot be undone from the dashboard.${skipped ? ` ${skipped} busy or previously submitted selected agent${skipped === 1 ? '' : 's'} will be left unchanged.` : ''}`;
    }
    appendResult(agent, message, failed = false) {
      if (!this.resultsNode) return;
      this.resultsNode.append(element('li', failed ? 'error' : '', `${text(agent.name, agent.id)} — ${message}`));
    }
    validArchiveResult(value, agent, id, includeFiles) {
      const job = value?.job;
      return !!job && job.id === id && validId(job.archive_id) && job.agent_id === agent.id &&
        ARCHIVE_ACK.has(job.status) && typeof job.include_files === 'boolean' && job.include_files === !!includeFiles;
    }
    currentRecord(id) {
      const values = this.agents();
      return (Array.isArray(values) ? values : []).find(agent => agent?.id === id) || null;
    }
    revalidate(agent, kind) {
      const current = this.currentRecord(agent.id);
      if (!sameIdentity(agent, current, kind)) return {reason: 'agent identity or saved session changed; review and retry'};
      const reason = kind === 'archive' ? archiveReason(current) : busyReason(current);
      return reason ? {reason} : {agent: current};
    }
    requestWithTimeout(...args) {
      return new Promise((resolve, reject) => {
        let settled = false, timer;
        const finish = (callback, value) => {
          if (settled) return; settled = true;
          if (timer !== undefined) this.clearTimer(timer);
          callback(value);
        };
        timer = this.setTimer(() => {
          const error = Error(`Request timed out after ${this.requestTimeout}ms`); error.timeout = true; finish(reject, error);
        }, this.requestTimeout);
        let pending;
        try { pending = this.api(...args); }
        catch (error) { finish(reject, error); return; }
        void (async () => {
          try { finish(resolve, await pending); }
          catch (error) { finish(reject, error); }
        })();
      });
    }
    uncertain(error) {
      const status = Number(error?.status);
      return error?.timeout === true || error?.noAcknowledgement === true || !Number.isFinite(status) || status >= 500;
    }
    errorMessage(error, fallback) { return text(error?.message, fallback); }

    async run(kind) {
      if (this.busy || this.completed || !this.dialog?.open || !this.current() || !['archive', 'forget'].includes(kind)) return false;
      const context = this.context;
      const includeFiles = kind === 'archive' && !!this.includeFiles?.checked;
      const targets = this.operationTargets(kind, context, includeFiles);
      if (!targets.length) return false;
      const skipped = context.targets.length - targets.length;
      if (!this.confirm(this.confirmation(kind, targets, skipped, includeFiles)) || !this.dialog?.open || !this.current(context)) return false;

      const ids = new Map();
      if (kind === 'archive') {
        try { for (const agent of targets) ids.set(agent.id, requestId()); }
        catch (error) { this.setError(this.errorMessage(error, 'Secure request IDs are unavailable.')); return false; }
      }

      const token = ++this.token;
      this.busy = true; this.setError(''); this.resultsNode?.replaceChildren(); this.updateControls();
      let succeeded = 0, failed = 0, attempted = 0, halted = false;
      for (const agent of context.targets) {
        if (!this.active(token, context)) break;
        const snapshotReason = kind === 'archive' ? archiveReason(agent) : busyReason(agent);
        const key = this.receiptKey(context, agent, kind, includeFiles);
        const prior = this.receipts.get(key);
        if (snapshotReason) { this.appendResult(agent, `left unchanged: ${snapshotReason}`); continue; }
        if (prior) { this.appendResult(agent, `left unchanged: prior ${prior.state} request${prior.requestId ? ` ${prior.requestId}` : ''}`); continue; }
        if (halted) { this.appendResult(agent, 'not sent after an earlier uncertain outcome', true); continue; }

        const checked = this.revalidate(agent, kind);
        if (checked.reason) { this.appendResult(agent, `left unchanged: ${checked.reason}`); continue; }
        attempted += 1;
        const receipt = {state: 'queued', requestId: kind === 'archive' ? ids.get(agent.id) : null};
        this.receipts.set(key, receipt);
        try {
          if (kind === 'archive') {
            const id = receipt.requestId;
            const path = sourcePath(context.source, `/api/agents/${encodeURIComponent(agent.id)}/archive-and-forget`);
            const value = await this.requestWithTimeout(path, 'POST', {include_files: includeFiles, request_id: id});
            if (!this.validArchiveResult(value, agent, id, includeFiles)) {
              const error = Error('Invalid archive acknowledgement'); error.noAcknowledgement = true; throw error;
            }
            receipt.state = 'queued'; succeeded += 1;
            if (this.active(token, context)) this.appendResult(agent, `archive queued (job ${id})`);
          } else {
            const path = sourcePath(context.source, `/api/agents/${encodeURIComponent(agent.id)}`);
            const value = await this.requestWithTimeout(path, 'DELETE');
            if (!value || value.id !== agent.id || value.forgotten !== true) {
              const error = Error('Invalid forget acknowledgement'); error.noAcknowledgement = true; throw error;
            }
            receipt.state = 'succeeded'; succeeded += 1;
            if (this.active(token, context)) this.appendResult(agent, 'forgotten; saved session kept');
          }
        } catch (error) {
          failed += 1;
          if (this.uncertain(error)) {
            receipt.state = 'uncertain'; halted = true;
            if (this.active(token, context)) this.appendResult(agent, `${this.errorMessage(error, `${kind} failed`)}; outcome uncertain${receipt.requestId ? ` (request ${receipt.requestId})` : ''}; do not retry blindly`, true);
          } else {
            this.receipts.delete(key);
            if (this.active(token, context)) this.appendResult(agent, this.errorMessage(error, `${kind} failed`), true);
          }
        }
      }
      if (!this.active(token, context)) {
        if (token === this.token && this.context === context) { this.busy = false; this.updateControls(); }
        return false;
      }
      this.busy = false; this.completed = true;
      if (failed) this.setError(`${failed} of ${attempted} request${attempted === 1 ? '' : 's'} failed${halted ? ' or had an uncertain outcome; remaining requests were not sent' : ''}. Successful requests were not rolled back.`);
      this.updateControls();
      if (succeeded) {
        const metadata = {refresh: true, noAutoWake: true, source: context.source, generation: context.generation};
        try { await this.onChanged(metadata); } catch { /* accepted mutations remain authoritative */ }
        if (this.active(token, context)) this.toast(kind === 'archive' ? `${succeeded} archive job${succeeded === 1 ? '' : 's'} queued.` : `${succeeded} agent${succeeded === 1 ? '' : 's'} forgotten.`);
      }
      return failed === 0 && succeeded === targets.length;
    }
  }

  const api = {BulkAgentArchiveController, sourcePath, busyReason, archiveReason};
  root.SwarmBulkAgentArchive = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
