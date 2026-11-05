/* Browser-local grouping storage and the explicit grouping-mode switch. */
(function (root) {
  'use strict';

  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const validUUID = value => typeof value === 'string' && uuidPattern.test(value);
  const validId = value => typeof value === 'string' && value.length > 0;
  const revision = value => Number.isInteger(value) && value >= 0;
  const clone = value => JSON.parse(JSON.stringify(value));
  const MAX_GROUPS = 100, MAX_ASSIGNMENTS = 10000, MAX_RECEIPTS = 256;
  const MAX_STORAGE_BYTES = 1024 * 1024, MAX_RECEIPT_PAYLOAD_BYTES = 128 * 1024;

  function utf8Bytes(value) {
    let bytes = 0;
    for (const character of value) {
      const point = character.codePointAt(0);
      bytes += point <= 0x7f ? 1 : point <= 0x7ff ? 2 : point <= 0xffff ? 3 : 4;
    }
    return bytes;
  }

  function nameValue(value) {
    const name = typeof value === 'string' ? value.trim() : '';
    const folded = name.normalize?.('NFKC').toLowerCase() || name.toLowerCase();
    if (!name || name.length > 80 || /\p{C}/u.test(name) || new Set(['defaultgroup', 'default group', 'default', 'ungrouped']).has(folded)) return null;
    return name;
  }

  function newUUID() {
    let value;
    if (root.crypto?.randomUUID) value = root.crypto.randomUUID();
    else {
      if (!root.crypto?.getRandomValues) throw Error('Secure random values are unavailable');
      const bytes = new Uint8Array(16); root.crypto.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const hex = [...bytes].map(item => item.toString(16).padStart(2, '0')).join('');
      value = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
    if (!validUUID(value)) throw Error('Invalid secure request ID');
    return value;
  }

  function projection(value, label = 'grouping projection') {
    const item = value?.agent_groups || value;
    if (!item || typeof item !== 'object' || !revision(item.revision) || !Array.isArray(item.groups) || item.groups.length > MAX_GROUPS ||
        !item.assignments || typeof item.assignments !== 'object' || Array.isArray(item.assignments) || Object.keys(item.assignments).length > MAX_ASSIGNMENTS) {
      throw Error(`Invalid ${label}`);
    }
    const groups = [], ids = new Set(), names = new Set();
    for (const group of item.groups) {
      const name = nameValue(group?.name), id = group?.id;
      if (!group || Object.keys(group).some(key => key !== 'id' && key !== 'name') || !validUUID(id) || !name || ids.has(id) || names.has(name.toLocaleLowerCase()))
        throw Error(`Invalid ${label}`);
      ids.add(id); names.add(name.toLocaleLowerCase()); groups.push({id, name});
    }
    const assignments = {};
    for (const [agentId, groupId] of Object.entries(item.assignments)) {
      // Agent fixtures historically use short IDs; groups are durable UUID identities.
      if (!validId(agentId) || !ids.has(groupId)) throw Error(`Invalid ${label}`);
      assignments[agentId] = groupId;
    }
    return {revision: item.revision, groups, assignments};
  }

  function knownAgents(agents) {
    if (!Array.isArray(agents)) throw TypeError('agents must be an array');
    const result = [];
    for (const agent of agents) if (agent && validId(agent.id) && !result.some(item => item.id === agent.id)) result.push(agent);
    return result;
  }

  function reconciled(value, agents, recordsValid = true) {
    if (!recordsValid) return clone(value);
    const records = knownAgents(agents), helper = root.SwarmAgentGroups?.effectiveAssignments;
    if (typeof helper !== 'function') throw Error('SwarmAgentGroups.effectiveAssignments is required');
    const effective = helper(records, value), assignments = {};
    for (const agent of records) if (validId(effective[agent.id])) assignments[agent.id] = effective[agent.id];
    if (Object.keys(assignments).length > MAX_ASSIGNMENTS) throw Error('Browser group assignments exceed the limit');
    return {revision: value.revision, groups: clone(value.groups), assignments};
  }

  function stable(value) {
    if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
    if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
    return JSON.stringify(value);
  }

  function conflict(message) { const error = Error(message); error.status = 409; return error; }

  class BrowserGroups {
    constructor(options = () => root.localStorage) {
      if (typeof options === 'function') {
        this.storageProvider = options;
        this.locksProvider = () => root.navigator?.locks;
        this.lockTimeout = 20000;
      } else {
        this.storageProvider = options.storage || (() => root.localStorage);
        this.locksProvider = typeof options.locks === 'function' ? options.locks : () => options.locks || root.navigator?.locks;
        this.lockTimeout = options.lockTimeout ?? 20000;
      }
    }

    locks() {
      const value = this.locksProvider();
      if (!value || typeof value.request !== 'function') throw Error('Browser-local grouping needs Web Locks; enable server-managed grouping or use HTTPS/recent browser');
      return value;
    }

    async locked(key, callback) {
      if (!validId(key)) throw Error('A scoped browser grouping key is required');
      const locks = this.locks(), controller = typeof root.AbortController === 'function' ? new root.AbortController() : null;
      let timer, expired = false;
      const timeout = new Promise((resolve, reject) => {
        timer = root.setTimeout(() => {
          expired = true; controller?.abort();
          reject(Error(`Browser grouping lock timed out after ${this.lockTimeout}ms`));
        }, this.lockTimeout);
      });
      try {
        const request = Promise.resolve(locks.request(`swarm-browser-groups:${key}`,
          {mode: 'exclusive', ...(controller ? {signal: controller.signal} : {})},
          () => {
            if (expired) throw Error(`Browser grouping lock timed out after ${this.lockTimeout}ms`);
            return callback();
          }));
        return await Promise.race([request, timeout]);
      } finally {
        if (timer !== undefined) root.clearTimeout(timer);
      }
    }

    storage() {
      const value = this.storageProvider();
      if (!value || typeof value.getItem !== 'function' || typeof value.setItem !== 'function') throw Error('Browser storage is unavailable');
      return value;
    }

    decode(raw) {
      if (typeof raw !== 'string' || utf8Bytes(raw) > MAX_STORAGE_BYTES) throw Error('Saved browser groups could not be read');
      let value;
      try { value = JSON.parse(raw); } catch { throw Error('Saved browser groups could not be read'); }
      const allowed = new Set(['version', 'revision', 'groups', 'assignments', 'receipts']);
      if (!value || typeof value !== 'object' || value.version !== 1 || Object.keys(value).some(key => !allowed.has(key)) ||
          !Array.isArray(value.receipts || []) || (value.receipts || []).length > MAX_RECEIPTS)
        throw Error('Saved browser groups could not be read');
      const clean = projection(value, 'saved browser groups');
      const receipts = [];
      for (const item of value.receipts || []) {
        if (!item || !validUUID(item.request_id) || typeof item.payload !== 'string' || utf8Bytes(item.payload) > MAX_RECEIPT_PAYLOAD_BYTES || !item.operation ||
            !['create', 'assign', 'rename', 'delete'].includes(item.operation.action) ||
            (item.operation.group_id !== null && !validUUID(item.operation.group_id)) || receipts.some(old => old.request_id === item.request_id))
          throw Error('Saved browser groups could not be read');
        receipts.push({request_id: item.request_id, payload: item.payload, operation: {action: item.operation.action, group_id: item.operation.group_id}});
      }
      return {...clean, version: 1, receipts};
    }

    encode(value) {
      const encoded = JSON.stringify({version: 1, revision: value.revision, groups: value.groups, assignments: value.assignments, receipts: value.receipts || []});
      if (utf8Bytes(encoded) > MAX_STORAGE_BYTES) throw Error('Saved browser groups exceed the 1 MiB storage limit');
      return encoded;
    }

    fitReceipts(value) {
      const next = {...value, receipts: [...(value.receipts || [])].slice(-MAX_RECEIPTS)};
      while (next.receipts.length > 1) {
        try { this.encode(next); return next; }
        catch (error) {
          if (!/1 MiB storage limit/.test(error.message)) throw error;
          next.receipts.shift();
        }
      }
      this.encode(next); // The latest receipt and actual grouping state are never truncated.
      return next;
    }

    public(value, operation) { return {...projection(value), ...(operation ? {operation: clone(operation)} : {})}; }

    initialize(key, seed, agents, {recordsValid = true} = {}) {
      if (!validId(key)) throw Error('A scoped browser grouping key is required');
      const storage = this.storage();
      if (storage.getItem(key) !== null) return this.load(key, seed, agents, {recordsValid});
      if (!recordsValid) throw conflict('Agent records are incomplete; refresh before initializing browser groups');
      const state = {...reconciled(projection(seed, 'browser grouping seed'), agents), version: 1, receipts: []};
      const encoded = this.encode(state);
      storage.setItem(key, encoded);
      return this.public(state);
    }

    load(key, seed, agents, options = {}) {
      if (!validId(key)) throw Error('A scoped browser grouping key is required');
      const storage = this.storage(), raw = storage.getItem(key);
      if (raw === null) return reconciled(projection(seed, 'browser grouping seed'), agents, options.recordsValid !== false);
      const value = this.decode(raw), settled = reconciled(value, agents, options.recordsValid !== false);
      if (stable(value.assignments) !== stable(settled.assignments)) {
        if (options.readOnly === true) return this.public({...value, assignments: settled.assignments});
        const next = this.fitReceipts({...value, revision: value.revision + 1, assignments: settled.assignments});
        storage.setItem(key, this.encode(next)); return this.public(next);
      }
      return this.public(value);
    }

    readSnapshot(key, seed, agents, options = {}) {
      return this.load(key, seed, agents, {...options, readOnly: true});
    }

    initializeLocked(key, seed, agents, options = {}) {
      return this.locked(key, () => this.initialize(key, seed, agents, options));
    }

    loadLocked(key, seed, agents, options = {}) {
      return this.locked(key, () => this.load(key, seed, agents, options));
    }

    applyLocked(key, body, agents, seed, options = {}) {
      return this.locked(key, () => this.apply(key, body, agents, seed, options));
    }

    apply(key, body, agents, seed, options = {}) {
      if (!validId(key)) throw Error('A scoped browser grouping key is required');
      if (!body || typeof body !== 'object' || !['create', 'assign', 'rename', 'delete'].includes(body.action) ||
          !revision(body.revision) || !validUUID(body.request_id)) throw Error('Invalid browser grouping request');
      const storage = this.storage(), raw = storage.getItem(key);
      let state = raw === null
        ? {...projection(seed, 'browser grouping seed'), version: 1, receipts: []}
        : this.decode(raw);
      const payload = stable(body);
      if (utf8Bytes(payload) > MAX_RECEIPT_PAYLOAD_BYTES) throw Error('Browser grouping request exceeds the 128 KiB receipt limit');
      const receipt = state.receipts.find(item => item.request_id === body.request_id);
      if (receipt) {
        if (receipt.payload !== payload) throw conflict('request_id was already used for a different operation');
        return this.public(state, receipt.operation);
      }
      if (options.recordsValid === false) throw conflict('Agent records are incomplete; refresh before changing browser groups');
      state = {...state, ...reconciled(state, agents), receipts: state.receipts || []};
      if (body.revision !== state.revision) throw conflict('Group revision changed; refresh and retry');
      const records = knownAgents(agents), known = new Set(records.map(agent => agent.id));
      const familyHelper = root.SwarmAgentGroups?.familyIndex;
      if (typeof familyHelper !== 'function') throw Error('SwarmAgentGroups.familyIndex is required');
      const families = familyHelper(records);
      const fields = {
        create: {required: ['action', 'revision', 'request_id', 'name'], optional: ['agent_ids']},
        assign: {required: ['action', 'revision', 'request_id', 'group_id', 'agent_ids'], optional: []},
        rename: {required: ['action', 'revision', 'request_id', 'group_id', 'name'], optional: []},
        delete: {required: ['action', 'revision', 'request_id', 'group_id'], optional: []},
      }[body.action];
      const allowedFields = new Set([...fields.required, ...fields.optional]), keys = Object.keys(body);
      if (keys.some(key => !allowedFields.has(key)) || fields.required.some(key => !own(body, key))) throw Error(`Invalid fields for ${body.action}`);
      const operation = {action: body.action, group_id: body.group_id || null};
      const groups = state.groups.map(group => ({...group})); let assignments = {...state.assignments};
      const group = id => groups.find(item => item.id === id);
      const selected = () => {
        const ids = body.agent_ids === undefined && body.action === 'create' ? [] : body.agent_ids;
        if (!Array.isArray(ids) || new Set(ids).size !== ids.length || ids.some(id => !known.has(id)))
          throw Error('agent_ids must contain distinct known agents');
        const expanded = new Set();
        for (const id of ids) for (const member of families.get(id) || [id]) expanded.add(member);
        return [...expanded];
      };
      if (body.action === 'create') {
        const name = nameValue(body.name); if (!name) throw Error('Invalid group name');
        if (groups.length >= MAX_GROUPS || groups.some(item => item.name.toLocaleLowerCase() === name.toLocaleLowerCase())) throw Error('Group names must be unique and within the group limit');
        operation.group_id = newUUID(); groups.push({id: operation.group_id, name});
        for (const id of selected()) assignments[id] = operation.group_id;
      } else if (body.action === 'assign') {
        if (body.group_id !== null && (!validUUID(body.group_id) || !group(body.group_id))) throw Error('assign requires an existing group_id or null');
        for (const id of selected()) body.group_id === null ? delete assignments[id] : assignments[id] = body.group_id;
      } else if (body.action === 'rename') {
        if (!validUUID(body.group_id) || !group(body.group_id)) throw Error('Unknown group');
        const name = nameValue(body.name); if (!name || groups.some(item => item.id !== body.group_id && item.name.toLocaleLowerCase() === name.toLocaleLowerCase())) throw Error('Invalid or duplicate group name');
        group(body.group_id).name = name;
      } else {
        if (!validUUID(body.group_id) || !group(body.group_id)) throw Error('Unknown group');
        groups.splice(groups.findIndex(item => item.id === body.group_id), 1);
        assignments = Object.fromEntries(Object.entries(assignments).filter(([, value]) => value !== body.group_id));
      }
      let next = reconciled({revision: state.revision + 1, groups, assignments}, records);
      next = this.fitReceipts({...next, version: 1, receipts: [...state.receipts, {request_id: body.request_id, payload, operation}]});
      storage.setItem(key, this.encode(next));
      return this.public(next, operation);
    }
  }

  function safeBrowserGroups(options) { return new BrowserGroups(options); }

  function validSettings(value) {
    return !!value && typeof value === 'object' && revision(value.revision) && typeof value.server_managed === 'boolean' &&
      typeof value.initialized === 'boolean' && Array.isArray(value.grouped_branches) && value.grouped_branches.length <= MAX_ASSIGNMENTS &&
      value.grouped_branches.every(validUUID) && new Set(value.grouped_branches).size === value.grouped_branches.length;
  }

  class GroupingModeController {
    constructor({query = selector => root.document.querySelector(selector), api, currentSwarm = () => 'local', currentGeneration = () => 0,
      ownerName = source => source === 'local' ? 'Local Swarm' : source, projection = () => null, branches = () => [], primeSnapshot = null,
      onChanged = async () => {}, onError = () => {}, confirm = text => root.confirm(text), requestTimeout = 20000,
      schedule = (callback, delay) => root.setTimeout(callback, delay), cancel = timer => root.clearTimeout(timer)} = {}) {
      Object.assign(this, {query, api, currentSwarm, currentGeneration, ownerName, projection, branches, primeSnapshot, onChanged, onError, confirm, requestTimeout, schedule, cancel});
      this.owner = query('#grouping-mode-owner'); this.checkbox = query('#grouping-server-managed');
      this.status = query('#grouping-mode-status'); this.button = query('#grouping-mode-change');
      this.dialog = query('#grouping-prime-dialog'); this.primeOwner = query('#grouping-prime-owner'); this.summary = query('#grouping-prime-summary');
      this.seedButton = query('#grouping-prime-current'); this.keepButton = query('#grouping-prime-server'); this.cancelButton = query('#grouping-prime-cancel');
      this.settings = null; this.error = null; this.warning = null; this.source = currentSwarm(); this.generation = currentGeneration();
      this.operation = 0; this.busy = false; this.mutationSent = false; this.mutationOperation = null;
      this.paintSignature = null; this.promptResolve = null;
      this.button?.addEventListener('click', () => void this.changeMode());
      this.checkbox?.addEventListener('change', () => { this.dirty = true; });
      this.seedButton?.addEventListener('click', () => this.resolvePrompt('seed'));
      this.keepButton?.addEventListener('click', () => this.resolvePrompt('keep'));
      this.cancelButton?.addEventListener('click', () => this.resolvePrompt(null));
      this.dialog?.querySelectorAll?.('[data-close]')?.forEach(node => node.addEventListener('click', () => this.resolvePrompt(null)));
      this.dialog?.addEventListener?.('cancel', event => { event.preventDefault?.(); this.resolvePrompt(null); });
      this.dialog?.addEventListener?.('close', () => this.resolvePrompt(null, false));
      this.render();
    }

    context(source = this.source, generation = this.generation) { return source === this.currentSwarm() && generation === this.currentGeneration(); }
    endpoint(source) { return source === 'local' ? '/api/grouping-settings' : `/api/federation/${encodeURIComponent(source)}/proxy/api/grouping-settings`; }

    setState(settings, error = null) {
      const source = this.currentSwarm(), generation = this.currentGeneration(), changedContext = source !== this.source || generation !== this.generation;
      const previous = this.settings, next = validSettings(settings) ? clone(settings) : null;
      const authoritativeChanged = !!previous && !!next && (previous.revision !== next.revision || previous.server_managed !== next.server_managed || previous.initialized !== next.initialized);
      const pendingAcknowledgement = !changedContext && this.busy && this.mutationSent && this.mutationOperation === this.operation;
      if (changedContext || (authoritativeChanged && !pendingAcknowledgement)) {
        const promptWasOpen = !!this.promptResolve;
        this.operation += 1; this.busy = false;
        this.resolvePrompt(null);
        if (!changedContext && promptWasOpen) this.warning = 'Grouping settings changed while the dialog was open. Review the current mode before applying.';
      }
      if (changedContext) this.warning = null;
      this.source = source; this.generation = generation; this.settings = next;
      this.error = error || (settings !== undefined && settings !== null && !next ? Error('Malformed grouping settings') : null);
      const signature = next ? `${source}:${generation}:${next.revision}:${next.server_managed}` : null;
      if (this.checkbox && (!this.dirty || signature !== this.paintSignature)) {
        this.checkbox.checked = !!next?.server_managed; this.dirty = false;
      }
      this.paintSignature = signature; this.render();
    }

    render() {
      if (this.owner) this.owner.textContent = `Owning server: ${this.ownerName(this.source)}`;
      const unsupported = !this.settings || !!this.error;
      if (this.checkbox) this.checkbox.disabled = unsupported || this.busy;
      if (this.button) { this.button.disabled = unsupported || this.busy; this.button.textContent = this.busy ? 'Applying…' : 'Apply grouping mode'; }
      if (this.status) {
        if (this.error) this.status.textContent = `Grouping mode unavailable: ${this.error.message || this.error}`;
        else if (!this.settings) this.status.textContent = 'Grouping mode is not supported by this server.';
        else if (this.warning) this.status.textContent = this.warning;
        else this.status.textContent = this.settings.server_managed
          ? `Server-managed grouping is on for ${this.ownerName(this.source)}.`
          : 'Browser-local grouping is on. The server’s saved grouping is preserved.';
      }
    }

    resolvePrompt(choice, close = true) {
      const resolve = this.promptResolve; if (!resolve || (choice === 'seed' && this.seedButton?.disabled)) return;
      this.promptResolve = null;
      if (close && this.dialog?.open) this.dialog.close();
      resolve(choice);
    }

    primePrompt(snapshot) {
      this.query('#notifications-dialog')?.close?.();
      if (this.primeOwner) this.primeOwner.textContent = `Owning server: ${this.ownerName(snapshot.source)}`;
      if (this.summary) this.summary.textContent = snapshot.seed
        ? `Prime with exactly ${snapshot.seed.groups.length} current UI group${snapshot.seed.groups.length === 1 ? '' : 's'} and ${Object.keys(snapshot.seed.assignments).length} agent assignment${Object.keys(snapshot.seed.assignments).length === 1 ? '' : 's'}. This replaces the owning server’s saved grouping on its first enable only.`
        : `The browser grouping source is unavailable${snapshot.seedError ? `: ${snapshot.seedError.message || snapshot.seedError}` : '.'} You can still keep the saved server grouping.`;
      const title = this.query('#grouping-prime-title') || this.dialog?.querySelector?.('h2'); if (title) title.textContent = 'Start server-managed grouping';
      if (this.seedButton) {
        this.seedButton.textContent = 'Prime from this browser — replace owning server saved grouping';
        this.seedButton.disabled = !snapshot.seed;
      }
      if (this.keepButton) this.keepButton.textContent = 'Keep saved server grouping';
      if (!this.dialog?.showModal) return Promise.resolve(null);
      this.dialog.showModal();
      return new Promise(resolve => { this.promptResolve = resolve; });
    }

    snapshotPriming(source = this.currentSwarm(), generation = this.currentGeneration()) {
      const value = projection(this.projection(), 'current grouping projection');
      const grouped = this.branches();
      if (!Array.isArray(grouped) || grouped.length > MAX_ASSIGNMENTS || grouped.some(id => !validUUID(id)) || new Set(grouped).size !== grouped.length)
        throw Error('Current grouped branches are not safe to seed');
      if (Object.keys(value.assignments).some(id => !validUUID(id))) throw Error('Current group assignments are not safe to seed');
      return {source, generation, seed: {groups: clone(value.groups), assignments: clone(value.assignments), grouped_branches: [...grouped]}};
    }

    validatePrimeSnapshot(value, source, generation) {
      const candidate = value?.seed || value;
      const clean = projection({revision: 0, groups: candidate?.groups, assignments: candidate?.assignments}, 'browser grouping source');
      const grouped = candidate?.grouped_branches;
      if (!Array.isArray(grouped) || grouped.length > MAX_ASSIGNMENTS || grouped.some(id => !validUUID(id)) || new Set(grouped).size !== grouped.length ||
          Object.keys(clean.assignments).some(id => !validUUID(id))) throw Error('Current browser grouping is not safe to seed');
      return {source, generation, seed: {groups: clean.groups, assignments: clean.assignments, grouped_branches: [...grouped]},
        revision: value?.revision, token: value?.token};
    }

    async capturePrimeSnapshot(source, generation) {
      try {
        const value = this.primeSnapshot
          ? await this.primeSnapshot(source, generation)
          : this.snapshotPriming(source, generation);
        return this.validatePrimeSnapshot(value, source, generation);
      } catch (seedError) {
        return {source, generation, seed: null, seedError};
      }
    }

    async guardedChanged(snapshot, value, metadata) {
      if (!this.context(snapshot.source, snapshot.generation) || snapshot.operation !== this.operation) return false;
      await this.onChanged(value, {...metadata, source: snapshot.source, generation: snapshot.generation});
      return this.context(snapshot.source, snapshot.generation) && snapshot.operation === this.operation;
    }

    async boundedRequest(path, method, body, timeoutMessage) {
      let timer;
      try {
        const request = Promise.resolve(this.api(path, method, body));
        const timeout = new Promise((resolve, reject) => {
          timer = this.schedule(() => reject(Error(timeoutMessage)), this.requestTimeout);
        });
        return await Promise.race([request, timeout]);
      } finally {
        if (timer !== undefined) this.cancel(timer);
      }
    }

    async changeMode() {
      if (this.busy || !this.settings || this.error) return false;
      const desired = !!this.checkbox?.checked, known = clone(this.settings), source = this.currentSwarm(), generation = this.currentGeneration();
      const snapshot = {source, generation, known, desired, operation: ++this.operation};
      this.warning = null;
      let seedSnapshot;
      this.busy = true; this.render();
      let requestId = null, mutationSent = false;
      try {
        const fresh = await this.boundedRequest(this.endpoint(source), undefined, undefined,
          `Grouping settings request timed out after ${this.requestTimeout}ms`);
        if (!this.context(source, generation) || snapshot.operation !== this.operation) return false;
        if (!validSettings(fresh)) throw Error('Owning server returned malformed grouping settings');
        if (fresh.revision !== known.revision || fresh.server_managed !== known.server_managed || fresh.initialized !== known.initialized) {
          this.onError('Grouping settings changed. Review the refreshed mode before applying.');
          await this.guardedChanged(snapshot, fresh, {refresh: true});
          return false;
        }
        let choice = null;
        if (desired && !fresh.initialized) {
          seedSnapshot = await this.capturePrimeSnapshot(source, generation);
          if (!this.context(source, generation) || snapshot.operation !== this.operation) return false;
          choice = await this.primePrompt(seedSnapshot);
          if (!this.context(source, generation) || snapshot.operation !== this.operation || choice === null) return false;
          if (choice === 'seed') {
            const currentSeed = await this.capturePrimeSnapshot(source, generation);
            if (!this.context(source, generation) || snapshot.operation !== this.operation) return false;
            if (!currentSeed.seed || stable(currentSeed) !== stable(seedSnapshot)) {
              this.onError('Source grouping changed while the dialog was open. Refresh and review it again.');
              await this.guardedChanged(snapshot, null, {refreshBrowser: true});
              return false;
            }
            seedSnapshot = currentSeed;
          }
        } else if (desired !== fresh.server_managed) {
          const text = desired
            ? 'Enable server-managed grouping on the owning server?'
            : 'Switch this swarm to browser-local grouping for all browsers? The saved canonical server grouping is preserved, and this does not stop any agents or processes.';
          if (!(await this.confirm(text))) return false;
          if (!this.context(source, generation) || snapshot.operation !== this.operation) return false;
        } else return true;
        requestId = newUUID();
        const body = {action: 'configure', revision: fresh.revision, request_id: requestId, server_managed: desired};
        if (choice === 'seed') body.seed = clone(seedSnapshot.seed);
        mutationSent = true; this.mutationSent = true; this.mutationOperation = snapshot.operation;
        const response = await this.boundedRequest(this.endpoint(source), 'POST', body,
          `Grouping mode update timed out after ${this.requestTimeout}ms`);
        if (!this.context(source, generation) || snapshot.operation !== this.operation) return false;
        if (!validSettings(response) || response.server_managed !== desired) throw Error('Invalid grouping settings update response');
        await this.guardedChanged(snapshot, response, {operation: 'configure'}); return true;
      } catch (error) {
        if (!this.context(source, generation) || snapshot.operation !== this.operation) return false;
        if (error?.status === 409) {
          this.onError(error.message || 'Grouping settings changed. Refresh and retry.');
          await this.guardedChanged(snapshot, null, {refresh: true});
        } else if (!mutationSent) this.onError(`Cannot load grouping settings: ${error?.message || error}`);
        else if (error?.status >= 400 && error.status < 500) this.onError(error.message || `Grouping mode update failed (${error.status}).`);
        else this.onError(`Grouping mode update outcome is uncertain. Do not retry blindly. Request ID ${requestId}.${error?.message ? ` ${error.message}` : ''}`);
        return false;
      } finally {
        if (this.mutationOperation === snapshot.operation) { this.mutationSent = false; this.mutationOperation = null; }
        if (this.context(source, generation) && snapshot.operation === this.operation) { this.busy = false; this.render(); }
      }
    }
  }

  const api = {safeBrowserGroups, BrowserGroups, GroupingModeController, validSettings};
  root.SwarmGroupingMode = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
