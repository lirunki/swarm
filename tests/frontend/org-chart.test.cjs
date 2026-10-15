const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {buildForest, renderForest} = require('../../src/swarm/static/org-chart.js');

const agent = (id, parent_id = null, name = id) => ({id, parent_id, name});

function flatten(roots) {
  const values = [];
  const stack = [...roots].reverse();
  while (stack.length) {
    const node = stack.pop();
    values.push(node);
    for (let i = node.children.length - 1; i >= 0; i -= 1) stack.push(node.children[i]);
  }
  return values;
}

function ids(nodes) {
  return nodes.map(node => node.agent.id);
}

test('exports the exact helper interface in CommonJS and classic browser contexts', () => {
  const api = require('../../src/swarm/static/org-chart.js');
  assert.deepEqual(Object.keys(api).sort(), ['buildForest', 'renderForest']);
  const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/org-chart.js'), 'utf8');
  const context = vm.createContext({});
  vm.runInContext(source, context);
  assert.equal(typeof context.SwarmOrgChart.buildForest, 'function');
  assert.equal(typeof context.SwarmOrgChart.renderForest, 'function');
});

test('builds branches in input order without mutating agents', () => {
  const agents = [agent('second', 'root'), agent('root'), agent('first', 'root'), agent('other')];
  const before = JSON.stringify(agents);
  const forest = buildForest(agents);
  assert.deepEqual(ids(forest.roots), ['root', 'other']);
  assert.deepEqual(ids(forest.roots[0].children), ['second', 'first']);
  assert.deepEqual(forest, {
    roots: forest.roots,
    matchedCount: 4,
    ancestorCount: 0,
    shownCount: 4,
    issuesCount: 0,
  });
  assert.equal(JSON.stringify(agents), before);
  for (const node of flatten(forest.roots)) {
    assert.equal(node.contextOnly, false);
    assert.equal(node.issue, null);
  }
});

test('filtering retains only matches and ancestors, never unrelated children', () => {
  const agents = [
    agent('root'), agent('branch', 'root'), agent('match', 'branch'),
    agent('sibling', 'root'), agent('descendant', 'match'), agent('other'),
  ];
  const forest = buildForest(agents, new Set(['match']));
  assert.deepEqual(ids(flatten(forest.roots)), ['root', 'branch', 'match']);
  assert.deepEqual(
    flatten(forest.roots).map(node => [node.agent.id, node.contextOnly]),
    [['root', true], ['branch', true], ['match', false]],
  );
  assert.deepEqual(
    {matchedCount: forest.matchedCount, ancestorCount: forest.ancestorCount, shownCount: forest.shownCount, issuesCount: forest.issuesCount},
    {matchedCount: 1, ancestorCount: 2, shownCount: 3, issuesCount: 0},
  );
  assert.deepEqual(buildForest(agents, new Set()).roots, []);
});

test('missing and self parents become roots with hierarchy issues', () => {
  const forest = buildForest([agent('missing', 'gone'), agent('self', 'self'), agent('normal')]);
  assert.deepEqual(ids(forest.roots), ['missing', 'self', 'normal']);
  assert.equal(forest.roots[0].issue, 'Missing parent: gone');
  assert.equal(forest.roots[1].issue, 'Agent is its own parent');
  assert.equal(forest.roots[2].issue, null);
  assert.equal(forest.issuesCount, 2);
});

test('cycles are cut deterministically and no records are lost', () => {
  const agents = [agent('c', 'b'), agent('outside'), agent('b', 'a'), agent('a', 'c'), agent('child', 'b')];
  const first = buildForest(agents);
  const second = buildForest(agents);
  assert.deepEqual(ids(first.roots), ['c', 'outside']);
  assert.deepEqual(ids(flatten(first.roots)), ['c', 'a', 'b', 'child', 'outside']);
  assert.equal(new Set(ids(flatten(first.roots))).size, agents.length);
  assert.equal(first.roots[0].issue, 'Cycle in parent hierarchy');
  assert.equal(first.issuesCount, 1);
  assert.deepEqual(
    flatten(first.roots).map(node => [node.agent.id, ids(node.children), node.issue]),
    flatten(second.roots).map(node => [node.agent.id, ids(node.children), node.issue]),
  );
});

test('duplicate and malformed IDs are ignored stably', () => {
  const first = agent('same');
  const duplicate = agent('same', null, 'duplicate');
  const forest = buildForest([
    null, {}, {id: ''}, {id: '   '}, {id: 3}, first, duplicate,
    agent('child', 'same'), agent('orphan', 'ignored'), {id: 'ignored', name: 'valid later'},
  ]);
  assert.deepEqual(ids(forest.roots), ['same', 'ignored']);
  assert.strictEqual(forest.roots[0].agent, first);
  assert.deepEqual(ids(forest.roots[0].children), ['child']);
  assert.deepEqual(ids(forest.roots[1].children), ['orphan']);
  assert.equal(forest.shownCount, 4);
});

test('buildForest validates arguments with stable errors', () => {
  assert.throws(() => buildForest(null), {name: 'TypeError', message: 'agents must be an array'});
  assert.throws(() => buildForest([], []), {name: 'TypeError', message: 'matchingIds must be a Set or null'});
});

test('deep chains use iterative hierarchy and filtering algorithms', () => {
  const count = 30000;
  const agents = Array.from({length: count}, (_, index) => agent(`a${index}`, index ? `a${index - 1}` : null));
  const forest = buildForest(agents, new Set([`a${count - 1}`]));
  assert.equal(forest.shownCount, count);
  assert.equal(forest.matchedCount, 1);
  assert.equal(forest.ancestorCount, count - 1);
  let node = forest.roots[0];
  for (let index = 0; index < count; index += 1) {
    assert.equal(node.agent.id, `a${index}`);
    node = node.children[0];
  }
  assert.equal(node, undefined);
});

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.attributes = new Map();
    this.className = '';
    this.title = '';
    this.textContent = '';
  }
  append(...children) { this.childNodes.push(...children); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  get classList() { return {contains: name => this.className.split(/\s+/).includes(name)}; }
}
const document = {createElement: tag => new Element(tag)};
function walk(root) {
  const output = [], stack = [root];
  while (stack.length) {
    const current = stack.pop();
    output.push(current);
    for (let index = current.childNodes.length - 1; index >= 0; index -= 1) stack.push(current.childNodes[index]);
  }
  return output;
}

test('renderForest renders every node once with safe hierarchy metadata', () => {
  const unsafe = '<img src=x onerror=bad()> & "owner"';
  const agents = [agent('root', 'missing', unsafe), agent('child', 'root', '<script>bad()</script>')];
  const forest = buildForest(agents, new Set(['child']));
  const calls = [];
  const rendered = renderForest(forest.roots, record => {
    calls.push(record.id);
    const card = document.createElement('article');
    card.title = `Card for ${record.name}`;
    card.textContent = record.name;
    return card;
  }, document);

  assert.equal(rendered.tagName, 'UL');
  assert.equal(rendered.className, 'org-forest');
  assert.deepEqual(calls, ['root', 'child']);
  const elements = walk(rendered);
  const items = elements.filter(element => element.tagName === 'LI');
  const lists = elements.filter(element => element.tagName === 'UL');
  const cards = elements.filter(element => element.tagName === 'ARTICLE');
  assert.equal(items.length, 2); assert.equal(cards.length, 2); assert.equal(lists.length, 2);
  assert.equal(items[0].classList.contains('is-context'), true);
  assert.equal(items[0].classList.contains('has-hierarchy-issue'), true);
  assert.equal(items[0].getAttribute('aria-label'), `${unsafe}; hierarchy issue: Missing parent: missing; hierarchy context`);
  assert.equal(lists[1].className, 'org-children');
  assert.equal(lists[1].getAttribute('aria-label'), `Created by ${unsafe}`);
  assert.match(cards[0].title, /Card for/);
  assert.match(cards[0].title, /hierarchy issue: Missing parent: missing/);
  assert.match(cards[0].title, /hierarchy context/);
  assert.equal(elements.some(element => ['IMG', 'SCRIPT'].includes(element.tagName)), false);
  assert.equal(items.every(item => item.textContent === ''), true);
});

test('renderForest handles deep trees iteratively and validates arguments', () => {
  const depth = 20000;
  let root = {agent: agent(`n${depth - 1}`), children: [], contextOnly: false, issue: null};
  for (let index = depth - 2; index >= 0; index -= 1) {
    root = {agent: agent(`n${index}`), children: [root], contextOnly: false, issue: null};
  }
  let rendered = 0;
  const forest = renderForest([root], () => { rendered += 1; return document.createElement('article'); }, document);
  assert.equal(forest.className, 'org-forest');
  assert.equal(rendered, depth);
  assert.throws(() => renderForest({}, () => {}, document), {name: 'TypeError', message: 'roots must be an array'});
  assert.throws(() => renderForest([], null, document), {name: 'TypeError', message: 'renderCard must be a function'});
  assert.throws(() => renderForest([], () => {}, {}), {name: 'TypeError', message: 'document must provide createElement'});
});
