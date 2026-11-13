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

  function projection(value) {
    return value && typeof value === 'object' && !Array.isArray(value) &&
      Number.isInteger(value.revision) && value.revision >= 0 &&
      typeof value.enabled === 'boolean' && typeof value.headless === 'boolean' &&
      (value.executable === null || typeof value.executable === 'string')
      ? {revision: value.revision, enabled: value.enabled, headless: value.headless, executable: value.executable}
      : null;
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
      for (const id of ['panel', 'owner', 'status', 'launch', 'refresh-tabs', 'back', 'forward',
        'reload', 'copy', 'paste', 'url', 'new', 'interval', 'pause', 'tabs', 'screen', 'placeholder'])
        this.nodes[id] = query(`#browser-${id}`);
      this.source = currentSwarm(); this.generation = currentGeneration(); this.lifecycle = 0;
      this.active = false; this.disposed = false; this.paused = false; this.connectionBlocked = false;
      this.status = null; this.tabs = []; this.selected = null; this.displayed = null;
      this.objectUrl = null; this.timer = null; this.pointer = null; this.shotBusy = false; this.shotAgain = false;
      this.queue = []; this.pending = null; this.selectQueue = Promise.resolve();
      this.bind(); this.loadInterval(); this.render();
    }

    absoluteAPI(suffix, source = this.source) { return endpoint(source, suffix); }
    scope() { return {source: this.source, generation: this.generation, lifecycle: this.lifecycle}; }
    valid(scope) {
      return !this.disposed && scope.source === this.source && scope.generation === this.generation &&
        scope.lifecycle === this.lifecycle && scope.source === this.currentSwarm() &&
        scope.generation === this.currentGeneration();
    }

    bind() {
      listen(this.nodes.launch, 'click', () => void this.launch());
      listen(this.nodes['refresh-tabs'], 'click', () => void this.refreshConnection());
      for (const action of ['back', 'forward', 'reload'])
        listen(this.nodes[action], 'click', () => void this.navigate(action));
      listen(this.nodes.copy, 'click', () => void this.copy());
      listen(this.nodes.paste, 'click', () => void this.paste());
      listen(this.nodes.new, 'click', () => void this.newTab());
      listen(this.nodes.url, 'keydown', event => {
        if (event.key === 'Enter') { event.preventDefault?.(); void this.newTab(); }
      });
      listen(this.nodes.interval, 'change', () => this.changeInterval());
      listen(this.nodes.pause, 'click', () => {
        this.paused = !this.paused; this.render(); this.schedule(); if (!this.paused) void this.shot();
      });
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
      if (this.nodes.interval) {
        this.nodes.interval.min = '.25'; this.nodes.interval.max = '60';
        this.nodes.interval.step = '.25'; this.nodes.interval.value = String(value);
      }
    }

    changeInterval() {
      let value = Number(this.nodes.interval?.value);
      if (!Number.isFinite(value)) value = 2;
      value = Math.min(60, Math.max(.25, value)); this.interval = value;
      if (this.nodes.interval) this.nodes.interval.value = String(value);
      try { root.localStorage?.setItem('swarm.remoteBrowserInterval', String(value)); } catch { /* unavailable */ }
      this.schedule();
    }

    setContext(state) {
      const source = this.currentSwarm(), generation = this.currentGeneration();
      if (source !== this.source || generation !== this.generation) {
        this.lifecycle += 1; this.clearQueue(); this.clearImage();
        this.tabs = []; this.selected = null; this.status = null; this.connectionBlocked = false;
        this.source = source; this.generation = generation;
      }
      if (state && typeof state === 'object') this.status = state;
      this.render();
    }

    async call(scope, suffix, method = 'GET', body, binary = false) {
      if (!this.valid(scope)) return null;
      if (typeof this.request !== 'function') throw Error('Authenticated browser request function is unavailable');
      return this.request(this.absoluteAPI(suffix, scope.source), method, body, binary);
    }

    render() {
      text(this.nodes.owner, `Owning server: ${this.ownerName(this.source)}`);
      const state = this.status;
      const label = this.connectionBlocked ? 'Browser controls paused after an uncertain request; refresh tabs to continue' :
        !state ? 'Browser status unavailable' : state.enabled === false ? 'Remote browser disabled' :
          state.running ? `${this.tabs.length} tab(s)` :
            state.state === 'error' ? `Browser error${state.error ? `: ${state.error}` : ''}` : 'Browser stopped';
      text(this.nodes.status, label);
      if (this.nodes.launch) this.nodes.launch.disabled = !state?.enabled || !!state?.running;
      const disabled = !state?.running || this.connectionBlocked;
      for (const id of ['back', 'forward', 'reload', 'copy', 'paste', 'new'])
        if (this.nodes[id]) this.nodes[id].disabled = disabled || (id !== 'new' && !this.selected);
      if (this.nodes['refresh-tabs']) this.nodes['refresh-tabs'].disabled = !state?.running;
      if (this.nodes.pause) this.nodes.pause.textContent = this.paused ? 'Resume refresh' : 'Pause refresh';
      if (this.nodes.screen) this.nodes.screen.hidden = !this.displayed;
      text(this.nodes.placeholder, this.displayed ? '' : state?.running ? 'Waiting for browser screenshot…' : label);
    }

    async activate() {
      if (this.disposed) return false;
      this.active = true; this.setContext(); const scope = this.scope();
      const state = await this.refreshStatus(scope);
      if (this.valid(scope) && this.active && state?.running) await this.loadTabs(scope);
      this.schedule(); return true;
    }

    deactivate() {
      this.active = false; this.lifecycle += 1;
      if (this.timer !== null) root.clearTimeout(this.timer);
      this.timer = null; this.shotAgain = false; this.pointer = null;
      this.clearQueue();
    }

    dispose() {
      if (this.disposed) return;
      this.deactivate(); this.disposed = true; this.clearImage();
    }

    schedule() {
      if (this.timer !== null) root.clearTimeout(this.timer);
      this.timer = null;
      if (!this.active || this.disposed || this.paused) return;
      const scope = this.scope();
      this.timer = root.setTimeout(async () => {
        this.timer = null;
        if (this.valid(scope) && this.active) await this.shot(scope);
        if (this.valid(scope) && this.active) this.schedule();
      }, this.interval * 1000);
    }

    async refreshStatus(scope = this.scope()) {
      try {
        const value = await this.call(scope, 'status');
        if (!this.valid(scope)) return null;
        this.status = value && typeof value === 'object' ? value : null; this.render();
        return this.status;
      } catch (error) {
        if (this.valid(scope)) { this.status = null; this.render(); this.toast(error?.message || String(error), true); }
        return null;
      }
    }

    async launch() {
      if (this.disposed) return false;
      this.setContext(); const scope = this.scope();
      try {
        await this.call(scope, 'launch', 'POST', {request_id: requestId()});
        if (!this.valid(scope)) return false;
        await this.refreshStatus(scope);
        if (!this.valid(scope) || !this.status?.running) return false;
        await this.loadTabs(scope);
        if (this.valid(scope)) await this.onChanged(this.status, {...scope, operation: 'browser-launch'});
        return this.valid(scope);
      } catch (error) {
        if (this.valid(scope)) this.toast(error?.message || String(error), true);
        return false;
      }
    }

    async refreshConnection() {
      this.connectionBlocked = false; this.clearQueue(); this.render();
      const scope = this.scope();
      const state = await this.refreshStatus(scope);
      if (this.valid(scope) && state?.running) await this.loadTabs(scope);
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
      if (!target || this.connectionBlocked || this.disposed) return Promise.resolve(false);
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
      onChanged = async () => {}, toast = () => {}} = {}) {
      this.request = request || api; this.currentSwarm = currentSwarm; this.currentGeneration = currentGeneration;
      this.ownerName = ownerName; this.onChanged = onChanged; this.toast = toast;
      this.owner = query('#browser-settings-owner'); this.enabled = query('#browser-enabled');
      this.headless = query('#browser-headless'); this.executable = query('#browser-executable');
      this.save = query('#browser-settings-save'); this.status = query('#browser-settings-status');
      this.source = currentSwarm(); this.generation = currentGeneration(); this.settings = null;
      this.dirty = new Set(); this.loading = false; this.saving = false; this.running = false; this.operation = 0; this.error = '';
      if (this.headless) this.headless.checked = true;
      if (this.executable) this.executable.placeholder = 'Auto-discover Edge or Chromium';
      for (const [node, field] of [[this.enabled, 'enabled'], [this.headless, 'headless'], [this.executable, 'executable']])
        listen(node, 'input', () => { this.dirty.add(field); this.render(); });
      listen(this.save, 'click', () => void this.saveSettings()); this.render();
    }

    absoluteAPI(source = this.source) { return endpoint(source, 'settings'); }
    valid(source, generation, operation = this.operation) {
      return source === this.currentSwarm() && generation === this.currentGeneration() && operation === this.operation;
    }

    setContext(state) {
      const source = this.currentSwarm(), generation = this.currentGeneration();
      if (source !== this.source || generation !== this.generation) {
        this.source = source; this.generation = generation; this.settings = null;
        this.dirty.clear(); this.error = ''; this.operation += 1;
        this.loading = false; this.saving = false; this.running = false;
      }
      if (state && typeof state.running === 'boolean') this.running = state.running;
      const value = projection(state?.settings || state);
      if (value) this.apply(value, false); else this.render();
    }

    updateSettings(state) { this.setContext(state); }

    apply(value, resetDirty) {
      const old = this.settings; this.settings = value;
      if (resetDirty) this.dirty.clear();
      if (this.enabled && (!this.dirty.has('enabled') || !old)) this.enabled.checked = value.enabled;
      if (this.headless && (!this.dirty.has('headless') || !old)) this.headless.checked = value.headless;
      if (this.executable && (!this.dirty.has('executable') || !old)) this.executable.value = value.executable || '';
      this.render();
    }

    async load() {
      this.setContext(); const source = this.source, generation = this.generation, operation = ++this.operation;
      this.loading = true; this.error = ''; this.dirty.clear(); this.render();
      try {
        const value = projection(await this.request(this.absoluteAPI(source), 'GET'));
        if (!this.valid(source, generation, operation)) return null;
        if (!value) throw Error('Owning server returned malformed browser settings');
        this.apply(value, true); return value;
      } catch (error) {
        if (this.valid(source, generation, operation)) this.error = `Cannot load browser settings: ${error?.message || error}`;
        return null;
      } finally { if (this.valid(source, generation, operation)) { this.loading = false; this.render(); } }
    }

    openSettings() { return this.load(); }

    async saveSettings() {
      this.setContext();
      if (this.loading || this.saving || this.running || !this.settings || !this.dirty.size) return false;
      const source = this.source, generation = this.generation, operation = ++this.operation;
      const body = {revision: this.settings.revision, request_id: requestId(), enabled: !!this.enabled?.checked,
        headless: !!this.headless?.checked, executable: this.executable?.value?.trim?.() || null};
      this.saving = true; this.error = ''; this.render();
      try {
        const value = projection(await this.request(this.absoluteAPI(source), 'POST', body));
        if (!this.valid(source, generation, operation)) return false;
        if (!value) throw Error('Owning server returned malformed browser settings');
        this.apply(value, true); await this.onChanged(value, {source, generation, operation: 'browser-settings'}); return true;
      } catch (error) {
        if (this.valid(source, generation, operation)) { this.error = error?.message || String(error); this.toast(this.error, true); }
        return false;
      } finally { if (this.valid(source, generation, operation)) { this.saving = false; this.render(); } }
    }

    render() {
      text(this.owner, `Owning server: ${this.ownerName(this.source)}`);
      const blocked = this.loading || this.saving || !this.settings;
      if (this.enabled) this.enabled.disabled = blocked || this.running;
      if (this.headless) this.headless.disabled = blocked || this.running;
      if (this.executable) { this.executable.disabled = blocked || this.running; this.executable.placeholder = 'Auto-discover Edge or Chromium'; }
      if (this.save) { this.save.disabled = blocked || this.running || !this.dirty.size; this.save.textContent = this.saving ? 'Saving…' : 'Save browser settings'; }
      if (this.status) this.status.textContent = this.loading ? 'Loading browser settings…' : this.saving ? 'Saving browser settings…' : this.error ||
        (this.running ? 'Browser settings cannot be changed while the browser is running.' : this.settings ?
          'Browser launch is opt-in. Saving settings never launches the browser.' : 'Browser settings have not been loaded.');
    }
  }

  const exported = {RemoteBrowserController, BrowserSettingsController, MAX_QUEUE, MAX_TEXT};
  root.SwarmRemoteBrowser = exported;
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
})(typeof window !== 'undefined' ? window : globalThis);
