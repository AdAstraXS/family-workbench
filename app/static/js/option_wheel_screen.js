(() => {
  const select = document.getElementById('wheel-expiry-choice');
  const custom = document.getElementById('wheel-custom-date');
  if (!select || !custom) return;
  const sync = () => {
    custom.hidden = select.value !== 'custom';
    custom.querySelector('input').required = select.value === 'custom';
  };
  select.addEventListener('change', sync);
  sync();
})();
