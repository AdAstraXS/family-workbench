(function () {
  "use strict";

  var dataElement = document.getElementById("cashflow-trend-data");
  var chart = document.getElementById("cashflow-trend-chart");
  var empty = document.getElementById("cashflow-trend-empty");
  if (!dataElement || !chart || !empty) {
    return;
  }

  var data = JSON.parse(dataElement.textContent);
  var labels = data.labels || [];
  var incomes = data.income || [];
  var expenses = data.expense || [];
  var nets = data.net || [];
  var svgNamespace = "http://www.w3.org/2000/svg";

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
    return Number(value).toFixed(2);
  }

  if (!labels.length) {
    empty.hidden = false;
    chart.hidden = true;
    return;
  }
  empty.hidden = true;
  chart.hidden = false;

  var width = 1000;
  var height = 360;
  var margin = { top: 38, right: 42, bottom: 54, left: 70 };
  var plotWidth = width - margin.left - margin.right;
  var plotHeight = height - margin.top - margin.bottom;
  var allValues = incomes.concat(expenses, nets, [0]);
  var minimum = Math.min.apply(null, allValues);
  var maximum = Math.max.apply(null, allValues);
  var range = Math.max(maximum - minimum, 1);
  var axisMinimum = minimum < 0 ? minimum - range * 0.12 : 0;
  var axisMaximum = maximum + range * 0.14;
  if (axisMaximum === axisMinimum) {
    axisMaximum = axisMinimum + 1;
  }
  var groupWidth = plotWidth / labels.length;
  var barWidth = Math.min(24, groupWidth * 0.26);
  var xCenter = function (index) {
    return margin.left + groupWidth * index + groupWidth / 2;
  };
  var yPosition = function (value) {
    return margin.top + ((axisMaximum - Number(value)) / (axisMaximum - axisMinimum)) * plotHeight;
  };
  var zeroY = yPosition(0);

  for (var tick = 0; tick <= 5; tick += 1) {
    var tickValue = axisMinimum + ((axisMaximum - axisMinimum) * tick) / 5;
    var tickY = yPosition(tickValue);
    chart.appendChild(svgElement("line", {
      x1: margin.left,
      y1: tickY,
      x2: width - margin.right,
      y2: tickY,
      class: "cashflow-trend-grid-line",
    }));
    chart.appendChild(svgElement("text", {
      x: margin.left - 12,
      y: tickY + 4,
      class: "cashflow-trend-axis-label",
      "text-anchor": "end",
    }, formatValue(tickValue)));
  }
  chart.appendChild(svgElement("line", {
    x1: margin.left,
    y1: zeroY,
    x2: width - margin.right,
    y2: zeroY,
    class: "cashflow-trend-zero-line",
  }));

  labels.forEach(function (label, index) {
    chart.appendChild(svgElement("text", {
      x: xCenter(index),
      y: height - 22,
      class: "cashflow-trend-axis-label",
      "text-anchor": "middle",
    }, label));

    [
      { value: incomes[index], offset: -barWidth - 2, className: "income", label: "收入" },
      { value: expenses[index], offset: 2, className: "expense", label: "支出" },
    ].forEach(function (bar, barIndex) {
      var valueY = yPosition(bar.value);
      var topY = Math.min(valueY, zeroY);
      var barHeight = Math.max(Math.abs(zeroY - valueY), bar.value === 0 ? 1 : 0);
      var rect = svgElement("rect", {
        x: xCenter(index) + bar.offset,
        y: topY,
        width: barWidth,
        height: barHeight,
        rx: "3",
        class: "cashflow-trend-bar " + bar.className,
        style: "--bar-delay:" + (index * 45 + barIndex * 40) + "ms",
      });
      rect.appendChild(svgElement("title", {}, label + " · " + bar.label + " " + formatValue(bar.value) + " 万元"));
      chart.appendChild(rect);
    });
  });

  var linePoints = nets.map(function (value, index) {
    return xCenter(index) + "," + yPosition(value);
  });
  chart.appendChild(svgElement("path", {
    d: "M " + linePoints.join(" L "),
    class: "cashflow-trend-net-line",
    pathLength: "1",
  }));
  nets.forEach(function (value, index) {
    var colorClass = value < 0 ? "negative" : "positive";
    var point = svgElement("circle", {
      cx: xCenter(index),
      cy: yPosition(value),
      r: "4.5",
      class: "cashflow-trend-net-point " + colorClass,
      style: "--point-delay:" + (420 + index * 45) + "ms",
    });
    point.appendChild(svgElement("title", {}, labels[index] + " · 结余 " + formatValue(value) + " 万元"));
    chart.appendChild(point);
    chart.appendChild(svgElement("text", {
      x: xCenter(index),
      y: yPosition(value) + (value < 0 ? 17 : -10),
      class: "cashflow-trend-value-label " + colorClass,
      "text-anchor": "middle",
      style: "--label-delay:" + (500 + index * 45) + "ms",
    }, formatValue(value)));
  });
})();
