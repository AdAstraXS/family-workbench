(function () {
  "use strict";

  var dataElement = document.getElementById("asset-trend-data");
  var chart = document.getElementById("asset-trend-chart");
  var legend = document.getElementById("asset-trend-legend");
  var empty = document.getElementById("asset-trend-empty");
  if (!dataElement || !chart || !legend || !empty) {
    return;
  }

  var data = JSON.parse(dataElement.textContent);
  var colors = ["#0f766e", "#e76f51", "#2563eb", "#7c3aed", "#d97706"];
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

  function colorFor(series, index) {
    return series.kind === "family" ? "#172554" : colors[index % colors.length];
  }

  function render(periodName) {
    var period = data[periodName];
    var labels = period.labels || [];
    var seriesList = period.series || [];
    chart.replaceChildren();
    legend.replaceChildren();

    if (!labels.length || !seriesList.length) {
      empty.hidden = false;
      chart.hidden = true;
      return;
    }
    empty.hidden = true;
    chart.hidden = false;

    var width = 1000;
    var height = 340;
    var margin = { top: 32, right: 46, bottom: 52, left: 68 };
    var plotWidth = width - margin.left - margin.right;
    var plotHeight = height - margin.top - margin.bottom;
    var allValues = seriesList.reduce(function (values, series) {
      return values.concat(series.values);
    }, []);
    var maximum = Math.max.apply(null, allValues.concat([1]));
    var axisMaximum = Math.ceil(maximum * 1.12 / 10) * 10;
    if (axisMaximum < 10) {
      axisMaximum = 10;
    }
    var xPosition = function (index) {
      return labels.length === 1
        ? margin.left + plotWidth / 2
        : margin.left + (plotWidth * index) / (labels.length - 1);
    };
    var yPosition = function (value) {
      return margin.top + plotHeight - (Number(value) / axisMaximum) * plotHeight;
    };

    var definitions = svgElement("defs");
    var gradient = svgElement("linearGradient", {
      id: "asset-family-gradient",
      x1: "0",
      y1: "0",
      x2: "0",
      y2: "1",
    });
    gradient.appendChild(svgElement("stop", { offset: "0%", "stop-color": "#2563eb", "stop-opacity": "0.22" }));
    gradient.appendChild(svgElement("stop", { offset: "100%", "stop-color": "#2563eb", "stop-opacity": "0" }));
    definitions.appendChild(gradient);
    chart.appendChild(definitions);

    for (var tick = 0; tick <= 5; tick += 1) {
      var tickValue = (axisMaximum * tick) / 5;
      var tickY = yPosition(tickValue);
      chart.appendChild(svgElement("line", {
        x1: margin.left,
        y1: tickY,
        x2: width - margin.right,
        y2: tickY,
        class: "asset-trend-grid-line",
      }));
      chart.appendChild(svgElement("text", {
        x: margin.left - 14,
        y: tickY + 4,
        class: "asset-trend-axis-label",
        "text-anchor": "end",
      }, formatValue(tickValue)));
    }

    var labelStep = Math.max(1, Math.ceil(labels.length / 12));
    labels.forEach(function (label, index) {
      if (index % labelStep !== 0 && index !== labels.length - 1) {
        return;
      }
      chart.appendChild(svgElement("text", {
        x: xPosition(index),
        y: height - 28,
        class: "asset-trend-axis-label",
        "text-anchor": "middle",
      }, label));
    });

    var familySeries = seriesList.find(function (series) {
      return series.kind === "family";
    });
    if (familySeries) {
      var areaPoints = familySeries.values.map(function (value, index) {
        return xPosition(index) + "," + yPosition(value);
      });
      var areaPath = "M " + margin.left + " " + (margin.top + plotHeight) +
        " L " + areaPoints.join(" L ") +
        " L " + xPosition(labels.length - 1) + " " + (margin.top + plotHeight) + " Z";
      chart.appendChild(svgElement("path", {
        d: areaPath,
        class: "asset-trend-area",
        fill: "url(#asset-family-gradient)",
      }));
    }

    seriesList.forEach(function (series, seriesIndex) {
      var color = colorFor(series, seriesIndex);
      var points = series.values.map(function (value, index) {
        return xPosition(index) + "," + yPosition(value);
      });
      chart.appendChild(svgElement("path", {
        d: "M " + points.join(" L "),
        class: "asset-trend-line " + (series.kind === "family" ? "family" : ""),
        stroke: color,
        pathLength: "1",
        style: "--series-delay:" + seriesIndex * 110 + "ms",
      }));
      series.values.forEach(function (value, index) {
        var point = svgElement("circle", {
          cx: xPosition(index),
          cy: yPosition(value),
          r: series.kind === "family" ? "5.5" : "4",
          class: "asset-trend-point " + (series.kind === "family" ? "family" : ""),
          fill: color,
          style: "--point-delay:" + (360 + seriesIndex * 90 + index * 35) + "ms",
          tabindex: "0",
        });
        point.appendChild(svgElement("title", {}, series.name + " · " + labels[index] + " · " + formatValue(value) + " 万元"));
        chart.appendChild(point);
        var labelOffset = series.kind === "family" ? -12 : (seriesIndex % 2 === 0 ? -10 : 17);
        var textAnchor = index === 0 ? "start" : (index === labels.length - 1 ? "end" : "middle");
        chart.appendChild(svgElement("text", {
          x: xPosition(index),
          y: yPosition(value) + labelOffset,
          class: "asset-trend-value-label " + (series.kind === "family" ? "family" : ""),
          fill: color,
          "text-anchor": textAnchor,
          style: "--label-delay:" + (430 + seriesIndex * 90 + index * 35) + "ms",
        }, formatValue(value)));
      });

      var legendItem = document.createElement("span");
      var marker = document.createElement("i");
      marker.style.backgroundColor = color;
      legendItem.appendChild(marker);
      legendItem.appendChild(document.createTextNode(series.name));
      legend.appendChild(legendItem);
    });
  }

  document.querySelectorAll("[data-trend-period]").forEach(function (button) {
    button.addEventListener("click", function () {
      document.querySelectorAll("[data-trend-period]").forEach(function (item) {
        item.classList.toggle("active", item === button);
      });
      render(button.dataset.trendPeriod);
    });
  });

  render("monthly");
})();
