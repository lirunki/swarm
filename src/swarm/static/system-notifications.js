/* Browser-local, permission-gated operating-system notifications. */
(function (root) {
  'use strict';

  const preferenceKey = 'swarm.system-notifications.enabled';
  const workerPath = '/static/notification-worker.js?v=system-notifications-1';

  class SystemNotifications {
    constructor(options = {}) {
      this.root = options.root || root;
      this.navigator = options.navigator === undefined ? this.root.navigator : options.navigator;
      this.Notification = options.Notification === undefined ? this.root.Notification : options.Notification;
      if (options.storage !== undefined) this.storage = options.storage;
      else {
        try { this.storage = this.root.localStorage; }
        catch (_) { this.storage = null; }
      }
      this.workerPath = options.workerPath || workerPath;
      this.registrationPromise = null;
    }

    supported() {
      if (this.root?.isSecureContext === false) return false;
      return !!this.Notification && (!!this.navigator?.serviceWorker || typeof this.Notification === 'function');
    }

    permission() {
      return typeof this.Notification?.permission === 'string' ? this.Notification.permission : 'default';
    }

    preferred() {
      try { return this.storage?.getItem(preferenceKey) === 'true'; }
      catch (_) { return false; }
    }

    setPreferred(enabled) {
      try {
        if (enabled) this.storage?.setItem(preferenceKey, 'true');
        else this.storage?.removeItem(preferenceKey);
      } catch (_) {}
    }

    status() {
      if (!this.supported()) return 'unsupported';
      if (this.permission() === 'denied') return 'blocked';
      return this.preferred() && this.permission() === 'granted' ? 'enabled' : 'disabled';
    }

    async registration() {
      if (!this.navigator?.serviceWorker) return null;
      if (!this.registrationPromise) {
        this.registrationPromise = this.navigator.serviceWorker.register(this.workerPath).catch(error => {
          this.registrationPromise = null;
          throw error;
        });
      }
      return this.registrationPromise;
    }

    async enable() {
      if (!this.supported()) throw new Error('System notifications are unavailable in this browser.');
      let permission = this.permission();
      if (permission === 'default' && typeof this.Notification.requestPermission === 'function') {
        permission = await this.Notification.requestPermission();
      }
      if (permission !== 'granted') {
        this.setPreferred(false);
        throw new Error('System notifications are blocked. Allow them in the browser site settings.');
      }
      if (this.navigator?.serviceWorker) await this.registration();
      this.setPreferred(true);
      return true;
    }

    disable() {
      this.setPreferred(false);
    }

    async notify(item = {}) {
      if (this.status() !== 'enabled') return false;
      const title = String(item.title || 'Swarm notification');
      const options = {
        body: String(item.body || ''),
        tag: String(item.tag || 'swarm-notification'),
        icon: '/static/swarm-notification.svg',
        data: item.data || {},
      };
      try {
        const registration = await this.registration();
        if (registration?.showNotification) {
          await registration.showNotification(title, options);
          return true;
        }
        if (typeof this.Notification === 'function') {
          new this.Notification(title, options);
          return true;
        }
      } catch (_) {
        return false;
      }
      return false;
    }
  }

  const api = {SystemNotifications, preferenceKey, workerPath};
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.SwarmSystemNotifications = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
