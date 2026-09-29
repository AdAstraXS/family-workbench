(() => {
  const tabs = document.querySelector('.account-tabs');
  const content = document.querySelector('.account-tab-content');
  if (!tabs || !content) return;
  let pending;

  async function show(url, { push = true } = {}) {
    const target = new URL(url, location.href);
    if (target.pathname !== location.pathname) {
      location.assign(target.href);
      return;
    }
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    const scrollTop = window.scrollY;
    content.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(target.href, {
        credentials: 'same-origin',
        signal: controller.signal,
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
      });
      if (!response.ok || new URL(response.url).pathname !== target.pathname) {
        throw new Error('Account tab unavailable');
      }
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const nextTabs = page.querySelector('.account-tabs');
      const nextContent = page.querySelector('.account-tab-content');
      if (!nextTabs || !nextContent) throw new Error('Account tab content missing');
      const nextLinks = nextTabs.querySelectorAll('a');
      tabs.querySelectorAll('a').forEach((link, index) => {
        link.href = nextLinks[index].href;
        link.className = nextLinks[index].className;
        if (nextLinks[index].hasAttribute('aria-current')) {
          link.setAttribute('aria-current', 'page');
        } else {
          link.removeAttribute('aria-current');
        }
      });
      content.innerHTML = nextContent.innerHTML;
      if (push) history.pushState({ accountTab: true }, '', target.pathname + target.search);
      window.scrollTo(0, scrollTop);
    } catch (error) {
      if (error.name !== 'AbortError') location.assign(target.href);
    } finally {
      if (pending === controller) {
        pending = null;
        content.removeAttribute('aria-busy');
      }
    }
  }

  tabs.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (!link || event.defaultPrevented || event.button !== 0 ||
        event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (link.getAttribute('aria-current') !== 'page') show(link.href);
  });

  function openTrade(row) {
    const dialog = content.querySelector('[data-trade-dialog]');
    if (!dialog) return;
    for (const key of ['title', 'logic', 'source', 'exit', 'note']) {
      dialog.querySelector(`[data-detail-${key}]`).textContent = row.dataset[key];
    }
    dialog.showModal();
  }

  content.addEventListener('click', event => {
    if (event.target.closest('[data-dialog-close]')) {
      content.querySelector('[data-trade-dialog]').close();
      return;
    }
    const row = event.target.closest('[data-trade-row]');
    if (row && !event.target.closest('a, button, form, input, select')) openTrade(row);
  });
  content.addEventListener('keydown', event => {
    if (event.target.matches('[data-trade-row]') &&
        (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      openTrade(event.target);
    }
  });
  window.addEventListener('popstate', () => show(location.href, { push: false }));
})();
