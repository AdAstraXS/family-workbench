(function () {
  "use strict";

  document.querySelectorAll("[data-page-back]").forEach(function (button) {
    button.addEventListener("click", function () {
      var fallbackUrl = button.dataset.fallbackUrl || "/";
      if (document.referrer) {
        try {
          var referrer = new URL(document.referrer);
          if (referrer.origin === window.location.origin && window.history.length > 1) {
            window.history.back();
            return;
          }
        } catch (error) {
          // Use the explicit module fallback when the referrer is invalid.
        }
      }
      window.location.assign(fallbackUrl);
    });
  });
})();
