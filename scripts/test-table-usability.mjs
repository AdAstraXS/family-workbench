import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../app/static/js/table-usability.js', import.meta.url), 'utf8');
const nodes = [];
function table(count, managed = false) {
  return {
    tBodies: [{rows: Array.from({length: count}, (_, id) => ({id, input: {value: `value-${id}`}}))}],
    closest() { return managed || this.wrapper; },
    getAttribute() { return 'Comparison'; },
    before(...items) { nodes.push(...items); },
  };
}
const tables = [table(65), table(30), table(80, true)];
const originalRows = [...tables[0].tBodies[0].rows];
let observer;
vm.runInNewContext(source, {
  document: {
    querySelectorAll: () => tables,
    querySelector: () => ({}),
    createElement: () => ({
      setAttribute(name, value) { this[name] = value; },
      appendChild(child) { child.wrapper = this; },
    }),
  },
  window: {MutationObserver: true},
  MutationObserver: class { constructor(callback) { observer = callback; } observe() {} },
  requestAnimationFrame: callback => callback(),
});
assert.equal(nodes.length, 2, 'Only the unmanaged table over 30 rows gains a region and hint');
assert.equal(tables[0].wrapper.role, 'region');
assert.equal(tables[0].wrapper.tabIndex, 0);
assert.deepEqual(tables[0].tBodies[0].rows, originalRows, 'Rows and editable inputs remain intact');
observer();
assert.equal(nodes.length, 2, 'Repeated DOM updates must not nest wrappers');
tables.push(table(31));
observer();
assert.equal(nodes.length, 4, 'Newly rendered long tables are also enhanced');
console.log('Long-table checks passed: threshold, opt-out, complete rows and inputs, repeat updates.');
