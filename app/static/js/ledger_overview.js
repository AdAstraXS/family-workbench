(function () {
  "use strict";

  var source = document.getElementById("ledger-overview-chart-data");
  if (!source) {
    return;
  }

  var data = JSON.parse(source.textContent);
  var svgNamespace = "http://www.w3.org/2000/svg";
  var palette = [
    "#2563eb", "#0f9f8f", "#f59e0b", "#8b5cf6", "#e76f51",
    "#0891b2", "#84cc16", "#ec4899", "#64748b", "#14b8a6",
  ];
  var budgetPalette = [
    "#1d4ed8", "#60a5fa",
    "#b42318", "#f87171",
    "#047857", "#34d399",
  ];
  var assetCategoryColors = {};
  var nextAssetColor = 0;
  data.asset_charts.forEach(function (chart) {
    chart.items.forEach(function (item) {
      if (!assetCategoryColors[item.name]) {
        assetCategoryColors[item.name] = palette[nextAssetColor % palette.length];
        nextAssetColor += 1;
      }
    });
  });

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

  function formatValue(value) {
    return Math.round(Number(value)).toLocaleString("zh-CN");
  }

  function polarPoint(angle, radius) {
    var radians = (angle - 90) * Math.PI / 180;
    return {
      x: 160 + radius * Math.cos(radians),
      y: 124 + radius * Math.sin(radians),
    };
  }

  function donutPath(startAngle, endAngle) {
    var startOuter = polarPoint(startAngle, 92);
    var endOuter = polarPoint(endAngle, 92);
    var startInner = polarPoint(endAngle, 50);
    var endInner = polarPoint(startAngle, 50);
    var largeArc = endAngle - startAngle > 180 ? 1 : 0;
    return [
      "M", startOuter.x, startOuter.y,
      "A", 92, 92, 0, largeArc, 1, endOuter.x, endOuter.y,
      "L", startInner.x, startInner.y,
      "A", 50, 50, 0, largeArc, 0, endInner.x, endInner.y,
      "Z",
    ].join(" ");
  }

  function renderAssetPies() {
    var container = document.getElementById("overview-asset-pies");
    if (!container) {
      return;
    }
    if (!data.asset_charts.length) {
      container.className = "asset-trend-empty";
      container.textContent = "暂无资产快照数据";
      return;
    }

    data.asset_charts.forEach(function (chartData) {
      var card = document.createElement("article");
      card.className = "expense-pie-card";
      var heading = document.createElement("div");
      heading.className = "expense-pie-card-head";
      var title = document.createElement("h2");
      title.textContent = chartData.label;
      var scope = document.createElement("span");
      scope.textContent = "按资产类别";
      heading.append(title, scope);
      card.appendChild(heading);

      var items = chartData.items.filter(function (item) {
        return Number(item.value) > 0;
      });
      if (!items.length) {
        var empty = document.createElement("div");
        empty.className = "expense-pie-empty";
        empty.textContent = "暂无资产数据";
        card.appendChild(empty);
        container.appendChild(card);
        return;
      }

      var chart = svgElement("svg", {
        class: "expense-pie-chart",
        viewBox: "0 0 320 250",
        role: "img",
        "aria-label": chartData.label + "资产类别分布饼图",
      });
      var legend = document.createElement("div");
      legend.className = "expense-pie-legend";
      var total = items.reduce(function (sum, item) {
        return sum + Number(item.value);
      }, 0);
      var angle = 0;

      items.forEach(function (item, index) {
        var portion = Number(item.value) / total;
        var nextAngle = index === items.length - 1 ? 359.999 : angle + portion * 360;
        var color = assetCategoryColors[item.name];
        var segment = svgElement("path", {
          d: donutPath(angle, nextAngle),
          fill: color,
          class: "expense-pie-segment",
          style: "--segment-delay:" + index * 55 + "ms",
        });
        segment.appendChild(svgElement(
          "title",
          {},
          item.name + " · " + formatValue(item.value) + "元 · " + (portion * 100).toFixed(1) + "%"
        ));
        chart.appendChild(segment);

        if (portion >= 0.075) {
          var labelPoint = polarPoint(angle + (nextAngle - angle) / 2, 71);
          chart.appendChild(svgElement("text", {
            x: labelPoint.x,
            y: labelPoint.y + 3,
            class: "expense-pie-percent",
            "text-anchor": "middle",
          }, (portion * 100).toFixed(0) + "%"));
        }

        var legendItem = document.createElement("div");
        legendItem.className = "overview-pie-legend-item";
        var marker = document.createElement("i");
        marker.style.backgroundColor = color;
        var name = document.createElement("span");
        name.textContent = item.name;
        var value = document.createElement("strong");
        value.textContent = formatValue(item.value);
        legendItem.append(marker, name, value);
        legend.appendChild(legendItem);
        angle = nextAngle;
      });

      chart.appendChild(svgElement("text", {
        x: 160, y: 119, class: "expense-pie-total-label", "text-anchor": "middle",
      }, "合计"));
      chart.appendChild(svgElement("text", {
        x: 160, y: 140, class: "expense-pie-total-value", "text-anchor": "middle",
      }, formatValue(total)));
      chart.appendChild(svgElement("text", {
        x: 160, y: 157, class: "expense-pie-total-unit", "text-anchor": "middle",
      }, "元"));
      card.append(chart, legend);
      container.appendChild(card);
    });
  }

  function renderHorizontalBars() {
    var chart = document.getElementById("overview-budget-chart");
    var empty = document.getElementById("overview-budget-empty");
    var items = data.budget && data.budget.items;
    if (!chart || !empty || !items || !items.length) {
      if (chart) chart.hidden = true;
      if (empty) empty.hidden = false;
      return;
    }

    var width = 720;
    var margin = { top: 22, right: 126, bottom: 18, left: 100 };
    var plotWidth = width - margin.left - margin.right;
    var values = items.map(function (item) { return Number(item.value); });
    var minimum = Math.min.apply(null, values.concat([0]));
    var maximum = Math.max.apply(null, values.concat([0]));
    if (minimum === maximum) maximum = minimum + 1;
    var span = maximum - minimum;
    var x = function (value) {
      return margin.left + ((Number(value) - minimum) / span) * plotWidth;
    };
    var zeroX = x(0);
    var rowY = function (index) {
      return margin.top + Math.floor(index / 2) * 92 + (index % 2) * 33;
    };

    chart.appendChild(svgElement("line", {
      x1: zeroX, x2: zeroX, y1: margin.top - 8,
      y2: rowY(items.length - 1) + 27,
      class: "overview-zero-line",
    }));
    items.forEach(function (item, index) {
      var value = Number(item.value);
      var y = rowY(index);
      var valueX = x(value);
      chart.appendChild(svgElement("text", {
        x: margin.left - 12, y: y + 19,
        class: "overview-chart-label", "text-anchor": "end",
      }, item.name));
      var bar = svgElement("rect", {
        x: Math.min(zeroX, valueX),
        y: y,
        width: Math.max(Math.abs(valueX - zeroX), 1),
        height: 27,
        rx: 5,
        class: "overview-budget-bar",
        fill: budgetPalette[index],
        style: "--bar-delay:" + index * 55 + "ms",
      });
      bar.appendChild(svgElement("title", {}, item.name + " · " + formatValue(value) + "元"));
      chart.appendChild(bar);
      chart.appendChild(svgElement("text", {
        x: width - margin.right + 12, y: y + 19,
        class: "overview-chart-value",
      }, formatValue(value)));
    });
  }

  function renderReturnBars() {
    var chart = document.getElementById("overview-return-chart");
    var empty = document.getElementById("overview-return-empty");
    var items = data.investment_returns || [];
    if (!chart || !empty || !items.length) {
      if (chart) chart.hidden = true;
      if (empty) empty.hidden = false;
      return;
    }

    var width = 720;
    var height = 350;
    var margin = { top: 38, right: 34, bottom: 62, left: 34 };
    var plotWidth = width - margin.left - margin.right;
    var plotHeight = height - margin.top - margin.bottom;
    var values = items.map(function (item) { return Number(item.value); });
    var minimum = Math.min.apply(null, values.concat([0]));
    var maximum = Math.max.apply(null, values.concat([0]));
    if (minimum === maximum) {
      maximum += 1;
      minimum -= 1;
    }
    var padding = (maximum - minimum) * 0.12;
    maximum += padding;
    minimum -= padding;
    var y = function (value) {
      return margin.top + ((maximum - Number(value)) / (maximum - minimum)) * plotHeight;
    };
    var zeroY = y(0);
    var slotWidth = plotWidth / items.length;
    var barWidth = Math.min(92, slotWidth * 0.5);

    chart.appendChild(svgElement("line", {
      x1: margin.left, x2: width - margin.right,
      y1: zeroY, y2: zeroY, class: "overview-zero-line",
    }));
    items.forEach(function (item, index) {
      var value = Number(item.value);
      var centerX = margin.left + slotWidth * (index + 0.5);
      var valueY = y(value);
      var top = Math.min(valueY, zeroY);
      var bar = svgElement("rect", {
        x: centerX - barWidth / 2,
        y: top,
        width: barWidth,
        height: Math.max(Math.abs(valueY - zeroY), 1),
        rx: 7,
        class: "overview-return-bar",
        fill: value >= 0 ? palette[index % palette.length] : "#e76f51",
        style: "--bar-delay:" + index * 75 + "ms",
      });
      bar.appendChild(svgElement("title", {}, item.name + " · " + formatValue(value) + "元"));
      chart.appendChild(bar);
      chart.appendChild(svgElement("text", {
        x: centerX,
        y: value >= 0 ? top - 10 : top + Math.abs(valueY - zeroY) + 18,
        class: "overview-chart-value",
        "text-anchor": "middle",
      }, formatValue(value)));
      chart.appendChild(svgElement("text", {
        x: centerX, y: height - 28,
        class: "overview-chart-label", "text-anchor": "middle",
      }, item.name));
    });
  }

  renderAssetPies();
  renderHorizontalBars();
  renderReturnBars();
})();
