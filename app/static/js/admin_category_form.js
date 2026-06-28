(function () {
  "use strict";

  function optionData(select) {
    if (!select) {
      return [];
    }
    return Array.from(select.options).map(function (option) {
      return {
        value: option.value,
        label: option.text,
        familyId: option.dataset.familyId || "",
        parentId: option.dataset.parentId || "",
      };
    });
  }

  function replaceOptions(select, options, selectedValue) {
    if (!select) {
      return;
    }
    select.replaceChildren();
    options.forEach(function (item) {
      var option = new Option(item.label, item.value, false, item.value === selectedValue);
      option.dataset.familyId = item.familyId;
      option.dataset.parentId = item.parentId;
      select.add(option);
    });
    if (!Array.from(select.options).some(function (option) {
      return option.value === selectedValue;
    })) {
      select.value = "";
    }
  }

  function initializeCategoryForm() {
    var familySelect = document.getElementById("id_family");
    var levelSelect = document.getElementById("id_category_level");
    var primarySelect = document.getElementById("id_primary_category");
    var secondarySelect = document.getElementById("id_secondary_category");
    if (!levelSelect || !primarySelect) {
      return;
    }

    var primaryOptions = optionData(primarySelect);
    var secondaryOptions = optionData(secondarySelect);

    function familyOptions(options) {
      var familyId = familySelect ? familySelect.value : "";
      return options.filter(function (item) {
        return !item.value || !familyId || item.familyId === familyId;
      });
    }

    function filterSecondary(preserveSelection) {
      if (!secondarySelect) {
        return;
      }
      var selectedValue = preserveSelection ? secondarySelect.value : "";
      var primaryId = primarySelect.value;
      var options = familyOptions(secondaryOptions).filter(function (item) {
        return !item.value || (primaryId && item.parentId === primaryId);
      });
      replaceOptions(secondarySelect, options, selectedValue);
    }

    function applyLevelState(preserveSelection) {
      var level = levelSelect.value;
      var primaryEnabled = level === "2" || level === "3";
      var secondaryEnabled = level === "3" && Boolean(secondarySelect);

      primarySelect.disabled = !primaryEnabled;
      if (!primaryEnabled) {
        primarySelect.value = "";
      }

      if (secondarySelect) {
        secondarySelect.disabled = !secondaryEnabled;
        if (!secondaryEnabled) {
          secondarySelect.value = "";
        } else {
          filterSecondary(preserveSelection);
        }
      }
    }

    levelSelect.addEventListener("change", function () {
      applyLevelState(false);
    });
    primarySelect.addEventListener("change", function () {
      filterSecondary(false);
    });
    if (familySelect) {
      familySelect.addEventListener("change", function () {
        var selectedPrimary = primarySelect.value;
        replaceOptions(primarySelect, familyOptions(primaryOptions), selectedPrimary);
        filterSecondary(false);
        applyLevelState(false);
      });
    }

    applyLevelState(true);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeCategoryForm);
  } else {
    initializeCategoryForm();
  }
})();
