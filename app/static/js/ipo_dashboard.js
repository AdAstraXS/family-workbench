(() => {
  const dataElement = document.getElementById("ipo-dashboard-chart-data");
  if (!dataElement) return;

  const data = JSON.parse(dataElement.textContent);
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const svgNamespace = "http://www.w3.org/2000/svg";

  function formatWan(value) {
    return `${(Number(value) / 10000).toFixed(2)}万`;
  }

  function svgElement(name, attributes = {}) {
    const element = document.createElementNS(svgNamespace, name);
    Object.entries(attributes).forEach(([key, value]) => {
      element.setAttribute(key, value);
    });
    return element;
  }

  function renderRankingChart(containerId, series) {
    const container = document.getElementById(containerId);
    if (!container) return;
    container.replaceChildren();

    if (!series.length) {
      const empty = document.createElement("p");
      empty.className = "ipo-chart-empty";
      empty.textContent = "当前年份暂无已实现盈利数据";
      container.appendChild(empty);
      return;
    }

    const values = series.map((item) => Number(item.value));
    let minimum = Math.min(0, ...values);
    let maximum = Math.max(0, ...values);
    if (minimum === maximum) {
      maximum = minimum + 1;
    }
    const span = maximum - minimum;
    const zeroPosition = ((0 - minimum) / span) * 100;

    const rows = document.createElement("div");
    rows.className = "ipo-ranking-chart-rows";
    rows.setAttribute("role", "list");

    series.forEach((item, index) => {
      const value = Number(item.value);
      const valuePosition = ((value - minimum) / span) * 100;
      const row = document.createElement("div");
      row.className = "ipo-ranking-row";
      row.setAttribute("role", "listitem");
      row.style.setProperty("--row-delay", `${Math.min(index * 26, 360)}ms`);

      const label = document.createElement("span");
      label.className = "ipo-ranking-label";
      label.textContent = item.label;
      label.title = item.label;

      const plot = document.createElement("span");
      plot.className = "ipo-ranking-plot";

      const zero = document.createElement("span");
      zero.className = "ipo-ranking-zero";
      zero.style.left = `${zeroPosition}%`;

      const bar = document.createElement("span");
      bar.className = `ipo-ranking-bar ${value >= 0 ? "positive" : "negative"}`;
      bar.style.left = `${Math.min(zeroPosition, valuePosition)}%`;
      bar.style.width = `${Math.max(Math.abs(valuePosition - zeroPosition), 0.35)}%`;
      bar.style.transformOrigin = value >= 0 ? "left center" : "right center";
      bar.title = `${item.label}：${Number(value).toLocaleString("zh-CN", {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`;

      const number = document.createElement("strong");
      number.className = value >= 0 ? "positive" : "negative";
      number.textContent = formatWan(value);

      plot.append(zero, bar);
      row.append(label, plot, number);
      rows.appendChild(row);

      if (!reduceMotion) {
        bar.style.transform = "scaleX(0)";
        requestAnimationFrame(() => {
          bar.style.transform = "scaleX(1)";
        });
      }
    });

    container.appendChild(rows);
  }

  function renderTrendChart() {
    const container = document.getElementById("ipo-profit-trend-chart");
    if (!container) return;
    container.replaceChildren();

    const labels = data.trend.labels;
    const values = data.trend.values.map(Number);
    if (!labels.length) {
      const empty = document.createElement("p");
      empty.className = "ipo-chart-empty";
      empty.textContent = "当前年份暂无趋势数据";
      container.appendChild(empty);
      return;
    }

    const width = 960;
    const height = 340;
    const margin = { top: 38, right: 28, bottom: 48, left: 64 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    let minimum = Math.min(0, ...values);
    let maximum = Math.max(0, ...values);
    if (minimum === maximum) {
      const padding = Math.max(Math.abs(maximum) * 0.15, 1000);
      minimum -= padding;
      maximum += padding;
    } else {
      const padding = (maximum - minimum) * 0.12;
      minimum -= padding;
      maximum += padding;
    }
    const span = maximum - minimum;
    const x = (index) =>
      margin.left + (labels.length === 1 ? plotWidth / 2 : (plotWidth * index) / (labels.length - 1));
    const y = (value) => margin.top + ((maximum - value) / span) * plotHeight;

    const svg = svgElement("svg", {
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": "盈利趋势折线图",
    });
    const title = svgElement("title");
    title.textContent = "盈利趋势";
    svg.appendChild(title);

    const gridCount = 4;
    for (let index = 0; index <= gridCount; index += 1) {
      const value = maximum - (span * index) / gridCount;
      const gridY = y(value);
      const line = svgElement("line", {
        x1: margin.left,
        x2: width - margin.right,
        y1: gridY,
        y2: gridY,
        class: "ipo-trend-grid-line",
      });
      const label = svgElement("text", {
        x: margin.left - 12,
        y: gridY + 4,
        "text-anchor": "end",
        class: "ipo-trend-axis-label",
      });
      label.textContent = formatWan(value);
      svg.append(line, label);
    }

    const zeroY = y(0);
    svg.appendChild(
      svgElement("line", {
        x1: margin.left,
        x2: width - margin.right,
        y1: zeroY,
        y2: zeroY,
        class: "ipo-trend-zero-line",
      }),
    );

    const points = values.map((value, index) => [x(index), y(value)]);
    const path = svgElement("path", {
      d: points.map((point, index) => `${index ? "L" : "M"} ${point[0]} ${point[1]}`).join(" "),
      class: "ipo-trend-path",
    });
    svg.appendChild(path);

    const compactLabels = container.clientWidth < 620 && labels.length > 8;
    points.forEach(([pointX, pointY], index) => {
      const value = values[index];
      const isFirst = index === 0;
      const isLast = index === labels.length - 1;
      const edgeOffset = isFirst ? 9 : isLast ? -9 : 0;
      const edgeAnchor = isFirst ? "start" : isLast ? "end" : "middle";
      const point = svgElement("circle", {
        cx: pointX,
        cy: pointY,
        r: 4.5,
        class: value >= 0 ? "ipo-trend-point positive" : "ipo-trend-point negative",
      });
      const valueLabel = svgElement("text", {
        x: pointX + edgeOffset,
        y: pointY + (value >= 0 ? -12 : 19),
        "text-anchor": edgeAnchor,
        class: value >= 0 ? "ipo-trend-value positive" : "ipo-trend-value negative",
      });
      valueLabel.textContent = formatWan(value);
      svg.append(point, valueLabel);

      if (!compactLabels || index % 2 === 0 || index === labels.length - 1) {
        const xLabel = svgElement("text", {
          x: pointX + edgeOffset,
          y: height - 16,
          "text-anchor": edgeAnchor,
          class: "ipo-trend-axis-label",
        });
        xLabel.textContent = labels[index];
        svg.appendChild(xLabel);
      }
    });

    container.appendChild(svg);
    if (!reduceMotion && path.getTotalLength) {
      const length = path.getTotalLength();
      path.style.strokeDasharray = String(length);
      path.style.strokeDashoffset = String(length);
      requestAnimationFrame(() => {
        path.style.strokeDashoffset = "0";
      });
    }
  }

  renderRankingChart("ipo-stock-profit-chart", data.stock);
  renderRankingChart("ipo-account-profit-chart", data.account);
  renderTrendChart();

  let resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(renderTrendChart, 120);
  });
})();
