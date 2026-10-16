/* Translate touch gestures to negotiated terminal mouse events, never editor keys. */
(function (root) {
  'use strict';

  function mouseMode(term) {
    return ['vt200', 'drag', 'any'].includes(term?.modes?.mouseTrackingMode);
  }

  function supportsScroll(term) {
    return mouseMode(term) || term?.buffer?.active?.type === 'normal';
  }

  function scrollTerminal(term, direction, point) {
    if (!mouseMode(term)) {
      // Do not dispatch wheel events in a mouse-free alternate screen: xterm
      // may turn those into Up/Down editor keys. Only scroll a real local buffer.
      if (term?.buffer?.active?.type !== 'normal') return false;
      term.scrollLines(direction);
      return true;
    }
    const target = term.element?.querySelector('.xterm-screen') || term.element;
    const view = target?.ownerDocument?.defaultView || root;
    if (!target || typeof view.WheelEvent !== 'function') return false;
    const rect = target.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const clientX = Math.max(rect.left + 1, Math.min(rect.right - 1, point.x));
    const clientY = Math.max(rect.top + 1, Math.min(rect.bottom - 1, point.y));
    // xterm chooses SGR/legacy encoding and cell coordinates itself. Its normal
    // onData handler forwards the report through the already existing PTY stream.
    target.dispatchEvent(new view.WheelEvent('wheel', {
      deltaY: direction, deltaMode: 1, clientX, clientY,
      bubbles: true, cancelable: true, view,
    }));
    return true;
  }

  class TerminalTouchScroll {
    constructor({element, canScroll = () => true, wheel, onGesture = () => {}, pixelsPerStep = 24, pointerEvents = typeof root.PointerEvent === 'function'}) {
      this.element = element; this.canScroll = canScroll; this.wheel = wheel; this.onGesture = onGesture;
      this.pixelsPerStep = pixelsPerStep; this.gesture = null; this.disposed = false;
      this.pointerId = null; this.contacts = new Map(); this.pointerEvents = pointerEvents; this.fallback = null;
      this.listeners = pointerEvents ? [
        ['pointerdown', event => this.pointerStart(event), {capture: true, passive: true}],
        ['pointermove', event => this.pointerMove(event), {capture: true, passive: false}],
        ['pointerup', event => this.pointerEnd(event), {capture: true, passive: true}],
        ['pointercancel', event => this.pointerEnd(event), {capture: true, passive: true}],
        ['lostpointercapture', event => { if (event.target === element && event.pointerId === this.pointerId) { this.contacts.delete(event.pointerId); this.reset(); } }, true],
        ['touchstart', event => { if (event.touches.length > 1) this.reset(); }, {capture: true, passive: true}],
        ['touchmove', event => { if (this.gesture?.scrolling) { if (event.cancelable) event.preventDefault(); event.stopPropagation(); } }, {capture: true, passive: false}],
      ] : [
        ['touchstart', event => this.start(event), {capture: true, passive: true}],
        ['touchmove', event => this.move(event), {capture: true, passive: false}],
        ['touchend', () => this.reset(), {capture: true, passive: true}],
        ['touchcancel', () => this.reset(), {capture: true, passive: true}],
      ];
      for (const [name, callback, options] of this.listeners) element.addEventListener(name, callback, options);
    }

    reset() {
      this.clearFallback(); this.gesture = null;
      const id = this.pointerId; this.pointerId = null;
      if (id !== null && this.element.hasPointerCapture?.(id)) this.element.releasePointerCapture(id);
    }

    pointerEvent(event) {
      return {touches: [...this.contacts.values()], timeStamp: event.timeStamp, cancelable: event.cancelable,
        preventDefault: () => event.preventDefault(), stopPropagation: () => event.stopPropagation()};
    }

    pointerStart(event) {
      if (event.pointerType !== 'touch') return;
      this.contacts.set(event.pointerId, {clientX: event.clientX, clientY: event.clientY});
      if (this.contacts.size !== 1) { this.reset(); return; }
      this.pointerId = event.pointerId; this.start(this.pointerEvent(event));
    }

    pointerMove(event) {
      if (event.pointerType !== 'touch' || !this.contacts.has(event.pointerId)) return;
      this.contacts.set(event.pointerId, {clientX: event.clientX, clientY: event.clientY});
      this.move(this.pointerEvent(event));
    }

    pointerEnd(event) {
      if (event.pointerType !== 'touch') return;
      this.contacts.delete(event.pointerId);
      if (event.pointerId === this.pointerId || !this.contacts.size) this.reset();
    }

    clearFallback() {
      if (!this.fallback) return;
      for (const [type, callback, options] of this.fallback.listeners) this.fallback.target.removeEventListener(type, callback, options);
      this.fallback = null;
    }

    start(event) {
      this.clearFallback();
      if (this.disposed || !this.canScroll() || event.touches.length !== 1) { this.reset(); return; }
      const point = event.touches[0];
      this.gesture = {x: point.clientX, y: point.clientY, lastY: point.clientY,
        started: event.timeStamp || 0, scrolling: false, pending: 0};
      if (!this.pointerEvents && event.target && event.target !== this.element) {
        // Older TouchEvent-only browsers keep targeting the original row even
        // after xterm removes it. Listen there too until this gesture ends.
        const listeners = [['touchmove', value => this.move(value), {passive: false}],
          ['touchend', () => this.reset(), false], ['touchcancel', () => this.reset(), false]];
        this.fallback = {target: event.target, listeners};
        for (const [type, callback, options] of listeners) event.target.addEventListener(type, callback, options);
      }
    }

    move(event) {
      const gesture = this.gesture;
      if (this.disposed || !gesture) return;
      if (!this.canScroll() || event.touches.length !== 1) { this.reset(); return; }
      const point = event.touches[0], dx = point.clientX - gesture.x, dy = point.clientY - gesture.y;
      if (!gesture.scrolling) {
        // Preserve taps, browser back gestures, selection holds, and pinch/zoom.
        if ((event.timeStamp || 0) - gesture.started > 500 || (Math.abs(dx) >= 10 && Math.abs(dx) >= Math.abs(dy))) { this.reset(); return; }
        if (Math.abs(dy) < 10 || Math.abs(dy) < Math.abs(dx) * 1.25) return;
        gesture.scrolling = true;
        // Capture only actual drags, not taps. xterm redraws/replaces the touched
        // row when tmux enters copy mode; keep subsequent moves on this stable root.
        if (this.pointerId !== null) this.element.setPointerCapture?.(this.pointerId);
        this.onGesture();
      }
      gesture.pending -= point.clientY - gesture.lastY;
      gesture.lastY = point.clientY;
      const count = Math.min(12, Math.floor(Math.abs(gesture.pending) / this.pixelsPerStep));
      const direction = Math.sign(gesture.pending);
      for (let i = 0; i < count; i++) this.wheel(direction, {x: gesture.x, y: gesture.y});
      gesture.pending -= direction * count * this.pixelsPerStep;
      if (Math.abs(gesture.pending) >= this.pixelsPerStep) gesture.pending %= this.pixelsPerStep;
      if (event.cancelable) event.preventDefault();
      event.stopPropagation();
    }

    dispose() {
      this.disposed = true; this.reset(); this.contacts.clear();
      for (const [name, callback, options] of this.listeners) this.element.removeEventListener(name, callback, options);
      this.listeners = [];
    }
  }

  const api = {TerminalTouchScroll, scrollTerminal, supportsScroll};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmTerminalTouch = api;
})(globalThis);
