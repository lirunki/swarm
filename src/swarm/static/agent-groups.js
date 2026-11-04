/* Family-unit selection and agent grouping. Remote names are always rendered as text. */
(function (root) {
  'use strict';

  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const validId = value => typeof value === 'string' && value.length > 0;
  const lexical = (left, right) => left < right ? -1 : left > right ? 1 : 0;

  function familyIndex(agents) {
    if (!Array.isArray(agents)) throw new TypeError('agents must be an array');
    const records = new Map();
    for (const agent of agents) if (agent && validId(agent.id) && !records.has(agent.id)) records.set(agent.id, agent);

    // Match the backend: only edges between records that currently exist form families.
    const parent = new Map(), children = new Map();
    const find = value => {
      let cursor = value;
      while (parent.get(cursor) !== cursor) cursor = parent.get(cursor);
      while (parent.get(value) !== value) { const next = parent.get(value); parent.set(value, cursor); value = next; }
      return cursor;
    };
    const join = (left, right) => {
      const a = find(left), b = find(right);
      if (a !== b) parent.set(a < b ? b : a, a < b ? a : b);
    };
    for (const id of records.keys()) { parent.set(id, id); children.set(id, new Set()); }
    for (const [id, agent] of records) {
      const parentId = agent.parent_id;
      if (validId(parentId) && parentId !== id && records.has(parentId)) {
        join(id, parentId); children.get(parentId).add(id);
      }
    }

    const components = new Map();
    for (const id of records.keys()) {
      const representative = find(id);
      if (!components.has(representative)) components.set(representative, []);
      components.get(representative).push(id);
    }
    const families = [...components.values()].map(ids => ids.sort(lexical)).sort((a, b) => lexical(a[0], b[0]));
    const index = new Map(), ranks = new Map();
    for (const ids of families) {
      for (const id of ids) index.set(id, ids);
      const idSet = new Set(ids);
      const roots = ids.filter(id => !idSet.has(records.get(id)?.parent_id)).sort(lexical);
      const rootId = roots[0] || ids[0]; // A lexical cycle cut is the backend's deterministic fallback.
      const queue = [[rootId, 0]], seen = new Set();
      while (queue.length) {
        const [id, depth] = queue.shift();
        if (seen.has(id)) continue;
        seen.add(id); ranks.set(id, depth);
        for (const child of [...children.get(id)].sort(lexical)) if (!seen.has(child)) queue.push([child, depth + 1]);
      }
      for (const id of ids) if (!seen.has(id)) ranks.set(id, ids.length);
    }
    // Useful to callers which need one entry per family; arrays are shared intentionally.
    Object.defineProperties(index, {
      families: {value: families, enumerable: false},
      ranks: {value: ranks, enumerable: false},
    });
    return index;
  }

  function projectionValue(projection) {
    const value = projection && projection.agent_groups ? projection.agent_groups : projection;
    return value && typeof value === 'object' ? value : {};
  }

  function effectiveAssignments(agents, projection) {
    const index = familyIndex(agents);
    const value = projectionValue(projection);
    const assignments = value.assignments && typeof value.assignments === 'object' ? value.assignments : {};
    const groupIds = new Set((Array.isArray(value.groups) ? value.groups : []).filter(Boolean).map(group => group.id).filter(validId));
    const result = {};

    for (const ids of index.families) {
      const candidates = ids.filter(id => own(assignments, id) && validId(assignments[id]) && groupIds.has(assignments[id]));
      const winner = candidates.sort((a, b) => index.ranks.get(a) - index.ranks.get(b) || lexical(a, b))[0];
      const selected = winner === undefined ? null : assignments[winner];
      for (const id of ids) result[id] = selected;
    }
    return result;
  }

  function defaultCreate(tag, className = '', text = '') {
    const node = root.document.createElement(tag);
    node.className = className; node.textContent = text;
    return node;
  }

  function requestId() {
    const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
    const shared = root.SwarmAgentMessages?.requestId;
    let id;
    if (typeof shared === 'function') id = shared();
    else if (root.crypto?.randomUUID) id = root.crypto.randomUUID();
    else {
      if (!root.crypto?.getRandomValues) throw Error('Secure random values are unavailable');
      const bytes = new Uint8Array(16); root.crypto.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
      id = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
    if (!uuid.test(id)) throw Error('Invalid request ID');
    return id;
  }

  function setClass(node, name, enabled) {
    if (!node) return;
    if (node.classList?.toggle) node.classList.toggle(name, !!enabled);
    else {
      const names = new Set(String(node.className || '').split(/\s+/).filter(Boolean));
      enabled ? names.add(name) : names.delete(name); node.className = [...names].join(' ');
    }
  }

  function interactiveTarget(target, boundary) {
    for (let node = target; node && node !== boundary; node = node.parentElement || node.parentNode) {
      const tag = String(node.tagName || '').toLowerCase();
      if (['button', 'a', 'input', 'select', 'textarea', 'summary'].includes(tag)) return true;
    }
    return false;
  }

  function validProjection(projection) {
    const value = projectionValue(projection);
    return !!value && Number.isInteger(value.revision) && value.revision >= 0 && Array.isArray(value.groups) &&
      value.groups.every(group => group && validId(group.id) && typeof group.name === 'string') &&
      value.assignments && typeof value.assignments === 'object' && !Array.isArray(value.assignments);
  }

  function normalizedProjection(projection) {
    const value = projectionValue(projection);
    const revision = Number.isInteger(value.revision) && value.revision >= 0 ? value.revision : 0;
    const groups = Array.isArray(value.groups) ? value.groups.filter(group => group && validId(group.id) && typeof group.name === 'string')
      .map(group => ({id: group.id, name: group.name})) : [];
    const assignments = value.assignments && typeof value.assignments === 'object' ? {...value.assignments} : {};
    return {revision, groups, assignments};
  }

  function groupName(value) {
    const name = typeof value === 'string' ? value.trim() : '';
    const folded = name.normalize?.('NFKC').toLowerCase() || name.toLowerCase();
    const reserved = new Set(['defaultgroup', 'default group', 'default', 'ungrouped']);
    if (!name || name.length > 80 || /\p{C}/u.test(name) || reserved.has(folded)) return null;
    return name;
  }

  class AgentGroupsController {
    constructor({query = selector => root.document.querySelector(selector), create = defaultCreate, api,
      currentSwarm = () => 'local', currentGeneration = () => 0, onChanged = async () => {}, onOpenGroup = () => {},
      confirm = text => root.confirm(text), prompt = (text, initial) => root.prompt(text, initial), toast = () => {},
      requestTimeout = 20000} = {}) {
      Object.assign(this, {query, create, api, currentSwarm, currentGeneration, onChanged, onOpenGroup, confirm, prompt, toast, requestTimeout});
      this.agents = []; this.projection = normalizedProjection(null); this.projectionValid = false; this.error = null;
      this.index = familyIndex([]); this.selectedIDs = new Set(); this.cards = new Map(); this.busy = false; this.operation = 0;
      this.source = currentSwarm(); this.generation = currentGeneration();
      this.bar = query('#group-selection-bar'); this.count = query('#group-selection-count');
      this.openButton = query('#group-selection-open'); this.clearButton = query('#group-selection-clear');
      this.menu = query('#group-context-menu'); this.submenuButton = query('#grouping-submenu-button');
      this.submenu = query('#grouping-submenu');
      this.menu?.setAttribute?.('role', 'menu'); this.submenu?.setAttribute?.('role', 'menu');
      this.submenuButton?.setAttribute?.('role', 'menuitem'); this.submenuButton?.setAttribute?.('aria-haspopup', 'menu');
      this.submenuButton?.setAttribute?.('aria-expanded', 'false');
      this.openButton?.addEventListener('click', event => { event.stopPropagation?.(); this.openSelected(); });
      this.clearButton?.addEventListener('click', () => this.clearSelection());
      this.submenuButton?.addEventListener('click', event => { event?.stopPropagation?.(); this.toggleSubmenu(); });
      const dismissOutside = event => { if (this.menuOpen() && !this.menu?.contains?.(event.target)) this.closeMenu(); };
      root.document?.addEventListener?.('pointerdown', dismissOutside);
      root.document?.addEventListener?.('click', dismissOutside);
      root.document?.addEventListener?.('keydown', event => { if (event.key === 'Escape' && this.menuOpen()) { event.preventDefault?.(); this.closeMenu(); } });
      this.menu?.addEventListener('keydown', event => {
        const items=[...this.menu.querySelectorAll('[role="menuitem"]')].filter(item=>!item.disabled && !item.closest('[hidden]'));
        const index=items.indexOf(event.target);
        if(event.key==='ArrowRight' && event.target===this.submenuButton){event.preventDefault();this.toggleSubmenu(true);this.submenu?.querySelector('[role="menuitem"]')?.focus();}
        else if(event.key==='ArrowLeft'){event.preventDefault();this.toggleSubmenu(false);this.submenuButton?.focus();}
        else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key) && items.length){
          event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length;items[next].focus();
        }
      });
      root.addEventListener?.('blur', () => this.closeMenu());
      this.renderMenu(); this.renderSelection();
    }

    contextCurrent(source = this.source, generation = this.generation) {
      return source === this.currentSwarm() && generation === this.currentGeneration();
    }

    ensureCurrent() {
      if (this.contextCurrent()) return true;
      this.source = this.currentSwarm(); this.generation = this.currentGeneration();
      this.operation += 1; this.busy = false;
      this.selectedIDs.clear(); this.closeMenu(); this.renderMenu(); this.renderSelection();
      return false;
    }

    setState(agents, projection, error = null) {
      const source = this.currentSwarm(), generation = this.currentGeneration();
      const contextChanged = source !== this.source || generation !== this.generation;
      const menuWasOpen = this.menuOpen(), submenuWasOpen = !!this.submenu && !this.submenu.hidden;
      const previousGroups = JSON.stringify(this.projection.groups.slice().sort((a, b) => lexical(a.id, b.id)));
      const mutationsWereDisabled = !!this.error || !this.projectionValid;
      this.source = source; this.generation = generation; this.error = error || null;
      if (contextChanged) { this.operation += 1; this.busy = false; }
      for (const entry of this.cards.values()) { setClass(entry.card, 'is-group-selected', false); if (entry.checkbox) entry.checkbox.checked = false; }
      this.cards.clear();
      this.agents = Array.isArray(agents) ? agents.filter(agent => agent && validId(agent.id)) : [];
      this.index = familyIndex(this.agents); this.projectionValid = validProjection(projection); this.projection = normalizedProjection(projection);
      if (contextChanged) this.selectedIDs.clear();
      else {
        const retained = new Set([...this.selectedIDs].filter(id => this.index.has(id)));
        this.selectedIDs.clear();
        for (const id of retained) for (const member of this.index.get(id)) this.selectedIDs.add(member);
      }
      const groupsChanged = previousGroups !== JSON.stringify(this.projection.groups.slice().sort((a, b) => lexical(a.id, b.id)));
      const mutationAvailabilityChanged = mutationsWereDisabled !== (!!this.error || !this.projectionValid);
      if (groupsChanged || mutationAvailabilityChanged) this.renderMenu();
      if (contextChanged) this.closeMenu();
      else if (menuWasOpen) {
        this.menu.hidden = false;
        if (this.submenu) this.submenu.hidden = !submenuWasOpen;
        this.submenuButton?.setAttribute?.('aria-expanded', submenuWasOpen ? 'true' : 'false');
        this.reclampMenu();
      }
      this.renderSelection();
    }

    family(id) { return this.index.get(id) || (validId(id) ? [id] : []); }

    selectFamily(id, selected = true, additive = true) {
      if (!this.ensureCurrent()) return;
      if (!additive) this.selectedIDs.clear();
      for (const member of this.family(id)) selected ? this.selectedIDs.add(member) : this.selectedIDs.delete(member);
      this.renderSelection();
    }

    clearSelection() { this.selectedIDs.clear(); this.closeMenu(); this.renderSelection(); }

    decorateCard(card, agent) {
      if (!card || !agent || !validId(agent.id)) return card;
      if (!card.getAttribute || card.getAttribute('tabindex') === null) card.setAttribute?.('tabindex', '0');
      const previous = this.cards.get(agent.id);
      if (previous?.card !== card) setClass(previous?.card, 'is-group-selected', false);
      const checkbox = this.create('input', 'group-family-select');
      checkbox.type = 'checkbox'; checkbox.setAttribute?.('aria-label', `Select family of ${agent.name || agent.id}`);
      checkbox.checked = this.selectedIDs.has(agent.id);
      checkbox.addEventListener('click', event => {
        event.stopPropagation?.(); this.selectFamily(agent.id, !!checkbox.checked, true);
      });
      card.append?.(checkbox);
      card.addEventListener?.('click', event => {
        if (interactiveTarget(event.target, card)) return;
        if (event.ctrlKey || event.metaKey) {
          const selected = this.family(agent.id).every(id => this.selectedIDs.has(id));
          this.selectFamily(agent.id, !selected, true);
        } else this.selectFamily(agent.id, true, false);
      });
      card.addEventListener?.('contextmenu', event => {
        event.preventDefault?.();
        if (!this.selectedIDs.has(agent.id)) this.selectFamily(agent.id, true, true);
        this.openMenu(event.clientX, event.clientY, false);
      });
      card.addEventListener?.('keydown', event => {
        if (event.key === 'F10' && event.shiftKey) {
          event.preventDefault?.();
          if (!this.selectedIDs.has(agent.id)) this.selectFamily(agent.id, true, true);
          const box = card.getBoundingClientRect?.() || {left: 0, bottom: 0}; this.openMenu(box.left, box.bottom, true);
        }
      });
      card.addEventListener?.('pointerup', event => {
        if (event.pointerType === 'touch' && !interactiveTarget(event.target, card)) this.selectFamily(agent.id, true, false);
      });
      this.cards.set(agent.id, {card, checkbox}); this.renderSelection(); return card;
    }

    selectedFamilies() {
      const seen = new Set(), values = [];
      for (const id of this.selectedIDs) {
        const family = this.family(id), key = family[0];
        if (!seen.has(key)) { seen.add(key); values.push(family); }
      }
      return values.sort((a, b) => lexical(a[0], b[0]));
    }

    renderSelection() {
      for (const [id, entry] of this.cards) {
        const selected = this.selectedIDs.has(id);
        setClass(entry.card, 'is-group-selected', selected); entry.checkbox.checked = selected;
      }
      const count = this.selectedFamilies().length;
      if (this.count) this.count.textContent = `${count} family unit${count === 1 ? '' : 's'} selected`;
      if (this.bar) { this.bar.hidden = count === 0; this.bar.setAttribute?.('aria-hidden', count ? 'false' : 'true'); }
      const mutationsDisabled = this.busy || !!this.error || !this.projectionValid;
      if (this.openButton) this.openButton.disabled = count === 0 || mutationsDisabled;
      if (this.clearButton) this.clearButton.disabled = count === 0;
      if (this.submenuButton) this.submenuButton.disabled = count === 0 || mutationsDisabled;
      return count;
    }

    menuOpen() { return !!this.menu && !this.menu.hidden; }
    closeMenu() {
      if (this.menu) this.menu.hidden = true;
      if (this.submenu) this.submenu.hidden = true;
      this.submenuButton?.setAttribute?.('aria-expanded', 'false');
    }
    toggleSubmenu(force) {
      if (!this.submenu) return;
      this.submenu.hidden = force === undefined ? !this.submenu.hidden : !force;
      this.submenuButton?.setAttribute?.('aria-expanded', this.submenu.hidden ? 'false' : 'true');
      if (this.menuOpen()) this.reclampMenu();
    }

    reclampMenu(x, y) {
      if (!this.menu || !this.menuOpen()) return;
      const width = this.menu.offsetWidth || 220, height = this.menu.offsetHeight || 200;
      const viewportWidth = root.innerWidth || root.document?.documentElement?.clientWidth || width;
      const viewportHeight = root.innerHeight || root.document?.documentElement?.clientHeight || height;
      const requestedLeft = x === undefined ? parseFloat(this.menu.style?.left) || 0 : Number(x) || 0;
      const requestedTop = y === undefined ? parseFloat(this.menu.style?.top) || 0 : Number(y) || 0;
      const left = Math.max(0, Math.min(requestedLeft, viewportWidth - Math.min(width, viewportWidth)));
      const top = Math.max(0, Math.min(requestedTop, viewportHeight - Math.min(height, viewportHeight)));
      if (this.menu.style) {
        this.menu.style.left = `${left}px`; this.menu.style.top = `${top}px`;
        this.menu.style.maxHeight = `calc(100vh - ${top}px)`; this.menu.style.overflowY = 'auto';
      }
    }

    openMenu(x = 0, y = 0, keyboard = false) {
      if (!this.menu || !this.selectedIDs.size || this.busy || this.error || !this.projectionValid) return;
      this.renderMenu(); this.menu.hidden = false; this.reclampMenu(x, y);
      if (keyboard) (this.menu.querySelector?.('[role="menuitem"]') || this.submenuButton)?.focus?.();
    }

    menuButton(text, callback) {
      const button = this.create('button', 'group-menu-item', text); button.type = 'button'; button.setAttribute?.('role', 'menuitem');
      button.disabled = this.busy || !!this.error || !this.projectionValid;
      button.addEventListener('click', event => { event.stopPropagation?.(); this.closeMenu(); void callback(); });
      return button;
    }

    renderMenu() {
      if (!this.submenu) return;
      const children = [
        this.menuButton('Default group', () => this.assignGroup(null)),
        this.menuButton('New group…', () => this.createGroup()),
      ];
      for (const group of this.projection.groups.slice().sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id)))
        children.push(this.menuButton(group.name, () => this.assignGroup(group.id)));
      this.submenu.replaceChildren(...children); this.submenu.setAttribute?.('role', 'menu');
    }

    openSelected() {
      if (!this.ensureCurrent() || !this.selectedIDs.size || !this.openButton) return;
      const box = this.openButton.getBoundingClientRect?.() || {left: 0, bottom: 0};
      this.openMenu(box.left, box.bottom, true);
      this.toggleSubmenu(true);
    }

    snapshot(requireSelection = true) {
      if (!this.ensureCurrent() || this.busy || this.error || !this.projectionValid) return null;
      const agentIds = [...this.selectedIDs].sort();
      if (requireSelection && (!agentIds.length || agentIds.some(id => !validId(id) || !this.index.has(id)))) return null;
      return {source: this.source, generation: this.generation, revision: this.projection.revision,
        agent_ids: agentIds, operation: ++this.operation};
    }

    setBusy(value) { this.busy = !!value; this.renderMenu(); this.renderSelection(); }

    mutationCurrent(snapshot) {
      return snapshot?.operation === this.operation && this.contextCurrent(snapshot.source, snapshot.generation);
    }

    releaseBusy(snapshot) {
      if (!this.mutationCurrent(snapshot)) return false;
      this.setBusy(false); return true;
    }

    transitionStaleContext(snapshot) {
      if (!this.contextCurrent(snapshot.source, snapshot.generation)) this.ensureCurrent();
    }

    async mutation(action, snapshot, fields) {
      const callApi = this.api, endpoint = '/api/groups';
      let id;
      try { id = requestId(); }
      catch { this.toast('This browser cannot create a secure request ID. Use a recent browser or HTTPS.'); return false; }
      if (!this.mutationCurrent(snapshot)) { this.transitionStaleContext(snapshot); return false; }
      const body = {action, revision: snapshot.revision, request_id: id, ...fields};
      let timer;
      try {
        const request = Promise.resolve(callApi(endpoint, 'POST', body));
        const timeout = new Promise((resolve, reject) => {
          timer = root.setTimeout(() => reject(Error(`Group update timed out after ${this.requestTimeout}ms`)), this.requestTimeout);
        });
        const response = await Promise.race([request, timeout]);
        if (!this.mutationCurrent(snapshot)) { this.transitionStaleContext(snapshot); return false; }
        if (!validProjection(response)) throw Error('Invalid group update response');
        const value = normalizedProjection(response);
        await this.onChanged(value, {operation: action});
        return true;
      } catch (error) {
        if (!this.mutationCurrent(snapshot)) { this.transitionStaleContext(snapshot); return false; }
        if (error?.status === 409) {
          this.toast(error.message || 'Groups changed. Refreshing before another edit.');
          await this.onChanged(null, {refresh: true});
        } else if (error?.status >= 400 && error.status < 500) this.toast(error.message || `Group update failed (${error.status}).`);
        else this.toast(`Group update outcome is uncertain. Do not retry blindly. Request ID ${id}. ${error?.message || ''}`.trim());
        return false;
      } finally {
        if (timer !== undefined) root.clearTimeout(timer);
      }
    }

    async createGroup() {
      const snapshot = this.snapshot(true); if (!snapshot) return false; this.setBusy(true);
      try {
        const proposed = await this.prompt('Name this group (1–80 characters):', '');
        if (!this.mutationCurrent(snapshot) || proposed === null || proposed === undefined) return false;
        const name = groupName(proposed);
        if (!name) { this.toast('Group names must contain 1–80 characters and cannot use a reserved default-group name.'); return false; }
        return await this.mutation('create', snapshot, {name, agent_ids: snapshot.agent_ids});
      } finally { this.releaseBusy(snapshot); }
    }

    async assignGroup(groupId) {
      const snapshot = this.snapshot(true); if (!snapshot) return false; this.setBusy(true);
      try {
        if (groupId !== null && !this.projection.groups.some(group => group.id === groupId)) { this.toast('That group no longer exists.'); return false; }
        return await this.mutation('assign', snapshot, {group_id: groupId, agent_ids: snapshot.agent_ids});
      } finally { this.releaseBusy(snapshot); }
    }

    async renameGroup(id) {
      const snapshot = this.snapshot(false), group = this.projection.groups.find(value => value.id === id); if (!snapshot || !group) return false;
      this.setBusy(true);
      try {
        const proposed = await this.prompt(`Rename “${group.name}”:`, group.name);
        if (!this.mutationCurrent(snapshot) || proposed === null || proposed === undefined) return false;
        const name = groupName(proposed);
        if (!name) { this.toast('Group names must contain 1–80 characters and cannot use a reserved default-group name.'); return false; }
        return await this.mutation('rename', snapshot, {group_id: id, name});
      } finally { this.releaseBusy(snapshot); }
    }

    async deleteGroup(id) {
      const snapshot = this.snapshot(false), group = this.projection.groups.find(value => value.id === id); if (!snapshot || !group) return false;
      this.setBusy(true);
      try {
        const accepted = await this.confirm(`Delete group “${group.name}”?\n\nIts families return to the default group. Agents are never deleted.`);
        if (!accepted || !this.mutationCurrent(snapshot)) return false;
        return await this.mutation('delete', snapshot, {group_id: id});
      } finally { this.releaseBusy(snapshot); }
    }
  }

  const api = {familyIndex, effectiveAssignments, validProjection, AgentGroupsController};
  root.SwarmAgentGroups = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
