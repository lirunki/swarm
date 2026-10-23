const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {BrowserAuthClient, AuthCancelledError} = require('../../src/swarm/static/auth.js');

const response = (status, value) => ({status, ok: status >= 200 && status < 300, json: async () => value});
function fixture(handler, options = {}) {
  const calls = [];
  const root = {
    Headers, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    dispatchEvent(event) { (this.events ||= []).push(event); },
    location: {protocol: 'https:', host: 'swarm.test', origin: 'https://swarm.test', href: 'https://swarm.test/'},
  };
  Object.defineProperty(root, 'localStorage', {get() { throw Error('auth touched localStorage'); }});
  Object.defineProperty(root, 'sessionStorage', {get() { throw Error('auth touched sessionStorage'); }});
  const rawFetch = async (url, request = {}) => {
    const record = {url, method: request.method || 'GET', headers: Object.fromEntries(new Headers(request.headers || {}).entries()), body: request.body};
    calls.push(record); return handler(record, calls);
  };
  const client = new BrowserAuthClient({root, location: root.location, document: null, fetch: rawFetch, prompt: options.prompt});
  return {client, calls, root};
}

test('optional auth leaves no-auth HTTP and websocket flows unchanged and uses no storage', async () => {
  const {client, calls} = fixture(call => {
    if (call.url === '/api/auth/status') return response(200, {required: false});
    if (call.url === '/api/state') return response(200, {agents: []});
    throw Error(`unexpected ${call.url}`);
  });
  assert.equal((await client.fetch('/api/state')).status, 200);
  assert.equal(await client.socketURL('/ws/state'), 'wss://swarm.test/ws/state');
  assert.deepEqual(calls.map(call => call.url), ['/api/auth/status', '/api/state']);
  assert.equal(calls[1].headers.authorization, undefined);
  assert.equal(calls.some(call => call.url === '/api/auth/ticket'), false);
});

test('local and remote tokens stay separated and concurrent requests coalesce each login', async () => {
  const prompts = [];
  const {client, calls} = fixture(call => {
    if (call.url === '/api/auth/status') return response(200, {required: true});
    if (call.url === '/api/auth/login') return response(200, {token: 'LOCAL_TOKEN'});
    if (call.url.endsWith('/proxy/api/auth/status')) return response(200, {required: true});
    if (call.url.endsWith('/proxy/api/auth/login')) return response(200, {token: 'REMOTE_TOKEN'});
    if (call.url.endsWith('/proxy/api/state')) return response(200, {agents: []});
    throw Error(`unexpected ${call.url}`);
  }, {prompt: async details => { prompts.push(details.node); return details.node === 'local' ? 'local-password' : 'remote-password'; }});
  const target = '/api/federation/beta/proxy/api/state';
  await Promise.all([client.fetch(target), client.fetch(target)]);
  assert.deepEqual(prompts.sort(), ['beta', 'local']);
  assert.equal(calls.filter(call => call.url === '/api/auth/login').length, 1);
  assert.equal(calls.filter(call => call.url.endsWith('/proxy/api/auth/login')).length, 1);
  const remoteLogin = calls.find(call => call.url.endsWith('/proxy/api/auth/login'));
  assert.equal(remoteLogin.headers.authorization, 'Bearer LOCAL_TOKEN');
  assert.equal(remoteLogin.headers['x-swarm-authorization'], undefined);
  for (const data of calls.filter(call => call.url.endsWith('/proxy/api/state'))) {
    assert.equal(data.headers.authorization, 'Bearer LOCAL_TOKEN');
    assert.equal(data.headers['x-swarm-authorization'], 'Bearer REMOTE_TOKEN');
  }
  assert.equal([...client.tokens.keys()].sort().join(','), 'beta,local');
  assert.equal(calls.some(call => /password|TOKEN/.test(call.url)), false);
});

test('cancel rejects all coalesced waiters once without an automatic prompt loop', async () => {
  let prompts = 0;
  const {client, calls} = fixture(call => {
    if (call.url === '/api/auth/status') return response(200, {required: true});
    throw Error(`unexpected ${call.url}`);
  }, {prompt: async () => { prompts += 1; throw new AuthCancelledError(); }});
  const results = await Promise.allSettled([client.fetch('/api/state'), client.fetch('/api/state')]);
  assert.equal(prompts, 1);
  assert.ok(results.every(item => item.status === 'rejected' && item.reason.name === 'AuthCancelledError'));
  assert.deepEqual(calls.map(call => call.url), ['/api/auth/status']);
});

test('remote websocket ticket carries both headers and binds the exact websocket path', async () => {
  const {client, calls} = fixture(call => {
    if (call.url === '/api/auth/status') return response(200, {required: true});
    if (call.url === '/api/auth/login') return response(200, {token: 'LOCAL'});
    if (call.url.endsWith('/proxy/api/auth/status')) return response(200, {required: true});
    if (call.url.endsWith('/proxy/api/auth/login')) return response(200, {token: 'REMOTE'});
    if (call.url === '/api/auth/ticket') return response(200, {ticket: 'ONE_USE'});
    throw Error(`unexpected ${call.url}`);
  }, {prompt: async ({node}) => `${node}-password`});
  const socketPath = '/ws/federation/beta/terminal/a?cols=80&rows=24';
  const url = await client.socketURL(socketPath);
  assert.equal(url, `wss://swarm.test${socketPath}&access_ticket=ONE_USE`);
  const ticket = calls.find(call => call.url === '/api/auth/ticket');
  assert.deepEqual(JSON.parse(ticket.body), {path: socketPath});
  assert.equal(ticket.headers.authorization, 'Bearer LOCAL');
  assert.equal(ticket.headers['x-swarm-authorization'], 'Bearer REMOTE');
  assert.doesNotMatch(url, /LOCAL|REMOTE|password/i);
});

test('resource-link gate preserves known no-auth links and tickets protected links without fetching blobs', async () => {
  const {client, calls, root} = fixture(call => {
    if (call.url === '/api/auth/ticket') return response(200, {ticket: 'FILE_TICKET'});
    throw Error(`unexpected ${call.url}`);
  });
  const resource = '/api/agents/a/files/11111111-1111-4111-8111-111111111111/content?download=true';
  const anchor = {href: `https://swarm.test${resource}`, target: '_blank', dataset: {}, closest: () => anchor};
  let prevented = false;
  client.required.set('local', false);
  await client.handleResourceClick({target: anchor, button: 0, preventDefault() { prevented = true; }});
  assert.equal(prevented, false); assert.equal(calls.length, 0);

  client.required.set('local', true); client.tokens.set('local', 'LOCAL');
  const popup = {location: {}, close() { this.closed = true; }}; root.open = () => popup; prevented = false;
  await client.handleResourceClick({target: anchor, button: 0, preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(calls.length, 1);
  assert.deepEqual(JSON.parse(calls[0].body), {path: resource});
  assert.equal(calls[0].headers.authorization, 'Bearer LOCAL');
  assert.match(popup.location.href, /access_ticket=FILE_TICKET/);
  assert.doesNotMatch(popup.location.href, /Bearer|LOCAL/);
});

test('page loads accessible auth UI before app and app routes APIs and sockets through it', () => {
  const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
  const app = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');
  const auth = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/auth.js'), 'utf8');
  assert.ok(html.indexOf('/static/auth.js') < html.indexOf('/static/app.js'));
  assert.match(html, /id="auth-dialog"[^>]*aria-labelledby="auth-title"[^>]*aria-describedby="auth-description"/);
  assert.match(html, /type="password"/); assert.doesNotMatch(auth, /localStorage|sessionStorage/);
  assert.match(app, /const authenticatedFetch = \(path, options\) => browserAuth \? browserAuth\.fetch/);
  assert.match(app, /request\(apiPath\(path\)/);
  assert.match(app, /authenticatedSocketURL\(path\)/);
  assert.match(app, /authenticatedSocketURL\('\/ws\/federation\/presence'\)/);
  assert.match(app, /swarm-auth-unauthorized/);
});


test('presence socket authenticates only to local gateway, not a peer named presence', async () => {
  const t = fixture(call => {
    if (call.url === '/api/auth/status') return response(200,{required:false});
    throw Error(`unexpected ${call.url}`);
  });
  assert.equal(await t.client.socketURL('/ws/federation/presence'), 'wss://swarm.test/ws/federation/presence');
  assert.deepEqual(t.calls.map(c => c.url), ['/api/auth/status']);
});

test('a password enabled after reconnect invalidates cached no-auth status', async () => {
  let enabled=false, attempts=0, prompts=0;
  const t = fixture(call => {
    if (call.url === '/api/auth/status') return response(200,{required:enabled});
    if (call.url === '/api/auth/login') return response(200,{token:'new-token'});
    attempts++;
    if (enabled && !call.headers.authorization) return response(401,{detail:'Authentication required'});
    return response(200,{agents:[]});
  }, {prompt:async () => {prompts++;return 'password';}});
  await t.client.fetch('/api/state'); enabled=true;
  assert.equal((await t.client.fetch('/api/state')).status,200);
  assert.equal(prompts,1);assert.equal(attempts,3);
  assert.equal(t.calls.filter(c => c.url === '/api/auth/status').length,2);
});

test('gateway expiry during remote access reauthenticates local without dropping remote token', async () => {
  let gatewayExpired=true;const prompts=[];
  const t = fixture(call => {
    if (call.url === '/api/auth/status') return response(200,{required:true});
    if (call.url === '/api/auth/login') {gatewayExpired=false;return response(200,{token:'new-local'});}
    if (gatewayExpired) return {...response(401,{detail:'Authentication required'}),headers:{get:()=>'local'}};
    assert.equal(call.headers.authorization,'Bearer new-local');
    assert.equal(call.headers['x-swarm-authorization'],'Bearer existing-remote');
    return response(200,{agents:[]});
  },{prompt:async details=>{prompts.push(details.node);return 'localpassword';}});
  t.client.required.set('local',true);t.client.tokens.set('local','old-local');
  t.client.required.set('beta',true);t.client.tokens.set('beta','existing-remote');
  assert.equal((await t.client.fetch('/api/federation/beta/proxy/api/state')).status,200);
  assert.deepEqual(prompts,['local']);
  assert.equal(t.client.tokens.get('beta'),'existing-remote');
});

test('removing password protection does not trap login in a prompt loop', async () => {
  let prompts=0;
  const t=fixture(call=>call.url==='/api/auth/login'?response(200,{required:false,token:null}):response(200,{agents:[]}),
    {prompt:async()=>{prompts++;return 'old password';}});
  t.client.required.set('local',true);
  assert.equal((await t.client.fetch('/api/state')).status,200);
  assert.equal(prompts,1);assert.equal(t.client.required.get('local'),false);
});
