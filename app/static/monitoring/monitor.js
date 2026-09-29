(() => {
  const root = document.getElementById('runtime-monitor');
  if (!root) return;

  const status = document.createElement('span');
  status.className = 'mon-sr-only';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  root.append(status);
  let pending;

  function updateNavigation(next) {
    const currentPeriods = root.querySelectorAll('.mon-period a');
    const nextPeriods = next.querySelectorAll('.mon-period a');
    currentPeriods.forEach((link, index) => {
      link.href = nextPeriods[index].href;
      if (nextPeriods[index].hasAttribute('aria-current')) {
        link.setAttribute('aria-current', 'page');
      } else {
        link.removeAttribute('aria-current');
      }
    });
    root.querySelector('#vendor').value = next.querySelector('#vendor').value;
    root.querySelector('.mon-filter input[name="period"]').value =
      next.querySelector('.mon-filter input[name="period"]').value;
  }

  function updateCoverage(next) {
    const current = root.querySelector('.mon-coverage');
    const replacement = next.querySelector('.mon-coverage');
    if (current && replacement) current.replaceWith(replacement);
    else if (current) current.remove();
    else if (replacement) root.querySelector('.mon-stats').after(replacement);
  }

  async function show(url, { push = true, focusVendor = null } = {}) {
    const target = new URL(url, location.href);
    if (target.pathname !== location.pathname) {
      location.assign(target.href);
      return;
    }
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    const scrollTop = window.scrollY;
    root.querySelector('.mon-columns').setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(target.href, {
        credentials: 'same-origin',
        signal: controller.signal,
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      });
      if (!response.ok || new URL(response.url).pathname !== target.pathname) {
        throw new Error('Monitoring page unavailable');
      }
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const next = page.getElementById('runtime-monitor');
      if (!next || !next.querySelector('.mon-columns')) throw new Error('Monitoring content missing');
      for (const selector of ['.mon-stats', '.mon-accounts', '.mon-columns']) {
        root.querySelector(selector).replaceWith(next.querySelector(selector));
      }
      updateCoverage(next);
      const records = root.querySelector('.mon-records');
      const wasOpen = records.open;
      records.replaceWith(next.querySelector('.mon-records'));
      root.querySelector('.mon-records').open = wasOpen;
      updateNavigation(next);
      if (push) history.pushState({ monitorFilter: true }, '', target.pathname + target.search);
      window.scrollTo(0, scrollTop);
      if (focusVendor) {
        root.querySelector(`.mon-card-target[data-vendor="${focusVendor}"]`)?.focus({ preventScroll: true });
      }
      const selected = root.querySelector('#vendor').selectedOptions[0].textContent;
      status.textContent = `已显示${selected}的费用数据`;
    } catch (error) {
      if (error.name !== 'AbortError') location.assign(target.href);
    } finally {
      if (pending === controller) {
        pending = null;
        root.querySelector('.mon-columns')?.removeAttribute('aria-busy');
      }
    }
  }

  root.addEventListener('click', event => {
    const bar = event.target.closest('.mon-bar-hit');
    if (bar) {
      root.querySelector('#chart-detail').textContent =
        `${bar.dataset.label} · 已计价费用 ¥${bar.dataset.amount}`;
      return;
    }
    const link = event.target.closest('.mon-card-target, .mon-period a');
    if (!link || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    show(link.href, { focusVendor: link.dataset.vendor || null });
  });

  const filter = root.querySelector('.mon-filter form');
  function submitFilter(event) {
    event?.preventDefault();
    const url = new URL(filter.action);
    url.search = new URLSearchParams(new FormData(filter)).toString();
    show(url);
  }
  filter.addEventListener('submit', submitFilter);
  filter.querySelector('#vendor').addEventListener('change', submitFilter);
  window.addEventListener('popstate', () => show(location.href, { push: false }));
})();
