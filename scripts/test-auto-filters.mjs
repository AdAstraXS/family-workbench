import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(new URL('../app/static/js/auto-filters.js', import.meta.url), 'utf8');

function fixture(method = 'get') {
  const timers = new Map(), navigations = [], windows = {};
  function control(name, value, type = 'text', tagName = 'INPUT') {
    return { name, value, type, tagName, listeners: {}, hidden: false,
      hasAttribute: () => false, addEventListener(event, callback) { this.listeners[event] = callback; },
      emit(event, extra = {}) { this.listeners[event]?.({preventDefault() {}, ...extra}); } };
  }
  const query = control('q', ''), market = control('market', 'HK', 'select-one', 'SELECT');
  const page = control('page', '3', 'hidden'), cursor = control('cursor', 'old', 'hidden');
  const category = control('category', 'old', 'hidden'), submit = control('', '', 'submit', 'BUTTON');
  const tablePage = control('table_page_expenses', '4', 'hidden');
  const fields = [query, market, page, cursor, category, submit, tablePage];
  const form = {method, action: 'https://test.invalid/list/#results', elements: fields,
    valid: true, checkValidity() {return this.valid;}, appendChild() {},
    addEventListener(name, callback) {this[name] = callback;}};
  const location = {href: 'https://test.invalid/list/?page=3', origin: 'https://test.invalid',
    pathname: '/list/', search: '?page=3', assign(url) {navigations.push(new URL(url));}};
  let serial = 0;
  const sandbox = {URL, URLSearchParams, Date, location,
    FormData: class {constructor() {return fields.filter(c=>c.name && c.type!=='submit').map(c=>[c.name,c.value]);}},
    document: {querySelectorAll: selector => selector.startsWith('form') ? [form] : [],
      createElement: () => ({setAttribute(){}}), activeElement: query},
    window: {scrollY: 200, addEventListener(name, callback) {windows[name] = callback;}},
    sessionStorage: {getItem: () => null, setItem(){}, removeItem(){}},
    requestAnimationFrame() {}, setTimeout(fn) {timers.set(++serial, fn);return serial;}, clearTimeout(id) {timers.delete(id);}};
  vm.runInNewContext(source, sandbox);
  return {query, market, category, submit, form, navigations, windows,
    flush() {const all=[...timers.values()];timers.clear();all.forEach(fn=>fn());}};
}

{
  const f = fixture();
  f.query.emit('compositionstart');
  f.query.value='中文';
  f.query.emit('input', {isComposing: true});
  f.flush();
  assert.equal(f.navigations.length, 0, 'IME must not navigate mid-composition');
  f.query.emit('compositionend');
  f.flush();
  assert.equal(f.navigations[0].searchParams.get('q'), '中文');
  assert.equal(f.navigations[0].searchParams.has('page'), false);
  assert.equal(f.navigations[0].searchParams.has('cursor'), false);
  assert.equal(f.navigations[0].searchParams.has('table_page_expenses'), false);
  assert.equal(f.navigations[0].hash, '#results');
  assert.equal(f.submit.hidden, true);
  f.windows.pageshow();
  f.market.value='US';
  f.market.emit('change');
  f.flush();
  assert.equal(f.navigations[1].searchParams.get('market'), 'US', 'back/forward restored form must remain active');
}
{
  const f=fixture();
  f.form.submit({preventDefault(){}, submitter:{name:'category',value:'商品'}});
  assert.deepEqual(f.navigations[0].searchParams.getAll('category'), ['商品']);
}
{
  const f=fixture();
  f.form.valid=false;
  f.market.emit('change');
  f.flush();
  assert.equal(f.navigations.length,0);
}
{
  const f=fixture('post');
  f.query.emit('input');
  f.flush();
  assert.equal(f.navigations.length,0);
  assert.equal(f.submit.hidden,false,'mutation forms must stay manual');
}
console.log('Auto-filter regressions passed: IME, debounce, pagination reset, named category, back navigation, validity, POST exclusion.');
