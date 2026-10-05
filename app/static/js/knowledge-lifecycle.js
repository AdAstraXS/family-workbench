(() => {
  document.querySelectorAll('[data-version-cleanup]').forEach((form) => {
    const choices = Array.from(form.querySelectorAll('input[name="revision_ids"]:not(:disabled)'));
    const summary = form.querySelector('[data-version-selection-summary]');
    const submit = form.querySelector('[data-cleanup-submit]');
    const update = () => {
      const selected = choices.filter((choice) => choice.checked);
      summary.textContent = selected.length
        ? `已选择 ${selected.length} 个旧版本：${selected.map((choice) => `v${choice.dataset.versionNumber}`).join('、')}。未选择的版本保留。`
        : '尚未选择旧版本，不会清理任何内容。';
      submit.disabled = selected.length === 0;
    };
    form.addEventListener('change', update);
    window.addEventListener('pageshow', update);
    update();
  });
})();
