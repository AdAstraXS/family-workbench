(function () {
  'use strict';
  // Comparison grids and editable forms keep their complete rows and controls.
  // A local scroll region bounds their height without altering filters or data.
  var nextId = 0;
  function enhance() { document.querySelectorAll('main table').forEach(function (table) {
    var count = Array.from(table.tBodies).reduce(function (n, body) { return n + body.rows.length; }, 0);
    if (count <= 30 || table.closest('[data-table-scroll-managed], .long-table-scroll, .knowledge-rich-content, #reading-surface')) return;
    var region = document.createElement('div');
    region.className = 'long-table-scroll';
    region.tabIndex = 0;
    region.setAttribute('role', 'region');
    var caption = table.caption && table.caption.textContent.trim();
    region.setAttribute('aria-label', (caption || table.getAttribute('aria-label') || '明细表格') + '，可滚动查看');
    var hint = document.createElement('p');
    hint.className = 'table-scroll-hint';
    hint.id = 'table-scroll-hint-' + nextId++;
    hint.textContent = '表格可在下方区域内滚动，表头保持可见。';
    region.setAttribute('aria-describedby', hint.id);
    table.before(hint, region);
    region.appendChild(table);
  }); }
  enhance();
  var main = document.querySelector('main'), pending = false;
  if (main && window.MutationObserver) new MutationObserver(function () {
    if (pending) return;
    pending = true;
    requestAnimationFrame(function () { pending = false; enhance(); });
  }).observe(main, {childList: true, subtree: true});
})();
