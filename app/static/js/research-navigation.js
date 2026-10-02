/* Keep the viewport stable across research links, including back/forward. */
(() => {
  const key = 'research-navigation-scroll-v1';
  const pageKey = location.pathname + location.search;
  let states;
  try { states = JSON.parse(sessionStorage.getItem(key) || '{}'); } catch (_) { states = {}; }
  const save = () => {
    states[pageKey] = {y: scrollY, open: Array.from(document.querySelectorAll('.rn-body details')).map(d => d.open)};
    try { sessionStorage.setItem(key, JSON.stringify(states)); } catch (_) {}
  };
  const visit = href => {
    const target = new URL(href, location.href);
    if (target.origin !== location.origin || !target.pathname.startsWith('/research/')) return;
    save();
    try { sessionStorage.setItem('research-navigation-next-y', String(scrollY)); } catch (_) {}
  };
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  const restore = () => {
    const state = states[pageKey];
    const back = performance.getEntriesByType('navigation')[0]?.type === 'back_forward';
    let pending;
    try { pending = sessionStorage.getItem('research-navigation-next-y'); sessionStorage.removeItem('research-navigation-next-y'); } catch (_) {}
    if (state && (back || pending === null)) document.querySelectorAll('.rn-body details').forEach((d, i) => { if (typeof state.open[i] === 'boolean') d.open = state.open[i]; });
    // Explicit source citations retain their anchor semantics.
    if (location.hash) return;
    const y = back && state ? state.y : pending !== null && pending !== undefined ? Number(pending) : state?.y;
    if (Number.isFinite(y)) requestAnimationFrame(() => scrollTo(0, y));
  };
  addEventListener('pageshow', restore);
  addEventListener('pagehide', save);
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href]');
    if (a && !e.ctrlKey && !e.metaKey && !e.shiftKey && !a.target) visit(a.href);
  });
  document.addEventListener('submit', e => visit(e.target.action || location.href));
  document.querySelector('[data-research-company]')?.addEventListener('change', e => { visit(e.target.value); location.assign(e.target.value); });
  const form = document.querySelector('#confirmation form');
  if (form) {
    const draftKey = 'research-question-draft:' + location.pathname + ':' + form.elements.report.value + ':' + form.elements.revision.value;
    const fields = Array.from(form.elements).filter(e => e.name && ['text', 'textarea', 'checkbox'].includes(e.type));
    try {
      const saved = JSON.parse(sessionStorage.getItem(draftKey) || 'null');
      if (saved) fields.forEach(e => { if (Object.hasOwn(saved, e.name)) { if (e.type === 'checkbox') e.checked = saved[e.name]; else e.value = saved[e.name]; } });
    } catch (_) {}
    form.addEventListener('input', () => {
      const saved = Object.fromEntries(fields.map(e => [e.name, e.type === 'checkbox' ? e.checked : e.value]));
      try { sessionStorage.setItem(draftKey, JSON.stringify(saved)); } catch (_) {}
    });
  }
})();
