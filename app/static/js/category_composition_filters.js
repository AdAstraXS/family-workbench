(function () {
  "use strict";
  var busy = false;
  var selectors = ["#monthly-expense-category-pies", "#monthly-income-category-pies",
    "#expense-category-pie-data", "#income-category-pie-data"];

  document.addEventListener("change", async function (event) {
    var form = event.target.closest(".expense-category-pie-section .cashflow-trend-filter");
    if (!form || event.target.tagName !== "SELECT" || busy) return;
    busy = true;
    var url = new URL(form.action, window.location.href);
    url.hash = "";
    url.search = new URLSearchParams(new FormData(form)).toString();
    var controls = document.querySelectorAll(".expense-category-pie-section select");
    controls.forEach(function (control) { control.disabled = true; });
    var oldError = document.getElementById("category-filter-error");
    if (oldError) oldError.remove();
    try {
      var response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error("request failed");
      var page = new DOMParser().parseFromString(await response.text(), "text/html");
      var replacements = selectors.map(function (selector) {
        var current = document.querySelector(selector);
        var next = page.querySelector(selector);
        if (!current || !next) throw new Error("missing chart");
        return { current: current, next: next };
      });
      var x = window.scrollX;
      var y = window.scrollY;
      replacements.forEach(function (item) {
        item.current.replaceChildren.apply(item.current, Array.from(item.next.childNodes));
      });
      document.dispatchEvent(new Event("category-composition-updated"));
      window.history.replaceState(null, "", url);
      window.scrollTo({ left: x, top: y, behavior: "instant" });
    } catch (error) {
      var message = document.createElement("p");
      message.id = "category-filter-error";
      message.className = "form-errors";
      message.setAttribute("role", "alert");
      message.textContent = "筛选更新失败，请重试；若登录已过期，请刷新页面重新登录。";
      form.closest("section").appendChild(message);
    } finally {
      document.querySelectorAll(".expense-category-pie-section select").forEach(function (control) {
        control.disabled = false;
      });
      busy = false;
    }
  });
})();
