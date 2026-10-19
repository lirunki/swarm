/* Reference library: metadata on open, bytes only when a user follows a file link. */
(function (root) {
  'use strict';
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  function element(tag, className, text = '') { const value = document.createElement(tag); value.className = className; value.textContent = text; return value; }
  function bytes(value) {
    if (!Number.isFinite(value)) return 'size unknown';
    return value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / 1048576).toFixed(1)} MiB`;
  }

  class AgentFilesDialog {
    constructor({query = selector => document.querySelector(selector), api, onChange = async () => {}, confirm = text => root.confirm(text), create = element}) {
      Object.assign(this, {query, api, onChange, confirm, create});
      this.dialog = query('#files-dialog'); this.list = query('#agent-file-list');
      this.generation = 0; this.loading = false; this.refreshPending = false; this.files = []; this.revision = null; this.agentId = null;
      query('#refresh-agent-files').addEventListener('click', () => this.refresh());
      this.dialog.addEventListener('close', () => { this.generation += 1; this.loading = false; this.refreshPending = false; });
    }

    async open(agent, basePath, workspace) {
      this.generation += 1; this.agentId = agent.id; this.base = basePath;
      this.revision = agent.files_version ?? null; this.loading = false; this.refreshPending = false; this.files = [];
      this.query('#files-title').textContent = `Files · ${agent.name}`;
      this.query('#files-workspace').textContent = workspace;
      this.query('#agent-files-error').textContent = ''; this.dialog.showModal();
      await this.refresh();
    }

    async refresh() {
      if (!this.dialog.open || this.loading) return;
      const generation = this.generation, base = this.base;
      this.loading = true; this.query('#refresh-agent-files').disabled = true;
      this.query('#agent-files-error').textContent = '';
      if (!this.files.length) this.list.replaceChildren(this.create('p', 'hint', 'Reading file references…'));
      try {
        const value = await this.api(base);
        if (generation !== this.generation || !this.dialog.open) return;
        if (!Array.isArray(value.files) || value.files.length > 64 || !Number.isInteger(value.revision) || value.revision < 0) throw Error('Invalid file list response');
        this.files = value.files; this.revision = value.revision; this.render();
      } catch (error) {
        if (generation !== this.generation || !this.dialog.open) return;
        this.query('#agent-files-error').textContent = /Not Found/i.test(error.message)
          ? 'Files API is unavailable. Update/restart this Swarm controller to enable file references.' : error.message;
        if (!this.files.length) this.list.replaceChildren();
      } finally {
        if (generation === this.generation && this.dialog.open) {
          this.loading = false; this.query('#refresh-agent-files').disabled = false;
          if (this.refreshPending) { this.refreshPending = false; this.refresh(); }
        }
      }
    }

    sync(agent) {
      if (!this.dialog.open) return;
      if (!agent) { this.dialog.close(); return; }
      if ((agent.files_version ?? null) !== this.revision) {
        if (this.loading) this.refreshPending = true;
        else this.refresh();
      }
    }

    render() {
      this.list.replaceChildren();
      this.query('#agent-file-count').textContent = `${this.files.length} registered file${this.files.length === 1 ? '' : 's'}`;
      if (!this.files.length) {
        this.list.append(this.create('p', 'hint', 'No files registered. The agent can use swarm file add PATH --label "Results".'));
        return;
      }
      for (const file of this.files) {
        const row = this.create('article', 'agent-file-row');
        const info = this.create('div', 'agent-file-info');
        const label = this.create('strong', '', file.label || file.filename || 'File');
        info.append(label, this.create('code', '', file.filename || 'Unnamed file'),
          this.create('span', 'hint', `${bytes(file.size)} · ${file.media_type || 'unknown type'}`));
        const actions = this.create('div', 'agent-file-actions');
        if (file.available && typeof file.id === 'string' && uuid.test(file.id)) {
          const open = this.create('a', 'file-open', 'Open');
          open.href = `${this.base}/${encodeURIComponent(file.id)}/content`; open.target = '_blank'; open.rel = 'noopener noreferrer';
          open.title = 'Open in browser (unsupported/active types download)';
          const download = this.create('a', 'file-download', 'Download');
          download.href = `${open.href}?download=true`; download.download = file.filename || 'file';
          actions.append(open, download);
        } else info.append(this.create('span', 'file-unavailable', file.error || 'File is unavailable'));
        if (typeof file.id === 'string' && uuid.test(file.id)) {
          const remove = this.create('button', 'small danger', 'Remove reference'); remove.type = 'button';
          remove.addEventListener('click', () => this.remove(file, remove)); actions.append(remove);
        }
        row.append(info, actions); this.list.append(row);
      }
    }

    async remove(file, button) {
      if (!this.confirm(`Remove the reference to ${file.label || file.filename}? The original file will NOT be deleted.`)) return;
      const base = this.base, generation = this.generation;
      button.disabled = true;
      try {
        await this.api(`${base}/${encodeURIComponent(file.id)}`, 'DELETE');
        await this.onChange();
        if (generation === this.generation && this.dialog.open) await this.refresh();
      } catch (error) {
        if (generation === this.generation && this.dialog.open) this.query('#agent-files-error').textContent = error.message;
      } finally { button.disabled = false; }
    }
  }
  const api = {AgentFilesDialog, bytes};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmAgentFiles = api;
})(globalThis);
