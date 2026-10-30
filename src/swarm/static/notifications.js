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
      this.boundClearAll = event => this.clearAll(event.currentTarget);
      this.button?.addEventListener('click', this.boundOpen);
      this.query?.('#refresh-notifications')?.addEventListener('click', this.boundRefresh);
      this.query?.('#clear-notifications')?.addEventListener('click', this.boundClearAll);
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

    groups() {
      const groups = new Map();
      for (const item of this.items()) {
        const key = `${item.source.id}\u0000${item.agent_id}`;
        if (!groups.has(key)) groups.set(key, {source: item.source, agent_id: item.agent_id, agent_name: item.agent_name, items: []});
        groups.get(key).items.push(item);
      }
      return [...groups.values()];
    }

    open() {
      if (!this.dialog) return;
      this.dialog.showModal();
      this.refresh();
    }

    async removeGroup(group, control) {
      if (!group?.source || !group.agent_id || !group.items?.length || !control) return false;
      const generation = this.generation;
      control.disabled = true;
      control.setAttribute('aria-busy', 'true');
      if (this.error) this.error.textContent = '';
      const results = await Promise.allSettled(group.items.map(async item => {
        const path = `${sourcePath(item.source)}/${encodeURIComponent(item.id)}`;
        const response = await this.fetch(path, {method: 'DELETE'});
        if (!response.ok) throw responseError(response);
        return item.id;
      }));
      if (generation !== this.generation) return false;
      const removed = new Set(results.filter(result => result.status === 'fulfilled').map(result => result.value));
      const cached = this.cache.get(group.source.id);
      if (cached) cached.notifications = cached.notifications.filter(value => !removed.has(value.id));
      this.render();
      const failed = results.filter(result => result.status === 'rejected');
      if (failed.length) {
        if (this.error) this.error.textContent = `Could not remove ${failed.length} notification${failed.length === 1 ? '' : 's'}. Refresh and try again.`;
        return false;
      }
      return true;
    }

    async removeAndOpen(group, control) {
      if (!await this.removeGroup(group, control)) return;
      this.dialog?.close();
      await this.onOpenAgent({swarmId: group.source.id, swarmName: group.source.name, agentId: group.agent_id});
    }

    async clearAll(control) {
      const sources = this.sources.filter(source => (this.cache.get(source.id)?.notifications || []).length);
      if (!sources.length || !control) return;
      const generation = this.generation;
      control.disabled = true;
      control.setAttribute('aria-busy', 'true');
      if (this.error) this.error.textContent = '';
      const results = await Promise.allSettled(sources.map(async source => {
        const response = await this.fetch(sourcePath(source), {method: 'DELETE'});
        if (!response.ok) throw responseError(response);
        return source.id;
      }));
      if (generation !== this.generation) return;
      for (const result of results) if (result.status === 'fulfilled') {
        const cached = this.cache.get(result.value);
        if (cached) cached.notifications = [];
      }
      this.render();
      control.setAttribute('aria-busy', 'false');
      const failed = results.filter(result => result.status === 'rejected').length;
      if (failed && this.error) this.error.textContent = `Could not clear notifications from ${failed} Swarm${failed === 1 ? '' : 's'}.`;
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
      const clearAll = this.query?.('#clear-notifications');
      if (clearAll) clearAll.disabled = items.length === 0;
      if (!this.list || !this.document) return;
      this.list.replaceChildren();
      if (!items.length) {
        const empty = this.document.createElement('p'); empty.className = 'notification-empty';
        empty.textContent = this.failures.size ? 'No notifications are available from the Swarms that responded.' : 'No unread notifications.';
        this.list.append(empty); return;
      }
      for (const group of this.groups()) {
        const row = this.document.createElement('article'); row.className = 'notification-item notification-group'; row.setAttribute('role', 'listitem');
        const heading = this.document.createElement('div'); heading.className = 'notification-heading';
        const identity = this.document.createElement('div');
        const swarm = this.document.createElement('span'); swarm.className = 'notification-swarm'; swarm.textContent = group.source.name;
        const agent = this.document.createElement('h3'); agent.textContent = group.agent_name;
        identity.append(swarm, agent);
        const total = this.document.createElement('span'); total.className = 'badge notification-total'; total.textContent = `${group.items.length} unread`;
        heading.append(identity, total);
        const stack = this.document.createElement('div'); stack.className = 'notification-stack';
        for (const item of group.items) {
          const notification = this.document.createElement('section'); notification.className = 'notification-entry';
          const meta = this.document.createElement('div'); meta.className = 'notification-entry-meta';
          const status = this.document.createElement('span'); status.className = `badge notification-state ${notificationStates.has(item.status) ? item.status : ''}`.trim();
          status.textContent = item.status || 'Unknown';
          const time = this.document.createElement('time'); time.dateTime = item.created_at; time.textContent = item.created_at ? new Date(item.created_at).toLocaleString() : 'Time unknown';
          meta.append(status, time);
          const text = this.document.createElement('p'); text.className = 'notification-text'; text.textContent = item.text;
          notification.append(meta, text); stack.append(notification);
        }
        const footer = this.document.createElement('footer');
        const summary = this.document.createElement('span'); summary.className = 'hint'; summary.textContent = `Clears all ${group.items.length} notification${group.items.length === 1 ? '' : 's'} from this agent.`;
        const actions = this.document.createElement('div'); actions.className = 'notification-group-actions';
        const discard = this.document.createElement('button'); discard.type = 'button'; discard.className = 'small danger'; discard.textContent = 'Discard';
        discard.setAttribute('aria-label', `Discard ${group.items.length} notification${group.items.length === 1 ? '' : 's'} from ${group.agent_name}`);
        discard.addEventListener('click', () => this.removeGroup(group, discard));
        const open = this.document.createElement('button'); open.type = 'button'; open.className = 'small primary'; open.textContent = 'Open agent';
        open.addEventListener('click', () => this.removeAndOpen(group, open));
        actions.append(discard, open);
        footer.append(summary, actions); row.append(heading, stack, footer); this.list.append(row);
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
      this.query?.('#clear-notifications')?.removeEventListener('click', this.boundClearAll);
      this.clear();
    }
  }

  const api = {NotificationsInbox, normalizePayload, sourcePath};
  root.SwarmNotifications = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
