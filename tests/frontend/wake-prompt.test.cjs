const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../../src/swarm/static/app.js'), 'utf8');

test('wake dialog documents that empty instructions submit no prompt', () => {
  const start = html.indexOf('<form id="wake-form">');
  const wakeForm = html.slice(start, html.indexOf('</form>', start));
  assert.match(wakeForm, /Leave instructions empty to wake it without submitting any prompt/);
  assert.match(wakeForm, /optional · empty means no prompt/);
});

test('wake form sends the explicit empty string and reports promptless resume', () => {
  const start = source.indexOf("$('#wake-form').addEventListener('submit'");
  const end = source.indexOf("\n$('#edit-form').addEventListener", start);
  const handler = source.slice(start, end);
  assert.match(handler, /const message = form\.elements\.message\.value/);
  assert.match(handler, /'POST', \{message\}/);
  assert.match(handler, /message \? 'Resume instructions sent\.' : 'Session resumed without a prompt\.'/);
  assert.doesNotMatch(handler, /message \|\|/);
});
