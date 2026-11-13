const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.css'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');

class Element {
  constructor(id = '') {
    this.id = id; this.children = []; this.parentNode = null; this.hidden = false;
    this.attributes = {}; this.classes = new Set();
    this.classList = {toggle: (name, force) => force ? this.classes.add(name) : this.classes.delete(name), contains: name => this.classes.has(name)};
  }
  append(...items) { for (const item of items) { item.remove(); item.parentNode = this; this.children.push(item); } }
  remove() { if (!this.parentNode) return; const values = this.parentNode.children; values.splice(values.indexOf(this), 1); this.parentNode = null; }
  insertBefore(item, before) { if (item === before) return; item.remove(); const index = before ? this.children.indexOf(before) : this.children.length; assert.ok(index >= 0); this.children.splice(index, 0, item); item.parentNode = this; }
  get nextSibling() { if (!this.parentNode) return null; return this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null; }
  setAttribute(name, value) { this.attributes[name] = value; }
}

test('Browser tab, screenshot-only viewer, settings, and controller asset are present', () => {
  assert.match(html, /id="tabs"[\s\S]*id="dashboard-tab"[\s\S]*id="browser-tab"/);
  for (const id of ['browser-panel', 'browser-owner', 'browser-status', 'browser-launch', 'browser-refresh-tabs',
    'browser-back', 'browser-forward', 'browser-reload', 'browser-copy', 'browser-paste', 'browser-url',
    'browser-new', 'browser-interval', 'browser-pause', 'browser-tabs', 'browser-screen', 'browser-placeholder',
    'browser-settings-section', 'browser-settings-owner', 'browser-enabled', 'browser-headless',
    'browser-executable', 'browser-settings-save', 'browser-settings-status']) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(html, /Screenshots, not video/);
  assert.doesNotMatch(html.slice(html.indexOf('id="browser-panel"'), html.indexOf('id="group-context-menu"')), /<iframe|<video/i);
  assert.ok(html.indexOf('/static/remote-browser.js?') < html.indexOf('/static/app.js?'));
  assert.match(css, /#dashboard-tab\{[^}]*flex:0 0 102px/);
  assert.match(css, /#browser-tab\.active\{[^}]*position:sticky[^}]*left:102px/);
  assert.match(css, /#browser-screen\{[^}]*max-width:100%/);
});

test('activation exposes Browser as a global workspace and deactivates it when leaving', () => {
  const dashboard = new Element('dashboard'), terminalsNode = new Element('terminals');
  const dashboardTab = new Element('dashboard-tab'), browserTab = new Element('browser-tab'), browserPanel = new Element('browser-panel');
  const terminal = {pane: new Element(), tab: new Element(), group: new Element(), mobileKeys: {update() {}}};
  const elements = new Map([['#dashboard', dashboard], ['#terminals', terminalsNode], ['#dashboard-tab', dashboardTab],
    ['#browser-tab', browserTab], ['#browser-panel', browserPanel]]);
  const calls = [];
  const context = vm.createContext({
    selectedAgentGroup: null, openedAgentGroups: new Set(), activeTab: 'dashboard', terminals: new Map([['agent-a', terminal]]),
    agentGroupFor: () => null, isAgentGroupTab: () => false, groupNameMenu: {dropdown: {close() {}}},
    remoteBrowserController: {active: true, deactivate() { calls.push('deactivate'); this.active = false; }},
    $: selector => elements.get(selector), syncAgentGroupTabs() {}, render() {}, initTerminal() {},
    requestAnimationFrame: callback => callback(), rememberUIViewState() {},
  });
  vm.runInContext(source.slice(source.indexOf('function activate('), source.indexOf('function openTerminal(')), context);
  context.activate('browser', false);
  assert.equal(context.activeTab, 'browser'); assert.equal(browserPanel.hidden, false);
  assert.equal(dashboard.hidden, true); assert.equal(terminalsNode.hidden, true);
  assert.equal(browserTab.attributes['aria-current'], 'page');
  context.activate('dashboard', false);
  assert.equal(browserPanel.hidden, true); assert.deepEqual(calls, ['deactivate']);
  assert.equal(dashboardTab.attributes['aria-current'], 'page');
});

test('Browser is last while inactive and pinned immediately after Dashboard while active', () => {
  const tabs = new Element('tabs'), dashboard = new Element('dashboard-tab'), browser = new Element('browser-tab');
  const terminalA = new Element(), terminalB = new Element(); tabs.append(dashboard, browser, terminalA, terminalB);
  const context = vm.createContext({
    selectedAgentGroup: null, openedAgentGroups: new Set(), agentGroupTabs: new Map(), activeTab: 'dashboard',
    state: {agents: [{id: 'a'}, {id: 'b'}]}, agentGroupProjection: () => ({groups: []}),
    SwarmAgentGroups: {effectiveAssignments: () => ({})}, terminals: new Map([['a', {group: terminalA}], ['b', {group: terminalB}]]),
    $: selector => selector === '#tabs' ? tabs : selector === '#dashboard-tab' ? dashboard : browser,
  });
  vm.runInContext(source.slice(source.indexOf('function syncAgentGroupTabs()'), source.indexOf('function renderAgentFolders(')), context);
  context.syncAgentGroupTabs();
  assert.deepEqual(tabs.children, [dashboard, terminalA, terminalB, browser]);
  context.activeTab = 'browser'; context.syncAgentGroupTabs();
  assert.deepEqual(tabs.children, [dashboard, browser, terminalA, terminalB]);
});

test('browser request helper enforces authenticated HTTP PNG and size checks', () => {
  assert.match(source, /async function browserRequest\(path, method = 'GET', body, binary = false\)/);
  assert.match(source, /type !== 'image\/png'/);
  assert.match(source, /8 \* 1024 \* 1024/);
  assert.match(source, /new SwarmRemoteBrowser\.RemoteBrowserController\(\{[\s\S]*request: browserRequest/);
  assert.doesNotMatch(source.slice(source.indexOf('async function browserRequest'), source.indexOf('const agentById')), /WebSocket/);
});
