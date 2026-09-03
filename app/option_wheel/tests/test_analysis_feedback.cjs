// node --test app/option_wheel/tests/test_analysis_feedback.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/js/option_wheel_analysis.js'), 'utf8');

function setup(fetcher, values = {account_ids: ['1'], symbols: ['TSLA']}) {
  const ids = ['form', 'feedback', 'status', 'detail', 'elapsed', 'check'];
  const nodes = Object.fromEntries(ids.map(id => [id, {hidden: true, textContent: '', attrs: {}}]));
  let listener, calls = 0, now = 0;
  const timers = new Map();
  const unload = new Set();
  const form = nodes.form;
  form.elements = [{disabled: false}, {disabled: false}];
  form.action = '/option-wheel/refresh-analysis/';
  form.reportValidity = () => true;
  form.setAttribute = (k, v) => { form.attrs[k] = v; };
  form.addEventListener = (_, fn) => { listener = fn; };
  class Clock extends Date { static now() { return now; } }
  vm.runInNewContext(source, {
    document: {getElementById: id => nodes[id.replace('wheel-analysis-', '')]},
    FormData: class { getAll(k) { return values[k] || []; } }, Date: Clock,
    fetch: async (...args) => { calls++; return fetcher(...args); }, AbortController,
    setInterval: fn => { timers.set('tick', fn); return 'tick'; },
    setTimeout: fn => { timers.set('timeout', fn); return 'timeout'; },
    clearInterval: id => timers.delete(id), clearTimeout: id => timers.delete(id),
    window: {addEventListener: (_, fn) => unload.add(fn), removeEventListener: (_, fn) => unload.delete(fn)},
  });
  return {nodes, timers, unload, calls: () => calls, advance: ms => { now = ms; timers.get('tick')(); },
    submit: () => listener({preventDefault() {}})};
}
const response = (outcome) => ({ok: true, status: 200, redirected: false,
  headers: {get: () => 'application/json'},
  json: async () => ({kind: 'option-wheel-analysis-v1', outcome, message: '<b>仅作为文本显示</b>'})});

test('waiting, elapsed time and duplicate-click lock; cleanup after success', async () => {
  let resolve;
  const app = setup(() => new Promise(r => { resolve = r; }));
  const pending = app.submit();
  assert.equal(app.nodes.form.attrs['aria-busy'], 'true');
  assert.ok(app.nodes.form.elements.every(e => e.disabled));
  app.advance(61000);
  assert.match(app.nodes.detail.textContent, /等待超过一分钟/);
  await app.submit();
  assert.equal(app.calls(), 1);
  resolve(response('saved'));
  await pending;
  assert.equal(app.nodes.status.textContent, '分析已保存');
  assert.equal(app.nodes.detail.textContent, '<b>仅作为文本显示</b>');
  assert.equal(app.nodes.check.hidden, false);
  assert.equal(app.timers.size, 0);
  assert.equal(app.unload.size, 0);
  await app.submit();
  assert.equal(app.calls(), 1);
});
test('confirmed rejection is distinct from unknown result', async () => {
  const app = setup(async () => response('not_saved'));
  await app.submit();
  assert.equal(app.nodes.status.textContent, '本次未保存分析');
});
for (const [name, fetcher] of Object.entries({
  proxy502: async () => ({ok: false, status: 502, headers: {get: () => 'text/html'}}),
  html200: async () => ({ok: true, status: 200, headers: {get: () => 'text/html'}}),
  network: async () => { throw new Error('offline'); },
  loginRedirect: async () => ({redirected: true}),
  malformed: async () => ({...response('saved'), json: async () => ({outcome: 'saved'})}),
})) test(`${name}: never claim success or retry`, async () => {
  const app = setup(fetcher);
  await app.submit(); await app.submit();
  assert.match(app.nodes.status.textContent, /结果尚未确认/);
  assert.equal(app.calls(), 1);
  assert.equal(app.nodes.check.hidden, false);
});
test('client deadline stops waiting without claiming server cancellation', async () => {
  const app = setup((_, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('abort')));
  }));
  const pending = app.submit(); app.timers.get('timeout')(); await pending;
  assert.match(app.nodes.detail.textContent, /不等于取消分析/);
  assert.equal(app.timers.size, 0);
});
test('invalid selection never sends a request', async () => {
  const app = setup(() => response('saved'), {symbols: ['TSLA']});
  await app.submit(); assert.equal(app.calls(), 0);
  assert.equal(app.nodes.status.textContent, '尚未提交');
});
test('CSRF or authorization rejection is not rendered as provider HTML', async () => {
  const app = setup(async () => ({status: 403}));
  await app.submit(); assert.equal(app.nodes.status.textContent, '请求未获受理');
});
