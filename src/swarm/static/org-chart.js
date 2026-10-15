(function (root) {
  'use strict';

  function validId(value) {
    return typeof value === 'string' && value.trim().length > 0;
  }

  function buildForest(agents, matchingIds = null) {
    if (!Array.isArray(agents)) throw new TypeError('agents must be an array');
    if (matchingIds !== null && !(matchingIds instanceof Set)) {
      throw new TypeError('matchingIds must be a Set or null');
    }

    const records = [];
    const byId = new Map();
    for (const agent of agents) {
      if (!agent || typeof agent !== 'object' || !validId(agent.id) || byId.has(agent.id)) continue;
      const record = {
        index: records.length,
        agent,
        parent: null,
        children: [],
        issue: null,
      };
      records.push(record);
      byId.set(agent.id, record);
    }

    for (const record of records) {
      const parentId = record.agent.parent_id;
      if (!validId(parentId)) continue;
      if (parentId === record.agent.id) {
        record.issue = 'Agent is its own parent';
      } else if (!byId.has(parentId)) {
        record.issue = `Missing parent: ${parentId}`;
      } else {
        record.parent = byId.get(parentId);
      }
    }

    // Every record has at most one parent. Walk those links iteratively and cut
    // the earliest input record in each cycle so the result is deterministic.
    const finished = new Set();
    for (const start of records) {
      if (finished.has(start)) continue;
      const path = [];
      const position = new Map();
      let current = start;
      while (current && !finished.has(current) && !position.has(current)) {
        position.set(current, path.length);
        path.push(current);
        current = current.parent;
      }
      if (current && position.has(current)) {
        const cycle = path.slice(position.get(current));
        let cut = cycle[0];
        for (const record of cycle) if (record.index < cut.index) cut = record;
        cut.parent = null;
        cut.issue = 'Cycle in parent hierarchy';
      }
      for (const record of path) finished.add(record);
    }

    for (const record of records) {
      record.children.length = 0;
      if (record.parent) record.parent.children.push(record);
    }

    const matched = new Set();
    if (matchingIds === null) {
      for (const record of records) matched.add(record);
    } else {
      for (const record of records) if (matchingIds.has(record.agent.id)) matched.add(record);
    }

    const kept = new Set(matched);
    if (matchingIds !== null) {
      for (const record of matched) {
        let parent = record.parent;
        while (parent && !kept.has(parent)) {
          kept.add(parent);
          parent = parent.parent;
        }
      }
    }

    const output = new Map();
    for (const record of records) {
      if (!kept.has(record)) continue;
      output.set(record, {
        agent: record.agent,
        children: [],
        contextOnly: matchingIds !== null && !matched.has(record),
        issue: record.issue,
      });
    }

    const roots = [];
    let issuesCount = 0;
    for (const record of records) {
      if (!kept.has(record)) continue;
      const result = output.get(record);
      if (result.issue !== null) issuesCount += 1;
      if (record.parent && kept.has(record.parent)) output.get(record.parent).children.push(result);
      else roots.push(result);
    }

    return {
      roots,
      matchedCount: matched.size,
      ancestorCount: kept.size - matched.size,
      shownCount: kept.size,
      issuesCount,
    };
  }

  function renderForest(roots, renderCard, document = root.document) {
    if (!Array.isArray(roots)) throw new TypeError('roots must be an array');
    if (typeof renderCard !== 'function') throw new TypeError('renderCard must be a function');
    if (!document || typeof document.createElement !== 'function') {
      throw new TypeError('document must provide createElement');
    }

    const forest = document.createElement('ul');
    forest.className = 'org-forest';
    const stack = [];
    for (let i = roots.length - 1; i >= 0; i -= 1) stack.push({node: roots[i], list: forest});

    while (stack.length) {
      const item = stack.pop();
      const hierarchyNode = item.node;
      const li = document.createElement('li');
      const classes = ['org-node'];
      if (hierarchyNode.contextOnly) classes.push('is-context');
      if (hierarchyNode.issue) classes.push('has-hierarchy-issue');
      li.className = classes.join(' ');

      const agent = hierarchyNode.agent || {};
      const name = String(agent.name || agent.id || 'Unnamed agent');
      const descriptions = [];
      if (hierarchyNode.issue) descriptions.push(`hierarchy issue: ${hierarchyNode.issue}`);
      if (hierarchyNode.contextOnly) descriptions.push('hierarchy context');
      li.setAttribute('aria-label', [name, ...descriptions].join('; '));

      const card = renderCard(agent);
      if (descriptions.length) {
        const information = descriptions.join('; ');
        card.title = card.title ? `${card.title}\n${information}` : information;
      }
      li.append(card);
      item.list.append(li);

      const children = Array.isArray(hierarchyNode.children) ? hierarchyNode.children : [];
      if (children.length) {
        const childList = document.createElement('ul');
        childList.className = 'org-children';
        childList.setAttribute('aria-label', `Created by ${name}`);
        li.append(childList);
        for (let i = children.length - 1; i >= 0; i -= 1) {
          stack.push({node: children[i], list: childList});
        }
      }
    }
    return forest;
  }

  const api = {buildForest, renderForest};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SwarmOrgChart = api;
})(globalThis);
