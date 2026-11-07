/* Safe UI controller for deterministic, append-only Pi history archives. */
(function (root) {
  'use strict';

  const FINAL_STATUSES = new Set(['completed', 'unchanged', 'failed']);
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

  function restartRunning(agent) {
    if (!agent) return false;
    if (Object.prototype.hasOwnProperty.call(agent, 'restart_job_id')) return !!agent.restart_job_id;
    const restart = agent.restart;
    return !!restart?.job_id && !['completed', 'cancelled', 'failed'].includes(restart.state);
  }

  function unavailableReason(agent) {
    if (!agent) return 'This agent is no longer registered.';
    if (agent.managed !== true || agent.backend !== 'pi') return 'Only managed Pi agents can archive saved history.';
    if (typeof agent.session_file !== 'string' || !agent.session_file) return 'A persisted Pi session is required.';
    if (restartRunning(agent)) return 'Fleet restart is active. Wait for it to finish.';
    if (![undefined, null, 'ready', 'completed'].includes(agent.spawn_state)) return 'Agent setup or launch is still in progress.';
    if (agent.alive || agent.pid != null || agent.stop_pending || agent.orchestration_stop) return 'Stop first. History archive requires a stopped agent.';
    const jobs = [agent.meta_compaction, agent.tool_input_compaction, agent.tool_output_repair];
    if (jobs.some(job => job?.status === 'running')) return 'Another saved-session operation is running. Wait for it to finish.';
    if (agent.context_archive?.status === 'running') return 'History archive is already running. The agent will remain stopped.';
    return '';
  }

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

  function endpoint(source, id) {
    const path = `/api/agents/${encodeURIComponent(id)}/archive-history`;
    return source === 'local' ? path : `/api/federation/${encodeURIComponent(source)}/proxy${path}`;
  }

  function safeString(value, fallback = '') {
    return typeof value === 'string' ? value : fallback;
  }

  function safeNumber(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
  }

  class HistoryArchiveController {
    constructor({query = selector => root.document.querySelector(selector), api,
      currentSwarm = () => 'local', currentGeneration = () => 0, agent = () => null,
      onChanged = async () => {}, onBusyChange = () => {}, additionalReason = () => '', toast = () => {},
      confirm = text => root.confirm(text), requestId = secureRequestId,
      requestTimeout = 20000, setTimer = (callback, delay) => root.setTimeout(callback, delay),
      clearTimer = timer => root.clearTimeout(timer)} = {}) {
      Object.assign(this, {query, api, currentSwarm, currentGeneration, agent, onChanged,
        onBusyChange, additionalReason, toast, confirm, requestId, requestTimeout, setTimer, clearTimer});
      this.button = query('#archive-history');
      this.percent = query('#history-archive-percent');
      this.availability = query('#history-archive-availability');
      this.reportNode = query('#history-archive-report');
      this.status = query('#history-archive-status');
      this.state = query('#history-archive-state');
      this.result = query('#history-archive-result');
      this.error = query('#history-archive-error');
      this.file = query('#history-archive-file');
      this.original = query('#history-archive-original');
      this.fileRow = query('#history-archive-file-row');
      this.originalRow = query('#history-archive-original-row');
      this.requests = new Map();
      this.uncertain = new Map();
      this.button?.addEventListener('click', () => void this.start());
    }

    capture(agent, percent) {
      return {
        id: agent.id,
        sessionFile: agent.session_file,
        sessionId: agent.session_id ?? null,
        source: this.currentSwarm(),
        generation: this.currentGeneration(),
        percent,
      };
    }

    ownerKey(owner) {
      return `${owner.source}\u0000${owner.generation}\u0000${owner.id}\u0000${owner.sessionFile}\u0000${owner.sessionId ?? ''}`;
    }

    sameOwner(left, right) {
      return !!left && !!right && left.id === right.id && left.sessionFile === right.sessionFile &&
        left.sessionId === right.sessionId && left.source === right.source && left.generation === right.generation;
    }

    current(owner) {
      if (!owner || owner.source !== this.currentSwarm() || owner.generation !== this.currentGeneration()) return false;
      let current;
      try { current = this.agent(); } catch { return false; }
      if (!current) return false;
      return current.id === owner.id && current.session_file === owner.sessionFile &&
        (current.session_id ?? null) === owner.sessionId;
    }

    inFlight(agent = this.agent()) {
      if (!agent) return false;
      const request = this.requests.get(agent.id);
      return !!request && request.owner.source === this.currentSwarm() &&
        request.owner.generation === this.currentGeneration() && request.owner.sessionFile === agent.session_file &&
        request.owner.sessionId === (agent.session_id ?? null);
    }

    percentValue() {
      const raw = this.percent?.value;
      if ((typeof raw !== 'string' && typeof raw !== 'number') || typeof raw === 'boolean') return null;
      if (typeof raw === 'string' && !raw.trim()) return null;
      const value = Number(raw);
      return Number.isInteger(value) && value >= 1 && value <= 95 ? value : null;
    }

    setError(message) {
      if (!this.error) return;
      const text = safeString(message);
      this.error.textContent = text;
      this.error.hidden = !text;
    }

    renderReport(report) {
      const valid = report && typeof report === 'object' && !Array.isArray(report);
      if (this.reportNode) this.reportNode.hidden = !valid;
      if (this.status) this.status.hidden = !valid;
      if (!valid) {
        if (this.state) this.state.textContent = '';
        if (this.result) { this.result.textContent = ''; this.result.hidden = true; }
        this.setError('');
        if (this.file) this.file.textContent = '';
        if (this.original) this.original.textContent = '';
        if (this.fileRow) this.fileRow.hidden = true;
        if (this.originalRow) this.originalRow.hidden = true;
        return;
      }
      if (this.state) this.state.textContent = safeString(report.status, 'unknown');
      const before = safeNumber(report.context_before_bytes);
      const archived = safeNumber(report.archive_bytes);
      const actual = safeNumber(report.actual_percent);
      const hasResult = before !== null || archived !== null || actual !== null;
      if (this.result) {
        this.result.hidden = !hasResult;
        this.result.textContent = hasResult
          ? `Original context ${before ?? 'unknown'} serialized-content bytes; archived ${archived ?? 'unknown'} bytes; actual ${actual === null ? 'unknown' : actual.toFixed(1)}%.`
          : '';
      }
      this.setError(safeString(report.error));
      const historyFile = safeString(report.history_file);
      const original = safeString(report.archive);
      if (this.file) this.file.textContent = historyFile;
      if (this.original) this.original.textContent = original;
      if (this.fileRow) this.fileRow.hidden = !historyFile;
      if (this.originalRow) this.originalRow.hidden = !original;
    }

    update(agent = this.agent()) {
      const report = agent?.context_archive;
      if (report?.request_id && FINAL_STATUSES.has(report.status)) {
        const key = this.ownerKey(this.capture(agent, report.percent_requested));
        const uncertain = this.uncertain.get(key);
        if (uncertain?.requestId === report.request_id) this.uncertain.delete(key);
      }
      const active = this.inFlight(agent);
      const owner = agent ? this.capture(agent, this.percentValue()) : null;
      const uncertain = owner ? this.uncertain.get(this.ownerKey(owner)) : null;
      let reason = unavailableReason(agent) || this.additionalReason(agent);
      if (active) reason = 'Archiving saved history. The agent will remain stopped.';
      else if (uncertain) reason = `Archive outcome is uncertain. Inspect request ID ${uncertain.requestId} before retrying.`;
      if (this.availability) this.availability.textContent = reason || 'Available while this managed Pi agent is stopped.';
      if (this.button) {
        this.button.disabled = !!reason;
        this.button.textContent = active ? 'Archiving…' : 'Archive history (no LLM)…';
      }
      if (this.percent) this.percent.disabled = !!reason;
      this.renderReport(report);
      return reason;
    }

    validReport(report, request) {
      if (!report || typeof report !== 'object' || Array.isArray(report)) return false;
      if (typeof report.id !== 'string' || !report.id || !FINAL_STATUSES.has(report.status)) return false;
      if (report.archive_id !== undefined && report.archive_id !== report.id) return false;
      if (report.request_id !== request.requestId || report.percent_requested !== request.owner.percent) return false;
      if (report.source !== request.owner.sessionFile || typeof report.session_id !== 'string' || !report.session_id) return false;
      if (request.owner.sessionId !== null && report.session_id !== request.owner.sessionId) return false;
      return true;
    }

    requestWithTimeout(...args) {
      return new Promise((resolve, reject) => {
        let settled = false, timer;
        const finish = (callback, value) => {
          if (settled) return;
          settled = true;
          if (timer !== undefined) this.clearTimer(timer);
          callback(value);
        };
        timer = this.setTimer(() => {
          const error = Error(`Request timed out after ${this.requestTimeout}ms`); error.timeout = true;
          finish(reject, error);
        }, this.requestTimeout);
        Promise.resolve().then(() => this.api(...args)).then(
          value => finish(resolve, value), error => finish(reject, error));
      });
    }

    async start(selected = this.agent()) {
      const reason = unavailableReason(selected) || this.additionalReason(selected);
      if (reason) { this.update(selected); return null; }
      const percent = this.percentValue();
      if (percent === null) {
        this.setError('Use a whole percentage from 1 through 95.');
        this.percent?.reportValidity?.();
        return null;
      }
      const owner = this.capture(selected, percent);
      if (this.inFlight(selected)) return null;
      const prior = this.uncertain.get(this.ownerKey(owner));
      if (prior) {
        this.setError(`Archive outcome is uncertain. Inspect request ID ${prior.requestId} before retrying.`);
        this.update(selected);
        return prior.requestId;
      }
      const name = safeString(selected.name, String(selected.id));
      const warning = `Archive approximately ${percent}% of older model context for ${name}?\n\n` +
        'The percentage is measured from serialized model-content bytes, not tokens. Safe cuts keep whole user/tool batches; the actual share may be smaller or larger than selected. The newest user span and the complete raw source are preserved. Old prompts, images, thinking, tool calls, and outputs remain in the original JSON archive.\n\n' +
        'No LLM is called and no generated summary becomes history truth. Older history is retrieved explicitly from the bounded archive reader. The saved session is changed only by an append-only native checkpoint. The agent stays stopped until you manually wake it.\n\n' +
        `Session:\n${owner.sessionFile}`;
      if (!this.confirm(warning)) return null;
      let currentAgent;
      try { currentAgent = this.agent(); } catch { return null; }
      const after = currentAgent ? this.capture(currentAgent, percent) : null;
      if (!this.sameOwner(owner, after) || unavailableReason(currentAgent) || this.additionalReason(currentAgent)) {
        if (this.current(owner)) this.update(currentAgent);
        return null;
      }
      let requestId;
      try {
        requestId = this.requestId();
        if (typeof requestId !== 'string' || !UUID.test(requestId)) throw Error('Invalid request ID');
      } catch {
        if (this.current(owner)) this.setError('This browser cannot create a secure request ID. Use a recent browser or HTTPS.');
        return null;
      }
      const request = {owner, requestId, path: endpoint(owner.source, owner.id)};
      this.requests.set(owner.id, request);
      this.setError('');
      this.onBusyChange(true, request);
      if (this.current(owner)) this.update(currentAgent);
      try {
        const report = await this.requestWithTimeout(request.path, 'POST', {percent: owner.percent, request_id: requestId});
        if (!this.validReport(report, request)) throw Error('Invalid history-archive acknowledgement');
        if (!this.current(owner)) return requestId;
        this.renderReport(report);
        try { await this.onChanged(report); } catch { /* The acknowledged archive outcome remains authoritative. */ }
        if (!this.current(owner)) return requestId;
        const message = report.status === 'failed'
          ? safeString(report.error, 'History archive failed.')
          : report.status === 'unchanged' ? 'No safe older history needed archiving.'
            : `History archived without waking ${name}.`;
        this.toast(message, report.status === 'failed');
      } catch (error) {
        if (!this.current(owner)) return requestId;
        const uncertain = error?.timeout || !error?.status || error.status >= 500;
        let message = safeString(error?.message, 'History archive request failed.');
        if (error?.status === 404) message = 'The selected controller does not support history archive yet. Update that controller and try again.';
        else if (uncertain) {
          this.uncertain.set(this.ownerKey(owner), {requestId});
          message = `${message} Archive outcome is uncertain; do not retry blindly. Inspect request ID ${requestId} and the persisted report.`;
        }
        request.error = message;
        this.toast(message, true);
      } finally {
        if (this.requests.get(owner.id) === request) this.requests.delete(owner.id);
        let currentAgent = null;
        try { currentAgent = this.agent(); } catch { /* Treat an unavailable parent selection as not busy. */ }
        this.onBusyChange(this.inFlight(currentAgent), request);
        if (this.current(owner)) {
          this.update(currentAgent);
          if (request.error) this.setError(request.error);
        }
      }
      return requestId;
    }
  }

  const api = {HistoryArchiveController, unavailableReason};
  root.SwarmHistoryArchive = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
