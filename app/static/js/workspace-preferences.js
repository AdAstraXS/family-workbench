// Read preferences before first paint; storage may be blocked in private browsers.
(function () {
  'use strict';
  var palette = 'sky', motion = 'on';
  try { palette = localStorage.getItem('workbench-palette') || palette; motion = localStorage.getItem('workbench-motion') || motion; } catch (_) {}
  document.documentElement.dataset.palette = ['forest', 'sky', 'warm', 'white'].includes(palette) ? palette : 'sky';
  document.documentElement.dataset.motion = motion === 'off' || matchMedia('(prefers-reduced-motion: reduce)').matches ? 'off' : 'on';
})();
