(function () {
  "use strict";

  var dataElement = document.getElementById("investment-goal-chart-data");
  var chart = document.getElementById("investment-goal-chart");
  var legend = document.getElementById("investment-goal-chart-legend");
  var empty = document.getElementById("investment-goal-chart-empty");
  if (!dataElement || !chart || !legend || !empty) return;

  var data = JSON.parse(dataElement.textContent);
  var svgNamespace = "http://www.w3.org/2000/svg";

  function svgElement(name, attributes, text) {
    var element = document.createElementNS(svgNamespace, name);
    Object.keys(attributes || {}).forEach(function (key) {
      element.setAttribute(key, attributes[key]);
    });
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function formatValue(value) {
    return Number(value).toFixed(2);
  }

  function render(scopeId) {
    var scope = data.scopes.find(function (item) { return item.id === scopeId; }) || data.scopes[0];
    var labels = data.labels || [];
    chart.replaceChildren();
    legend.replaceChildren();
    if (!scope || !labels.length) {
      chart.hidden = true;
      empty.hidden = false;
      return;
    }
    chart.hidden = false;
    empty.hidden = true;

    var width = 1000;
    var height = 380;
    var margin = { top: 38, right: 48, bottom: 62, left: 72 };
    var plotWidth = width - margin.left - margin.right;
    var plotHeight = height - margin.top - margin.bottom;
    var allValues = scope.target.concat(scope.actual).filter(function (value) { return value !== null; });
    var maximum = Math.max.apply(null, allValues.concat([1]));
    var axisMaximum = Math.ceil(maximum * 1.12 / 50) * 50;
    if (axisMaximum < 50) axisMaximum = 50;
    var xPosition = function (index) {
      return labels.length === 1 ? margin.left + plotWidth / 2 : margin.left + (plotWidth * index) / (labels.length - 1);
    };
    var yPosition = function (value) {
      return margin.top + plotHeight - (Number(value) / axisMaximum) * plotHeight;
    };

    for (var tick = 0; tick <= 5; tick += 1) {
      var tickValue = axisMaximum * tick / 5;
      var tickY = yPosition(tickValue);
      chart.appendChild(svgElement("line", {
        x1: margin.left, y1: tickY, x2: width - margin.right, y2: tickY,
        class: "asset-trend-grid-line",
      }));
      chart.appendChild(svgElement("text", {
        x: margin.left - 12, y: tickY + 4, class: "asset-trend-axis-label", "text-anchor": "end",
      }, formatValue(tickValue)));
    }

    var labelStep = Math.max(1, Math.ceil(labels.length / 10));
    labels.forEach(function (label, index) {
      if (index % labelStep !== 0 && index !== labels.length - 1) return;
      chart.appendChild(svgElement("text", {
        x: xPosition(index), y: height - 31, class: "asset-trend-axis-label", "text-anchor": "middle",
      }, label.slice(0, 7)));
    });

    var seriesList = [
      { name: "目标", values: scope.target, color: "#2563eb", kind: "target" },
      { name: "实际", values: scope.actual, color: "#e76f51", kind: "actual" },
    ];
    seriesList.forEach(function (series, seriesIndex) {
      var available = [];
      series.values.forEach(function (value, index) {
        if (value !== null) available.push({ value: value, index: index });
      });
      if (!available.length) return;
      var path = available.map(function (point, index) {
        return (index === 0 ? "M " : "L ") + xPosition(point.index) + " " + yPosition(point.value);
      }).join(" ");
      chart.appendChild(svgElement("path", {
        d: path,
        class: "asset-trend-line investment-goal-line " + series.kind,
        stroke: series.color,
        pathLength: "1",
        style: "--series-delay:" + seriesIndex * 120 + "ms",
      }));
      available.forEach(function (point, pointIndex) {
        var circle = svgElement("circle", {
          cx: xPosition(point.index), cy: yPosition(point.value), r: series.kind === "actual" ? 5.5 : 4,
          class: "asset-trend-point investment-goal-point " + series.kind,
          fill: series.color,
          style: "--point-delay:" + (280 + seriesIndex * 90 + pointIndex * 28) + "ms",
          tabindex: "0",
        });
        circle.appendChild(svgElement("title", {}, scope.label + " · " + series.name + " · " + labels[point.index] + " · " + formatValue(point.value) + " 万元"));
        chart.appendChild(circle);
        var shouldLabel = series.kind === "actual" || point.index % labelStep === 0 || point.index === labels.length - 1;
        if (shouldLabel) {
          chart.appendChild(svgElement("text", {
            x: xPosition(point.index),
            y: yPosition(point.value) + (
              series.kind === "actual" ? (pointIndex % 2 === 0 ? 20 : 36) : -11
            ),
            class: "asset-trend-value-label " + series.kind,
            fill: series.color,
            "text-anchor": point.index === 0 ? "start" : (point.index === labels.length - 1 ? "end" : "middle"),
            style: "--label-delay:" + (360 + seriesIndex * 90 + pointIndex * 28) + "ms",
          }, formatValue(point.value)));
        }
      });

      var item = document.createElement("span");
      var marker = document.createElement("i");
      marker.style.backgroundColor = series.color;
      item.appendChild(marker);
      item.appendChild(document.createTextNode(scope.label + " " + series.name));
      legend.appendChild(item);
    });
  }

  document.querySelectorAll("[data-goal-scope]").forEach(function (button) {
    button.addEventListener("click", function () {
      document.querySelectorAll("[data-goal-scope]").forEach(function (item) {
        item.classList.toggle("active", item === button);
      });
      render(button.dataset.goalScope);
    });
  });

  render(data.scopes[0] ? data.scopes[0].id : "family");
})();
