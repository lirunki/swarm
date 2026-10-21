/* Reference library: metadata on open, bytes only for explicit download, upload, or insert actions. */
(function (root) {
  'use strict';
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const MAX_FILE_BYTES = 128 * 1024 * 1024;
  const MAX_FILES = 64;
  const MAX_CHUNK_BYTES = 196608;
  function element(tag, className, text = '') { const value = document.createElement(tag); value.className = className; value.textContent = text; return value; }
  function bytes(value) {
    if (!Number.isFinite(value)) return 'size unknown';
    return value < 1024 ? `${value} B` : value < 1048576 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / 1048576).toFixed(1)} MiB`;
  }
  function uploadId() {
    if (root.crypto?.randomUUID) return root.crypto.randomUUID();
    const value = new Uint8Array(16); root.crypto.getRandomValues(value);
    value[6] = (value[6] & 0x0f) | 0x40; value[8] = (value[8] & 0x3f) | 0x80;
    const hex = [...value].map(byte => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  function encodeChunk(buffer) {
    const value = new Uint8Array(buffer); let binary = '';
    for (let offset = 0; offset < value.length; offset += 8192) binary += String.fromCharCode(...value.subarray(offset, offset + 8192));
    return root.btoa(binary);
  }
  const clipboardTypes = new Map([
    ['image/png', 'png'], ['image/jpeg', 'jpg'], ['image/webp', 'webp'], ['image/gif', 'gif'],
    ['image/bmp', 'bmp'], ['image/tiff', 'tiff'], ['image/avif', 'avif'], ['image/svg+xml', 'svg'],
    ['application/pdf', 'pdf'], ['application/octet-stream', 'bin'],
  ]);
  function clipboardFile(blob) {
    if (typeof blob.name === 'string' && blob.name.trim()) return blob;
    const extension = clipboardTypes.get(blob.type) || 'bin';
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    return new root.File([blob], `clipboard-${stamp}.${extension}`, {type: blob.type});
  }
  function clipboardFiles(data) {
    const files = Array.from(data?.files || []);
    return files.length ? files : Array.from(data?.items || [])
      .filter(item => item.kind === 'file').map(item => item.getAsFile()).filter(Boolean);
  }
  function canInsert(agent) {
    const compacting = agent?.meta_compaction?.status === 'running' || agent?.tool_input_compaction?.status === 'running' || agent?.compaction?.status === 'running' || agent?.compaction === true;
    return agent?.alive === true && agent?.managed === true && agent?.spawn_state !== 'provisioning' && !compacting && !agent.stop_pending && !agent.orchestration_stop;
  }

  class AgentFilesDialog {
    constructor({query = selector => document.querySelector(selector), api, onChange = async () => {}, onInsert = () => {}, confirm = text => root.confirm(text), create = element,
      readClipboard = () => {
        if (!root.navigator?.clipboard?.read) throw Error('Clipboard button access is unavailable on this browser or connection.');
        return root.navigator.clipboard.read();
      }}) {
      Object.assign(this, {query, api, onChange, onInsert, confirm, create, readClipboard});
      this.dialog = query('#files-dialog'); this.list = query('#agent-file-list'); this.input = query('#agent-file-upload-input');
      this.generation = 0; this.loading = false; this.refreshPending = false; this.files = []; this.revision = null; this.agentId = null;
      this.uploading = null; this.inserting = null; this.insertionEnabled = false; this.recovering = false; this.clipboardRead = null;
      query('#refresh-agent-files').addEventListener('click', () => this.refresh());
      this.pickerGeneration = null;
      query('#upload-agent-file').addEventListener('click', () => {
        if (!this.dialog.open || this.uploading || this.inserting || this.clipboardRead) return;
        this.pickerGeneration = this.generation; this.input.click();
      });
      this.input.addEventListener('change', event => {
        const file = event.currentTarget.files?.[0], generation = this.pickerGeneration;
        event.currentTarget.value = ''; this.pickerGeneration = null;
        if (file && generation === this.generation && this.dialog.open) void this.upload(file);
      });
      query('#cancel-agent-file-upload').addEventListener('click', () => this.cancelUpload(true));
      query('#paste-agent-file').addEventListener('click', () => this.pasteClipboard());
      this.dialog.addEventListener('paste', event => this.handlePaste(event));
      void this.cleanupPending();
      this.dialog.addEventListener('close', () => {
        this.generation += 1; this.clipboardRead = null; this.loading = false; this.refreshPending = false; this.cancelUpload(false);
      });
    }

    async open(agent, basePath, workspace) {
      this.cancelUpload(false); this.uploading = null; this.clipboardRead = null;
      this.generation += 1; this.agentId = agent.id; this.base = basePath; this.insertionEnabled = canInsert(agent); this.inserting = null;
      this.revision = agent.files_version ?? null; this.loading = false; this.refreshPending = false; this.files = [];
      this.query('#files-title').textContent = `Files · ${agent.name}`;
      this.query('#files-workspace').textContent = workspace;
      this.query('#agent-files-error').textContent = ''; this.query('#agent-files-insert-status').textContent = '';
      this.setUploadStatus('', null, false); this.updateUploadControls(); this.dialog.showModal();
      void this.cleanupPending();
      await this.refresh();
    }

    clipboardError(message) {
      this.query('#agent-files-error').textContent = message;
    }

    async handlePaste(event) {
      if (!this.dialog.open || event.target?.isContentEditable || event.target?.closest?.('input, textarea, [contenteditable]')) return;
      const generation = this.generation;
      let files;
      try { files = clipboardFiles(event.clipboardData); }
      catch { this.clipboardError('Could not read the clipboard file. Use Upload instead.'); return; }
      if (!files.length) {
        this.clipboardError('Clipboard has no readable image or file. Text, URLs and copied paths are not uploaded; use Upload for files the browser cannot paste.');
        return;
      }
      event.preventDefault(); event.stopPropagation();
      if (this.uploading || this.inserting || this.clipboardRead) {
        this.clipboardError('Wait for the current operation before pasting another file.'); return;
      }
      if (files.length !== 1) { this.clipboardError('Paste one image or file at a time.'); return; }
      try { await this.upload(clipboardFile(files[0])); }
      catch { if (generation === this.generation && this.dialog.open) this.clipboardError('Could not prepare the clipboard file. Use Upload instead.'); }
    }

    async pasteClipboard() {
      if (!this.dialog.open || this.uploading || this.inserting || this.clipboardRead) return;
      if (this.files.length >= MAX_FILES) { this.clipboardError(`This library already has the maximum of ${MAX_FILES} files.`); return; }
      const job = {generation: this.generation, base: this.base};
      const current = () => job.generation === this.generation && job.base === this.base && this.dialog.open;
      this.clipboardRead = job; this.clipboardError(''); this.render();
      try {
        // Invoked directly from the button gesture, never on open/refresh or a timer.
        const items = await this.readClipboard();
        if (!current()) return;
        const readable = Array.from(items).map(item => ({item,
          type: [...clipboardTypes.keys()].find(type => item.types.includes(type))})).filter(v => v.type);
        if (!readable.length) {
          this.clipboardError('Clipboard has no readable image or file. Text, URLs and copied paths are not uploaded; try Ctrl+V / ⌘V here or use Upload.'); return;
        }
        if (readable.length !== 1) { this.clipboardError('Paste one image or file at a time.'); return; }
        const blob = await readable[0].item.getType(readable[0].type);
        if (!current()) return;
        const file = clipboardFile(blob);
        this.clipboardRead = null;
        await this.upload(file);
      } catch (error) {
        if (current()) this.clipboardError(`Clipboard access failed or was denied. Try Ctrl+V / ⌘V inside Files, allow clipboard access on HTTPS/localhost, or use Upload. ${error?.message || ''}`);
      } finally {
        if (this.clipboardRead === job) this.clipboardRead = null;
        if (current()) this.render();
      }
    }

    pendingUploads() {
      try {
        const raw = root.sessionStorage?.getItem('swarm.pendingUploads') || '[]';
        if (raw.length > 8192) return [];
        const values = JSON.parse(raw);
        return Array.isArray(values) ? values.filter(v => v && uuid.test(v.id) && typeof v.base === 'string' && /^\/api\/(?:federation\/[A-Za-z0-9][A-Za-z0-9._-]{0,63}\/proxy\/api\/)?agents\/[0-9a-f-]{36}\/files$/i.test(v.base)).slice(0, 32) : [];
      } catch { return []; }
    }

    rememberUpload(job, remove = false) {
      try {
        const values = this.pendingUploads().filter(v => v.id !== job.id || v.base !== job.base);
        if (!remove) values.push({id: job.id, base: job.base});
        root.sessionStorage?.setItem('swarm.pendingUploads', JSON.stringify(values.slice(-32)));
      } catch { /* Storage can be unavailable; server-side expiration still applies. */ }
    }

    async cleanupPending() {
      if (this.recovering) return;
      this.recovering = true;
      try {
        for (const job of this.pendingUploads()) {
          if (this.uploading?.id === job.id && this.uploading?.base === job.base) continue;
          try {
            await this.api(`${job.base}/uploads/${encodeURIComponent(job.id)}`, 'DELETE');
            this.rememberUpload(job, true);
          } catch (error) {
            if (error.status === 404) this.rememberUpload(job, true);
            // Other failures retain the captured target for cleanup on open/reload.
          }
        }
      } finally { this.recovering = false; }
    }

    updateUploadControls() {
      const busy = !!this.uploading || !!this.inserting || !!this.clipboardRead;
      const paste = this.query('#paste-agent-file');
      paste.disabled = busy || this.files.length >= MAX_FILES;
      paste.textContent = this.clipboardRead ? 'Reading…' : 'Paste';
      paste.ariaBusy = String(!!this.clipboardRead);
      this.query('#upload-agent-file').disabled = busy || this.files.length >= MAX_FILES;
      this.input.disabled = busy || this.files.length >= MAX_FILES;
      this.query('#cancel-agent-file-upload').disabled = !this.uploading || this.uploading.phase === 'finishing' || this.uploading.cancelled;
    }

    setUploadStatus(message, percent = null, cancel = false) {
      const region = this.query('#agent-file-upload-status'), progress = this.query('#agent-file-upload-progress');
      region.hidden = !message; this.query('#agent-file-upload-message').textContent = message;
      progress.hidden = percent === null; if (percent !== null) progress.value = Math.max(0, Math.min(100, percent));
      this.query('#cancel-agent-file-upload').hidden = !cancel;
    }

    async refresh() {
      if (!this.dialog.open || this.loading) return;
      void this.cleanupPending();
      const generation = this.generation, base = this.base;
      this.loading = true; this.query('#refresh-agent-files').disabled = true;
      this.query('#agent-files-error').textContent = '';
      if (!this.files.length) this.list.replaceChildren(this.create('p', 'hint', 'Reading file references…'));
      try {
        const value = await this.api(base);
        if (generation !== this.generation || !this.dialog.open) return;
        if (!Array.isArray(value.files) || value.files.length > MAX_FILES || !Number.isInteger(value.revision) || value.revision < 0) throw Error('Invalid file list response');
        this.files = value.files; this.revision = value.revision; this.render();
      } catch (error) {
        if (generation !== this.generation || !this.dialog.open) return;
        this.query('#agent-files-error').textContent = /Not Found/i.test(error.message)
          ? 'Files API is unavailable. Update/restart this Swarm controller to enable file references.' : error.message;
        if (!this.files.length) this.list.replaceChildren();
      } finally {
        if (generation === this.generation && this.dialog.open) {
          this.loading = false; this.query('#refresh-agent-files').disabled = false; this.updateUploadControls();
          if (this.refreshPending) { this.refreshPending = false; this.refresh(); }
        }
      }
    }

    sync(agent) {
      if (!this.dialog.open) return;
      if (!agent) { this.dialog.close(); return; }
      const enabled = canInsert(agent);
      if (enabled !== this.insertionEnabled) { this.insertionEnabled = enabled; this.render(); }
      if ((agent.files_version ?? null) !== this.revision) {
        if (this.loading) this.refreshPending = true;
        else this.refresh();
      }
    }

    render() {
      this.list.replaceChildren();
      this.query('#agent-file-count').textContent = `${this.files.length} registered file${this.files.length === 1 ? '' : 's'}`;
      this.updateUploadControls();
      if (!this.files.length) {
        this.list.append(this.create('p', 'hint', 'No files registered. Upload one here, or use swarm file add PATH --label "Results".'));
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
          const insert = this.create('button', 'small file-insert', 'Insert'); insert.type = 'button';
          insert.disabled = !this.insertionEnabled || !!this.inserting || !!this.uploading || !!this.clipboardRead;
          insert.title = this.insertionEnabled ? 'Insert a reference into the live agent terminal' : 'Insert requires a live managed agent that is not stopping or compacting';
          insert.addEventListener('click', () => this.insert(file, insert));
          actions.append(open, download, insert);
        } else info.append(this.create('span', 'file-unavailable', file.error || 'File is unavailable'));
        if (typeof file.id === 'string' && uuid.test(file.id)) {
          const remove = this.create('button', 'small danger', file.uploaded ? 'Remove upload' : 'Remove reference'); remove.type = 'button';
          remove.disabled = !!this.inserting || !!this.uploading || !!this.clipboardRead; remove.addEventListener('click', () => this.remove(file, remove)); actions.append(remove);
        }
        row.append(info, actions); this.list.append(row);
      }
    }

    async insert(file, button) {
      if (!this.insertionEnabled || this.inserting || this.uploading || this.clipboardRead) return;
      const base = this.base, generation = this.generation, agentId = this.agentId;
      this.inserting = file.id; button.disabled = true; button.textContent = 'Inserting…'; this.updateUploadControls();
      this.query('#agent-files-error').textContent = ''; this.query('#agent-files-insert-status').textContent = '';
      try {
        const value = await this.api(`${base}/${encodeURIComponent(file.id)}/insert`, 'POST', {});
        if (generation !== this.generation || !this.dialog.open) return;
        if (value?.inserted !== true || value.file_id !== file.id || typeof value.path !== 'string' || typeof value.text !== 'string') throw Error('Invalid insert response');
        this.query('#agent-files-insert-status').textContent = `Inserted ${file.filename || 'file'} from ${value.path}.`;
        this.onInsert(agentId, base);
      } catch (error) {
        if (generation === this.generation && this.dialog.open) this.query('#agent-files-error').textContent = /^(Not Found|Method Not Allowed)$/i.test(error.message)
          ? 'Insert API is unavailable. Update/restart the selected Swarm controller.'
          : `Insert could not be confirmed. Check the terminal before retrying; it may already have succeeded. ${error.message}`;
      } finally {
        if (generation === this.generation && this.dialog.open) { this.inserting = null; this.render(); }
      }
    }

    async upload(file) {
      if (!this.dialog.open || this.uploading || this.inserting || this.clipboardRead) return;
      this.query('#agent-files-error').textContent = '';
      if (!Number.isFinite(file.size) || file.size < 0 || file.size > MAX_FILE_BYTES) {
        this.query('#agent-files-error').textContent = `Choose a file no larger than ${bytes(MAX_FILE_BYTES)}.`; return;
      }
      if (this.files.length >= MAX_FILES) { this.query('#agent-files-error').textContent = `This library already has the maximum of ${MAX_FILES} files.`; return; }
      let id;
      try { id = uploadId(); }
      catch { this.query('#agent-files-error').textContent = 'This browser cannot create a secure upload ID. Use a recent browser or HTTPS.'; return; }
      const job = {id, base: this.base, agentId: this.agentId, generation: this.generation, file, phase: 'starting', cancelled: false, cleanupStarted: false};
      this.uploading = job; this.rememberUpload(job); this.setUploadStatus(`Starting upload of ${file.name}…`, 0, true); this.render();
      try {
        const started = await this.api(`${job.base}/uploads`, 'POST', {upload_id: job.id, filename: file.name, size: file.size});
        this.ensureUploadCurrent(job);
        if (started?.upload_id !== job.id || !Number.isInteger(started.offset) || started.offset < 0 || started.offset > file.size || !Number.isInteger(started.chunk_bytes) || started.chunk_bytes < 1 || started.chunk_bytes > MAX_CHUNK_BYTES || !['receiving', 'complete'].includes(started.status)) throw Error('Invalid upload response');
        let offset = started.offset; job.phase = 'receiving';
        while (offset < file.size) {
          this.ensureUploadCurrent(job);
          const end = Math.min(file.size, offset + Math.min(started.chunk_bytes, MAX_CHUNK_BYTES));
          const data = encodeChunk(await file.slice(offset, end).arrayBuffer());
          this.ensureUploadCurrent(job);
          const chunk = await this.api(`${job.base}/uploads/${encodeURIComponent(job.id)}/chunks`, 'POST', {offset, data});
          this.ensureUploadCurrent(job);
          if (!Number.isInteger(chunk?.offset) || chunk.offset !== end) throw Error('Invalid upload chunk response');
          offset = chunk.offset;
          this.setUploadStatus(`Uploading ${file.name} · ${bytes(offset)} of ${bytes(file.size)}`, file.size ? offset / file.size * 100 : 100, true);
        }
        this.ensureUploadCurrent(job);
        job.phase = 'finishing'; this.setUploadStatus(`Registering ${file.name}…`, 100, false); this.updateUploadControls();
        let result;
        try { result = await this.api(`${job.base}/uploads/${encodeURIComponent(job.id)}/finish`, 'POST', {}); }
        catch (error) { throw Object.assign(Error(`Upload completion is uncertain. Refresh the library before trying again. ${error.message}`), {finishUnknown: true}); }
        if (result?.agent_id !== job.agentId || !result.file || !Number.isInteger(result.revision) || result.revision < 0) throw Object.assign(Error('Upload completion returned an invalid response. Refresh the library before trying again.'), {finishUnknown: true});
        job.phase = 'complete'; this.rememberUpload(job, true);
        if (job.generation !== this.generation || !this.dialog.open) return;
        this.setUploadStatus(`${file.name} uploaded and registered. Use Insert when you want to send it to the agent.`, 100, false);
        await this.onChange();
        if (job.generation === this.generation && this.dialog.open) await this.refresh();
      } catch (error) {
        if (job.phase !== 'finishing' && job.phase !== 'complete') await this.abortPartial(job);
        if (!job.cancelled && job.generation === this.generation && this.dialog.open) {
          this.query('#agent-files-error').textContent = job.phase === 'starting' && /^(Not Found|Method Not Allowed)$/i.test(error.message)
            ? 'Upload API is unavailable. Update/restart the selected Swarm controller.' : error.message;
          this.setUploadStatus(job.phase === 'finishing' ? 'Upload completion is unconfirmed — refresh Files.' : 'Upload failed.', null, false);
        }
      } finally {
        if (this.uploading === job) this.uploading = null;
        if (job.generation === this.generation && this.dialog.open) {
          if (job.cancelled) this.setUploadStatus('Upload canceled.', null, false);
          this.render();
        }
      }
    }

    ensureUploadCurrent(job) {
      if (job.cancelled || job.generation !== this.generation || !this.dialog.open) throw Error('Upload canceled');
    }

    cancelUpload(showStatus) {
      const job = this.uploading; if (!job || job.phase === 'complete') return;
      job.cancelled = true;
      if (showStatus && job.generation === this.generation && this.dialog.open) this.setUploadStatus(job.phase === 'finishing' ? 'Finishing upload; close and refresh to confirm it.' : 'Canceling upload…', null, false);
      this.updateUploadControls();
      // The in-flight request observes cancelled before its next step, then cleans up. Waiting avoids DELETE racing the initial POST or a chunk POST.
    }

    async abortPartial(job) {
      if (job.cleanupStarted || job.phase === 'finishing' || job.phase === 'complete') return;
      job.cleanupStarted = true;
      try { await this.api(`${job.base}/uploads/${encodeURIComponent(job.id)}`, 'DELETE'); this.rememberUpload(job, true); } catch { /* best-effort cleanup against the captured workspace */ }
    }

    async remove(file, button) {
      if (this.uploading || this.inserting || this.clipboardRead) return;
      const question = file.uploaded
        ? `Remove ${file.label || file.filename} and delete its uploaded temporary copy? The file on your browser's computer is NOT deleted. Paths already inserted into the agent will stop working.`
        : `Remove the reference to ${file.label || file.filename}? The original file will NOT be deleted.`;
      if (!this.confirm(question)) return;
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
  const api = {AgentFilesDialog, bytes, canInsert, encodeChunk, clipboardFile, clipboardFiles};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmAgentFiles = api;
})(globalThis);
