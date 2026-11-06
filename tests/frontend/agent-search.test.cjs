const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {cardText, matches} = require('../../src/swarm/static/agent-search.js');

const agent = {
  id:'agent-id',name:'Coordinator',backend:'pi',managed:true,status:'running',alive:true,
  goal:'Build a dashboard',activity:'Checking results',folder:'/workspace',repository:'/source',
  worktree:'/tree',branch:'feature/card-search',session_id:'session-xyz',pid:9876,
  notes:'Durable checkpoint evidence',wait_reason:'Awaiting review',last_error:'A previous fault',
  activities:[{title:'Investigate selector regression',status:'done'}],
  product:{kind:'file',id:'product-id',label:'Design notes',filename:'architecture.pdf'},
  file_count:1,file_search:[{label:'Benchmark plot',filename:'runtime.png',media_type:'image/png'}],
  message_count:3,
};

test('full-card metadata matches independently of compact DOM and labels', () => {
  for(const query of ['coordinator','Dashboard','selector regression','architecture.pdf','DESIGN NOTES',
    'benchmark plot','runtime.png','image/png','checkpoint evidence','awaiting review','previous fault',
    '/source','/tree','feature/card-search','session-xyz','9876','PID 9876','Files 1','3 outstanding messages','manual','Process online']) {
    assert.equal(matches(agent,query),true,query);
  }
  assert.equal(matches(agent,'Parent agent',{parentName:'Parent agent'}),true);
  assert.equal(matches(agent,'Research folder',{groupName:'Research folder'}),true);
  assert.equal(matches(agent,'no such metadata'),false);
  assert.equal(matches(agent,'   '),true);
  assert.equal(matches(null,'anything'),false);
});

test('link products, setup/restart errors, imported details and wake state are searchable', () => {
  const value={...agent,product:{kind:'link',label:'Pull request 321',url:'https://example.com/team/pr/321'},
    spawn_state:'provisioning',creation:{state:'running',stage:'Creating worktree',error:'Fetch failed',log:'/setup/log'},
    restart_job_id:'job',restart:{state:'running',stage:'Waiting for idle',error:'Blocked tool'},
    import_source:{backend:'grok',session_id:'foreign-session',session_file:'/import/history.json'},
    wake_at:'2030-01-01T00:00:00Z',wake_delivery:{state:'pending',id:'event-unique',retry_at:'2030-01-02',attempts:2}};
  for(const query of ['pull request 321','team/pr/321','creating worktree','fetch failed','/setup/log',
    'waiting for idle','blocked tool','foreign-session','/import/history.json','event-unique','paused','2030-01-02']) {
    assert.equal(matches(value,query),true,query);
  }
});

test('safe explicit fields exclude originals, hidden secrets, nested receipts and message bodies', () => {
  const value={...agent,api_key:'secret-never-indexed',private_metadata:{text:'unknown-field'},
    messages:[{text:'mailbox-body'}],session_file:'/hidden/current-session.jsonl',
    file_search:[{label:'Visible label',filename:'public.txt',media_type:'text/plain',path:'/private/original-location',sha256:'hidden-hash'}]};
  for(const query of ['secret-never-indexed','unknown-field','mailbox-body','/hidden/current-session','/private/original-location','hidden-hash']) {
    assert.equal(matches(value,query),false,query);
  }
  assert.equal(matches(value,'public.txt'),true);
  for(const url of ['javascript:alert(1)','https://username:password@example.com/hidden','not a URL']) {
    assert.equal(matches({...value,product:{kind:'link',label:'invalid-product-label',url}},'invalid-product-label'),false);
  }
});

test('Unicode canonical forms, literal markup and absent metadata are handled safely', () => {
  const value={name:'Café <script>literal</script>',activities:[null,{}, {title:'Résumé'}],file_search:[null,{}]};
  assert.equal(matches(value,'CAFE\u0301'),true);
  assert.equal(matches(value,'re\u0301sume\u0301'),true);
  assert.equal(matches(value,'<script>literal'),true);
  assert.equal(matches({name:'Straße'},'STRASSE'),true);
  assert.equal(matches({name:'Σίσυφος'},'ΣΊΣΥΦΟΣ'),true);
  assert.doesNotThrow(()=>cardText({}));
});

test('live metadata replacement/removal changes matching without stale search cache', () => {
  const value={...agent,product:{kind:'file',id:'first',label:'Old output',filename:'first.txt'}};
  assert.equal(matches(value,'old output'),true);
  value.product={kind:'file',id:'second',label:'New output',filename:'second.txt'};
  assert.equal(matches(value,'old output'),false);assert.equal(matches(value,'second.txt'),true);
  value.file_search=[];assert.equal(matches(value,'runtime.png'),false);
});

test('dashboard root/group/folder filtering shares one predicate and no file-fetch path', () => {
  const app=fs.readFileSync(path.join(__dirname,'../../src/swarm/static/app.js'),'utf8');
  assert.ok(app.includes('SwarmAgentSearch.cardText(agent,'));
  assert.ok(app.includes('scopedAgents.filter(agent=>matchesCard.has(agent.id))'));
  assert.ok(app.includes('renderAgentFolders(matchesCard, !needle && dashboardControls.status.all)'));
  assert.ok(app.includes('(members.length || !includeEmpty)'));
  const html=fs.readFileSync(path.join(__dirname,'../../src/swarm/static/index.html'),'utf8');
  assert.ok(html.indexOf('/static/agent-search.js')<html.indexOf('/static/app.js'));
  const module=fs.readFileSync(path.join(__dirname,'../../src/swarm/static/agent-search.js'),'utf8');
  assert.doesNotMatch(module,/fetch\(|innerHTML|textContent|localStorage/);
});
