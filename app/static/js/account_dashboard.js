(function () {
  "use strict";

  document.querySelectorAll("[data-account-collapse-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var rows = document.getElementById(button.dataset.accountCollapseTarget);
      if (!rows) return;
      var shouldExpand = rows.hidden;
      rows.hidden = !shouldExpand;
      button.setAttribute("aria-expanded", String(shouldExpand));
      button.textContent = shouldExpand
        ? button.dataset.expandedLabel
        : button.dataset.collapsedLabel;
    });
  });
})();
