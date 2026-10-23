/* Optional browser authentication. Tokens exist only in this JavaScript instance. */
(function (root) {
  'use strict';

  class AuthCancelledError extends Error {
    constructor() { super('Sign in was cancelled.'); this.name = 'AuthCancelledError'; }
  }

  class BrowserAuthClient {
    constructor(options = {}) {
      this.root = options.root || root;
      this.rawFetch = options.fetch || root.fetch?.bind(root);
      this.location = options.location || root.location;
      this.document = options.document === undefined ? root.document : options.document;
      this.tokens = new Map();
      this.required = new Map();
      this.pending = new Map();
      this.prompt = options.prompt || (details => this.queueLogin(details));
      this.loginQueue = Promise.resolve();
      this.lastFailedPath = '/api/state';
      this.resourceListener = event => this.handleResourceClick(event);
      this.document?.addEventListener?.('click', this.resourceListener);
      this.bindDialog();
    }

    remoteId(path) {
      const value = typeof path === 'string' ? path : path?.toString?.() || '';
      if (value.split('?')[0] === '/ws/federation/presence') return null;
      const match = value.match(/^\/api\/federation\/([^/]+)\/proxy(?:\/|$)/) || value.match(/^\/ws\/federation\/([^/]+)(?:\/|$)/);
      if (!match) return null;
      try { return decodeURIComponent(match[1]); } catch { return match[1]; }
    }

    proxyPath(id, suffix) {
      return `/api/federation/${encodeURIComponent(id)}/proxy${suffix}`;
    }

    workspaceLabel(node) {
      if (node === 'local') return 'Local Swarm';
      const select = this.document?.querySelector?.('#swarm-selector');
      const option = select ? Array.from(select.options || []).find(item => item.value === node) : null;
      return option?.textContent?.replace(/\s+— offline$/, '') || node;
    }

    authHeaders(remote = null, includeRemote = true) {
      const headers = {};
      const local = this.tokens.get('local');
      if (local) headers.Authorization = `Bearer ${local}`;
      const target = remote && this.tokens.get(remote);
      if (includeRemote && target) headers['X-Swarm-Authorization'] = `Bearer ${target}`;
      return headers;
    }

    mergeHeaders(source, additions) {
      const headers = new this.root.Headers(source || {});
      for (const [name, value] of Object.entries(additions)) if (value) headers.set(name, value);
      return headers;
    }

    async readJSON(response) {
      try { return await response.json(); }
      catch { throw new Error(`Authentication server returned an invalid response (${response.status || 'unknown'}).`); }
    }

    async status(node) {
      if (this.required.has(node)) return this.required.get(node);
      const key = `status:${node}`;
      if (this.pending.has(key)) return this.pending.get(key);
      const operation = (async () => {
        if (node !== 'local') await this.ensure('local');
        const path = node === 'local' ? '/api/auth/status' : this.proxyPath(node, '/api/auth/status');
        const response = await this.rawFetch(path, {headers: this.mergeHeaders(undefined, this.authHeaders(null, false))});
        const value = await this.readJSON(response);
        if (response.status === 401 && node !== 'local' && response.headers?.get?.('X-Swarm-Auth-Node') === 'local') {
          this.invalidate('/api/state'); await this.ensure('local');
          return this.readStatusAgain(node);
        }
        if (response.status === 404) { this.required.set(node, false); return false; }
        if (!response.ok || typeof value.required !== 'boolean') throw new Error(value?.detail || `Could not read authentication status (${response.status}).`);
        this.required.set(node, value.required);
        return value.required;
      })();
      this.pending.set(key, operation);
      try { return await operation; } finally { if (this.pending.get(key) === operation) this.pending.delete(key); }
    }

    async readStatusAgain(node) {
      const path = this.proxyPath(node, '/api/auth/status');
      const response = await this.rawFetch(path, {headers: this.mergeHeaders(undefined, this.authHeaders(null, false))});
      const value = await this.readJSON(response);
      if (response.status === 404) { this.required.set(node, false); return false; }
      if (!response.ok || typeof value.required !== 'boolean') throw new Error('Could not read remote authentication status.');
      this.required.set(node, value.required); return value.required;
    }

    async login(node, password) {
      const remote = node === 'local' ? null : node;
      const path = remote ? this.proxyPath(remote, '/api/auth/login') : '/api/auth/login';
      // A remote login authenticates only to the local gateway; no stale target token is sent.
      const headers = this.mergeHeaders({'Content-Type': 'application/json'}, this.authHeaders(null, false));
      const response = await this.rawFetch(path, {method: 'POST', headers, body: JSON.stringify({password})});
      const value = await this.readJSON(response);
      if (remote && response.status === 401 && response.headers?.get?.('X-Swarm-Auth-Node') === 'local') {
        this.invalidate('/api/state'); await this.ensure('local');
        return this.login(node, password);
      }
      if (response.ok && value.required === false) {
        this.required.set(node, false); this.tokens.delete(node); return;
      }
      if (!response.ok || typeof value.token !== 'string' || !value.token) {
        const error = new Error(typeof value?.detail === 'string' ? value.detail : 'Incorrect password.');
        error.status = response.status; throw error;
      }
      this.tokens.set(node, value.token);
    }

    async ensure(node) {
      const key = `ensure:${node}`;
      if (this.pending.has(key)) return this.pending.get(key);
      const operation = (async () => {
        if (node !== 'local') await this.ensure('local');
        if (!await this.status(node) || this.tokens.has(node)) return;
        let error = '';
        while (this.required.get(node) !== false && !this.tokens.has(node)) {
          let password;
          try { password = await this.prompt({node, workspace: this.workspaceLabel(node), error}); }
          catch (reason) { this.showRetry(this.lastFailedPath); throw reason instanceof AuthCancelledError ? reason : new AuthCancelledError(); }
          try { await this.login(node, password); error = ''; }
          catch (reason) { error = reason.message || 'Sign in failed.'; }
          finally { password = ''; }
        }
        this.hideRetry();
      })();
      this.pending.set(key, operation);
      try { return await operation; } finally { if (this.pending.get(key) === operation) this.pending.delete(key); }
    }

    async ensurePath(path) {
      this.lastFailedPath = path;
      await this.ensure('local');
      const remote = this.remoteId(path);
      if (remote) await this.ensure(remote);
      return remote;
    }

    invalidate(path, atLocal = false) {
      const remote = atLocal ? null : this.remoteId(path);
      const node = remote || 'local';
      this.tokens.delete(node); this.required.delete(node); this.showRetry(path);
      this.dispatch('swarm-auth-unauthorized', {node, remote});
    }

    async fetch(path, options = {}) {
      const remote = await this.ensurePath(path);
      const request = () => {
        const headers = this.mergeHeaders(options.headers, this.authHeaders(remote));
        return this.rawFetch(path, {...options, headers});
      };
      let response = await request();
      if (response.status === 401 && !options.__swarmAuthRetried) {
        this.invalidate(path, response.headers?.get?.('X-Swarm-Auth-Node') === 'local');
        await this.ensurePath(path);
        this.dispatch('swarm-auth-retry', {path});
        response = await request();
      }
      return response;
    }

    websocketURL(path) {
      const protocol = this.location.protocol === 'https:' ? 'wss:' : 'ws:';
      return `${protocol}//${this.location.host}${path}`;
    }

    async ticket(path) {
      const remote = await this.ensurePath(path);
      const headers = this.mergeHeaders({'Content-Type': 'application/json'}, this.authHeaders(remote));
      const response = await this.rawFetch('/api/auth/ticket', {method: 'POST', headers, body: JSON.stringify({path})});
      const value = await this.readJSON(response);
      if (!response.ok || typeof value.ticket !== 'string' || !value.ticket) {
        if (response.status === 401) this.invalidate('/api/state');
        throw new Error(typeof value?.detail === 'string' ? value.detail : 'Could not authorize this resource.');
      }
      return value.ticket;
    }

    async socketURL(path) {
      const remote = await this.ensurePath(path);
      const protectedSocket = this.required.get('local') === true || (remote && this.required.get(remote) === true);
      if (!protectedSocket) return this.websocketURL(path);
      const ticket = await this.ticket(path);
      const separator = path.includes('?') ? '&' : '?';
      return this.websocketURL(`${path}${separator}access_ticket=${encodeURIComponent(ticket)}`);
    }

    isResourcePath(pathname) {
      const local = /^\/api\/agents\/[^/]+\/(?:files\/[^/]+\/content|products\/[^/]+\/download)$/;
      const remote = /^\/api\/federation\/[^/]+\/proxy\/api\/agents\/[^/]+\/(?:files\/[^/]+\/content|products\/[^/]+\/download)$/;
      return local.test(pathname) || remote.test(pathname);
    }

    resourceNeedsTicket(path) {
      const remote = this.remoteId(path);
      if (!this.required.has('local') || (remote && !this.required.has(remote))) return null;
      return this.required.get('local') === true || (!!remote && this.required.get(remote) === true);
    }

    async handleResourceClick(event) {
      if (event.defaultPrevented || event.button > 0 || event.altKey) return;
      const anchor = event.target?.closest?.('a[href]');
      if (!anchor || anchor.dataset?.swarmTicket === 'true') return;
      let url;
      try { url = new URL(anchor.href, this.location.href); } catch { return; }
      if (url.origin !== this.location.origin || !this.isResourcePath(url.pathname)) return;
      // Resource links are rendered only after their workspace API has established status.
      // Unknown and known-no-auth links retain completely native legacy navigation.
      if (this.resourceNeedsTicket(url.pathname + url.search) !== true) return;
      event.preventDefault();
      const newContext = anchor.target === '_blank' || event.metaKey || event.ctrlKey || event.shiftKey;
      const blank = newContext ? this.root.open?.('', '_blank') : null;
      if (blank) { try { blank.opener = null; } catch { /* Browser already isolated it. */ } }
      try {
        const exactPath = url.pathname + url.search;
        const remote = await this.ensurePath(exactPath);
        const protectedResource = this.required.get('local') === true || (remote && this.required.get(remote) === true);
        if (!protectedResource) {
          if (blank) blank.location.href = url.href;
          else this.navigateAnchor(anchor, url.href);
          return;
        }
        const accessTicket = await this.ticket(exactPath);
        url.searchParams.set('access_ticket', accessTicket);
        if (blank) blank.location.href = url.href;
        else this.navigateAnchor(anchor, url.href);
      } catch (error) {
        try { blank?.close?.(); } catch { /* ignored */ }
        this.showRetry(url.pathname + url.search);
      }
    }

    navigateAnchor(source, href) {
      const anchor = this.document.createElement('a');
      anchor.href = href; anchor.dataset.swarmTicket = 'true';
      if (source.download) anchor.download = source.download;
      anchor.rel = 'noopener noreferrer'; anchor.hidden = true;
      this.document.body.append(anchor); anchor.click(); anchor.remove();
    }

    bindDialog() {
      const dialog = this.document?.querySelector?.('#auth-dialog');
      if (!dialog) return;
      this.dialog = dialog; this.form = dialog.querySelector('form'); this.passwordInput = this.form?.elements?.password;
      dialog.querySelector('[data-auth-cancel]')?.addEventListener('click', () => this.cancelDialog());
      dialog.addEventListener('cancel', event => { event.preventDefault(); this.cancelDialog(); });
      this.form?.addEventListener('submit', event => {
        event.preventDefault();
        const password = this.passwordInput.value;
        this.passwordInput.value = '';
        const resolve = this.dialogResolve; this.dialogResolve = null; this.dialogReject = null;
        dialog.close(); resolve?.(password);
      });
      this.document.querySelector('#auth-retry')?.addEventListener('click', async () => {
        this.hideRetry();
        try { await this.ensurePath(this.lastFailedPath); this.dispatch('swarm-auth-retry', {path: this.lastFailedPath}); }
        catch { this.showRetry(this.lastFailedPath); }
      });
    }

    queueLogin(details) {
      const queued = this.loginQueue.catch(() => {}).then(() => this.showLogin(details));
      this.loginQueue = queued.catch(() => {}); return queued;
    }

    showLogin({workspace, error = ''}) {
      if (!this.dialog || !this.form) return Promise.reject(new Error('Authentication dialog is unavailable.'));
      this.dialog.querySelector('#auth-workspace').textContent = workspace;
      this.dialog.querySelector('#auth-error').textContent = error;
      this.passwordInput.value = '';
      this.dialog.showModal();
      queueMicrotask(() => this.passwordInput.focus());
      return new Promise((resolve, reject) => { this.dialogResolve = resolve; this.dialogReject = reject; });
    }

    cancelDialog() {
      if (!this.dialog?.open) return;
      this.passwordInput.value = '';
      const reject = this.dialogReject; this.dialogResolve = null; this.dialogReject = null;
      this.dialog.close(); reject?.(new AuthCancelledError());
    }

    showRetry(path) {
      this.lastFailedPath = path || this.lastFailedPath;
      const region = this.document?.querySelector?.('#auth-required');
      if (region) region.hidden = false;
    }

    hideRetry() {
      const region = this.document?.querySelector?.('#auth-required');
      if (region) region.hidden = true;
    }

    dispatch(name, detail) {
      try { this.root.dispatchEvent(new this.root.CustomEvent(name, {detail})); } catch { /* test/minimal DOM */ }
    }
  }

  const api = {BrowserAuthClient, AuthCancelledError};
  if (root.document && root.fetch && root.Headers) api.BrowserAuth = new BrowserAuthClient();
  root.SwarmAuth = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
