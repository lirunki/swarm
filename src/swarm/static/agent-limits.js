/* Per-owning-server orchestration limit control. */
(function (root) {
  'use strict';

  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const validRevision = value => Number.isInteger(value) && value >= 0;
  const validLimit = value => Number.isInteger(value) && value >= 1 && value <= 10000;

  function validProjection(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value) &&
      validRevision(value.revision) && validLimit(value.max_active_managed) &&
      Number.isInteger(value.active_managed) && value.active_managed >= 0 &&
      value.scope === 'orchestration';
  }

  function copyProjection(value) {
    return {
      revision: value.revision,
      max_active_managed: value.max_active_managed,
      active_managed: value.active_managed,
      scope: value.scope,
    };
  }

  function defaultRequestId() {
    const value = root.crypto?.randomUUID?.();
    if (!uuidPattern.test(value || '')) throw Error('A secure UUID request ID is unavailable');
    return value;
  }

  class AgentLimitsController {
    constructor({query = selector => root.document.querySelector(selector), api,
      currentSwarm = () => 'local', currentGeneration = () => 0,
      ownerName = source => source === 'local' ? 'Local Swarm' : source,
      onChanged = async () => {}, onError = () => {}, requestTimeout = 20000,
      setTimer = (callback, delay) => root.setTimeout(callback, delay),
      clearTimer = timer => root.clearTimeout(timer), requestId = defaultRequestId,
      confirm = text => root.confirm(text)} = {}) {
      Object.assign(this, {query, api, currentSwarm, currentGeneration, ownerName, onChanged, onError,
        requestTimeout, setTimer, clearTimer, requestId, confirm});
      this.owner = query('#agent-limits-owner');
      this.count = query('#agent-limits-count');
      this.input = query('#agent-limits-max');
      this.button = query('#agent-limits-save');
      this.status = query('#agent-limits-status');
      this.errorNode = query('#agent-limits-error');
      this.source = currentSwarm(); this.generation = currentGeneration();
      this.settings = null; this.latestRevision = null;
      this.availabilityError = null; this.actionError = '';
      this.dirty = false; this.busy = false; this.mutationSent = false;
      this.operation = 0; this.pendingMutation = null; this.uncertain = null;
      this.input?.addEventListener('input', () => this.inputChanged());
      this.button?.addEventListener('click', () => void this.save());
      this.render();
    }

    endpoint(source) {
      return source === 'local' ? '/api/agent-limits' :
        `/api/federation/${encodeURIComponent(source)}/proxy/api/agent-limits`;
    }

    inContext(source, generation) {
      return source === this.currentSwarm() && generation === this.currentGeneration();
    }

    draft() {
      const raw = typeof this.input?.value === 'string' ? this.input.value.trim() : '';
      if (!/^(?:[1-9]\d*)$/.test(raw)) return null;
      const value = Number(raw);
      return validLimit(value) ? value : null;
    }

    inputChanged() {
      this.dirty = !!this.settings && this.input?.value !== String(this.settings.max_active_managed);
      this.render();
    }

    cancelOperation() {
      this.operation += 1; this.busy = false; this.mutationSent = false;
    }

    uncertainMessage(pending, detail) {
      return `Agent limit update outcome is uncertain. Do not retry blindly. Request ID ${pending.requestId}. ${detail}`;
    }

    setState(value, error = null) {
      const source = this.currentSwarm(), generation = this.currentGeneration();
      const contextChanged = source !== this.source || generation !== this.generation;
      const next = validProjection(value) ? copyProjection(value) : null;

      // State delivery and refreshes can race. A stale projection carries no fresh
      // evidence about either the value or an error delivered alongside it.
      if (!contextChanged && next && this.latestRevision !== null && next.revision < this.latestRevision) return;

      const invalid = value !== undefined && value !== null && !next;
      const unavailable = error || (invalid ? Error('Malformed agent limit projection') : null);
      const previous = this.settings;
      const authoritativeChanged = !!previous && !!next &&
        (previous.revision !== next.revision || previous.max_active_managed !== next.max_active_managed);

      // Once POST has been invoked, losing the projection cannot turn its outcome into
      // a definite failure. Preserve its identity before invalidating the operation so
      // the eventual HTTP result is harmless and only newer authority can resolve it.
      if (!contextChanged && (unavailable || !next) && this.busy && this.mutationSent && this.pendingMutation) {
        this.uncertain = {source, generation, ...this.pendingMutation};
        this.actionError = this.uncertainMessage(this.uncertain,
          unavailable ? String(unavailable.message || unavailable) : 'Authoritative agent limit state disappeared.');
        this.pendingMutation = null;
      }

      // A source change or loss of authoritative state owns a new operation token. An
      // old finally block can therefore never unlock controls belonging to the new state.
      if (contextChanged || unavailable || !next) this.cancelOperation();
      else if (authoritativeChanged && !(this.busy && this.mutationSent)) this.cancelOperation();

      if (contextChanged) {
        this.source = source; this.generation = generation; this.latestRevision = null;
        this.dirty = false; this.actionError = ''; this.pendingMutation = null; this.uncertain = null;
      }

      this.settings = next;
      this.availabilityError = unavailable;
      if (next) this.latestRevision = next.revision;

      if (next) {
        let resolvedUncertain = false;
        if (this.uncertain && !contextChanged && next.revision > this.uncertain.revision) {
          const pending = this.uncertain; this.uncertain = null; resolvedUncertain = true;
          if (next.max_active_managed === pending.desired) {
            this.dirty = false; this.actionError = '';
            if (this.input) this.input.value = String(next.max_active_managed);
          } else {
            this.actionError = `The request outcome is now known: the owning server kept limit ${next.max_active_managed}. Review and save again if needed.`;
          }
        }
        const draftMatchesAuthority = this.dirty && this.draft() === next.max_active_managed;
        if (contextChanged || !previous || !this.dirty || draftMatchesAuthority) {
          if (this.input) this.input.value = String(next.max_active_managed);
          this.dirty = false;
          if (draftMatchesAuthority && !this.uncertain) this.actionError = '';
        } else if (authoritativeChanged && !(this.busy && this.mutationSent) && !this.uncertain && !resolvedUncertain) {
          this.actionError = 'The agent limit changed while you were editing. Your draft was preserved; review it and save again.';
        }
      }
      this.render();
    }

    render() {
      if (this.owner) this.owner.textContent = `Owning server: ${this.ownerName(this.source)}`;
      if (this.count) this.count.textContent = this.settings
        ? `Active managed agents: ${this.settings.active_managed}` : 'Active managed agents: unavailable';
      const unsupported = !this.settings || !!this.availabilityError;
      const validDraft = this.draft() !== null;
      if (this.input) {
        this.input.min = '1'; this.input.max = '10000'; this.input.step = '1';
        this.input.disabled = unsupported || this.busy || !!this.uncertain;
      }
      if (this.button) {
        this.button.disabled = unsupported || this.busy || !!this.uncertain || !this.dirty || !validDraft;
        this.button.textContent = this.busy ? 'Saving…' : 'Save limit';
      }
      if (this.status) {
        if (this.availabilityError) this.status.textContent = 'Agent limit settings are unavailable.';
        else if (!this.settings) this.status.textContent = 'Agent limits are not supported by this server.';
        else if (this.uncertain) this.status.textContent = 'Waiting for a newer authoritative state before another save.';
        else this.status.textContent = 'This orchestration limit applies to active managed agents on the owning server. Changing it does not stop or wake agents.';
      }
      if (this.errorNode) {
        const availability = this.availabilityError ? String(this.availabilityError.message || this.availabilityError) : '';
        this.errorNode.textContent = [availability, this.actionError]
          .filter((message, index, messages) => message && messages.indexOf(message) === index).join(' ');
      }
    }

    reportError(message) {
      this.actionError = String(message); this.onError(this.actionError); this.render();
    }

    async bounded(path, method, body, message) {
      let timer;
      try {
        // api is invoked before this function yields, freezing the already captured path.
        const request = Promise.resolve(this.api(path, method, body));
        const timeout = new Promise((resolve, reject) => {
          timer = this.setTimer(() => reject(Error(message)), this.requestTimeout);
        });
        return await Promise.race([request, timeout]);
      } finally {
        if (timer !== undefined) this.clearTimer(timer);
      }
    }

    async changed(snapshot, value, metadata) {
      if (!this.inContext(snapshot.source, snapshot.generation) || snapshot.operation !== this.operation) return false;
      await this.onChanged(value, {...metadata, source: snapshot.source, generation: snapshot.generation});
      return this.inContext(snapshot.source, snapshot.generation) && snapshot.operation === this.operation;
    }

    async save() {
      if (this.busy || this.uncertain || !this.settings || this.availabilityError || !this.dirty) return false;
      const desired = this.draft();
      if (desired === null || desired === this.settings.max_active_managed) { this.render(); return false; }
      const source = this.currentSwarm(), generation = this.currentGeneration();
      if (!this.inContext(source, generation)) return false;
      const known = copyProjection(this.settings), path = this.endpoint(source);
      const snapshot = {source, generation, known, desired, operation: ++this.operation};
      this.busy = true; this.actionError = ''; this.render();
      let sent = false, id = null;
      try {
        const fresh = await this.bounded(path, undefined, undefined,
          `Agent limit request timed out after ${this.requestTimeout}ms`);
        if (!this.inContext(source, generation) || snapshot.operation !== this.operation) return false;
        if (!validProjection(fresh)) throw Error('Owning server returned a malformed agent limit projection');
        if (fresh.revision !== known.revision || fresh.max_active_managed !== known.max_active_managed) {
          this.settings = copyProjection(fresh); this.latestRevision = fresh.revision;
          this.actionError = 'The agent limit changed. Your draft was preserved; review it and save again.';
          this.onError(this.actionError); this.render();
          await this.changed(snapshot, copyProjection(fresh), {refresh: true});
          return false;
        }
        if (desired < fresh.active_managed) {
          const warning = `Reduce the maximum active managed agents to ${desired} while ${fresh.active_managed} are active? This does not stop or wake any agents; it only blocks additional orchestration starts.`;
          if (!(await this.confirm(warning))) return false;
          if (!this.inContext(source, generation) || snapshot.operation !== this.operation) return false;
        }
        id = this.requestId();
        if (typeof id !== 'string' || !uuidPattern.test(id)) throw Error('Invalid request ID');
        if (!this.inContext(source, generation) || snapshot.operation !== this.operation) return false;
        const body = {max_active_managed: desired, revision: fresh.revision, request_id: id};
        sent = true; this.mutationSent = true;
        this.pendingMutation = {requestId: id, desired, revision: fresh.revision};
        const response = await this.bounded(path, 'POST', body,
          `Agent limit update timed out after ${this.requestTimeout}ms`);
        if (!this.inContext(source, generation) || snapshot.operation !== this.operation) return false;
        if (!validProjection(response) || response.max_active_managed !== desired || response.revision < fresh.revision + 1)
          throw Error('Owning server returned an invalid agent limit update acknowledgement');

        let accepted = copyProjection(response);
        if (this.settings && this.settings.revision > accepted.revision) accepted = copyProjection(this.settings);
        if (accepted.max_active_managed !== desired) {
          this.settings = accepted; this.latestRevision = accepted.revision; this.pendingMutation = null;
          this.actionError = 'The agent limit changed again after this save. Review the current value before saving.';
          this.onError(this.actionError); this.render(); return false;
        }
        this.settings = accepted; this.latestRevision = accepted.revision;
        this.pendingMutation = null; this.dirty = false; this.actionError = '';
        if (this.input) this.input.value = String(desired);
        this.render();
        await this.changed(snapshot, accepted, {operation: 'set-limit'});
        return true;
      } catch (error) {
        if (!this.inContext(source, generation) || snapshot.operation !== this.operation) return false;
        const status = Number.isInteger(error?.status) ? error.status : null;
        if (!sent) {
          this.reportError(`Cannot load current agent limits: ${error?.message || error}`);
        } else if (status !== null && status >= 400 && status < 500) {
          this.pendingMutation = null;
          this.reportError(error?.message || `Agent limit update failed (${status}).`);
          if (status === 409) await this.changed(snapshot, null, {refresh: true, conflict: true});
        } else {
          const pending = this.pendingMutation || {requestId: id, desired, revision: known.revision};
          this.pendingMutation = null;
          this.uncertain = {source, generation, ...pending};
          this.reportError(this.uncertainMessage(this.uncertain, error?.message || error));
        }
        return false;
      } finally {
        if (snapshot.operation === this.operation && this.inContext(source, generation)) {
          this.busy = false; this.mutationSent = false; this.render();
        }
      }
    }
  }

  const api = {AgentLimitsController, validProjection};
  root.SwarmAgentLimits = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
