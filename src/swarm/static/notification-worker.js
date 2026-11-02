/* Service worker used only to display and focus Swarm system notifications. */
'use strict';

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const client of windows) {
      if ('focus' in client) return client.focus();
    }
    return self.clients.openWindow('/');
  })());
});
