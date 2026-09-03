/* Keep slow analysis requests on this page; never retry an uncertain POST. */
(() => {
  const form = document.getElementById('wheel-analysis-form');
  if (!form) return;
  const feedback = document.getElementById('wheel-analysis-feedback');
  const status = document.getElementById('wheel-analysis-status');
  const detail = document.getElementById('wheel-analysis-detail');
  const elapsed = document.getElementById('wheel-analysis-elapsed');
  const check = document.getElementById('wheel-analysis-check');
  const closeMode = form.getAttribute('data-analysis-mode') === 'close';
  let submitted = false;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (submitted || !form.reportValidity()) return;
    const body = new FormData(form);
    feedback.hidden = false;
    if ((!closeMode && !body.getAll('account_ids').length) || !body.getAll('symbols').length) {
      status.textContent = '尚未提交';
      detail.textContent = closeMode ? '请选择一只标的。' : '请至少选择一个账户和一个标的。';
      return;
    }
    submitted = true;
    for (const control of form.elements) control.disabled = true;
    form.setAttribute('aria-busy', 'true');
    status.textContent = '正在等待分析结果';
    detail.textContent = '请求已发起，仅分析、不下单。正在等待服务端返回，不代表具体处理进度；请勿重复提交。';
    const started = Date.now();
    const updateElapsed = () => {
      const seconds = Math.floor((Date.now() - started) / 1000);
      elapsed.textContent = `已等待 ${seconds} 秒 · 发起时间 ${new Date(started).toLocaleTimeString()}`;
      if (seconds >= 60) detail.textContent = closeMode
        ? '等待超过一分钟。历史行情查询可能仍在进行；本模式不新增实时订阅，请勿重复提交。'
        : '等待超过一分钟。行情查询及临时订阅清理可能仍在进行，请勿重复提交；系统不会自动重试。';
    };
    updateElapsed();
    const ticker = setInterval(updateElapsed, 1000);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 150000);
    const warnLeaving = (event) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warnLeaving);
    try {
      const response = await fetch(form.action, {
        method: 'POST', body, credentials: 'same-origin',
        headers: {Accept: 'application/json'}, signal: controller.signal,
      });
      if (response.redirected) throw new Error('unconfirmed');
      if ([400, 403, 405].includes(response.status)) {
        status.textContent = '请求未获受理';
        detail.textContent = '请重新打开页面，检查登录、权限、确认勾选及账户—标的配置后再操作。';
        return;
      }
      if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('unconfirmed');
      }
      const result = await response.json();
      if (result.kind !== 'option-wheel-analysis-v1' ||
          !['saved', 'not_saved'].includes(result.outcome) || typeof result.message !== 'string') {
        throw new Error('unconfirmed');
      }
      status.textContent = result.outcome === 'saved' ? '分析已保存' : '本次未保存分析';
      detail.textContent = result.message;
    } catch (_) {
      status.textContent = '结果尚未确认，请勿立即重试';
      detail.textContent = '连接中断、响应异常或等待超时，页面未取得服务端的最终确认。后台可能仍在处理或已保存结果；停止页面等待不等于取消分析。请稍后读取最近记录，按发起时间、账户及标的核对；仍无法确认时请先检查后台日志。';
    } finally {
      clearInterval(ticker);
      clearTimeout(timeout);
      window.removeEventListener('beforeunload', warnLeaving);
      form.setAttribute('aria-busy', 'false');
      check.hidden = false;
      // Keep the submitted form locked even on error. A fresh GET is safe;
      // automatically repeating the POST is not.
    }
  });
})();
