(function () {
  "use strict";

  var source = document.getElementById("expense-category-pie-data");
  if (!source) {
    return;
  }
  var data = JSON.parse(source.textContent);
  var unit = data.unit || "万元";
  var svgNamespace = "http://www.w3.org/2000/svg";
  var palette = [
    "#2563eb", "#0f9f8f", "#f59e0b", "#8b5cf6", "#e76f51",
    "#0891b2", "#84cc16", "#ec4899", "#64748b", "#14b8a6",
    "#f97316", "#6366f1",
  ];
  var state = {
    primaryId: null,
    secondaryId: null,
    tertiaryId: null,
  };
  var charts = {
    primary: {
      svg: document.getElementById("primary-expense-pie"),
      legend: document.getElementById("primary-expense-pie-legend"),
      empty: document.getElementById("primary-expense-pie-empty"),
      scope: document.getElementById("primary-pie-scope"),
    },
    secondary: {
      svg: document.getElementById("secondary-expense-pie"),
      legend: document.getElementById("secondary-expense-pie-legend"),
      empty: document.getElementById("secondary-expense-pie-empty"),
      scope: document.getElementById("secondary-pie-scope"),
    },
    tertiary: {
      svg: document.getElementById("tertiary-expense-pie"),
      legend: document.getElementById("tertiary-expense-pie-legend"),
      empty: document.getElementById("tertiary-expense-pie-empty"),
      scope: document.getElementById("tertiary-pie-scope"),
    },
  };

  function svgElement(name, attributes, text) {
    var element = document.createElementNS(svgNamespace, name);
    Object.keys(attributes || {}).forEach(function (key) {
      element.setAttribute(key, attributes[key]);
    });
    if (text !== undefined) {
      element.textContent = text;
    }
    return element;
  }

  function polarPoint(centerX, centerY, radius, angle) {
    var radians = (angle - 90) * Math.PI / 180;
    return {
      x: centerX + radius * Math.cos(radians),
      y: centerY + radius * Math.sin(radians),
    };
  }

  function donutPath(startAngle, endAngle) {
    var centerX = 160;
    var centerY = 124;
    var outerRadius = 92;
    var innerRadius = 50;
    var startOuter = polarPoint(centerX, centerY, outerRadius, startAngle);
    var endOuter = polarPoint(centerX, centerY, outerRadius, endAngle);
    var startInner = polarPoint(centerX, centerY, innerRadius, endAngle);
    var endInner = polarPoint(centerX, centerY, innerRadius, startAngle);
    var largeArc = endAngle - startAngle > 180 ? 1 : 0;
    return [
      "M", startOuter.x, startOuter.y,
      "A", outerRadius, outerRadius, 0, largeArc, 1, endOuter.x, endOuter.y,
      "L", startInner.x, startInner.y,
      "A", innerRadius, innerRadius, 0, largeArc, 0, endInner.x, endInner.y,
      "Z",
    ].join(" ");
  }

  function formatValue(value) {
    return Number(value).toFixed(unit === "元" ? 0 : 2);
  }

  function selectedIdFor(level) {
    if (level === "primary") {
      return state.primaryId;
    }
    if (level === "secondary") {
      return state.secondaryId;
    }
    return state.tertiaryId;
  }

  function chooseItem(level, item) {
    if (level === "primary") {
      state.primaryId = state.primaryId === item.id ? null : item.id;
      state.secondaryId = null;
      state.tertiaryId = null;
      renderAll();
    } else if (level === "secondary") {
      state.secondaryId = state.secondaryId === item.id ? null : item.id;
      state.tertiaryId = null;
      renderSecondaryAndTertiary();
    } else {
      state.tertiaryId = state.tertiaryId === item.id ? null : item.id;
      renderPie("tertiary", filteredTertiary());
    }
  }

  function renderPie(level, items) {
    var chart = charts[level];
    if (!chart.svg || !chart.legend || !chart.empty) {
      return;
    }
    chart.svg.replaceChildren();
    chart.legend.replaceChildren();
    var positiveItems = items.filter(function (item) {
      return Number(item.value) > 0;
    });
    if (!positiveItems.length) {
      chart.svg.hidden = true;
      chart.empty.hidden = false;
      return;
    }
    chart.svg.hidden = false;
    chart.empty.hidden = true;

    var total = positiveItems.reduce(function (sum, item) {
      return sum + Number(item.value);
    }, 0);
    var selectedId = selectedIdFor(level);
    var angle = 0;
    positiveItems.forEach(function (item, index) {
      var portion = Number(item.value) / total;
      var nextAngle = index === positiveItems.length - 1 ? 359.999 : angle + portion * 360;
      var color = palette[index % palette.length];
      var selected = selectedId === item.id;
      var muted = selectedId !== null && !selected;
      var segment = svgElement("path", {
        d: donutPath(angle, nextAngle),
        fill: color,
        class: "expense-pie-segment" + (selected ? " selected" : "") + (muted ? " muted" : ""),
        style: "--segment-delay:" + index * 55 + "ms",
        tabindex: "0",
        role: "button",
        "aria-label": item.name + "，" + formatValue(item.value) + unit + "，占比" + (portion * 100).toFixed(1) + "%",
      });
      segment.appendChild(svgElement("title", {}, item.name + " · " + formatValue(item.value) + " " + unit + " · " + (portion * 100).toFixed(1) + "%"));
      segment.addEventListener("click", function () {
        chooseItem(level, item);
      });
      segment.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          chooseItem(level, item);
        }
      });
      chart.svg.appendChild(segment);

      if (portion >= 0.075) {
        var middleAngle = angle + (nextAngle - angle) / 2;
        var labelPoint = polarPoint(160, 124, 71, middleAngle);
        chart.svg.appendChild(svgElement("text", {
          x: labelPoint.x,
          y: labelPoint.y + 3,
          class: "expense-pie-percent",
          "text-anchor": "middle",
          "pointer-events": "none",
        }, (portion * 100).toFixed(0) + "%"));
      }

      var legendButton = document.createElement("button");
      legendButton.type = "button";
      legendButton.className = "expense-pie-legend-item" + (selected ? " selected" : "");
      if (muted) {
        legendButton.classList.add("muted");
      }
      var marker = document.createElement("i");
      marker.style.backgroundColor = color;
      var label = document.createElement("span");
      label.textContent = item.name;
      var value = document.createElement("strong");
      value.textContent = formatValue(item.value);
      legendButton.appendChild(marker);
      legendButton.appendChild(label);
      legendButton.appendChild(value);
      legendButton.addEventListener("click", function () {
        chooseItem(level, item);
      });
      chart.legend.appendChild(legendButton);
      angle = nextAngle;
    });

    chart.svg.appendChild(svgElement("text", {
      x: "160",
      y: "119",
      class: "expense-pie-total-label",
      "text-anchor": "middle",
    }, "合计"));
    chart.svg.appendChild(svgElement("text", {
      x: "160",
      y: "140",
      class: "expense-pie-total-value",
      "text-anchor": "middle",
    }, formatValue(total)));
    chart.svg.appendChild(svgElement("text", {
      x: "160",
      y: "157",
      class: "expense-pie-total-unit",
      "text-anchor": "middle",
    }, unit));
  }

  function selectedPrimary() {
    return data.primary.find(function (item) {
      return item.id === state.primaryId;
    });
  }

  function selectedSecondary() {
    return data.secondary.find(function (item) {
      return item.id === state.secondaryId;
    });
  }

  function filteredSecondary() {
    return data.secondary.filter(function (item) {
      return state.primaryId === null || item.parent_id === state.primaryId;
    });
  }

  function filteredTertiary() {
    return data.tertiary.filter(function (item) {
      if (state.secondaryId !== null) {
        return item.parent_id === state.secondaryId;
      }
      return state.primaryId === null || item.primary_id === state.primaryId;
    });
  }

  function updateScopes() {
    var primary = selectedPrimary();
    var secondary = selectedSecondary();
    charts.primary.scope.textContent = primary ? "已选：" + primary.name : "全部一级分类";
    charts.secondary.scope.textContent = primary ? primary.name + " · 二级" : "全部二级分类";
    charts.tertiary.scope.textContent = secondary
      ? secondary.name + " · 三级"
      : (primary ? primary.name + " · 全部三级" : "全部三级分类");
  }

  function renderSecondaryAndTertiary() {
    updateScopes();
    renderPie("secondary", filteredSecondary());
    renderPie("tertiary", filteredTertiary());
  }

  function renderAll() {
    updateScopes();
    renderPie("primary", data.primary);
    renderPie("secondary", filteredSecondary());
    renderPie("tertiary", filteredTertiary());
  }

  renderAll();
})();
