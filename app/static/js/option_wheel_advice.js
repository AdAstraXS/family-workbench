// Only GET polling; refresh, timeout and network errors never resubmit paid POSTs.
(() => {
  const panel = document.querySelector('[data-ai-pending]');
  if (!panel) return;
  let attempts = 0;
  async function poll() {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(window.location.href, {
        headers: {Accept: 'application/json'}, cache: 'no-store', signal: controller.signal,
      });
      if (!response.ok) throw new Error('Status unavailable');
      const result = await response.json();
      if (['success', 'failed', 'interrupted'].includes(result.status)) {
        window.location.reload();
        return;
      }
    } catch (_) { /* Preserve task; transient network errors only retry GET. */ }
    finally { clearTimeout(timer); }
    if (++attempts < 40) setTimeout(poll, 3000);
    else panel.querySelector('[role=status]').textContent = '暂时无法确认结果，请刷新此页找回任务。没有重新提交 AI 请求。';
  }
  setTimeout(poll, 3000);
})();
