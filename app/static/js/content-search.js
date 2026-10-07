(() => {
  const root = document.querySelector('[data-search-state]');
  if (!root) return;
  const key = 'content-search:' + root.dataset.searchState + ':' + location.pathname + location.search;
  addEventListener('pagehide', () => {
    try { sessionStorage.setItem(key, String(scrollY)); } catch (_) {}
  });
  addEventListener('pageshow', () => {
    try {
      const saved = sessionStorage.getItem(key);
      if (saved !== null && Number.isFinite(Number(saved))) requestAnimationFrame(() => scrollTo(0, Number(saved)));
    } catch (_) {}
  });
})();
