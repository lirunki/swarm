/* A mobile native-selection layer, not a terminal/history transport. */
(function (root) {
  'use strict';

  function visibleText(term) {
    const buffer = term.buffer.active, rows = [];
    for (let row = 0; row < term.rows; row++) {
      rows.push(buffer.getLine(buffer.viewportY + row)?.translateToString(true, 0, term.cols) || '');
    }
    return rows.join('\n').replace(/\n+$/, '');
  }

  class TerminalCopy {
    constructor({term, screen, button, onOpen = () => {}, onClose = () => {}, document = screen.ownerDocument, media}) {
      Object.assign(this, {term, screen, button, onOpen, onClose, document});
      this.view = document.defaultView || root;
      this.media = media || this.view.matchMedia?.('(hover: none) and (pointer: coarse)') || {matches: false};
      this.active = false; this.disposed = false; this.generation = 0; this.timer = null;
      this.text = document.createElement('textarea');
      this.text.className = 'terminal-copy-text'; this.text.readOnly = true; this.text.wrap = 'off';
      this.text.setAttribute('inputmode', 'none'); this.text.setAttribute('spellcheck', 'false');
      this.text.setAttribute('autocomplete', 'off'); this.text.setAttribute('aria-label', 'Visible terminal text, read only. Select text and use Copy.');
      this.text.id = `terminal-copy-${button.dataset.agentId}`;
      button.setAttribute('aria-controls', this.text.id); button.setAttribute('aria-pressed', 'false');
      this.screen.append(this.text);
      this.click = () => this.active ? this.close() : this.open();
      this.keydown = event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); this.close(); } };
      this.copy = event => {
        if (!this.active || this.text.selectionStart === this.text.selectionEnd) return;
        if (event.target !== this.text && document.activeElement !== this.text) return;
        const generation = this.generation;
        this.view.clearTimeout(this.timer);
        // Leave selection intact until the browser's native copy action finishes.
        this.timer = this.view.setTimeout(() => { if (this.active && generation === this.generation) this.close(); }, 0);
      };
      this.mediaChange = () => { if (!this.media.matches && this.active) this.close(); this.render(); };
      button.addEventListener('click', this.click);
      this.text.addEventListener('keydown', this.keydown);
      document.addEventListener('copy', this.copy, true);
      this.media.addEventListener?.('change', this.mediaChange);
      if (!this.media.addEventListener) this.media.addListener?.(this.mediaChange);
      this.render();
    }

    render() {
      this.button.hidden = !this.media.matches;
      this.button.textContent = this.active ? 'Done copying' : 'Copy text';
      this.button.setAttribute('aria-pressed', String(this.active));
      this.button.title = this.active ? 'Close text selection and return to terminal' : 'Select and copy visible terminal text';
      this.text.hidden = !this.active;
      this.screen.classList.toggle('copying-text', this.active);
    }

    open() {
      if (this.disposed || this.active || !this.media.matches) return;
      this.generation += 1; this.active = true;
      this.text.value = visibleText(this.term);
      const options = this.term.options;
      this.text.style.fontFamily = options.fontFamily;
      this.text.style.fontSize = `${options.fontSize}px`;
      this.text.style.fontWeight = String(options.fontWeight ?? 'normal');
      this.text.style.letterSpacing = `${options.letterSpacing ?? 0}px`;
      this.text.style.fontKerning = 'none'; this.text.style.fontVariantLigatures = 'none';
      // Use the rendered cell stride, including xterm's device-pixel rounding.
      const grid = this.term.element?.querySelector('.xterm-screen');
      const rowHeight = grid?.getBoundingClientRect().height / this.term.rows;
      this.text.style.lineHeight = Number.isFinite(rowHeight) && rowHeight > 0 ? `${rowHeight}px` : 'normal';
      this.render(); this.onOpen();
      this.text.focus({preventScroll: true}); this.text.setSelectionRange(0, 0);
      this.text.scrollTop = 0; this.text.scrollLeft = 0;
    }

    close({focus = true} = {}) {
      if (!this.active) return;
      this.generation += 1; this.active = false;
      this.view.clearTimeout(this.timer); this.timer = null;
      this.text.value = ''; this.render(); this.onClose(focus);
    }

    dispose() {
      this.disposed = true; this.close({focus: false});
      this.view.clearTimeout(this.timer);
      this.button.removeEventListener('click', this.click); this.text.removeEventListener('keydown', this.keydown);
      this.document.removeEventListener('copy', this.copy, true);
      this.media.removeEventListener?.('change', this.mediaChange);
      if (!this.media.removeEventListener) this.media.removeListener?.(this.mediaChange);
      this.text.remove();
    }
  }

  const api = {TerminalCopy, visibleText};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmTerminalCopy = api;
})(globalThis);
