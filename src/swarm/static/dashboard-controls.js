/* Compact dashboard controls; no agent or backend state changes. */
(function (root) {
  'use strict';
  const STATES = ['running', 'waiting', 'dormant', 'idle', 'done'];
  const LABELS = {running: 'Running', waiting: 'Waiting', dormant: 'Dormant', idle: 'Idle', done: 'Done'};

  class Dropdown {
    constructor({container, button, panel, document = container.ownerDocument}) {
      Object.assign(this, {container, button, panel, document}); this.active = false; this.generation = 0;
      this.click = () => this.active ? this.close() : this.open();
      this.outside = event => { if (this.active && !container.contains(event.target)) this.close(); };
      this.keys = event => {
        if (event.key === 'Escape' && this.active) { event.preventDefault(); event.stopPropagation(); this.close(true); }
        else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
          const editingText = ['INPUT', 'TEXTAREA'].includes(event.target?.tagName) && !['checkbox', 'radio', 'button', 'submit'].includes(event.target?.type);
          if (editingText) return;
          if (!this.active && event.target !== button) return;
          event.preventDefault(); this.open();
          const items = [...panel.querySelectorAll('button:not([disabled]),input:not([disabled])')];
          if (!items.length) return;
          const index = items.indexOf(document.activeElement);
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : event.key === 'ArrowUp' ? (index <= 0 ? items.length - 1 : index - 1) : (index + 1) % items.length;
          items[next].focus();
        }
      };
      this.blur = () => {
        const generation = this.generation;
        (document.defaultView || root).setTimeout(() => {
          if (generation === this.generation && this.active && !container.contains(document.activeElement)) this.close();
        }, 0);
      };
      button.addEventListener('click', this.click); container.addEventListener('keydown', this.keys);
      container.addEventListener('focusout', this.blur); document.addEventListener('pointerdown', this.outside, true);
      this.close();
    }
    open() { this.active = true; this.generation += 1; this.panel.hidden = false; this.button.setAttribute('aria-expanded', 'true'); }
    close(focus = false) { this.active = false; this.generation += 1; this.panel.hidden = true; this.button.setAttribute('aria-expanded', 'false'); if (focus) this.button.focus(); }
    dispose() {
      this.close(); this.button.removeEventListener('click', this.click); this.container.removeEventListener('keydown', this.keys);
      this.container.removeEventListener('focusout', this.blur); this.document.removeEventListener('pointerdown', this.outside, true);
    }
  }

  class StatusFilter {
    constructor({query, onChange}) {
      this.query = query; this.onChange = onChange; this.selected = new Set(STATES); this.agents = [];
      this.dropdown = new Dropdown({container: query('#status-filter'), button: query('#status-filter-button'), panel: query('#status-filter-menu')});
      this.inputs = [...query('#status-filter-menu').querySelectorAll('input[data-status]')];
      for (const input of this.inputs) input.addEventListener('change', () => {
        if (input.dataset.status === 'all') this.selected = new Set(input.checked ? STATES : []);
        else if (input.checked) this.selected.add(input.dataset.status);
        else this.selected.delete(input.dataset.status);
        this.update(this.agents); this.onChange();
      });
      this.update([]);
    }
    get all() { return this.selected.size === STATES.length; }
    matches(status) { return this.all || this.selected.has(status); }
    reset() { this.selected = new Set(STATES); this.update(this.agents); }
    update(agents) {
      this.agents = agents;
      const counts = Object.fromEntries(STATES.map(status => [status, 0]));
      for (const agent of agents) if (STATES.includes(agent.status)) counts[agent.status] += 1;
      for (const input of this.inputs) {
        const status = input.dataset.status, all = status === 'all', count = all ? agents.length : counts[status];
        input.checked = all ? this.all : this.selected.has(status);
        input.indeterminate = all && this.selected.size > 0 && !this.all;
        input.setAttribute('aria-label', `${all ? 'All' : LABELS[status]}, ${count} ${count === 1 ? 'agent' : 'agents'}`);
        this.query(`[data-status-count="${status}"]`).textContent = String(count);
      }
      const selected = [...this.selected], single = selected.length === 1 ? selected[0] : null;
      this.query('#status-filter-label').textContent = this.all ? 'All states' : !selected.length ? 'No states' : single ? LABELS[single] : `${selected.length} states`;
      this.query('#status-filter-count').textContent = String(this.all ? agents.length : selected.reduce((total, status) => total + counts[status], 0));
      this.query('#status-filter-dot').className = `status-dot ${single || 'all'}`;
      const search = this.query('#search')?.value?.trim() || '';
      const marker = this.query('#status-filter-search'); if (marker) marker.hidden = !search;
      this.query('#status-filter-button').title = (this.all ? 'Show all agent states' : selected.length ? selected.map(status => LABELS[status]).join(', ') : 'Choose one or more states') + (search ? `; Text filter: ${search}` : '');
    }
  }

  class DashboardControls {
    constructor({query = selector => document.querySelector(selector), onStatusChange}) {
      this.status = new StatusFilter({query, onChange: onStatusChange});
      this.adoption = new Dropdown({container: query('#adoption-menu'), button: query('#adoption-menu-button'), panel: query('#adoption-options')});
      query('#help-button').addEventListener('click', () => query('#help-dialog').showModal());
    }
    update(agents) { this.status.update(agents); }
  }

  const api = {STATES, LABELS, Dropdown, StatusFilter, DashboardControls};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmDashboardControls = api;
})(globalThis);
