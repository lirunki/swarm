/* Mobile terminal accessory keys. Geometry/focus gate every key; desktop is untouched. */
(function (root) {
  'use strict';
  const ESC = '\x1b';
  const baselines = new WeakMap();
  const KEYS = [['ctrl', 'Ctrl', 'Control modifier'], ['alt', 'Alt', 'Alt modifier'],
    ['left', '←', 'Arrow left'], ['up', '↑', 'Arrow up'], ['down', '↓', 'Arrow down'], ['right', '→', 'Arrow right'],
    ['escape', 'Esc', 'Escape'], ['delete', 'Supr', 'Delete forward']];

  function controlCharacter(text) {
    const code = text.codePointAt(0);
    if ((code >= 65 && code <= 90) || (code >= 97 && code <= 122)) return String.fromCharCode(code & 31);
    const codes = {' ': 0, '@': 0, '`': 0, '2': 0, '[': 27, '3': 27, '\\': 28, '4': 28, ']': 29, '5': 29, '^': 30, '6': 30, '_': 31, '7': 31, '?': 127, '8': 127};
    return Object.hasOwn(codes, text) ? String.fromCharCode(codes[text]) : text;
  }

  function encodeKey(key, modifiers = {}, modes = {}) {
    const {ctrl = false, alt = false} = modifiers, parameter = 1 + (ctrl ? 4 : 0) + (alt ? 2 : 0);
    const arrows = {up: 'A', down: 'B', right: 'C', left: 'D'};
    if (Object.hasOwn(arrows, key)) return parameter > 1 ? `${ESC}[1;${parameter}${arrows[key]}` : `${ESC}${modes.applicationCursorKeysMode ? 'O' : '['}${arrows[key]}`;
    if (key === 'delete') return parameter > 1 ? `${ESC}[3;${parameter}~` : `${ESC}[3~`;
    if (key === 'escape') return alt ? ESC + ESC : ESC;
    throw new Error('Unsupported terminal accessory key');
  }

  function metrics(view, document) {
    const viewport = view.visualViewport;
    const width = view.innerWidth || document.documentElement.clientWidth;
    const height = Math.max(view.innerHeight || 0, document.documentElement.clientHeight || 0);
    const scale = viewport?.scale || 1;
    const physicalHeight = (viewport?.height || height) * scale;
    let baseline = baselines.get(view);
    if (!baseline || Math.abs(width - baseline.width) > 50) {
      baseline = {width, height: Math.max(height, physicalHeight)}; baselines.set(view, baseline);
    } else baseline.height = Math.max(baseline.height, height, physicalHeight);
    const keyboard = view.navigator?.virtualKeyboard?.boundingRect;
    const explicit = !!keyboard && keyboard.height > 0;
    const visible = explicit || baseline.height - physicalHeight > Math.max(120, baseline.height * 0.18);
    const top = viewport?.offsetTop || 0;
    let bottom = top + (viewport?.height || height);
    if (explicit && keyboard.top > top) bottom = Math.min(bottom, keyboard.top);
    return {visible, top, bottom, left: viewport?.offsetLeft || 0, width: viewport?.width || width};
  }

  class TerminalKeys {
    constructor({term, pane, isActive, ready, send, document = pane.ownerDocument, media}) {
      Object.assign(this, {term, pane, isActive, ready, send, document}); this.view = document.defaultView || root;
      this.media = media || this.view.matchMedia?.('(hover: none) and (pointer: coarse)') || {matches: false};
      this.ctrl = false; this.alt = false; this.visible = false; this.disposed = false; this.listeners = [];
      this.originalBottom = pane.style.bottom || ''; this.reserved = false;
      this.bar = document.createElement('div'); this.bar.className = 'terminal-keybar'; this.bar.hidden = true;
      this.bar.setAttribute('role', 'toolbar'); this.bar.setAttribute('aria-label', 'Mobile terminal keyboard');
      this.bar.title = 'Ctrl and Alt apply once to the next key';
      this.buttons = new Map();
      for (const [key, label, description] of KEYS) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
        button.dataset.key = key; button.setAttribute('aria-label', description); button.title = description;
        if (key === 'ctrl' || key === 'alt') button.setAttribute('aria-pressed', 'false');
        this.listen(button, 'pointerdown', event => event.preventDefault(), {passive: false});
        this.listen(button, 'mousedown', event => event.preventDefault());
        this.listen(button, 'click', () => this.press(key));
        this.buttons.set(key, button); this.bar.append(button);
      }
      document.body.append(this.bar);
      const update = () => this.update();
      this.listen(document, 'focusin', update, true); this.listen(document, 'focusout', update, true);
      this.listen(document, 'visibilitychange', update);
      this.listen(document, 'paste', () => { if (this.focused()) this.clearModifiers(); }, true);
      this.listen(this.view, 'resize', update); this.listen(this.view, 'orientationchange', update);
      if (this.view.visualViewport) {
        this.listen(this.view.visualViewport, 'resize', update); this.listen(this.view.visualViewport, 'scroll', update);
      }
      if (this.view.navigator?.virtualKeyboard) this.listen(this.view.navigator.virtualKeyboard, 'geometrychange', update);
      if (this.media.addEventListener) this.listen(this.media, 'change', update);
      else { this.media.addListener?.(update); this.legacyMedia = update; }
      this.update();
    }

    listen(target, name, callback, options = false) {
      target.addEventListener(name, callback, options); this.listeners.push([target, name, callback, options]);
    }

    focused() {
      const input = this.term.textarea || this.term.element?.querySelector('.xterm-helper-textarea');
      return !!input && this.document.activeElement === input;
    }

    renderModifiers() {
      for (const key of ['ctrl', 'alt']) this.buttons.get(key).setAttribute('aria-pressed', String(this[key]));
    }

    clearModifiers() { this.ctrl = false; this.alt = false; this.renderModifiers(); }

    update() {
      if (this.disposed) return;
      const geometry = metrics(this.view, this.document);
      this.visible = !!this.media.matches && !this.document.hidden && this.isActive() && this.focused() && geometry.visible;
      this.bar.hidden = !this.visible;
      const ready = this.visible && this.ready();
      for (const button of this.buttons.values()) button.disabled = !ready;
      if (!ready) this.clearModifiers();
      if (!this.visible) {
        if (this.reserved) { this.pane.style.bottom = this.originalBottom; this.reserved = false; }
        return;
      }
      this.bar.style.left = `${geometry.left}px`; this.bar.style.width = `${geometry.width}px`;
      const height = this.bar.getBoundingClientRect().height || 48;
      const top = Math.max(geometry.top, geometry.bottom - height);
      this.bar.style.top = `${top}px`;
      const parent = this.pane.parentElement?.getBoundingClientRect();
      if (parent) { this.pane.style.bottom = `${Math.max(0, parent.bottom - top)}px`; this.reserved = true; }
    }

    press(key) {
      if (!this.visible || !this.focused() || !this.ready()) { this.clearModifiers(); return; }
      if (key === 'ctrl' || key === 'alt') { this[key] = !this[key]; this.renderModifiers(); return; }
      const data = encodeKey(key, {ctrl: this.ctrl, alt: this.alt}, this.term.modes);
      this.clearModifiers(); this.send(data);
    }

    transform(data) {
      if (!this.ctrl && !this.alt) return data;
      if (!this.visible || !this.focused() || !this.ready()) { this.clearModifiers(); return data; }
      const shortcuts = {'\x1b[A': 'up', '\x1b[B': 'down', '\x1b[C': 'right', '\x1b[D': 'left',
        '\x1bOA': 'up', '\x1bOB': 'down', '\x1bOC': 'right', '\x1bOD': 'left', '\x1b[3~': 'delete', '\x1b': 'escape'};
      if (Object.hasOwn(shortcuts, data)) {
        const value = encodeKey(shortcuts[data], {ctrl: this.ctrl, alt: this.alt}, this.term.modes);
        this.clearModifiers(); return value;
      }
      if (data.startsWith('\x1b[200~')) { this.clearModifiers(); return data; }
      // Mouse reports, device replies and other protocol sequences aren't a typed key.
      if (data.startsWith(ESC)) return data;
      if ([...data].length !== 1) { this.clearModifiers(); return data; } // Never control-encode a paste/IME word.
      const value = (this.alt ? ESC : '') + (this.ctrl ? controlCharacter(data) : data);
      this.clearModifiers(); return value;
    }

    dispose() {
      this.disposed = true; this.visible = false; this.clearModifiers();
      for (const [target, name, callback, options] of this.listeners) target.removeEventListener(name, callback, options);
      if (this.legacyMedia) this.media.removeListener?.(this.legacyMedia);
      this.listeners = []; if (this.reserved) this.pane.style.bottom = this.originalBottom;
      this.bar.remove();
    }
  }

  // Capture the pre-keyboard viewport before the first terminal is focused.
  if (root.document) metrics(root, root.document);
  const api = {TerminalKeys, encodeKey, controlCharacter, metrics};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmTerminalKeys = api;
})(globalThis);
