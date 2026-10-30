/* Durable agent inbox UI. Message bodies are always rendered as text. */
(function (root) {
  'use strict';
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const statuses = new Set(['pending', 'delivering', 'executing', 'failed', 'uncertain']);
  function element(tag, className = '', text = '') {
    const value = document.createElement(tag); value.className = className; value.textContent = text; return value;
  }
  function requestId() {
    if (root.crypto?.randomUUID) return root.crypto.randomUUID();
    const value = new Uint8Array(16); root.crypto.getRandomValues(value);
    value[6] = (value[6] & 0x0f) | 0x40; value[8] = (value[8] & 0x3f) | 0x80;
    const hex = [...value].map(byte => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function validList(value, agentId) {
    return value?.agent_id === agentId && Number.isInteger(value.revision) && value.revision >= 0 && Array.isArray(value.messages) && value.messages.every(message =>
      message && typeof message.id === 'string' && ['message', 'command'].includes(message.kind) && typeof message.text === 'string' &&
      (message.sender_id === null || typeof message.sender_id === 'string') && statuses.has(message.status) && typeof message.created_at === 'string' &&
      (message.last_error === null || typeof message.last_error === 'string'));
  }
  function dateLabel(value) {
    const date = new Date(value); return Number.isFinite(date.getTime()) ? date.toLocaleString() : 'Unknown date';
  }

  class MessagesController {
    constructor({query = selector => document.querySelector(selector), api, onChange = async () => {}, confirm = text => root.confirm(text), create = element} = {}) {
      Object.assign(this, {query, api, onChange, confirm, create});
      this.cache = new Map(); this.mounts = new WeakMap(); this.generation = 0; this.agentId = null; this.base = null;
      this.messages = []; this.revision = null; this.loading = false; this.sending = false; this.deleting = new Set();
      this.dialog = query('#messages-dialog'); this.list = query('#agent-message-list');
      query('#refresh-agent-messages')?.addEventListener('click', () => this.refreshDialog());
      query('#agent-message-form')?.addEventListener('submit', event => { event.preventDefault(); void this.send(); });
      this.dialog?.addEventListener('close', () => { this.generation += 1; this.loading = false; this.sending = false; this.deleting.clear(); });
    }

    current(job) { return job.generation === this.generation && job.base === this.base && job.agentId === this.agentId && !!this.dialog?.open; }

    async open(agent, base, workspace) {
      this.generation += 1; this.agentId = agent.id; this.base = base; this.revision = agent.messages_revision ?? null;
      this.messages = []; this.loading = false; this.sending = false; this.deleting.clear();
      this.query('#messages-title').textContent = `Messages · ${agent.name}`;
      this.query('#messages-workspace').textContent = workspace;
      this.query('#agent-messages-error').textContent = '';
      this.query('#agent-message-form').reset();
      this.dialog.showModal();
      await this.refreshDialog();
    }

    async fetchList(base, agentId) {
      const value = await this.api(base);
      if (!validList(value, agentId)) throw Error('Invalid message list response');
      return value;
    }

    async refreshDialog() {
      if (!this.dialog?.open || this.loading) return;
      const job = {generation: this.generation, base: this.base, agentId: this.agentId};
      this.loading = true; this.query('#refresh-agent-messages').disabled = true; this.query('#agent-messages-error').textContent = '';
      if (!this.messages.length) this.list.replaceChildren(this.create('p', 'hint', 'Reading outstanding messages…'));
      try {
        const value = await this.fetchList(job.base, job.agentId);
        if (!this.current(job)) return;
        this.messages = value.messages; this.revision = value.revision;
        this.cache.set(job.base, {revision: value.revision, messages: value.messages}); this.renderDialog();
      } catch (error) {
        if (!this.current(job)) return;
        this.query('#agent-messages-error').textContent = /^(Not Found|Method Not Allowed)$/i.test(error.message)
          ? 'Messages are unavailable on this controller. Update or restart it to enable durable messages.' : error.message;
        if (!this.messages.length) this.list.replaceChildren();
      } finally {
        if (this.current(job)) { this.loading = false; this.query('#refresh-agent-messages').disabled = false; this.updateSend(); }
      }
    }

    sync(agent) {
      if (!this.dialog?.open) return;
      if (!agent || agent.id !== this.agentId || !agent.managed) { this.dialog.close(); return; }
      if ((agent.messages_revision ?? null) !== this.revision && !this.loading && !this.sending) void this.refreshDialog();
    }

    updateSend() {
      const submit = this.query('#send-agent-message');
      if (submit) { submit.disabled = this.sending; submit.textContent = this.sending ? 'Sending…' : 'Send message'; }
    }

    renderMessages(container, messages, base, options = {}) {
      container.replaceChildren();
      if (!messages.length) { container.append(this.create('p', 'hint', 'No outstanding messages.')); return; }
      for (const message of messages) {
        const row = this.create('article', `agent-message-row status-${message.status}`);
        const head = this.create('div', 'agent-message-head');
        head.append(this.create('strong', 'agent-message-status', message.status),
          this.create('span', 'hint', `${message.sender_id ? `From ${message.sender_id} · ` : ''}${dateLabel(message.created_at)}`));
        const body = this.create('p', 'agent-message-text', message.text);
        const footer = this.create('div', 'agent-message-footer');
        if (message.last_error) footer.append(this.create('span', 'agent-message-error', message.last_error));
        const remove = this.create('button', 'small danger agent-message-delete', 'Delete'); remove.type = 'button';
        const inFlight = ['delivering', 'executing'].includes(message.status);
        remove.disabled = inFlight || this.deleting.has(message.id);
        remove.title = inFlight ? 'In-flight messages or commands cannot be recalled safely' : 'Delete this outstanding message';
        remove.addEventListener('click', () => void this.remove(message, remove, base, options)); footer.append(remove);
        row.append(head, body, footer); container.append(row);
      }
    }

    renderDialog() {
      this.query('#agent-message-count').textContent = `${this.messages.length} outstanding`;
      this.renderMessages(this.list, this.messages, this.base, {dialog: true, generation: this.generation, agentId: this.agentId});
      this.updateSend();
    }

    async send() {
      if (!this.dialog?.open || this.sending) return;
      const form = this.query('#agent-message-form'), text = form.elements.text.value.trim();
      if (!text) { form.elements.text.reportValidity?.(); return; }
      let id;
      try { id = requestId(); }
      catch { this.query('#agent-messages-error').textContent = 'This browser cannot create a secure request ID. Use a recent browser or HTTPS.'; return; }
      const job = {generation: this.generation, base: this.base, agentId: this.agentId, requestId: id};
      this.sending = true; this.updateSend(); this.query('#agent-messages-error').textContent = '';
      try {
        const value = await this.api(job.base, 'POST', {text, request_id: id});
        if (!this.current(job)) return;
        if (value?.agent_id !== job.agentId || !value.message || value.message.text !== text || !statuses.has(value.message.status)) throw Error('Invalid send response');
        form.reset(); await this.onChange();
        if (this.current(job)) await this.refreshDialog();
      } catch (error) {
        if (!this.current(job)) return;
        this.query('#agent-messages-error').textContent = error.status >= 400 && error.status < 500
          ? error.message
          : `Send outcome is uncertain. Do not retry blindly. Inspect request ID ${id}. ${error.message}`;
      } finally { if (this.current(job)) { this.sending = false; this.updateSend(); } }
    }

    async remove(message, button, base, options = {}) {
      if (['delivering', 'executing'].includes(message.status) || this.deleting.has(message.id)) return;
      const warning = message.status === 'uncertain' ? '\n\nDelivery may already have happened. This removes tracking only; it cannot recall content or commands already accepted by Pi.' : '';
      if (!this.confirm(`Delete this outstanding message?${warning}\n\n${message.text}`)) return;
      const generation = options.dialog ? options.generation : null, agentId = options.agentId || this.agentId;
      this.deleting.add(message.id); button.disabled = true;
      try {
        const value = await this.api(`${base}/${encodeURIComponent(message.id)}`, 'DELETE');
        if (value?.deleted !== true) throw Error('Invalid delete response');
        const dialogCurrent = options.dialog && generation === this.generation && base === this.base && agentId === this.agentId && this.dialog.open;
        const mountCurrent = options.mount && this.mounts.get(options.mount)?.token === options.token;
        if (!dialogCurrent && !mountCurrent) return;
        this.cache.delete(base); await this.onChange();
        if (dialogCurrent && generation === this.generation && base === this.base && this.dialog.open) await this.refreshDialog();
        else if (mountCurrent && this.mounts.get(options.mount)?.token === options.token) await this.mountExpanded(options.mount, options.agent, base, true);
      } catch (error) {
        const text = error.status === 409 ? 'This message is already delivering and cannot be recalled from injection.' : error.message;
        if (options.dialog && generation === this.generation && base === this.base && this.dialog.open) this.query('#agent-messages-error').textContent = text;
        else if (options.mount && this.mounts.get(options.mount)?.token === options.token) this.renderExpandedError(options.mount, text);
      } finally { this.deleting.delete(message.id); button.disabled = false; }
    }

    renderExpandedError(container, text) {
      const current = this.mounts.get(container); if (!current) return;
      current.error = text; const error = container.querySelector?.('.expanded-messages-error'); if (error) error.textContent = text;
    }

    async mountExpanded(container, agent, base, force = false) {
      container.replaceChildren();
      const heading = this.create('div', 'expanded-messages-head');
      heading.append(this.create('h4', '', 'Messages'));
      const open = this.create('button', 'small', 'Open messages'); open.type = 'button'; open.disabled = !agent.managed;
      open.addEventListener('click', () => this.open(agent, base, optionsWorkspace(agent))); heading.append(open); container.append(heading);
      if (!agent.managed) { container.append(this.create('p', 'hint', 'Messages are unavailable for observed agents because this controller cannot manage their inbox.')); return; }
      const token = {}; this.mounts.set(container, {token, base, agentId: agent.id});
      const cached = this.cache.get(base), revision = agent.messages_revision ?? null;
      if (!force && cached && cached.revision === revision) {
        this.renderExpandedBody(container, heading, cached.messages, agent, base, token); return;
      }
      container.append(this.create('p', 'hint', 'Reading outstanding messages…'));
      try {
        const value = await this.fetchList(base, agent.id);
        if (this.mounts.get(container)?.token !== token) return;
        this.cache.set(base, {revision: value.revision, messages: value.messages});
        this.renderExpandedBody(container, heading, value.messages, agent, base, token);
      } catch (error) {
        if (this.mounts.get(container)?.token !== token) return;
        container.append(this.create('p', 'form-error expanded-messages-error', error.message));
      }
    }

    renderExpandedBody(container, heading, messages, agent, base, token) {
      container.replaceChildren(heading);
      const summary = this.create('p', 'hint', `${messages.length} outstanding`);
      container.append(summary);
      if (messages.length) {
        const list = this.create('div', 'expanded-message-list');
        container.append(list);
        this.renderMessages(list, messages, base, {mount: container, token, agent, agentId: agent.id});
      }
    }
  }
  function optionsWorkspace(agent) { return agent.workspace || 'Agent workspace'; }
  const api = {MessagesController, AgentMessagesDialog: MessagesController, requestId, validList};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmAgentMessages = api;
})(globalThis);
