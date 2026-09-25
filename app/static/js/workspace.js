(function () {
  'use strict';
  var root = document.documentElement, reduced = matchMedia('(prefers-reduced-motion: reduce)'), requestedMotion = true;
  try { requestedMotion = localStorage.getItem('workbench-motion') !== 'off'; } catch (_) {}
  var motion = document.getElementById('ws-motion');
  function save(key, value) { try { localStorage.setItem(key, value); } catch (_) {} }
  function sync() {
    root.dataset.motion = requestedMotion && !reduced.matches ? 'on' : 'off';
    document.querySelectorAll('[data-ws-palette]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.wsPalette === root.dataset.palette)); });
    if (motion) { motion.checked = requestedMotion && !reduced.matches; motion.disabled = reduced.matches; }
    document.getElementById('ws-motion-description').textContent = reduced.matches ? '已遵循系统设置关闭动效' : '轻入场、悬停反馈与图表显现';
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest('button'); if (!b) return;
    if (b.dataset.wsOpen) document.getElementById(b.dataset.wsOpen).showModal();
    if (b.hasAttribute('data-ws-close')) b.closest('dialog').close();
    if (b.dataset.wsPalette) { root.dataset.palette = b.dataset.wsPalette; save('workbench-palette', b.dataset.wsPalette); sync(); }
    if (b.hasAttribute('data-ws-menu')) { var open = document.body.classList.toggle('ws-menu-open'); b.setAttribute('aria-expanded', String(open)); b.setAttribute('aria-label', open ? '收起所有模块' : '展开所有模块'); }
  });
  document.querySelectorAll('#ws-appearance, #ws-launcher').forEach(function (dialog) { dialog.addEventListener('click', function (e) { if (e.target === dialog) dialog.close(); }); });
  motion.addEventListener('change', function () { requestedMotion = motion.checked; save('workbench-motion', requestedMotion ? 'on' : 'off'); sync(); });
  reduced.addEventListener('change', sync);
  var query = document.getElementById('ws-module-query');
  query.addEventListener('input', function () {
    var count = 0, value = query.value.trim().toLowerCase();
    document.querySelectorAll('[data-ws-search]').forEach(function (link) { link.hidden = !link.dataset.wsSearch.toLowerCase().includes(value); if (!link.hidden) count++; });
    document.getElementById('ws-module-empty').hidden = count > 0;
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { document.body.classList.remove('ws-menu-open'); var toggle = document.querySelector('[data-ws-menu]'); if (toggle) { toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-label', '展开所有模块'); } }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && document.querySelector('[data-ws-open="ws-launcher"]') && !document.querySelector('dialog[open]')) { e.preventDefault(); document.getElementById('ws-launcher').showModal(); query.focus(); }
  });
  sync();
})();
