/* Relay settings always use the browser's local backend, never a remote workspace. */
(function (root) {
  'use strict';

  class RelaySettingsDialog {
    // Browser timers require the Window receiver, not this dialog instance.
    constructor({query = selector => document.querySelector(selector), api, onChange = async () => {}, onOpen = () => {}, schedule = (callback, delay) => setTimeout(callback, delay), cancel = timer => clearTimeout(timer)}) {
      this.query = query; this.api = api; this.onChange = onChange; this.onOpen = onOpen;
      this.schedule = schedule; this.cancel = cancel;
      this.dialog = query('#settings-dialog'); this.form = query('#relay-settings-form');
      this.section = query('#relay-settings-section');
      this.fields = query('#relay-settings-fields'); this.action = query('#relay-connect');
      this.key = this.form.elements.relay_api_key;
      this.current = null; this.busy = false; this.loading = false; this.timer = null; this.generation = 0;
      query('#settings-button').addEventListener('click', () => this.open());
      this.form.addEventListener('submit', event => { event.preventDefault(); this.submit(); });
      this.form.elements.relay_url.addEventListener('input', () => this.render());
      this.section?.addEventListener('toggle', () => this.toggle());
      this.dialog.addEventListener('close', () => this.deactivate());
    }

    expanded() { return !this.section || !!this.section.open; }
    active() { return !!this.dialog.open && this.expanded(); }

    deactivate() {
      this.generation += 1; this.cancel(this.timer); this.timer = null; this.loading = false;
      this.key.value = ''; // No credential survives a closed or collapsed relay section.
    }

    toggle() {
      if (!this.active()) { this.deactivate(); return; }
      return this.load();
    }

    error(text) { this.query('#relay-settings-error').textContent = text; }

    render() {
      const enabled = !!this.current?.enabled;
      this.action.textContent = this.loading ? 'Loading…' : this.busy ? (enabled ? 'Disconnecting…' : 'Connecting…') : enabled ? 'Disconnect' : 'Connect';
      this.action.disabled = this.busy || this.loading || !this.current;
      this.action.classList.toggle('danger', enabled);
      this.action.classList.toggle('primary', !enabled);
      this.fields.disabled = enabled || this.busy || this.loading;
      const newRelay = this.form.elements.relay_url.value.trim() !== (this.current?.relay_url || '');
      this.key.required = !this.current?.has_api_key || newRelay;
      this.key.placeholder = newRelay && this.current?.has_api_key ? 'Enter the key for this relay URL' : this.current?.has_api_key ? 'Saved key · leave blank to reuse' : 'Relay API key (at least 32 bytes)';
      const labels = {connected: 'Connected', connecting: 'Connecting…', reconnecting: 'Reconnecting…', disconnected: 'Disconnected'};
      const status = this.query('#relay-settings-status');
      status.textContent = this.loading ? 'Loading relay configuration…' : labels[this.current?.status] || 'Disconnected';
      status.classList.toggle('live', !!this.current?.connected);
      status.classList.toggle('offline', !this.current?.connected);
      this.query('#relay-settings-connection-error').textContent = this.current?.last_error || '';
    }

    fill(value) {
      this.current = value;
      this.form.elements.relay_url.value = value.relay_url || '';
      this.form.elements.relay_node_id.value = value.relay_node_id || '';
      this.form.elements.relay_name.value = value.relay_name || '';
      this.key.value = ''; // GET never supplies the actual saved key.
    }

    async open() {
      this.query('#notifications-dialog')?.close();
      this.deactivate(); this.current = null; this.error('');
      this.dialog.showModal(); this.render();
      this.onOpen();
      if (this.expanded()) await this.load(); // Old isolated fixtures without <details> remain eager.
    }

    async load() {
      if (!this.active() || this.loading) return;
      const generation = ++this.generation;
      this.cancel(this.timer); this.timer = null; this.current = null; this.error(''); this.loading = true; this.render();
      try {
        const value = await this.api('/api/federation/settings');
        if (generation !== this.generation || !this.active()) return;
        this.fill(value);
      } catch (error) {
        if (generation === this.generation && this.active()) this.error(`Cannot load relay settings: ${error.message}`);
      } finally {
        if (generation === this.generation && this.active()) { this.loading = false; this.render(); this.pollLater(generation); }
      }
    }

    pollLater(generation) {
      this.cancel(this.timer);
      this.timer = this.schedule(async () => {
        if (generation !== this.generation || !this.active()) return;
        if (!this.busy) {
          try {
            const value = await this.api('/api/federation/settings');
            if (generation !== this.generation || !this.active()) return;
            this.current = value; this.render(); // Do not overwrite a user's unsaved fields/key.
          } catch { /* Temporary local disconnects must not discard the form draft. */ }
        }
        if (generation === this.generation && this.active()) this.pollLater(generation);
      }, 1500);
    }

    async submit() {
      if (this.busy || this.loading || !this.current) return;
      const disconnect = !!this.current.enabled;
      if (!disconnect && !this.form.reportValidity()) return;
      const body = disconnect ? undefined : {
        relay_url: this.form.elements.relay_url.value.trim(),
        relay_node_id: this.form.elements.relay_node_id.value.trim(),
        relay_name: this.form.elements.relay_name.value.trim(),
        relay_api_key: this.key.value || null,
      };
      this.key.value = '';
      const generation = ++this.generation; // Own the view before POST; an earlier poll GET is now stale.
      this.cancel(this.timer); this.timer = null; this.loading = false;
      this.busy = true; this.error(''); this.render();
      try {
        const value = await this.api(disconnect ? '/api/federation/disconnect' : '/api/federation/connect', 'POST', body);
        if (generation === this.generation && this.active()) this.fill(value);
        await this.onChange(value); // A completed local configuration change refreshes every scope.
      } catch (error) {
        if (generation === this.generation && this.active()) {
          const secret = body?.relay_api_key;
          this.error(secret ? error.message.split(secret).join('[redacted]') : error.message);
        }
      } finally {
        this.busy = false;
        if (generation === this.generation && this.active()) {
          this.loading = false; this.render(); this.pollLater(generation);
        }
      }
    }
  }

  if (typeof module === 'object' && module.exports) module.exports = {RelaySettingsDialog};
  else root.SwarmRelaySettings = {RelaySettingsDialog};
})(globalThis);
