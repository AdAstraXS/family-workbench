/* Durable live jobs. Only status GETs retry; never repeat an uncertain POST. */
(() => {
  const form = document.getElementById('wheel-analysis-form');
  const page = document.getElementById('wheel-job-page');
  if (!form && !page) return;
  const feedback = document.getElementById('wheel-analysis-feedback');
  const status = document.getElementById('wheel-analysis-status');
  const detail = document.getElementById('wheel-analysis-detail');
  const elapsed = document.getElementById('wheel-analysis-elapsed');
  const check = document.getElementById('wheel-analysis-check');
  const labels = {queued: '等待启动', running: '分析与订阅清理中', saved: '分析已保存', failed: '本次未保存分析', interrupted: '任务超时或中断，需核对'};
  let submitted = false;
  let attempts = 0;
  const safePath = (url) => typeof url === 'string' && /^\/option-wheel\/(jobs\/[0-9a-f-]+\/(status\/)?|decisions\/[0-9]+\/)$/.test(url);
  async function request(url, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {...options, credentials: 'same-origin', headers: {Accept: 'application/json'}, signal: controller.signal});
      if (response.redirected || !response.ok || !response.headers.get('content-type')?.includes('application/json')) throw new Error('unconfirmed');
      const job = await response.json();
      if (job.kind !== 'option-wheel-job-v1' || !labels[job.status] || !safePath(job.status_url) || !safePath(job.detail_url)) throw new Error('invalid');
      return job;
    } finally { clearTimeout(timer); }
  }
  function display(job) {
    feedback.hidden = false;
    status.textContent = labels[job.status];
    detail.textContent = job.message;
    elapsed.textContent = `任务 ${job.id} · 提交时间 ${new Date(job.created_at).toLocaleString()}`;
    let results = document.getElementById('wheel-job-results');
    if (!results) { results = document.createElement('div'); results.id = 'wheel-job-results'; feedback.appendChild(results); }
    results.replaceChildren();
    const links = [{url: job.detail_url, label: '打开任务记录（离开页面后可找回）'}, ...(job.results || []).map(r => ({url: r.url, label: `查看分析 #${r.id}`}))];
    for (const item of links) {
      if (!safePath(item.url)) continue;
      const p = document.createElement('p'); const a = document.createElement('a');
      a.href = item.url; a.textContent = item.label; p.appendChild(a); results.appendChild(p);
    }
    check.hidden = false;
    if (form) form.setAttribute('aria-busy', ['queued', 'running'].includes(job.status) ? 'true' : 'false');
    return ['queued', 'running'].includes(job.status);
  }
  async function poll(url) {
    if (!safePath(url)) return;
    attempts++;
    let again = true;
    try { again = display(await request(url)); }
    catch (_) {
      feedback.hidden = false;
      status.textContent = '暂时无法读取任务状态';
      detail.textContent = '后台任务不会因此取消。仅重试读取状态，不会重复提交分析；也可稍后重新打开任务记录。';
      check.hidden = false;
    }
    if (again && attempts < 100) setTimeout(() => poll(url), 3000);
    else if (again) detail.textContent = '本页暂停自动查询，请重新打开任务记录核对结果；不会重新提交分析。';
  }
  if (page) poll(page.getAttribute('data-status-url'));
  if (form) form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitted || !form.reportValidity()) return;
    const body = new FormData(form);
    feedback.hidden = false;
    if (!body.getAll('account_ids').length || !body.getAll('symbols').length) {
      status.textContent = '尚未提交'; detail.textContent = '请至少选择一个账户和一个标的。'; return;
    }
    submitted = true;
    for (const control of form.elements) control.disabled = true;
    form.setAttribute('aria-busy', 'true');
    status.textContent = '正在提交任务'; detail.textContent = '仅保存分析证据，不连接交易账户、不下单。';
    try {
      const job = await request(form.action, {method: 'POST', body});
      if (display(job)) setTimeout(() => poll(job.status_url), 1500);
    } catch (_) {
      status.textContent = '任务受理结果尚未确认';
      detail.textContent = '请重新读取首页的最近分析任务，按标的和提交时间核对；系统不会自动重复提交。';
      form.setAttribute('aria-busy', 'false'); check.hidden = false;
      setTimeout(() => poll(form.getAttribute('data-status-url')), 1500);
    }
  });
})();
