(function () {
  'use strict';
  const originals = new WeakMap();
  function filter(primary, reset) {
    const secondary = Array.from(primary.form.elements).find(el => el.name === primary.name.replace(/asset_primary$/, 'asset_category'));
    if (!secondary) return;
    if (!originals.has(secondary)) originals.set(secondary, Array.from(secondary.options).map(el => el.cloneNode(true)));
    const current = reset ? '' : secondary.value;
    secondary.replaceChildren(...originals.get(secondary).filter(el => !el.value || el.dataset.parentId === primary.value).map(el => el.cloneNode(true)));
    secondary.value = Array.from(secondary.options).some(el => el.value === current) ? current : '';
  }
  function setup() {
    document.querySelectorAll('[data-asset-primary]').forEach(primary => {
      // Transaction form has its own server-backed category/security selector.
      if (primary.closest('[data-transaction-classification]') || primary.dataset.assetReady) return;
      primary.dataset.assetReady = '1';
      filter(primary, false);
      primary.addEventListener('change', () => filter(primary, true));
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup); else setup();
  new MutationObserver(setup).observe(document.documentElement, {childList: true, subtree: true});
})();
