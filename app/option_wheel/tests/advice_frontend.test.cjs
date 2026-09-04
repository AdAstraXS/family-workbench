const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/js/option_wheel_advice.js'), 'utf8');

function page(fetchImpl) {
  const scheduled = [], calls = [], status = {textContent: ''};
  let reloads = 0;
  vm.runInNewContext(source, {
    document: {querySelector: () => ({querySelector: () => status})},
    window: {location: {href: 'https://house.example/advice/?request=1', reload: () => reloads++}},
    setTimeout: (fn, ms) => {if (ms === 3000) scheduled.push(fn); return 1;},
    clearTimeout: () => {}, AbortController,
    fetch: async (url, options) => {calls.push({url, options}); return fetchImpl();},
  });
  return {scheduled, calls, status, reloads: () => reloads};
}

test('success reloads GET page, never repeats a paid submission', async () => {
  const p = page(async () => ({ok: true, json: async () => ({status: 'success'})}));
  await p.scheduled.shift()();
  assert.equal(p.reloads(), 1);
  assert.equal(p.calls.length, 1);
  assert.equal(p.calls[0].options.method, undefined);
  assert.equal(p.calls[0].options.headers.Accept, 'application/json');
  assert.equal(p.scheduled.length, 0);
});

test('network failures only retry bounded GET requests', async () => {
  const p = page(async () => {throw new Error('network');});
  for (let n = 0; n < 40; n++) await p.scheduled.shift()();
  assert.equal(p.reloads(), 0);
  assert.equal(p.scheduled.length, 0);
  assert.match(p.status.textContent, /没有重新提交/);
  assert.ok(p.calls.every(c => c.options.method === undefined && c.options.body === undefined));
});

test('pending preserves polling; failure reloads for fixed server message', async () => {
  const states = ['pending', 'failed'];
  const p = page(async () => ({ok: true, json: async () => ({status: states.shift()})}));
  await p.scheduled.shift()();
  assert.equal(p.reloads(), 0);
  await p.scheduled.shift()();
  assert.equal(p.reloads(), 1);
});
