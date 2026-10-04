(function () {
  'use strict';
  // Explicitly opted-in, read-only forms. Mutation and remote acquisition forms stay manual.
  var forms = Array.from(document.querySelectorAll('form[data-auto-filter]')).filter(function (f) {
    return f.method.toLowerCase() === 'get' && new URL(f.action, location.href).origin === location.origin;
  });
  var key = 'workbench-filter-focus', navigating = false;
  window.addEventListener('pageshow', function () {
    navigating = false;
    document.querySelectorAll('.auto-filter-status').forEach(function (el) { el.textContent = '选择后自动更新'; });
  });
  forms.forEach(function (form, index) {
    var timer, composing = false;
    var controls = Array.from(form.elements);
    controls.forEach(function (control) {
      if ((control.type === 'submit' && !control.name) || control.hasAttribute('data-filter-submit')) control.hidden = true;
    });
    var status = document.createElement('small');
    status.className = 'auto-filter-status'; status.setAttribute('role', 'status');
    status.textContent = '选择后自动更新'; form.appendChild(status);
    function submit(control, button) {
      clearTimeout(timer);
      if (composing || navigating || !form.checkValidity()) return;
      var url = new URL(form.action, location.href), params = new URLSearchParams(new FormData(form));
      if (button && button.name) params.set(button.name, button.value);
      params.delete('page'); params.delete('cursor');
      url.search = params.toString();
      if (url.href === location.href) { status.textContent = '选择后自动更新'; return; }
      try {
        sessionStorage.setItem(key, JSON.stringify({path: url.pathname, search: url.search, form: index,
          name: control && control.name, start: control && control.selectionStart, end: control && control.selectionEnd,
          scroll: url.hash ? null : window.scrollY, time: Date.now()}));
      } catch (_) {}
      status.textContent = '正在更新…'; navigating = true;
      location.assign(url.href);
    }
    function schedule(control, delay) {
      clearTimeout(timer);
      if (composing) return;
      status.textContent = '即将更新…';
      timer = setTimeout(function () { submit(control); }, delay);
    }
    controls.forEach(function (control) {
      if (!control.name || !['INPUT', 'SELECT'].includes(control.tagName) || control.type === 'hidden') return;
      control.addEventListener('compositionstart', function () { composing = true; clearTimeout(timer); });
      control.addEventListener('compositionend', function () { composing = false; schedule(control, 650); });
      control.addEventListener('input', function (e) {
        if (e.isComposing || composing || ['checkbox', 'radio'].includes(control.type) || control.tagName === 'SELECT') return;
        schedule(control, control.type === 'date' ? 900 : 650);
      });
      control.addEventListener('change', function () { schedule(control, control.type === 'date' ? 900 : 250); });
      control.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.isComposing && !composing) { e.preventDefault(); submit(control); }
      });
    });
    form.addEventListener('submit', function (e) {
      e.preventDefault(); submit(document.activeElement, e.submitter);
    });
  });
  try {
    var saved = JSON.parse(sessionStorage.getItem(key)); sessionStorage.removeItem(key);
    if (saved && saved.path === location.pathname && saved.search === location.search && Date.now() - saved.time < 30000) {
      var form = forms[saved.form], control = form && Array.from(form.elements).find(function (el) { return el.name === saved.name && el.type !== 'hidden'; });
      if (control) {
        control.focus({preventScroll: true});
        if (typeof saved.start === 'number' && control.setSelectionRange) {
          try { control.setSelectionRange(saved.start, saved.end); } catch (_) {}
        }
      }
      if (saved.scroll !== null) requestAnimationFrame(function () { window.scrollTo(0, saved.scroll); });
    }
  } catch (_) {}
})();
