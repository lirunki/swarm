/* Multiply only xterm's negotiated mouse-wheel reports for a real wheel event. */
(function (root) {
  'use strict';

  const STORAGE_KEY = 'swarm.terminal-wheel-multiplier';
  const DEFAULT_MULTIPLIER = 1;
  const MAX_INPUT_BYTES = 131072;
  const MOUSE_MODIFIERS = 4 | 8 | 16;

  function validMultiplier(value) {
    return Number.isInteger(value) && value >= 1 && value <= 20;
  }

  function storedMultiplier(value) {
    if (typeof value !== 'string' || !/^(?:[1-9]|1\d|20)$/.test(value)) return DEFAULT_MULTIPLIER;
    const parsed = Number(value);
    return validMultiplier(parsed) ? parsed : DEFAULT_MULTIPLIER;
  }

  function browserStorage() {
    try { return root.localStorage; } catch { return null; }
  }

  class WheelPreferences {
    constructor(options = {}) {
      // Accept a Storage object directly as a convenience for small embedders.
      if (options && typeof options.getItem === 'function' && !Object.hasOwn(options, 'storage')) options = {storage: options};
      const supplied = Object.hasOwn(options, 'storage');
      this.storage = supplied ? options.storage : browserStorage();
      this.key = options.key || STORAGE_KEY;
      this.listeners = new Set();
      if (typeof options.onChanged === 'function') this.listeners.add(options.onChanged);
      this.current = DEFAULT_MULTIPLIER;
      this.reload();
    }

    get value() { return this.current; }

    subscribe(callback) {
      if (typeof callback !== 'function') throw new TypeError('Wheel preference subscriber must be a function');
      this.listeners.add(callback);
      return () => this.listeners.delete(callback);
    }

    changed(value) {
      for (const callback of [...this.listeners]) callback(value);
    }

    set(value) {
      if (!validMultiplier(value)) throw new RangeError('Mouse wheel multiplier must be an integer from 1 to 20');
      if (!this.storage || typeof this.storage.setItem !== 'function') throw new Error('Browser storage is unavailable');
      // Persistence comes first: a failed write must never silently change the live setting.
      this.storage.setItem(this.key, String(value));
      if (this.current !== value) { this.current = value; this.changed(value); }
      return value;
    }

    reload() {
      let value = DEFAULT_MULTIPLIER;
      try {
        if (this.storage && typeof this.storage.getItem === 'function') value = storedMultiplier(this.storage.getItem(this.key));
      } catch { value = DEFAULT_MULTIPLIER; }
      if (this.current !== value) { this.current = value; this.changed(value); }
      return value;
    }
  }

  class WheelSettingsController {
    constructor({query = selector => root.document?.querySelector(selector), preferences = new WheelPreferences(), onError = () => {}} = {}) {
      Object.assign(this, {query, preferences, onError});
      this.input = query('#mouse-wheel-multiplier');
      this.saveButton = query('#mouse-wheel-save');
      this.status = query('#mouse-wheel-status');
      this.dirty = false; this.message = '';
      if (this.input) {
        this.input.type = 'number'; this.input.min = '1'; this.input.max = '20'; this.input.step = '1';
        this.inputListener = () => { this.dirty = true; this.message = ''; this.render(); };
        this.input.addEventListener('input', this.inputListener);
      }
      if (this.saveButton) {
        this.saveListener = event => { event?.preventDefault?.(); this.save(); };
        this.saveButton.addEventListener('click', this.saveListener);
      }
      this.render(true);
    }

    report(error) {
      const value = error instanceof Error ? error : new Error(String(error));
      this.message = value.message;
      try { this.onError(value); } catch { /* Reporting must not hide the original settings state. */ }
      this.render();
      return false;
    }

    render(force = false) {
      if (this.input && (force || !this.dirty)) this.input.value = String(this.preferences.value);
      if (this.status) this.status.textContent = this.message || (this.dirty ? 'Unsaved changes' : `Saved · ${this.preferences.value}×`);
      return this.preferences.value;
    }

    open() {
      this.dirty = false; this.message = '';
      this.preferences.reload();
      return this.render(true);
    }

    reload() {
      const value = this.preferences.reload();
      this.render(); // Preserve a draft; callers may force a refresh with open()/render(true).
      return value;
    }

    save() {
      const raw = String(this.input?.value ?? '');
      if (!/^(?:[1-9]|1\d|20)$/.test(raw)) return this.report(new RangeError('Mouse wheel multiplier must be an integer from 1 to 20'));
      const value = Number(raw);
      try { this.preferences.set(value); }
      catch (error) { return this.report(error); }
      this.dirty = false; this.message = `Saved · ${value}×`; this.render(true);
      return true;
    }

    dispose() {
      if (this.inputListener) this.input?.removeEventListener('input', this.inputListener);
      if (this.saveListener) this.saveButton?.removeEventListener('click', this.saveListener);
    }
  }

  function mouseTracking(term) {
    return ['vt200', 'drag', 'any'].includes(term?.modes?.mouseTrackingMode);
  }

  function trustedVerticalWheel(event) {
    return event?.isTrusted === true && Number.isFinite(event.deltaY) && event.deltaY !== 0 &&
      Number.isFinite(event.deltaX) && (event.deltaX === 0 || Math.abs(event.deltaX) < Math.abs(event.deltaY));
  }

  function wheelButton(button) {
    // Bound the protocol byte before bitwise masking so huge decimal SGR values
    // cannot wrap to a valid button through JavaScript's 32-bit coercion.
    if (!Number.isInteger(button) || button < 0 || button > 255) return false;
    const base = button & ~MOUSE_MODIFIERS;
    return base === 64 || base === 65;
  }

  function coordinate(value, maximum = 65535) {
    return Number.isInteger(value) && value > 0 && value <= maximum;
  }

  function utf8Bytes(text) {
    let bytes = 0;
    for (let index = 0; index < text.length; index++) {
      const code = text.charCodeAt(index);
      if (code < 0x80) bytes += 1;
      else if (code < 0x800) bytes += 2;
      else if (code >= 0xd800 && code <= 0xdbff && index + 1 < text.length && text.charCodeAt(index + 1) >= 0xdc00 && text.charCodeAt(index + 1) <= 0xdfff) { bytes += 4; index += 1; }
      else bytes += 3;
      if (bytes > MAX_INPUT_BYTES) return bytes;
    }
    return bytes;
  }

  function duplicateReports(data, count, expectedButton) {
    const report = /\x1b\[<(\d+);(\d+);(\d+)M|\x1b\[M([\s\S])([\s\S])([\s\S])/gy;
    let offset = 0, transformed = '';
    while (offset < data.length) {
      report.lastIndex = offset;
      const match = report.exec(data);
      if (!match || match.index !== offset) return data;
      let button, column, row;
      if (match[1] !== undefined) {
        button = Number(match[1]); column = Number(match[2]); row = Number(match[3]);
        if (!coordinate(column) || !coordinate(row)) return data;
      } else {
        const buttonCode = match[4].charCodeAt(0), columnCode = match[5].charCodeAt(0), rowCode = match[6].charCodeAt(0);
        if ([buttonCode, columnCode, rowCode].some(code => code < 33 || code > 255)) return data;
        button = buttonCode - 32; column = columnCode - 32; row = rowCode - 32;
        if (!coordinate(column, 223) || !coordinate(row, 223)) return data;
      }
      if (!wheelButton(button) || (button & ~MOUSE_MODIFIERS) !== expectedButton) return data;
      transformed += match[0].repeat(count);
      offset = report.lastIndex;
    }
    if (!offset || utf8Bytes(transformed) > MAX_INPUT_BYTES) return data;
    return transformed;
  }

  const preferences = new WheelPreferences();

  class TerminalWheelMultiplier {
    constructor({element, term, canScale = () => true, multiplier = () => preferences.value,
      queueMicrotask: enqueue = root.queueMicrotask?.bind(root) || (callback => Promise.resolve().then(callback))}) {
      if (!element || typeof element.addEventListener !== 'function') throw new TypeError('A terminal element is required');
      Object.assign(this, {element, term, canScale, multiplier, enqueue});
      this.context = null; this.eventToken = 0; this.disposed = false;
      this.wheelListener = event => this.capture(event);
      this.customWheelHandler = event => {
        this.capture(event);
        // With mouse reporting disabled, xterm translates wheel input on an
        // alternate buffer into cursor keys. There is no scrollback there, so
        // suppress that translation while leaving the browser event uncanceled.
        return !(trustedVerticalWheel(event) && !mouseTracking(this.term) && this.term?.buffer?.active?.type === 'alternate');
      };
      this.listenerOptions = {capture: true, passive: true};
      this.usesCustomWheelHandler = typeof term?.attachCustomWheelEventHandler === 'function';
      if (this.usesCustomWheelHandler) term.attachCustomWheelEventHandler(this.customWheelHandler);
      else element.addEventListener('wheel', this.wheelListener, this.listenerOptions);
    }

    capture(event) {
      const token = ++this.eventToken;
      // Every wheel event owns the context, including synthetic touch wheels. A
      // synthetic event nested inside a native handler must not inherit it.
      this.context = null;
      if (this.disposed || !trustedVerticalWheel(event) || event.ctrlKey || event.metaKey || !mouseTracking(this.term)) return;
      let count;
      try { if (!this.canScale()) return; count = this.multiplier(); } catch { return; }
      if (!validMultiplier(count) || count <= 1) return;
      this.context = {token, count, button: event.deltaY < 0 ? 64 : 65};
      this.enqueue(() => {
        if (!this.disposed && this.context?.token === token && this.eventToken === token) this.context = null;
      });
    }

    transform(data) {
      const context = this.context;
      if (!context) return data;
      // xterm emits the report synchronously from the wheel hook. Consume the
      // event on its first data callback even when that callback is not a mouse
      // packet, so later keyboard or paste data can never inherit scaling.
      this.context = null;
      if (typeof data !== 'string' || !validMultiplier(context.count) || context.count <= 1) return data;
      try { if (!this.canScale()) return data; } catch { return data; }
      return duplicateReports(data, context.count, context.button);
    }

    dispose() {
      if (this.disposed) return;
      this.disposed = true; this.context = null; this.eventToken += 1;
      if (this.usesCustomWheelHandler) this.term.attachCustomWheelEventHandler(() => true);
      else this.element.removeEventListener('wheel', this.wheelListener, this.listenerOptions);
    }
  }

  const api = {WheelPreferences, WheelSettingsController, TerminalWheelMultiplier, validMultiplier, preferences};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmTerminalWheel = api;
})(globalThis);
