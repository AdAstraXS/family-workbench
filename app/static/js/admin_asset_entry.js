(function () {
  function getJQuery() {
    return window.django && window.django.jQuery ? window.django.jQuery : window.jQuery;
  }

  function cssEscape(value) {
    if (window.CSS && window.CSS.escape) {
      return window.CSS.escape(value);
    }
    return String(value).replace(/"/g, '\\"');
  }

  function rememberOptions(accountSelect) {
    if (accountSelect.dataset.optionsReady === "1") {
      return;
    }
    accountSelect._allAccountOptions = Array.from(accountSelect.options).map(function (option) {
      return {
        value: option.value,
        text: option.text,
        memberId: option.dataset.memberId || "",
      };
    });
    accountSelect.dataset.optionsReady = "1";
  }

  function filterRow(row) {
    var memberSelect = row.querySelector('select[name$="-member"], select[name="member"]');
    var accountSelect = row.querySelector('select[name$="-account"], select[name="account"]');
    if (!memberSelect || !accountSelect) {
      return;
    }

    rememberOptions(accountSelect);

    var memberId = memberSelect.value;
    var selectedValue = accountSelect.value;
    var selectedOption = accountSelect.querySelector('option[value="' + cssEscape(selectedValue) + '"]');
    var selectedMemberId = selectedOption ? selectedOption.dataset.memberId : "";

    accountSelect.innerHTML = "";
    accountSelect._allAccountOptions.forEach(function (item) {
      if (item.value && memberId && item.memberId !== memberId) {
        return;
      }
      var option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.text;
      if (item.memberId) {
        option.dataset.memberId = item.memberId;
      }
      accountSelect.appendChild(option);
    });

    if (selectedValue && (!memberId || selectedMemberId === memberId)) {
      accountSelect.value = selectedValue;
    } else {
      accountSelect.value = "";
    }
  }

  function bindRow(row) {
    if (!row || row.dataset.accountFilterReady === "1") {
      return;
    }
    row.dataset.accountFilterReady = "1";
    filterRow(row);
    var memberSelect = row.querySelector('select[name$="-member"], select[name="member"]');
    if (memberSelect) {
      memberSelect.addEventListener("change", function () {
        filterRow(row);
      });
    }
  }

  function bindAll() {
    document.querySelectorAll(".dynamic-entries, .form-row, .field-member").forEach(bindRow);
    document.querySelectorAll('select[name="member"]').forEach(function (select) {
      bindRow(select.closest("form") || document);
    });
  }

  document.addEventListener("DOMContentLoaded", bindAll);
  document.addEventListener("formset:added", function (event) {
    bindRow(event.target);
  });

  var $ = getJQuery();
  if ($) {
    $(document).on("formset:added", function (event, row) {
      bindRow(row instanceof Element ? row : event.target);
    });
  }
})();
