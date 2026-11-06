/* Search the full card's metadata, independent of its compact/expanded DOM state. */
(function (root) {
  'use strict';
  const LABELS = {running: 'Running', waiting: 'Waiting', dormant: 'Dormant', idle: 'Idle', done: 'Done'};
  const normalize = value => typeof value === 'string' ? value.normalize('NFC').toLowerCase().replace(/ß/g, 'ss').replace(/ς/g, 'σ').normalize('NFC') : '';

  function cardText(agent, {parentName = '', groupName = ''} = {}) {
    if (!agent || typeof agent !== 'object') return '';
    const text = [];
    const add = (...values) => {
      for (const value of values) if (typeof value === 'string' || typeof value === 'number') text.push(String(value));
    };
    add(agent.name, agent.id, agent.backend, agent.goal, agent.activity || 'No activity reported yet',
      agent.status, LABELS[agent.status], agent.alive ? 'Process online Online Attached' : 'Offline Not running', groupName);
    add('Current activity', 'Details', 'Folder', agent.folder, 'Repository', agent.repository,
      'Worktree', agent.worktree, 'Branch', agent.branch, 'Session', agent.session_id || 'Not persisted yet');
    if (!agent.managed) add('observed');
    if (agent.parent_id || parentName) add('Parent', parentName || agent.parent_id);
    if (agent.pid) add('Process', 'PID', agent.pid);
    add('Restore', agent.auto_resume ? 'Automatic' : 'Manual');
    if (agent.wake_at) {
      add('Wake at', agent.wake_at);
      const date = new Date(agent.wake_at);
      if (!Number.isNaN(date.getTime())) add(date.toLocaleString());
    }
    const delivery = agent.wake_delivery;
    if (delivery && ['pending', 'delivered'].includes(delivery.state)) {
      add('Wake delivery', 'Wake event', delivery.id);
      if (delivery.state === 'pending') add(`${agent.auto_resume ? 'Pending' : 'Paused'} · ${delivery.attempts} attempt(s)`, 'Wake retry', delivery.retry_at || 'Due');
      else add('Handed to agent process');
    }
    if (agent.import_source) add('Imported', 'Source', agent.import_source.backend,
      agent.import_source.session_id, agent.import_source.session_file);
    if (agent.notes) add('Checkpoint', agent.notes);
    add(agent.wait_reason, agent.last_error, agent.updated_at);
    if (Array.isArray(agent.activities)) for (const item of agent.activities) add(item?.title, item?.status);
    if (agent.creation || ['provisioning', 'failed', 'cancelled'].includes(agent.spawn_state)) {
      add('Setup', agent.creation?.stage, agent.creation?.state, agent.spawn_state,
        agent.creation?.error, agent.creation?.log);
      add({queued: 'Queued', running: 'Setting up', completed: 'Setup complete', failed: 'Setup failed', cancelled: 'Setup cancelled'}[agent.creation?.state]);
    }
    const restart = agent.restart;
    const restarting = Object.prototype.hasOwnProperty.call(agent, 'restart_job_id')
      ? !!agent.restart_job_id : !!restart?.job_id && !['completed', 'cancelled', 'failed'].includes(restart.state);
    if (restarting) add('Restarting', 'Restart in progress', restart?.stage, restart?.state, restart?.error);

    // Match only products the renderer would expose, never credential-bearing URLs.
    const product = agent.product;
    if (product?.kind === 'file' && typeof product.id === 'string' && product.id) {
      add('Product', 'Download latest product', product.label || 'Product', product.filename);
    } else if (product?.kind === 'link') {
      try {
        const url = new URL(product.url);
        if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password) {
          add('Product', 'Open latest product', product.label || 'Product', url.href);
        }
      } catch { /* Invalid products are not displayed or searched. */ }
    }
    if (Number.isInteger(agent.file_count) && agent.file_count > 0) add('Files', 'registered files', agent.file_count);
    add(agent.files_error);
    // Only safe labels/basenames/types supplied by the bounded reference index.
    // No original paths, file contents, hidden metadata or mailbox fetches.
    if (Array.isArray(agent.file_search)) for (const file of agent.file_search.slice(0, 64)) {
      add(file?.label, file?.filename, file?.media_type);
    }
    if (agent.managed) add('Messages', `${Number.isInteger(agent.message_count) ? agent.message_count : 0} outstanding message${agent.message_count === 1 ? '' : 's'}`);
    add(agent.messages_error);
    return normalize(text.join(' '));
  }

  function matches(agent, query, context) {
    const needle = normalize(typeof query === 'string' ? query.trim() : '');
    return !needle || cardText(agent, context).includes(needle);
  }

  const api = {normalize, cardText, matches};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmAgentSearch = api;
})(globalThis);
