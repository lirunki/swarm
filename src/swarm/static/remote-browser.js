/* Classic screenshot browser controller adapted from EdgeMCP remote_web.py.
 * Original input/UI semantics: EdgeMCP commit c09dc257886305539ea90bc454ceda63b9b150a6.
 * Swarm supplies authenticated HTTP requests; no EdgeMCP protocol or page content is embedded.
 */
(function (root) {
  'use strict';

  const MAX_QUEUE = 32;
  const MAX_TEXT = 4096;
  const REQUEST_TIMEOUT = 20000;
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

  function requestId() {
    const value = root.crypto?.randomUUID?.();
    if (!UUID.test(value || '')) throw Error('A secure UUID request ID is unavailable');
    return value;
  }

  function registryProjection(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
      !Number.isInteger(value.revision) || value.revision < 0 || !Array.isArray(value.browsers) ||
      value.browsers.length > 256) return null;
    const browsers = [];
    for (const item of value.browsers) {
      if (!item || typeof item !== 'object' || Array.isArray(item) || !UUID.test(item.id || '') ||
        typeof item.name !== 'string' || !item.name || item.name.length > 80 || /[\u0000-\u001f\u007f]/.test(item.name) ||
        !['managed', 'external'].includes(item.kind) ||
        !(item.port === null || Number.isInteger(item.port) && item.port >= 1 && item.port <= 65535) ||
        typeof item.running !== 'boolean' || typeof item.can_stop !== 'boolean' ||
        typeof item.state !== 'string' || item.state.length > 120 ||
        !(item.executable === null || typeof item.executable === 'string') ||
        typeof item.headless !== 'boolean' ||
        !(item.error === undefined || typeof item.error === 'string')) return null;
      browsers.push({id: item.id, name: item.name, kind: item.kind, port: item.port,
        running: item.running, can_stop: item.can_stop, state: item.state,
        executable: item.executable, headless: item.headless, ...(item.error === undefined ? {} : {error: item.error})});
    }
    return {revision: value.revision, browsers};
  }

  function discoveryProjection(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) ||
      !Array.isArray(value.browsers) || !Array.isArray(value.unavailable) ||
      value.browsers.length + value.unavailable.length > 64) return null;
    const safe = (field, limit) => typeof field === 'string' && field.length > 0 &&
      field.length <= limit && !/[\u0000-\u001f\u007f]/.test(field);
    const browsers = [];
    for (const item of value.browsers) {
      if (!item || typeof item !== 'object' || Array.isArray(item) || item.supported !== true ||
        !safe(item.name, 120) || !safe(item.executable, 4096)) return null;
      browsers.push({name: item.name, executable: item.executable, supported: true});
    }
    const unavailable = [];
    for (const item of value.unavailable) {
      if (!item || typeof item !== 'object' || Array.isArray(item) || !safe(item.name, 120) ||
        !safe(item.executable, 4096) || !safe(item.reason, 4096)) return null;
      unavailable.push({name: item.name, executable: item.executable, reason: item.reason});
    }
    return {browsers, unavailable};
  }

  function endpoint(source, suffix) {
    const path = `/api/browser/${suffix}`;
    return source === 'local' ? path : `/api/federation/${encodeURIComponent(source)}/proxy${path}`;
  }

  function listen(node, event, callback) { node?.addEventListener?.(event, callback); }
  function text(node, value) { if (node) node.textContent = value; }

  class RemoteBrowserController {
    constructor({query = selector => root.document.querySelector(selector), request, api,
      currentSwarm = () => 'local', currentGeneration = () => 0,
      ownerName = source => source === 'local' ? 'Local Swarm' : source,
      toast = () => {}, confirm = message => root.confirm(message), onChanged = async () => {}} = {}) {
      this.query = query; this.request = request || api; this.currentSwarm = currentSwarm;
      this.currentGeneration = currentGeneration; this.ownerName = ownerName;
      this.toast = toast; this.confirm = confirm; this.onChanged = onChanged;
      this.nodes = {};
      for (const id of ['panel', 'owner', 'status', 'connect', 'instance-select', 'refresh-tabs', 'back', 'forward',
        'reload', 'copy', 'paste', 'url', 'new', 'interval', 'pause', 'tabs', 'screen', 'placeholder'])
        this.nodes[id] = query(`#browser-${id}`);
      this.source = currentSwarm(); this.generation = currentGeneration(); this.lifecycle = 0;
      this.active = false; this.disposed = false; this.connected = false; this.paused = false;
      this.connectionBlocked = false; this.registry = null; this.status = null; this.browserId = null;
      this.tabs = []; this.selected = null; this.displayed = null;
      this.objectUrl = null; this.timer = null; this.pointer = null; this.shotBusy = false; this.shotAgain = false;
      this.queue = []; this.pending = null; this.selectQueue = Promise.resolve();
      this.bind(); this.loadInterval(); this.render();
    }

    registryAPI(source = this.source) { return endpoint(source, 'instances'); }
    absoluteAPI(suffix, source = this.source, browserId = this.browserId) {
      if (!UUID.test(browserId || '')) throw Error('Select a browser instance');
      return endpoint(source, `instances/${encodeURIComponent(browserId)}/${suffix}`);
    }
    scope() { return {source: this.source, generation: this.generation, lifecycle: this.lifecycle, browser_id: this.browserId}; }
    ownerValid(scope) {
      return !this.disposed && scope.source === this.source && scope.generation === this.generation &&
        scope.lifecycle === this.lifecycle && scope.source === this.currentSwarm() &&
        scope.generation === this.currentGeneration();
    }
    valid(scope) { return this.ownerValid(scope) && scope.browser_id === this.browserId && this.connected; }

    bind() {
      listen(this.nodes.connect, 'click', () => void (this.connected ? this.disconnect() : this.connect()));
      listen(this.nodes['instance-select'], 'change', () => this.changeInstance());
      listen(this.nodes['refresh-tabs'], 'click', () => void this.refreshConnection());
      for (const action of ['back', 'forward', 'reload']) listen(this.nodes[action], 'click', () => void this.navigate(action));
      listen(this.nodes.copy, 'click', () => void this.copy()); listen(this.nodes.paste, 'click', () => void this.paste());
      listen(this.nodes.new, 'click', () => void this.newTab());
      listen(this.nodes.url, 'keydown', event => { if (event.key === 'Enter') { event.preventDefault?.(); void this.newTab(); } });
      listen(this.nodes.interval, 'change', () => this.changeInterval());
      listen(this.nodes.pause, 'click', () => { this.paused = !this.paused; this.render(); this.schedule(); if (!this.paused) void this.shot(); });
      listen(this.nodes.screen, 'dragstart', event => event.preventDefault?.());
      listen(this.nodes.screen, 'pointerdown', event => this.pointerDown(event));
      listen(this.nodes.screen, 'pointermove', event => this.pointerMove(event));
      listen(this.nodes.screen, 'pointerup', event => this.pointerUp(event));
      listen(this.nodes.screen, 'pointercancel', () => this.pointerCancel());
      listen(this.nodes.screen, 'keydown', event => this.keydown(event));
      listen(this.nodes.screen, 'paste', event => this.pasteEvent(event));
    }

    loadInterval() {
      let value = 2;
      try { value = Number(root.localStorage?.getItem('swarm.remoteBrowserInterval') || 2); } catch { /* unavailable */ }
      if (!Number.isFinite(value) || value < .25 || value > 60) value = 2;
      this.interval = value;
      if (this.nodes.interval) { this.nodes.interval.min = '.25'; this.nodes.interval.max = '60'; this.nodes.interval.step = '.25'; this.nodes.interval.value = String(value); }
    }
    storageKey(source = this.source) { return `swarm.remoteBrowserInstance.${source}`; }
    remembered(source = this.source) { try { const id = root.localStorage?.getItem(this.storageKey(source)); return UUID.test(id || '') ? id : null; } catch { return null; } }
    remember() { try { if (this.browserId) root.localStorage?.setItem(this.storageKey(), this.browserId); } catch { /* unavailable */ } }
    changeInterval() {
      let value = Number(this.nodes.interval?.value); if (!Number.isFinite(value)) value = 2;
      value = Math.min(60, Math.max(.25, value)); this.interval = value;
      if (this.nodes.interval) this.nodes.interval.value = String(value);
      try { root.localStorage?.setItem('swarm.remoteBrowserInterval', String(value)); } catch { /* unavailable */ }
      this.schedule();
    }

    setContext(state) {
      const source = this.currentSwarm(), generation = this.currentGeneration();
      if (source !== this.source || generation !== this.generation) {
        this.disconnect(false); this.lifecycle += 1; this.source = source; this.generation = generation;
        this.registry = null; this.status = null; this.browserId = null; this.renderInstances();
      }
      const projected = registryProjection(state);
      if (projected) this.updateInstances(projected);
      else this.render();
    }

    async call(scope, suffix, method = 'GET', body, binary = false) {
      if (!this.valid(scope)) return null;
      if (typeof this.request !== 'function') throw Error('Authenticated browser request function is unavailable');
      return this.request(this.absoluteAPI(suffix, scope.source, scope.browser_id), method, body, binary);
    }

    chosen() { return this.registry?.browsers.find(item => item.id === this.browserId) || null; }
    render() {
      text(this.nodes.owner, `Owning server: ${this.ownerName(this.source)}`);
      const chosen = this.chosen(); this.status = this.registry;
      const label = this.connectionBlocked ? 'Browser controls paused after an uncertain request; refresh tabs to continue' :
        !this.registry ? 'Browser instances unavailable' : !chosen ? 'Select a browser instance' :
          !chosen.running ? `${chosen.name}: stopped` : !this.connected ? `${chosen.name}: running — connect to view` : `${chosen.name}: ${this.tabs.length} tab(s)`;
      text(this.nodes.status, label);
      if (this.nodes.connect) { this.nodes.connect.disabled = !chosen?.running; this.nodes.connect.textContent = this.connected ? 'Disconnect' : 'Connect'; }
      const disabled = !this.connected || !chosen?.running || this.connectionBlocked;
      for (const id of ['back', 'forward', 'reload', 'copy', 'paste', 'new'])
        if (this.nodes[id]) this.nodes[id].disabled = disabled || (id !== 'new' && !this.selected);
      if (this.nodes['refresh-tabs']) this.nodes['refresh-tabs'].disabled = !this.registry;
      if (this.nodes.pause) this.nodes.pause.textContent = this.paused ? 'Resume refresh' : 'Pause refresh';
      if (this.nodes.screen) this.nodes.screen.hidden = !this.displayed;
      text(this.nodes.placeholder, this.displayed ? '' : this.connected ? 'Waiting for browser screenshot…' : label);
    }

    renderInstances() {
      const host = this.nodes['instance-select'];
      if (host) {
        const doc = host.ownerDocument || root.document, nodes = [];
        for (const browser of this.registry?.browsers || []) {
          const option = doc?.createElement?.('option'); if (!option) break;
          option.value = browser.id;
          option.textContent = `${browser.name}${browser.kind === 'external' && browser.port ? ` — external :${browser.port}` : ''}${browser.running ? '' : ' — stopped'}`;
          nodes.push(option);
        }
        if (host.replaceChildren) host.replaceChildren(...nodes);
        else { while (host.firstChild) host.removeChild(host.firstChild); for (const node of nodes) host.appendChild?.(node); }
        host.value = this.browserId || '';
      }
      this.render();
    }

    updateInstances(value) {
      const projected = registryProjection(value); if (!projected) return false;
      if (this.registry && projected.revision < this.registry.revision) return false;
      const oldId = this.browserId, oldBrowser = this.chosen();
      const nextBrowser = projected.browsers.find(item => item.id === oldId) || null;
      const identityChanged = oldBrowser && nextBrowser &&
        ['kind', 'port', 'executable', 'headless', 'can_stop'].some(field => oldBrowser[field] !== nextBrowser[field]);
      if (this.connected && (!oldBrowser || !nextBrowser || !nextBrowser.running || identityChanged)) this.disconnect(false);
      this.registry = projected;
      if (!nextBrowser) {
        const remembered = this.remembered();
        this.browserId = projected.browsers.some(item => item.id === remembered) ? remembered : projected.browsers[0]?.id || null;
      }
      this.renderInstances(); return true;
    }

    async refreshInstances(silent = false, suppliedScope = this.scope()) {
      const scope = suppliedScope;
      try {
        const value = registryProjection(await this.request(this.registryAPI(scope.source), 'GET'));
        if (!this.ownerValid(scope)) return null;
        if (!value) throw Error('Owning server returned malformed browser instances');
        return this.updateInstances(value) ? value : this.registry;
      } catch (error) { if (this.ownerValid(scope) && !silent) this.toast(error?.message || String(error), true); return null; }
    }

    async activate() {
      if (this.disposed) return false;
      this.active = true; this.setContext(); this.disconnect(false); const scope = this.scope();
      await this.refreshInstances(false, scope); this.render(); return this.ownerValid(scope);
    }

    disconnect(render = true) {
      this.connected = false; this.lifecycle += 1;
      if (this.timer !== null) root.clearTimeout(this.timer);
      this.timer = null; this.shotAgain = false; this.pointer = null; this.connectionBlocked = false;
      this.clearQueue(); this.selectQueue = Promise.resolve(); this.tabs = []; this.selected = null; this.clearImage(); this.renderTabs();
      if (render) this.render(); return true;
    }

    deactivate() { this.active = false; this.disconnect(); }
    dispose() { if (this.disposed) return; this.deactivate(); this.disposed = true; this.clearImage(); }

    async connect() {
      if (this.disposed || !this.active || this.connected || !this.browserId) return false;
      const wanted = this.browserId, before = this.scope();
      const list = await this.refreshInstances(false, before);
      if (!this.ownerValid(before) || this.browserId !== wanted || !list) return false;
      const browser = list.browsers.find(item => item.id === wanted);
      if (!browser?.running) { this.render(); return false; }
      this.connected = true; this.connectionBlocked = false; this.remember(); this.render();
      const scope = this.scope();
      const result = await this.loadTabs(scope);
      if (!this.valid(scope) || !result) { if (this.valid(scope)) this.disconnect(); return false; }
      this.schedule(); return true;
    }

    changeInstance() {
      const value = this.nodes['instance-select']?.value || null;
      if (value === this.browserId) return;
      this.disconnect(false); this.browserId = this.registry?.browsers.some(item => item.id === value) ? value : null;
      this.remember(); this.renderInstances();
    }

    schedule() {
      if (this.timer !== null) root.clearTimeout(this.timer); this.timer = null;
      if (!this.active || !this.connected || this.disposed || this.paused || !this.chosen()?.running) return;
      const scope = this.scope();
      this.timer = root.setTimeout(async () => { this.timer = null; if (this.valid(scope) && this.active && this.connected) await this.shot(scope); if (this.valid(scope) && this.active && this.connected) this.schedule(); }, this.interval * 1000);
    }

    async refreshConnection() {
      this.connectionBlocked = false; this.clearQueue(); const scope = this.scope();
      if (!this.connected) { await this.refreshInstances(false, scope); return !!this.registry; }
      return this.loadTabs(scope);
    }

    normalizeTabs(value) {
      if (!value || !Array.isArray(value.tabs)) return null;
      const tabs = value.tabs.filter(tab => tab && typeof tab.id === 'string').slice(0, 256)
        .map(tab => ({id: tab.id, title: String(tab.title || '').slice(0, 4096), url: String(tab.url || '').slice(0, 4096)}));
      const selected = tabs.some(tab => tab.id === value.selected) ? value.selected : tabs[0]?.id || null;
      return {tabs, selected};
    }

    async loadTabs(scope = this.scope()) {
      try {
        const value = this.normalizeTabs(await this.call(scope, 'tabs'));
        if (!this.valid(scope) || !value) return false;
        this.tabs = value.tabs; this.renderTabs();
        if (!value.selected) { this.selected = null; this.clearImage(); this.render(); return true; }
        await this.select(value.selected, scope); return this.valid(scope);
      } catch (error) {
        if (this.valid(scope)) this.toast(error?.message || String(error), true);
        return false;
      }
    }

    renderTabs() {
      const host = this.nodes.tabs; if (!host) return;
      if (host.replaceChildren) host.replaceChildren(); else while (host.firstChild) host.removeChild(host.firstChild);
      for (const tab of this.tabs) {
        const doc = host.ownerDocument || root.document;
        const button = doc?.createElement?.('button'); if (!button) break;
        button.type = 'button'; button.className = tab.id === this.selected ? 'active' : '';
        button.textContent = tab.title || '(untitled)'; button.title = `${tab.title || '(untitled)'}\n${tab.url}`;
        button.dataset.id = tab.id;
        button.setAttribute?.('aria-selected', String(tab.id === this.selected));
        listen(button, 'click', () => void this.select(tab.id));
        const close = doc?.createElement?.('span');
        if (close) {
          close.className = 'tab-close'; close.textContent = '×'; close.title = 'Close tab';
          listen(close, 'click', event => { event.stopPropagation?.(); void this.closeTab(tab.id); });
          button.appendChild?.(close);
        }
        host.appendChild?.(button);
      }
    }

    select(target, suppliedScope) {
      const scope = suppliedScope || this.scope();
      if (!this.tabs.some(tab => tab.id === target) || !this.valid(scope)) return Promise.resolve(false);
      this.selected = target; this.clearImage(); this.clearQueuedInputs();
      const tab = this.tabs.find(item => item.id === target);
      if (this.nodes.url) this.nodes.url.value = tab?.url || '';
      this.renderTabs(); this.render();
      const task = async () => {
        if (!this.valid(scope) || !this.active || this.selected !== target) return false;
        await this.call(scope, 'select', 'POST', {target_id: target});
        if (!this.valid(scope) || !this.active || this.selected !== target) return false;
        await this.shot(scope, target); return this.valid(scope) && this.active && this.selected === target;
      };
      const result = this.selectQueue.catch(() => false).then(task);
      this.selectQueue = result.catch(error => {
        if (this.valid(scope)) this.toast(error?.message || String(error), true);
        return false;
      });
      return this.selectQueue;
    }

    async newTab() {
      const url = this.nodes.url?.value?.trim?.() || '';
      if (!/^https?:\/\/[^\s]+$/i.test(url) || url.length > MAX_TEXT) {
        this.toast('Enter an http(s) URL', true); return false;
      }
      const scope = this.scope();
      try {
        const made = await this.call(scope, 'new', 'POST', {url});
        if (!this.valid(scope)) return false;
        await this.loadTabs(scope);
        const target = typeof made?.id === 'string' ? made.id : typeof made?.target_id === 'string' ? made.target_id : null;
        if (target && this.tabs.some(tab => tab.id === target) && this.selected !== target) await this.select(target, scope);
        return this.valid(scope);
      } catch (error) { if (this.valid(scope)) this.toast(error?.message || String(error), true); return false; }
    }

    async closeTab(target) {
      if (!target) return false;
      const scope = this.scope();
      try {
        await this.call(scope, 'close', 'POST', {target_id: target});
        if (!this.valid(scope)) return false;
        if (this.selected === target) { this.selected = null; this.clearImage(); }
        await this.loadTabs(scope); return this.valid(scope);
      } catch (error) { if (this.valid(scope)) this.toast(error?.message || String(error), true); return false; }
    }

    navigate(action) {
      const target = this.selected; if (!target) return Promise.resolve(false);
      return this.enqueue(action, target, {target_id: target}, async scope => {
        await this.call(scope, action, 'POST', {target_id: target});
        if (this.valid(scope) && this.selected === target) await this.loadTabs(scope);
      });
    }

    clearImage() {
      this.displayed = null;
      if (this.objectUrl) root.URL?.revokeObjectURL?.(this.objectUrl);
      this.objectUrl = null;
      this.nodes.screen?.removeAttribute?.('src'); this.render();
    }

    async shot(scope = this.scope(), requestedTarget = this.selected) {
      if (!requestedTarget || !this.active || this.paused || this.connectionBlocked) return false;
      if (this.pointer) { this.shotAgain = true; return false; }
      if (this.shotBusy) { this.shotAgain = true; return false; }
      this.shotBusy = true;
      try {
        const suffix = `screenshot?target_id=${encodeURIComponent(requestedTarget)}&t=${Date.now()}`;
        const blob = await this.call(scope, suffix, 'GET', undefined, true);
        if (!this.valid(scope) || requestedTarget !== this.selected || this.pointer) return false;
        if (!(blob instanceof Blob) || blob.type && blob.type !== 'image/png') throw Error('Browser returned an invalid screenshot');
        const next = root.URL?.createObjectURL?.(blob);
        if (!next) throw Error('Cannot display browser screenshot');
        const old = this.objectUrl; this.objectUrl = next; this.displayed = requestedTarget;
        if (this.nodes.screen) this.nodes.screen.src = next;
        if (old) root.URL?.revokeObjectURL?.(old);
        this.render(); return true;
      } catch (error) {
        if (this.valid(scope)) this.toast(`Screenshot unavailable: ${error?.message || error}`, true);
        return false;
      } finally {
        this.shotBusy = false;
        if (this.shotAgain && !this.pointer) { this.shotAgain = false; void this.shot(); }
      }
    }

    clearQueuedInputs() {
      for (const item of this.queue.splice(0)) item.resolve(false);
    }

    clearQueue() {
      this.clearQueuedInputs();
      // A request already handed to fetch cannot be replayed or reliably cancelled. Its guarded result is ignored.
      const pending = this.pending; this.pending = null;
      if (pending) { root.clearTimeout(pending.timer); pending.resolve(false); }
    }

    enqueue(name, target, body, operation) {
      if (!target || !this.active || !this.connected || this.connectionBlocked || this.disposed) return Promise.resolve(false);
      if (this.queue.length + (this.pending ? 1 : 0) >= MAX_QUEUE) {
        this.toast('Browser command queue is full', true); return Promise.resolve(false);
      }
      const scope = this.scope();
      const item = {name, target, body: {...body}, scope, operation};
      const result = new Promise(resolve => { item.resolve = resolve; });
      this.queue.push(item); this.pump(); return result;
    }

    pump() {
      if (this.pending || this.connectionBlocked) return;
      const item = this.queue.shift(); if (!item) return;
      if (!this.valid(item.scope)) { item.resolve(false); return this.pump(); }
      this.pending = item;
      let timeout;
      const timed = new Promise((_, reject) => {
        timeout = root.setTimeout(() => reject(Object.assign(Error('Browser request timed out; its outcome is uncertain.'), {uncertain: true})), REQUEST_TIMEOUT);
        item.timer = timeout;
      });
      Promise.race([Promise.resolve().then(() => item.operation(item.scope)), timed]).then(() => {
        if (this.pending !== item) return;
        root.clearTimeout(timeout); this.pending = null; item.resolve(this.valid(item.scope)); this.pump();
      }).catch(error => {
        if (this.pending !== item) return;
        root.clearTimeout(timeout); this.pending = null; item.resolve(false);
        if (this.valid(item.scope)) this.toast(error?.message || String(error), true);
        if (error?.uncertain) {
          this.connectionBlocked = true; this.clearQueuedInputs(); this.render();
        } else this.pump();
      });
    }

    point(event) {
      const rect = this.nodes.screen?.getBoundingClientRect?.();
      if (!rect || rect.width <= 0 || rect.height <= 0) return null;
      return {x_ratio: Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width)),
        y_ratio: Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height))};
    }

    pointerDown(event) {
      if (event.button !== undefined && event.button !== 0 || !this.displayed || this.displayed !== this.selected) return;
      const point = this.point(event); if (!point) return;
      event.preventDefault?.(); this.nodes.screen?.focus?.(); this.nodes.screen?.setPointerCapture?.(event.pointerId);
      this.pointer = {id: event.pointerId, target: this.displayed, start: point, end: point};
    }

    pointerMove(event) {
      if (this.pointer?.id === event.pointerId) this.pointer.end = this.point(event) || this.pointer.end;
    }

    pointerUp(event) {
      const gesture = this.pointer;
      if (!gesture || gesture.id !== event.pointerId) return;
      event.preventDefault?.(); gesture.end = this.point(event) || gesture.end; this.pointer = null;
      this.nodes.screen?.releasePointerCapture?.(event.pointerId);
      const moved = Math.hypot(gesture.end.x_ratio - gesture.start.x_ratio, gesture.end.y_ratio - gesture.start.y_ratio) > .003;
      const body = moved ? {target_id: gesture.target, start: gesture.start, end: gesture.end} :
        {target_id: gesture.target, ...gesture.end};
      void this.enqueue(moved ? 'drag' : 'click', gesture.target, body, async scope => {
        await this.call(scope, moved ? 'drag' : 'click', 'POST', body);
        if (this.valid(scope) && this.selected === gesture.target) await this.shot(scope, gesture.target);
      });
      this.shotAgain = false;
    }

    pointerCancel() { this.pointer = null; if (this.shotAgain) { this.shotAgain = false; void this.shot(); } }

    async sendText(target, value) {
      if (!target || typeof value !== 'string' || !value || value.length > MAX_TEXT) return false;
      return this.enqueue('text', target, {target_id: target, text: value}, async scope => {
        for (let index = 0; index < value.length; index += 2048) {
          if (!this.valid(scope) || !this.active || this.selected !== target || this.connectionBlocked) return false;
          await this.call(scope, 'text', 'POST', {target_id: target, text: value.slice(index, index + 2048)});
        }
        if (this.valid(scope) && this.active && this.selected === target && !this.connectionBlocked)
          await this.shot(scope, target);
        return true;
      });
    }

    async copy() {
      const target = this.selected; if (!target) return false;
      return this.enqueue('copy', target, {target_id: target}, async scope => {
        const value = await this.call(scope, 'copy', 'POST', {target_id: target});
        if (!this.valid(scope) || typeof value?.text !== 'string') return;
        await root.navigator?.clipboard?.writeText?.(value.text);
        if (this.valid(scope)) text(this.nodes.status, `Copied ${value.text.length} character(s)`);
      });
    }

    async paste() {
      const target = this.selected; if (!target) return false;
      let value;
      try { value = await root.navigator?.clipboard?.readText?.(); }
      catch { this.toast('Clipboard read was denied', true); return false; }
      if (typeof value !== 'string' || !value) return false;
      if (value.length > MAX_TEXT) { this.toast('Paste is limited to 4096 characters', true); return false; }
      return this.sendText(target, value);
    }

    pasteEvent(event) {
      const target = this.displayed;
      if (!target || target !== this.selected) return;
      const value = event.clipboardData?.getData?.('text/plain') || '';
      if (!value || value.length > MAX_TEXT) { if (value.length > MAX_TEXT) this.toast('Paste is limited to 4096 characters', true); return; }
      event.preventDefault?.(); void this.sendText(target, value);
    }

    keydown(event) {
      const target = this.displayed;
      if (!target || target !== this.selected || event.isComposing) return;
      const shortcut = (event.ctrlKey || event.metaKey) && !event.altKey;
      if (shortcut && event.key?.toLowerCase?.() === 'c') { event.preventDefault?.(); void this.copy(); return; }
      if (shortcut && event.key?.toLowerCase?.() === 'v') return;
      event.preventDefault?.();
      if (event.key?.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
        void this.sendText(target, event.key); return;
      }
      const modifiers = [];
      if (event.ctrlKey) modifiers.push('ctrl'); if (event.altKey) modifiers.push('alt');
      if (event.shiftKey) modifiers.push('shift'); if (event.metaKey) modifiers.push('meta');
      const body = {target_id: target, key: event.key, modifiers};
      void this.enqueue('key', target, body, async scope => {
        await this.call(scope, 'key', 'POST', body);
        if (this.valid(scope) && this.selected === target) await this.shot(scope, target);
      });
    }
  }

  class BrowserSettingsController {
    constructor({query = selector => root.document.querySelector(selector), request, api,
      currentSwarm = () => 'local', currentGeneration = () => 0,
      ownerName = source => source === 'local' ? 'Local Swarm' : source,
      onChanged = async () => {}, toast = () => {}, confirm = message => root.confirm(message),
      prompt = (message, value) => root.prompt(message, value)} = {}) {
      this.query = query; this.request = request || api; this.currentSwarm = currentSwarm;
      this.currentGeneration = currentGeneration; this.ownerName = ownerName; this.onChanged = onChanged;
      this.toast = toast; this.confirm = confirm; this.prompt = prompt;
      this.owner = query('#browser-settings-owner'); this.status = query('#browser-settings-status');
      this.headless = query('#browser-headless'); this.executableSelect = query('#browser-executable-select');
      this.executable = query('#browser-executable-manual') || query('#browser-executable');
      this.customExecutable = query('#browser-executable-custom'); this.discoveryStatus = query('#browser-discovery-status');
      this.instanceName = query('#browser-instance-name'); this.list = query('#browser-management-list');
      this.launchButton = query('#browser-settings-launch'); this.existingName = query('#browser-existing-name');
      this.existingPort = query('#browser-existing-port'); this.addButton = query('#browser-existing-add');
      this.refreshButton = query('#browser-management-refresh');
      this.source = currentSwarm(); this.generation = currentGeneration(); this.registry = null;
      this.settings = null; // Compatibility: parent code historically inspected this field.
      this.discovery = {browsers: [], unavailable: []}; this.discoveryLoading = false; this.discoveryError = '';
      this.loading = false; this.saving = false; this.running = false; this.operation = 0;
      this.discoveryOperation = 0; this.error = ''; this.uncertain = false; this.operationIds = [];
      this.customMode = false;
      if (this.headless) this.headless.checked = true;
      if (this.instanceName) this.instanceName.value ||= '';
      listen(this.executableSelect, 'change', () => this.selectExecutable());
      listen(this.executable, 'input', () => { this.customMode = true; this.snapExecutableSelection(); this.render(); });
      listen(this.launchButton, 'click', () => void this.launch());
      listen(this.addButton, 'click', () => void this.addExisting());
      listen(this.refreshButton, 'click', () => void this.load(true));
      this.render();
    }

    absoluteAPI(source = this.source) { return endpoint(source, 'instances'); }
    absoluteDiscoveryAPI(source = this.source) { return endpoint(source, 'discovery'); }
    valid(source, generation, operation = this.operation) {
      return source === this.source && generation === this.generation && source === this.currentSwarm() &&
        generation === this.currentGeneration() && operation === this.operation;
    }
    validDiscovery(source, generation, operation) { return this.valid(source, generation) && operation === this.discoveryOperation; }

    setContext(value) {
      const source = this.currentSwarm(), generation = this.currentGeneration();
      if (source !== this.source || generation !== this.generation) {
        this.source = source; this.generation = generation; this.operation += 1; this.discoveryOperation += 1;
        this.registry = null; this.settings = null; this.discovery = {browsers: [], unavailable: []};
        this.discoveryLoading = false; this.discoveryError = ''; this.loading = false; this.saving = false;
        this.running = false; this.error = ''; this.uncertain = false; this.operationIds = []; this.customMode = false;
      }
      const projected = registryProjection(value?.registry || value);
      if (projected) this.apply(projected); else this.render();
    }
    updateSettings(value) { this.setContext(value); }
    apply(value) {
      if (this.registry && value.revision < this.registry.revision) return this.registry;
      this.registry = value; this.settings = value; this.render(); return value;
    }

    selectExecutable() {
      const value = this.executableSelect?.value;
      if (value === '__custom__') { this.customMode = true; this.render(); this.executable?.focus?.(); return; }
      if (value !== '' && !this.discovery.browsers.some(item => item.executable === value)) return;
      this.customMode = false; if (this.executable) this.executable.value = value || ''; this.render();
    }
    snapExecutableSelection() {
      if (!this.executableSelect) return;
      const value = this.executable?.value || '';
      this.executableSelect.value = this.customMode ? '__custom__' : !value ? '' :
        this.discovery.browsers.some(item => item.executable === value) ? value : '__custom__';
    }
    replaceOptions(nodes) {
      const host = this.executableSelect; if (!host) return;
      if (host.replaceChildren) host.replaceChildren(...nodes);
      else { while (host.firstChild) host.removeChild(host.firstChild); for (const node of nodes) host.appendChild?.(node); }
    }
    renderDiscoveryOptions() {
      const host = this.executableSelect; if (!host) return;
      const doc = host.ownerDocument || root.document, nodes = [];
      const add = (value, label, disabled = false) => { const node = doc?.createElement?.('option'); if (!node) return; node.value = value; node.textContent = label; node.disabled = disabled; nodes.push(node); };
      add('', 'Auto-discover');
      for (const item of this.discovery.browsers) add(item.executable, `${item.name} — ${item.executable}`);
      this.discovery.unavailable.forEach((item, index) => add(`__unavailable_${index}__`, `${item.name} — ${item.reason}`, true));
      add('__custom__', 'Custom executable'); this.replaceOptions(nodes); this.snapExecutableSelection();
    }

    async loadDiscovery(source, generation) {
      if (!this.executableSelect) return null;
      const operation = ++this.discoveryOperation; this.discoveryLoading = true; this.discoveryError = '';
      this.discovery = {browsers: [], unavailable: []}; this.render();
      try {
        const value = discoveryProjection(await this.request(this.absoluteDiscoveryAPI(source), 'GET'));
        if (!this.validDiscovery(source, generation, operation)) return null;
        if (!value) throw Error('Owning server returned malformed browser discovery');
        this.discovery = value; return value;
      } catch (error) {
        if (this.validDiscovery(source, generation, operation)) this.discoveryError = error?.status === 404 ?
          'Browser discovery is unavailable. You can enter a custom executable.' :
          `Cannot discover installed browsers: ${error?.message || error}. You can enter a custom executable.`;
        return null;
      } finally { if (this.validDiscovery(source, generation, operation)) { this.discoveryLoading = false; this.render(); } }
    }

    async timedRequest(path, method = 'GET', body) {
      let timer;
      const timed = new Promise((_, reject) => { timer = root.setTimeout(() => reject(Object.assign(Error('Browser registry request timed out'), {timeout: true})), REQUEST_TIMEOUT); });
      try { return await Promise.race([this.request(path, method, body), timed]); }
      finally { root.clearTimeout(timer); }
    }

    async fetchRegistry(source, generation, operation) {
      const value = registryProjection(await this.timedRequest(this.absoluteAPI(source), 'GET'));
      if (!this.valid(source, generation, operation)) return null;
      if (!value) throw Error('Owning server returned malformed browser instances');
      return value;
    }

    async load(explicit = true) {
      this.setContext(); const source = this.source, generation = this.generation, operation = ++this.operation;
      this.loading = true; this.error = ''; this.render();
      let value = null;
      try {
        value = await this.fetchRegistry(source, generation, operation);
        if (value) { this.apply(value); if (explicit) this.uncertain = false; }
      }
      catch (error) { if (this.valid(source, generation, operation)) this.error = `Cannot load browser instances: ${error?.message || error}`; }
      finally { if (this.valid(source, generation, operation)) { this.loading = false; this.render(); } }
      if (value && this.valid(source, generation, operation)) await this.loadDiscovery(source, generation);
      return value;
    }
    openSettings() { return this.load(true); }

    executableValue() { return this.executable?.value?.trim?.() || null; }
    async launch() {
      const fields = {name: this.instanceName?.value?.trim?.() || '', headless: this.headless ? !!this.headless.checked : true, executable: this.executableValue()};
      if (!fields.name) { this.error = 'Enter an instance name.'; this.render(); return false; }
      return this.mutate('launch', fields);
    }
    async addExisting() {
      const fields = {name: this.existingName?.value?.trim?.() || '', port: Number(this.existingPort?.value)};
      if (!fields.name || !Number.isInteger(fields.port) || fields.port < 1 || fields.port > 65535) { this.error = 'Enter a name and a valid port.'; this.render(); return false; }
      return this.mutate('add', fields);
    }
    async rename(id) {
      const item = this.registry?.browsers.find(browser => browser.id === id); if (!item || this.saving) return false;
      const name = this.prompt(`Rename browser instance “${item.name}”`, item.name);
      if (name === null) return false; const clean = String(name).trim(); if (!clean || clean === item.name) return false;
      return this.mutate('rename', {id: item.id, name: clean});
    }
    async stop(id) {
      const item = this.registry?.browsers.find(browser => browser.id === id);
      if (!item || item.kind !== 'managed' || !item.running || !item.can_stop || this.saving) return false;
      const fields = {id: item.id};
      if (!this.confirm(`Stop managed browser “${item.name}”? Its tabs will close; its profile is retained.`)) return false;
      return this.mutate('stop', fields);
    }
    async remove(id) {
      const item = this.registry?.browsers.find(browser => browser.id === id);
      if (!item || item.kind === 'managed' && (item.running || item.state !== 'stopped') || this.saving) return false;
      const fields = {id: item.id};
      if (!this.confirm(`Remove browser entry “${item.name}”?`)) return false;
      return this.mutate('remove', fields);
    }

    async mutate(action, fields) {
      this.setContext();
      if (this.loading || this.saving || this.uncertain || !this.registry) return false;
      const source = this.source, generation = this.generation, operation = ++this.operation;
      const knownRevision = this.registry.revision, request_id = requestId();
      const snapshot = Object.freeze({...fields}); this.saving = true; this.error = ''; this.render();
      let posted = false;
      try {
        const fresh = await this.fetchRegistry(source, generation, operation);
        if (!fresh) return false;
        if (fresh.revision !== knownRevision || this.registry?.revision !== knownRevision) {
          this.apply(fresh); this.error = 'Browser instances changed. Review the refreshed list and try again.'; return false;
        }
        this.apply(fresh);
        const current = snapshot.id ? fresh.browsers.find(item => item.id === snapshot.id) : null;
        const staleLifecycle = action === 'stop' && (!current || current.kind !== 'managed' || !current.running || !current.can_stop) ||
          action === 'remove' && (!current || current.kind === 'managed' && (current.running || current.state !== 'stopped')) ||
          action === 'rename' && !current;
        if (staleLifecycle) { this.error = 'Browser state changed. Review the refreshed list and try again.'; return false; }
        const body = {revision: knownRevision, request_id, ...snapshot};
        posted = true;
        const response = await this.timedRequest(`${this.absoluteAPI(source)}/${action}`, 'POST', body);
        if (!this.valid(source, generation, operation)) return false;
        const value = registryProjection(response), operationId = response?.operation?.browser_id;
        const created = action === 'launch' || action === 'add';
        if (!value || value.revision < knownRevision + 1 || !UUID.test(operationId || '') ||
          snapshot.id && operationId !== snapshot.id || created && !value.browsers.some(item => item.id === operationId))
          throw Error(`Owning server did not acknowledge ${action}. Request ID ${request_id}`);
        this.operationIds.push(operationId); this.apply(value);
        await this.onChanged(value, {source, generation, operation: `browser-${action}`, request_id, browser_id: operationId});
        return this.valid(source, generation, operation);
      } catch (error) {
        if (this.valid(source, generation, operation)) {
          const status = error?.status;
          const definitive = Number.isInteger(status) && status >= 400 && status < 500 && status !== 408;
          const reportsUncertain = error?.uncertain === true || /outcome uncertain|Request ID:/i.test(error?.message || '');
          const uncertain = posted && (reportsUncertain || !definitive);
          const detail = error?.message || String(error);
          this.error = uncertain ? `${detail}. Outcome uncertain; do not retry. Request ID ${request_id}` : detail;
          if (uncertain) this.uncertain = true; this.toast(this.error, true);
        }
        return false;
      } finally { if (this.valid(source, generation, operation)) { this.saving = false; this.render(); } }
    }

    makeButton(doc, label, callback, disabled = false, danger = false) {
      const button = doc?.createElement?.('button'); if (!button) return null;
      button.type = 'button'; button.textContent = label; button.disabled = disabled; if (danger) button.className = 'danger';
      listen(button, 'click', callback); return button;
    }
    renderList() {
      const host = this.list; if (!host) return;
      const doc = host.ownerDocument || root.document, rows = [];
      for (const item of this.registry?.browsers || []) {
        const row = doc?.createElement?.('div'); if (!row) break; row.className = 'browser-management-row';
        const label = doc?.createElement?.('span');
        if (label) { label.textContent = `${item.name} — ${item.kind === 'external' ? `external port ${item.port}` : 'managed'} — ${item.running ? 'running' : item.state || 'stopped'}`; row.appendChild?.(label); }
        const busy = this.loading || this.saving || this.uncertain;
        const rename = this.makeButton(doc, 'Rename', () => void this.rename(item.id), busy);
        const stop = this.makeButton(doc, 'Stop', () => void this.stop(item.id), busy || item.kind !== 'managed' || !item.running || !item.can_stop, true);
        const remove = this.makeButton(doc, 'Remove', () => void this.remove(item.id), busy || item.kind === 'managed' && (item.running || item.state !== 'stopped'), true);
        for (const button of [rename, stop, remove]) if (button) row.appendChild?.(button);
        rows.push(row);
      }
      if (host.replaceChildren) host.replaceChildren(...rows);
      else { while (host.firstChild) host.removeChild(host.firstChild); for (const row of rows) host.appendChild?.(row); }
    }
    render() {
      text(this.owner, `Owning server: ${this.ownerName(this.source)}`); this.renderDiscoveryOptions(); this.renderList();
      const blocked = this.loading || this.saving || this.uncertain;
      for (const node of [this.headless, this.executableSelect, this.executable, this.instanceName, this.existingName, this.existingPort]) if (node) node.disabled = blocked;
      if (this.launchButton) { this.launchButton.disabled = blocked || !this.registry; this.launchButton.textContent = this.saving ? 'Please wait…' : 'Launch browser'; }
      if (this.addButton) this.addButton.disabled = blocked || !this.registry;
      if (this.refreshButton) this.refreshButton.disabled = this.loading || this.saving;
      if (this.customExecutable) this.customExecutable.hidden = this.executableSelect?.value !== '__custom__';
      if (this.status) this.status.textContent = this.loading ? 'Loading browser instances…' : this.saving ? 'Updating browser instances…' : this.error ||
        (this.uncertain ? 'A request outcome is uncertain. Inspect the server, then explicitly refresh before another change.' :
          this.registry ? `${this.registry.browsers.length} browser instance(s).` : 'Browser instances have not been loaded.');
      if (this.discoveryStatus) {
        const unavailable = this.discovery.unavailable.map(item => `${item.name} is unavailable: ${item.reason}`).join(' ');
        this.discoveryStatus.textContent = this.discoveryLoading ? 'Discovering installed browsers…' : this.discoveryError ||
          [this.discovery.browsers.length ? `${this.discovery.browsers.length} installed browser option(s) found.` : 'No installed browser options were discovered.', unavailable].filter(Boolean).join(' ');
      }
    }
  }

  const exported = {RemoteBrowserController, BrowserSettingsController, MAX_QUEUE, MAX_TEXT};
  root.SwarmRemoteBrowser = exported;
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
})(typeof window !== 'undefined' ? window : globalThis);
