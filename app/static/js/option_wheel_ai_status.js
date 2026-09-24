/* Read-only polling for an explicitly requested AI explanation. */
(() => {
  const panel = document.querySelector('.wheel-results-panel[data-ai-status="pending"]');
  if (!panel) return;
  const url = panel.dataset.aiStatusUrl;
  if (!/^\/option-wheel\/jobs\/[0-9a-f-]+\/status\/$/.test(url || '')) return;
  let attempts = 0;
  async function check() {
    if (++attempts > 30) return;
    try {
      const response = await fetch(url, {credentials: 'same-origin', headers: {Accept: 'application/json'}});
      if (response.ok && !response.redirected) {
        const state = await response.json();
        if (state.kind === 'option-wheel-job-v1' && state.ai_status && state.ai_status !== 'pending') {
          window.location.reload();
          return;
        }
      }
    } catch (_) { /* A temporary read failure never resubmits the analysis. */ }
    window.setTimeout(check, 5000);
  }
  window.setTimeout(check, 5000);
})();
