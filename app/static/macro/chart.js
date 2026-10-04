/* Exact observation strings remain intact. Numbers are used only for SVG coordinates. */
(() => {
  "use strict";
  const host = document.querySelector("[data-macro-chart]");
  const dataNode = document.getElementById("macro-chart-data");
  if (!host || !dataNode) return;
  const data = JSON.parse(dataNode.textContent);
  const points = data.points;
  const inspector = document.querySelector("[data-macro-inspector]");
  const slider = document.querySelector("[data-macro-point]");
  const ns = "http://www.w3.org/2000/svg";
  const numbers = points.map(p => p.value === null ? null : Number(p.value));
  const dates = points.map(p => Date.parse(p.date + "T00:00:00Z"));
  let chosen = points.length - 1, geometry, svg, cursor, dot;
  function element(name, attributes, label) {
    const node = document.createElementNS(ns, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    if (label !== undefined) node.textContent = label;
    return node;
  }
  function exact(value) {
    return value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
  }
  function select(index) {
    chosen = Math.max(0, Math.min(points.length - 1, index));
    slider.value = chosen;
    const point = points[chosen];
    slider.setAttribute("aria-valuetext", point.label + " · " + (point.value === null ? "来源缺值" : exact(point.value) + " " + data.unit));
    inspector.textContent = point.label + " · " + (point.value === null ? "来源缺值" : exact(point.value) + " " + data.unit);
    if (!geometry) return;
    const x = geometry.x(chosen);
    cursor.setAttribute("x1", x); cursor.setAttribute("x2", x);
    const value = numbers[chosen];
    dot.style.display = value === null ? "none" : "";
    if (value !== null) {dot.setAttribute("cx", x); dot.setAttribute("cy", geometry.y(value));}
  }
  function draw() {
    const width = Math.max(140, host.clientWidth), height = host.clientHeight;
    const pad = {left: width < 400 ? 45 : 65, right: 14, top: 24, bottom: 32};
    let low = Infinity, high = -Infinity, valid = 0;
    for (const value of numbers) if (value !== null && Number.isFinite(value)) {low = Math.min(low, value); high = Math.max(high, value); valid++;}
    const ref = data.reference === null ? null : Number(data.reference);
    if (ref !== null && valid) {low = Math.min(low, ref); high = Math.max(high, ref);}
    if (!valid) {
      host.replaceChildren(); const message = document.createElement("p");
      message.className = "macro-empty"; message.textContent = "该范围全部为来源缺值，无法绘制曲线。";
      host.append(message); geometry = null; select(chosen); return;
    }
    const spread = high - low || Math.max(Math.abs(high) * 0.05, 1);
    low -= spread * 0.12; high += spread * 0.12;
    const first = dates[0], last = dates[dates.length - 1];
    const x = i => first === last ? (pad.left + width - pad.right) / 2 : pad.left + (dates[i] - first) / (last - first) * (width - pad.left - pad.right);
    const y = value => pad.top + (high - value) / (high - low) * (height - pad.top - pad.bottom);
    geometry = {x, y};
    svg = element("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": `${data.name}趋势；用下方逐期查看滑块或历史表格读取确切值。`});
    for (let i = 0; i <= 4; i++) {
      const value = low + (high - low) * i / 4, yy = y(value);
      svg.append(element("line", {x1: pad.left, x2: width-pad.right, y1: yy, y2: yy, class: "macro-chart-grid"}));
      // Use enough precision for adjacent ticks to stay distinguishable.
      // Normal registered series fit without changing the displayed unit.
      const digits = Math.max(0, Math.min(6, Math.ceil(-Math.log10((high-low)/4)) + 1));
      const label = Math.abs(value) >= 1e9 ? value.toExponential(3) : Number(value.toFixed(digits)).toString();
      svg.append(element("text", {x: pad.left-8, y: yy+4, "text-anchor": "end", class: "macro-chart-label"}, label));
    }
    if (ref !== null) {
      svg.append(element("line", {x1: pad.left, x2: width-pad.right, y1: y(ref), y2: y(ref), class: "macro-chart-reference"}));
      svg.append(element("text", {x: width-pad.right, y: y(ref)-6, "text-anchor": "end", class: "macro-chart-label"}, `参考线 ${ref}`));
    }
    let path = "", pen = false;
    numbers.forEach((value, index) => {
      if (value === null) {pen = false; return;}
      path += `${pen ? "L" : "M"}${x(index).toFixed(2)},${y(value).toFixed(2)} `; pen = true;
      // Keep isolated observations visible without fabricating a connected trend.
      if (numbers[index-1] == null && numbers[index+1] == null) svg.append(element("circle", {cx:x(index),cy:y(value),r:3,class:"macro-chart-dot"}));
    });
    svg.append(element("path", {d: path, class: "macro-chart-line"}));
    const ticks = width < 450 ? 2 : 4;
    const seen = new Set();
    for (let i = 0; i < ticks; i++) {
      const index = Math.round(i / (ticks-1) * (points.length-1));
      if (seen.has(index)) continue; seen.add(index);
      const label = points[index].date;
      svg.append(element("text", {x:x(index),y:height-7,"text-anchor":i===0?"start":i===ticks-1?"end":"middle",class:"macro-chart-label"}, label));
    }
    cursor = element("line", {y1:pad.top,y2:height-pad.bottom,class:"macro-chart-cursor"});
    dot = element("circle", {r:4,class:"macro-chart-dot"}); svg.append(cursor,dot);
    svg.addEventListener("pointermove", event => {
      const rect = svg.getBoundingClientRect(); const pointerX = (event.clientX - rect.left) * width / rect.width;
      let best = 0;
      for (let i = 1; i < points.length; i++) if (Math.abs(x(i)-pointerX) < Math.abs(x(best)-pointerX)) best = i;
      select(best);
    });
    host.replaceChildren(svg); select(chosen);
  }
  slider.addEventListener("input", () => select(Number(slider.value)));
  new ResizeObserver(draw).observe(host);
  draw();
})();
