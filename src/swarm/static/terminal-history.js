/* Read-only tmux history. Touch scrolling never becomes terminal keystrokes. */
(function (root) {
  'use strict';

  class TerminalHistory {
    constructor({screen, panel, text, status, toggleButton, refreshButton, load, onEnter = () => {}, onLive = () => {}}) {
      Object.assign(this, {screen, panel, text, status, toggleButton, refreshButton, load, onEnter, onLive});
      this.active = false; this.loading = false; this.disposed = false;
      this.generation = 0; this.pendingDelta = 0; this.gesture = null; this.abort = null; this.listeners = [];
      this.listen(toggleButton, 'click', () => this.active ? this.live() : this.enter());
      this.listen(refreshButton, 'click', () => this.refresh());
      this.listen(panel, 'keydown', event => { if (event.key === 'Escape') { event.preventDefault(); this.live(); } });
      this.listen(screen, 'touchstart', event => this.touchStart(event), {capture: true, passive: true});
      this.listen(screen, 'touchmove', event => this.touchMove(event), {capture: true, passive: false});
      this.listen(screen, 'touchend', () => { this.gesture = null; }, {capture: true, passive: true});
      this.listen(screen, 'touchcancel', () => { this.gesture = null; }, {capture: true, passive: true});
      panel.tabIndex = 0;
      panel.setAttribute('role', 'region'); panel.setAttribute('aria-label', 'Terminal history (read-only)');
      toggleButton.setAttribute('aria-controls', panel.id);
      status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
      this.render();
    }

    listen(element, name, callback, options = false) {
      element.addEventListener(name, callback, options); this.listeners.push([element, name, callback, options]);
    }

    render() {
      this.screen.hidden = this.active; this.panel.hidden = !this.active; this.status.hidden = !this.active;
      this.toggleButton.textContent = this.active ? 'Live input' : 'History';
      this.toggleButton.setAttribute('aria-expanded', String(this.active));
      this.toggleButton.title = this.active ? 'Return to the live terminal to type' : 'Read retained terminal output; swipes also open history';
      this.refreshButton.hidden = !this.active; this.refreshButton.disabled = this.loading;
    }

    async enter(initialDelta = 0) {
      if (this.disposed || this.active) return;
      this.active = true; this.pendingDelta = initialDelta;
      this.render(); this.onEnter(); this.panel.focus({preventScroll: true});
      await this.refresh(true);
    }

    async refresh(reset = false) {
      if (this.disposed || !this.active || this.loading) return;
      const generation = ++this.generation;
      const top = this.panel.scrollTop, atBottom = this.panel.scrollHeight - this.panel.clientHeight - top < 4;
      this.abort = typeof root.AbortController === 'function' ? new root.AbortController() : null;
      this.loading = true; this.status.textContent = 'Loading read-only terminal history…'; this.render();
      try {
        const value = await this.load(this.abort?.signal);
        if (this.disposed || !this.active || generation !== this.generation) return;
        if (typeof value?.text !== 'string') throw new Error('Invalid terminal history response.');
        // Output is text, never HTML or terminal escape commands.
        this.text.textContent = value.text || '(No retained terminal output.)';
        const bottom = Math.max(0, this.panel.scrollHeight - this.panel.clientHeight);
        this.panel.scrollTop = Math.max(0, Math.min(bottom, (reset || atBottom ? bottom : top) + this.pendingDelta));
        this.pendingDelta = 0;
        const count = Number.isInteger(value.lines) && value.lines >= 0 ? value.lines : value.text.split('\n').length;
        this.status.textContent = `History snapshot · ${count} lines${value.truncated ? ' (limited)' : ''}. Refresh for new output; Live input to type.`;
      } catch (error) {
        if (this.disposed || !this.active || generation !== this.generation) return;
        this.pendingDelta = 0;
        this.status.textContent = `History unavailable: ${error.message}. Return to Live input or try Refresh.`;
      } finally {
        if (!this.disposed && generation === this.generation) { this.loading = false; this.abort = null; this.render(); }
      }
    }

    live() {
      if (this.disposed || !this.active) return;
      this.generation += 1; this.abort?.abort(); this.abort = null;
      this.active = false; this.loading = false; this.pendingDelta = 0; this.gesture = null;
      this.render(); this.onLive();
    }

    touchStart(event) {
      if (this.disposed || this.active || event.touches.length !== 1) { this.gesture = null; return; }
      const touch = event.touches[0];
      this.gesture = {x: touch.clientX, y: touch.clientY, lastY: touch.clientY, dragging: false};
    }

    touchMove(event) {
      const gesture = this.gesture;
      if (this.disposed || !gesture) return;
      if (event.touches.length !== 1) { this.gesture = null; return; }
      const touch = event.touches[0], dx = touch.clientX - gesture.x, dy = touch.clientY - gesture.y;
      if (!gesture.dragging) {
        if (Math.abs(dx) > 12 && Math.abs(dx) >= Math.abs(dy)) { this.gesture = null; return; }
        if (Math.abs(dy) < 12 || Math.abs(dy) < Math.abs(dx) * 1.2) return;
        gesture.dragging = true;
        this.enter(-(touch.clientY - gesture.lastY));
      } else {
        const delta = -(touch.clientY - gesture.lastY);
        if (this.loading) this.pendingDelta += delta;
        else this.panel.scrollTop = Math.max(0, Math.min(Math.max(0, this.panel.scrollHeight - this.panel.clientHeight), this.panel.scrollTop + delta));
      }
      gesture.lastY = touch.clientY;
      if (event.cancelable) event.preventDefault();
      event.stopPropagation();
    }

    dispose() {
      this.disposed = true; this.generation += 1; this.abort?.abort(); this.abort = null;
      this.gesture = null;
      for (const [element, name, callback, options] of this.listeners) element.removeEventListener(name, callback, options);
      this.listeners = [];
    }
  }

  if (typeof module === 'object' && module.exports) module.exports = {TerminalHistory};
  else root.SwarmTerminalHistory = {TerminalHistory};
})(globalThis);
