/* Global notification inbox aggregated in the browser across connected Swarms. */
(function (root) {
  'use strict';

  const notificationStates = new Set(['running', 'waiting', 'dormant', 'idle', 'done']);

  function sourcePath(source) {
    return source.id === 'local'
      ? '/api/notifications'
      : `/api/federation/${encodeURIComponent(source.id)}/proxy/api/notifications`;
  }

  function normalizePayload(value) {
    const notifications = Array.isArray(value?.notifications) ? value.notifications : [];
    return {
      revision: value?.revision,
      notifications: notifications.filter(item => item && typeof item.id === 'string' && typeof item.agent_id === 'string').map(item => ({
        id: item.id,
        agent_id: item.agent_id,
        agent_name: typeof item.agent_name === 'string' ? item.agent_name : item.agent_id,
        status: typeof item.status === 'string' ? item.status : '',
        text: typeof item.text === 'string' ? item.text : '',
        created_at: typeof item.created_at === 'string' ? item.created_at : '',
      })),
    };
  }

  function responseError(response) {
    return new Error(`Notifications request failed (${response?.status || 'unknown'}).`);
  }

  class NotificationsInbox {
    constructor(options = {}) {
      this.root = options.root || root;
      this.document = options.document === undefined ? root.document : options.document;
      this.query = options.query || (selector => this.document?.querySelector(selector));
      this.fetch = options.fetch || root.fetch?.bind(root);
      this.onOpenAgent = options.onOpenAgent || (() => {});
      this.intervalMs = options.intervalMs || 30000;
      this.setInterval = options.setInterval || root.setInterval?.bind(root);
      this.clearInterval = options.clearInterval || root.clearInterval?.bind(root);
      this.sources = [];
      this.cache = new Map();
      this.failures = new Map();
      this.inFlight = null;
      this.refreshQueued = false;
      this.generation = 0;
      this.timer = null;
      this.button = this.query?.('#notifications-button');
      this.count = this.query?.('#notifications-count');
      this.dialog = this.query?.('#notifications-dialog');
      this.list = this.query?.('#notification-list');
      this.summary = this.query?.('#notifications-summary');
      this.error = this.query?.('#notifications-error');
      this.boundOpen = () => this.open();
      this.boundRefresh = () => this.refresh();
      this.button?.addEventListener('click', this.boundOpen);
      this.query?.('#refresh-notifications')?.addEventListener('click', this.boundRefresh);
      this.render();
    }

    setSources(sources) {
      const unique = new Map();
      for (const value of sources || []) {
        if (!value || typeof value.id !== 'string' || value.online === false) continue;
        unique.set(value.id, {id: value.id, name: typeof value.name === 'string' && value.name ? value.name : value.id});
      }
      if (!unique.has('local')) unique.set('local', {id: 'local', name: 'Local Swarm'});
      this.sources = [...unique.values()];
      for (const id of [...this.cache.keys()]) if (!unique.has(id)) this.cache.delete(id);
      for (const id of [...this.failures.keys()]) if (!unique.has(id)) this.failures.delete(id);
      this.render();
    }

    async refresh() {
      if (this.inFlight) { this.refreshQueued = true; return this.inFlight; }
      const generation = this.generation;
      const operation = Promise.all(this.sources.map(async source => {
        const path = sourcePath(source);
        try {
          const response = await this.fetch(path, {method: 'GET'});
          if (!response.ok) throw responseError(response);
          const payload = normalizePayload(await response.json());
          if (generation === this.generation) {
            this.cache.set(source.id, {...payload, source});
            this.failures.delete(source.id);
          }
        } catch (error) {
          if (generation === this.generation) this.failures.set(source.id, error);
        }
      })).then(() => {
        if (generation === this.generation) this.render();
        return this.items();
      });
      this.inFlight = operation;
      try { return await operation; }
      finally {
        if (this.inFlight === operation) {
          this.inFlight = null;
          if (this.refreshQueued) { this.refreshQueued = false; this.refresh(); }
        }
      }
    }

    items() {
      const items = [];
      for (const source of this.sources) {
        const entry = this.cache.get(source.id);
        for (const notification of entry?.notifications || []) items.push({...notification, source});
      }
      return items.sort((left, right) => {
        const difference = Date.parse(right.created_at) - Date.parse(left.created_at);
        return Number.isFinite(difference) && difference ? difference : left.id.localeCompare(right.id);
      });
    }

    open() {
      if (!this.dialog) return;
      this.dialog.showModal();
      this.refresh();
    }

    async removeAndOpen(item, control) {
      if (!item?.source || !item.id || !item.agent_id) return;
      const generation = this.generation;
      control.disabled = true;
      control.setAttribute('aria-busy', 'true');
      if (this.error) this.error.textContent = '';
      const path = `${sourcePath(item.source)}/${encodeURIComponent(item.id)}`;
      try {
        const response = await this.fetch(path, {method: 'DELETE'});
        if (!response.ok) throw responseError(response);
        if (generation !== this.generation) return;
        const cached = this.cache.get(item.source.id);
        if (cached) cached.notifications = cached.notifications.filter(value => value.id !== item.id);
        this.render();
        this.dialog?.close();
        await this.onOpenAgent({swarmId: item.source.id, swarmName: item.source.name, agentId: item.agent_id});
      } catch (error) {
        if (generation !== this.generation) return;
        if (this.error) this.error.textContent = error.message || 'Could not remove notification.';
        control.disabled = false;
        control.setAttribute('aria-busy', 'false');
      }
    }

    render() {
      const items = this.items();
      if (this.count) {
        this.count.textContent = items.length > 99 ? '99+' : String(items.length);
        this.count.hidden = items.length === 0;
      }
      if (this.button) {
        this.button.classList.toggle('has-notifications', items.length > 0);
        this.button.setAttribute('aria-label', `Notifications: ${items.length} unread`);
      }
      if (this.summary) this.summary.textContent = `${items.length} unread notification${items.length === 1 ? '' : 's'} across ${this.sources.length} connected Swarm${this.sources.length === 1 ? '' : 's'}.`;
      if (this.error) {
        const failedNames = this.sources.filter(source => this.failures.has(source.id)).map(source => source.name);
        this.error.textContent = failedNames.length ? `Could not refresh: ${failedNames.join(', ')}. Showing available results.` : '';
      }
      if (!this.list || !this.document) return;
      this.list.replaceChildren();
      if (!items.length) {
        const empty = this.document.createElement('p'); empty.className = 'notification-empty';
        empty.textContent = this.failures.size ? 'No notifications are available from the Swarms that responded.' : 'No unread notifications.';
        this.list.append(empty); return;
      }
      for (const item of items) {
        const row = this.document.createElement('article'); row.className = 'notification-item'; row.setAttribute('role', 'listitem');
        const heading = this.document.createElement('div'); heading.className = 'notification-heading';
        const identity = this.document.createElement('div');
        const swarm = this.document.createElement('span'); swarm.className = 'notification-swarm'; swarm.textContent = item.source.name;
        const agent = this.document.createElement('h3'); agent.textContent = item.agent_name;
        identity.append(swarm, agent);
        const status = this.document.createElement('span'); status.className = `badge notification-state ${notificationStates.has(item.status) ? item.status : ''}`.trim();
        status.textContent = item.status || 'Unknown';
        heading.append(identity, status);
        const text = this.document.createElement('p'); text.className = 'notification-text'; text.textContent = item.text;
        const footer = this.document.createElement('footer');
        const time = this.document.createElement('time'); time.dateTime = item.created_at; time.textContent = item.created_at ? new Date(item.created_at).toLocaleString() : 'Time unknown';
        const open = this.document.createElement('button'); open.type = 'button'; open.className = 'small primary'; open.textContent = 'Open agent';
        open.addEventListener('click', () => this.removeAndOpen(item, open));
        footer.append(time, open); row.append(heading, text, footer); this.list.append(row);
      }
    }

    clear() {
      this.generation += 1;
      this.refreshQueued = false;
      this.cache.clear(); this.failures.clear();
      if (this.error) this.error.textContent = '';
      this.render();
    }

    start() {
      if (!this.timer && this.setInterval) this.timer = this.setInterval(() => this.refresh(), this.intervalMs);
      return this.refresh();
    }

    destroy() {
      if (this.timer && this.clearInterval) this.clearInterval(this.timer);
      this.timer = null;
      this.button?.removeEventListener('click', this.boundOpen);
      this.query?.('#refresh-notifications')?.removeEventListener('click', this.boundRefresh);
      this.clear();
    }
  }

  const api = {NotificationsInbox, normalizePayload, sourcePath};
  root.SwarmNotifications = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
