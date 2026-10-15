/* Display preferences only: never modify agent status or process state. */
(function (root) {
  'use strict';

  function defaultCompact(agent) {
    return !agent.alive || ['idle', 'dormant', 'done'].includes(agent.status);
  }

  class CardViews {
    constructor(getStorage = () => root.localStorage) {
      this.getStorage = getStorage;
      this.registries = new Map();
    }

    key(home) {
      return `swarm.cardViews.v1:${home || ''}`;
    }

    overrides(home) {
      const key = this.key(home);
      if (!this.registries.has(key)) {
        const values = new Map();
        try {
          const saved = JSON.parse(this.getStorage()?.getItem(key) || '{}');
          if (saved && typeof saved === 'object' && !Array.isArray(saved)) {
            for (const [id, compact] of Object.entries(saved)) {
              if (typeof compact === 'boolean') values.set(id, compact);
            }
          }
        } catch { /* Blocked storage or malformed values must not break the dashboard. */ }
        this.registries.set(key, values);
      }
      return this.registries.get(key);
    }

    isCompact(home, agent) {
      const value = this.overrides(home).get(agent.id);
      return typeof value === 'boolean' ? value : defaultCompact(agent);
    }

    forget(home, id) {
      const values = this.overrides(home);
      if (!values.delete(id)) return;
      try {
        this.getStorage()?.setItem(this.key(home), JSON.stringify(Object.fromEntries(values)));
      } catch { /* Removing a record must work with blocked storage too. */ }
    }

    setCompact(home, id, compact) {
      if (typeof compact !== 'boolean') throw new TypeError('compact must be a boolean');
      const values = this.overrides(home);
      values.set(id, compact);
      try {
        this.getStorage()?.setItem(this.key(home), JSON.stringify(Object.fromEntries(values)));
      } catch { /* Keep the override in memory when persistence is unavailable. */ }
    }
  }

  function applyCardView({card, details, foot, avatar, subtitle, toggle}, compact, name) {
    card.classList.toggle('is-compact', compact);
    details.hidden = compact;
    foot.hidden = compact;
    avatar.hidden = compact;
    subtitle.hidden = compact;
    toggle.textContent = compact ? '▾' : '▴';
    toggle.setAttribute('aria-expanded', String(!compact));
    toggle.setAttribute('aria-label', `${compact ? 'Expand' : 'Compact'} card for ${name}`);
    toggle.title = compact ? 'Expand card' : 'Compact card';
  }

  // Classic browser script plus dependency-free Node tests; no frontend build step.
  const api = {CardViews, defaultCompact, applyCardView};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmCardViews = api;
})(globalThis);
