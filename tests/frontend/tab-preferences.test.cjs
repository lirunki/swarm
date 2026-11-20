const test=require('node:test');
const assert=require('node:assert/strict');
const {normalize,move,TabPreferences}=require('../../src/swarm/static/tab-preferences.js');

test('tab preferences are bounded, deduplicated and reject control characters',()=>{
  assert.deepEqual(normalize(null),{version:1,order:[],starred:[]});
  assert.deepEqual(normalize({version:1,order:['a','a',null,'bad\n'],starred:['agent:a','agent:a']}),{version:1,order:['a'],starred:['agent:a']});
  assert.equal(normalize({version:1,order:Array.from({length:600},(_,i)=>String(i))}).order.length,512);
});
test('moving tabs does not lose hidden entries or mutate the source',()=>{
 const original=['agent:a','agent:hidden','agent:b'];
 assert.deepEqual(move(original,'agent:a','agent:b',true),['agent:hidden','agent:b','agent:a']);
 assert.deepEqual(original,['agent:a','agent:hidden','agent:b']);
 assert.deepEqual(move(original,'missing','agent:b'),original);
});
test('stars persist independently per owner and storage failure does not fake success',()=>{
 const storage=new Map(),errors=[];
 const prefs=new TabPreferences({query:()=>null,storage:()=>({getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)}),onError:error=>errors.push(error)});
 prefs.load('local');prefs.toggle('agent:a');assert.deepEqual(prefs.value.starred,['agent:a']);
 prefs.load('remote');assert.deepEqual(prefs.value.starred,[]);prefs.toggle('agent:b');
 prefs.load('local');assert.deepEqual(prefs.value.starred,['agent:a']);
 prefs.storage=()=>({setItem(){throw Error('full');}});prefs.toggle('agent:a');
 assert.deepEqual(prefs.value.starred,['agent:a']);assert.equal(errors.length,1);
});
