/* LedgerSight UI layer (presentation only; no data, API or logic changes).
   Uses two vendored MIT libraries served from /static/vendor (the CSP allows only same-origin scripts):
   - Motion (motion.dev, the framework-free version of Framer Motion): springs, staggered entrances, press physics,
     the sliding active-menu pill.
   - Chart.js: interactive cash-movement and match-coverage charts.
   If either library is missing, the app falls back to its built-in SVG charts and CSS animations. */
(function () {
  "use strict";
  const M = window.Motion, CJ = window.Chart;
  const REDUCED = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  const motionOn = !!(M && M.animate && !REDUCED);
  const tok = (n, fb) => (getComputedStyle(document.documentElement).getPropertyValue(n) || "").trim() || fb;
  const spring = (visualDuration, bounce) => (M ? { type: M.spring, visualDuration, bounce } : {});
  if (motionOn) document.documentElement.classList.add("has-motion");

  // Motion leaves inline transform/opacity behind; clear it so cards never become containing blocks for popups.
  const settle = (els, anim) => {
    const clear = () => els.forEach((e) => { e.style.transform = ""; e.style.opacity = ""; });
    const t = setTimeout(clear, 1400);
    if (anim && anim.finished) anim.finished.then(() => { clearTimeout(t); clear(); }, () => {});
    else if (anim && anim.then) anim.then(() => { clearTimeout(t); clear(); }, () => {});
  };

  /* ---------- 1. sliding active pill in the sidebar (Framer "layoutId" style) ---------- */
  const nav = document.querySelector(".sidebar nav");
  let pill = null;
  function placePill(instant) {
    if (!pill) return;
    const a = nav.querySelector(".tab.active");
    if (!a || !a.offsetParent) { pill.style.opacity = "0"; return; }
    pill.style.opacity = "1";
    const box = { x: a.offsetLeft, y: a.offsetTop, width: a.offsetWidth, height: a.offsetHeight };
    pill.style.setProperty("--pl", `${a.offsetLeft}px`);
    if (!motionOn || instant) {
      pill.style.transform = `translate(${box.x}px, ${box.y}px)`; pill.style.width = `${box.width}px`; pill.style.height = `${box.height}px`;
      return;
    }
    M.animate(pill, { x: box.x, y: box.y, width: box.width, height: box.height }, spring(0.42, 0.22));
  }
  if (nav) {
    pill = document.createElement("span");
    pill.className = "nav-pill"; pill.setAttribute("aria-hidden", "true");
    nav.prepend(pill); nav.classList.add("has-pill");
    placePill(true);
    let raf = 0;
    if (window.ResizeObserver) new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => placePill(true)); }).observe(nav);
    new MutationObserver(() => placePill(false)).observe(nav, { subtree: true, attributes: true, attributeFilter: ["class"] });
    window.addEventListener("load", () => placePill(true));
  }

  /* ---------- 2. page transitions + entrance of freshly rendered content ---------- */
  let windowUntil = performance.now() + 1500;   // entrance animations only right after a page opens (never on live polling)
  const ENTER = ".ovh-l,.ovh-r,.kw,.ch-card,.cc,.pfc,li.tl,.ag-card,.stage,.kpi,tbody>tr,.ss-row";
  let queue = [], qTimer = 0;
  function flush() {
    const all = queue; queue = [];
    const els = all.filter((e) => e.isConnected && e.offsetParent).slice(0, 36);
    all.forEach((e) => { if (!els.includes(e)) e.style.opacity = ""; });
    if (!els.length) return;
    const anim = M.animate(els, { opacity: [0, 1], y: [14, 0] }, { ...spring(0.5, 0.14), delay: M.stagger(0.035) });
    settle(els, anim);
  }
  function enter(el) {
    if (!motionOn || performance.now() > windowUntil || el.dataset.entered || !el.offsetParent) return;
    el.dataset.entered = "1"; el.style.opacity = "0"; queue.push(el);
    clearTimeout(qTimer); qTimer = setTimeout(flush, 16);
  }
  function enterPanel(p) {
    if (!motionOn || !p) return;
    windowUntil = performance.now() + 1500;
    const kids = [...p.children].filter((e) => e.offsetParent);
    const anim = M.animate(kids, { opacity: [0, 1], y: [18, 0] }, { ...spring(0.55, 0.12), delay: M.stagger(0.05) });
    settle(kids, anim);
    p.querySelectorAll(ENTER).forEach((e) => { e.dataset.entered = "1"; });
  }
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => enterPanel(document.getElementById("p-" + t.dataset.tab))));
  window.LSMotion = { enter: enterPanel };
  const main = document.querySelector("main");
  if (main && motionOn) {
    new MutationObserver((recs) => {
      if (performance.now() > windowUntil) return;
      for (const r of recs) for (const n of r.addedNodes) {
        if (n.nodeType !== 1) continue;
        if (n.matches(ENTER)) enter(n); else n.querySelectorAll && n.querySelectorAll(ENTER).forEach(enter);
      }
    }).observe(main, { childList: true, subtree: true });
  }
  // Login / first paint: give the opening page a proper entrance too.
  window.addEventListener("load", () => enterPanel(document.querySelector(".panel.active")));

  /* ---------- 3. press physics on buttons and cards ---------- */
  if (motionOn) {
    const PRESS = "button:not(:disabled),.kw,.cc,a.linkbtn,.ws-pill,.period-chip";
    document.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0) return;
      const el = ev.target.closest && ev.target.closest(PRESS);
      if (!el) return;
      const s = el.matches(".kw,.cc") ? 0.985 : 0.96;
      M.animate(el, { scale: s }, { type: M.spring, stiffness: 700, damping: 32 });
      const up = () => {
        ["pointerup", "pointercancel", "pointerleave"].forEach((k) => el.removeEventListener(k, up));
        const a = M.animate(el, { scale: 1 }, { type: M.spring, stiffness: 520, damping: 16 });
        settle([el], a);
      };
      ["pointerup", "pointercancel", "pointerleave"].forEach((k) => el.addEventListener(k, up));
    }, true);
  }

  /* ---------- 4. charts (Chart.js) ---------- */
  if (!CJ) return;
  CJ.defaults.font.family = tok("--font", "Inter, Segoe UI, system-ui, sans-serif");
  CJ.defaults.font.size = 11.5;
  const colors = () => ({
    accent: tok("--accent", "#356AE6"), bad: tok("--bad-solid", "#E05252"), warn: tok("--warn-solid", "#E9A23B"),
    line: tok("--line", "#E5EAF2"), line2: tok("--line-2", "#D5DCE8"), faint: tok("--faint", "#A5AFBF"), muted: tok("--muted", "#8994A7"),
    text: tok("--text", "#172033"), surface: tok("--surface", "#FFFFFF"),
    navy: document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches) ? "#E8EEF8" : "#172B4D",
  });
  const sweep = () => Object.values(CJ.instances || {}).forEach((ch) => { if (!ch.canvas || !ch.canvas.isConnected) ch.destroy(); });
  const vgrad = (color, a1, a2) => (ctx) => {
    const { chart } = ctx, area = chart.chartArea;
    if (!area) return color;
    const v = ctx.raw || 0, g = chart.ctx.createLinearGradient(0, v >= 0 ? area.top : area.bottom, 0, chart.scales.y.getPixelForValue(0));
    g.addColorStop(0, CJ.helpers.color(color).alpha(a1).rgbString()); g.addColorStop(1, CJ.helpers.color(color).alpha(a2).rgbString());
    return g;
  };
  // vertical guide at the hovered day
  const crosshair = { id: "lsCross", afterDatasetsDraw(chart) {
    const a = chart.tooltip && chart.tooltip.getActiveElements(); if (!a || !a.length) return;
    const x = a[0].element.x, { top, bottom } = chart.chartArea, c = chart.ctx;
    c.save(); c.strokeStyle = colors().line2; c.setLineDash([4, 4]); c.lineWidth = 1;
    c.beginPath(); c.moveTo(x, top); c.lineTo(x, bottom); c.stroke(); c.restore();
  } };
  const tooltipBase = (col) => ({
    backgroundColor: "#172B4D", titleColor: "#fff", bodyColor: "#E8EEF8", footerColor: "#A9B8D6", padding: 11, cornerRadius: 10,
    titleFont: { weight: "600", size: 12.5 }, bodyFont: { size: 12 }, footerFont: { size: 11, weight: "400" },
    boxPadding: 5, usePointStyle: true, caretSize: 5, displayColors: true, borderColor: "rgba(255,255,255,.08)", borderWidth: 1,
  });
  function mount(canvas, cfg) {
    requestAnimationFrame(() => { if (!canvas.isConnected) return; sweep(); new CJ(canvas, cfg); });
  }

  // Cash movement: same per-day bank figures as the built-in chart (cents in, dollars only for the axis scale).
  function flow({ keys, days, cum, label, fmtAxis, fmtDate, usd }) {
    const col = colors();
    const inC = keys.map((k) => days[k].in), outC = keys.map((k) => days[k].out);
    const wrap = document.createElement("div"); wrap.className = "cj-wrap cj-flow";
    const canvas = document.createElement("canvas"); canvas.setAttribute("role", "img"); canvas.setAttribute("aria-label", label);
    wrap.append(canvas);
    mount(canvas, {
      type: "bar",
      data: {
        labels: keys.map(fmtDate),
        datasets: [
          { type: "line", label: "Running net", data: cum.map((v) => v / 100), borderColor: col.navy, backgroundColor: col.navy, borderWidth: 2.2,
            cubicInterpolationMode: "monotone", stack: "net", pointRadius: 0, pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHoverBorderColor: col.surface, order: 0, cents: cum },
          { type: "bar", label: "Money in", data: inC.map((v) => v / 100), backgroundColor: vgrad(col.accent, 1, 0.55), hoverBackgroundColor: col.accent,
            borderRadius: 6, borderSkipped: false, stack: "s", barPercentage: 0.62, categoryPercentage: 0.7, order: 1, cents: inC },
          { type: "bar", label: "Money out", data: outC.map((v) => -v / 100), backgroundColor: vgrad(col.bad, 0.85, 0.4), hoverBackgroundColor: col.bad,
            borderRadius: 6, borderSkipped: false, stack: "s", barPercentage: 0.62, categoryPercentage: 0.7, order: 2, cents: outC.map((v) => -v) },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        animation: REDUCED ? false : { duration: 900, easing: "easeOutQuart",
          delay: (c) => (c.type === "data" && c.mode === "default" ? c.dataIndex * 28 + c.datasetIndex * 60 : 0) },
        scales: {
          x: { stacked: true, grid: { display: false }, border: { display: false }, ticks: { color: col.muted, maxRotation: 0, autoSkipPadding: 18 } },
          y: { stacked: true, border: { display: false }, grid: { color: col.line, drawTicks: false },
            ticks: { color: col.muted, padding: 8, maxTicksLimit: 6, callback: (v) => fmtAxis(Math.round(v * 100)) } },
        },
        plugins: {
          legend: { display: false },
          tooltip: { ...tooltipBase(col),
            callbacks: {
              label: (c) => { const v = c.dataset.cents[c.dataIndex]; return ` ${c.dataset.label}  ${usd(c.datasetIndex === 0 ? v : Math.abs(v))}`; },
              footer: (items) => { const n = days[keys[items[0].dataIndex]].n; return `${n} transaction${n === 1 ? "" : "s"}`; },
            } },
        },
      },
      plugins: [crosshair],
    });
    return wrap;
  }

  // Match coverage donut: matched / raised for review / not matched (counts computed by the app).
  function donut({ segs, total, label }) {
    const col = colors(), map = { "seg-m": col.accent, "seg-r": col.warn, "seg-o": col.line2 };
    const canvas = document.createElement("canvas"); canvas.setAttribute("role", "img"); canvas.setAttribute("aria-label", label);
    canvas.className = "cj-donut";
    mount(canvas, {
      type: "doughnut",
      data: { labels: segs.map((s) => s[0]), datasets: [{ data: segs.map((s) => s[1]), backgroundColor: segs.map((s) => map[s[2]]),
        borderWidth: 0, borderRadius: 6, spacing: 3, hoverOffset: 5 }] },
      options: {
        responsive: true, maintainAspectRatio: false, cutout: "76%", layout: { padding: 6 },
        animation: REDUCED ? false : { animateRotate: true, duration: 1100, easing: "easeOutQuart" },
        plugins: { legend: { display: false },
          tooltip: { ...tooltipBase(col), callbacks: { label: (c) => ` ${c.label}: ${c.raw} of ${total} (${Math.round(c.raw / total * 100)}%)` } } },
      },
    });
    return canvas;
  }

  // Variance: running bank-minus-ledger by date (series computed by the app, ends exactly at the run's variance).
  function variance({ values, dates, fmtDate, usd }) {
    const col = colors();
    const lo = Math.min(...values), loI = values.indexOf(lo), last = values.length - 1;
    const wrap = document.createElement("div"); wrap.className = "cj-var";
    const box = document.createElement("div"); box.className = "cj-wrap cj-var-plot";
    const canvas = document.createElement("canvas"); canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `Running variance from ${fmtDate(dates[0])} to ${fmtDate(dates[last])}, ending at ${usd(values[last])}; lowest ${usd(lo)} on ${fmtDate(dates[loI])}`);
    box.append(canvas);
    const zeroFill = (color, up) => (ctx) => {
      const { chart } = ctx, a = chart.chartArea; if (!a) return color;
      const y0 = chart.scales.y.getPixelForValue(0), g = chart.ctx.createLinearGradient(0, up ? a.top : a.bottom, 0, y0);
      g.addColorStop(0, CJ.helpers.color(color).alpha(0.32).rgbString()); g.addColorStop(1, CJ.helpers.color(color).alpha(0.02).rgbString());
      return g;
    };
    const sign = (v) => (v < 0 ? col.bad : col.accent);
    // soft halo on the latest point
    const halo = { id: "lsHalo", afterDatasetsDraw(chart) {
      const p = chart.getDatasetMeta(0).data[last]; if (!p) return;
      const c = chart.ctx; c.save(); c.fillStyle = CJ.helpers.color(sign(values[last])).alpha(0.16).rgbString();
      c.beginPath(); c.arc(p.x, p.y, 9, 0, Math.PI * 2); c.fill(); c.restore();
    } };
    const zeroLine = { id: "lsZero", beforeDatasetsDraw(chart) {
      const a = chart.chartArea, y = chart.scales.y.getPixelForValue(0), c = chart.ctx;
      if (y < a.top || y > a.bottom) return;
      c.save(); c.strokeStyle = col.line2; c.setLineDash([3, 4]); c.lineWidth = 1;
      c.beginPath(); c.moveTo(a.left, y); c.lineTo(a.right, y); c.stroke(); c.restore();
    } };
    mount(canvas, {
      type: "line",
      data: { labels: dates.map(fmtDate), datasets: [{
        data: values.map((v) => v / 100), cents: values, borderWidth: 2.2, cubicInterpolationMode: "monotone",
        borderColor: (ctx) => {   // hard colour stop at zero: blue above, red below
          const { chart } = ctx, a = chart.chartArea; if (!a) return col.accent;
          const f = Math.min(1, Math.max(0, (chart.scales.y.getPixelForValue(0) - a.top) / (a.bottom - a.top)));
          const g = chart.ctx.createLinearGradient(0, a.top, 0, a.bottom);
          g.addColorStop(0, col.accent); g.addColorStop(f, col.accent); g.addColorStop(f, col.bad); g.addColorStop(1, col.bad);
          return g;
        },
        fill: { target: "origin", above: zeroFill(col.accent, true), below: zeroFill(col.bad, false) },
        pointRadius: (c) => (c.dataIndex === last ? 4 : c.dataIndex === loI && lo < 0 ? 3 : 0),
        pointBackgroundColor: (c) => sign(values[c.dataIndex]), pointBorderColor: col.surface, pointBorderWidth: 2,
        pointHoverRadius: 5, pointHoverBorderWidth: 2, pointHitRadius: 12,
      }] },
      options: {
        responsive: true, maintainAspectRatio: false, layout: { padding: { top: 10, right: 10, left: 4, bottom: 0 } },
        interaction: { mode: "index", intersect: false },
        animation: REDUCED ? false : { duration: 1000, easing: "easeOutQuart" },
        scales: {
          x: { display: false },
          y: { display: false, grace: "12%" },
        },
        plugins: { legend: { display: false },
          tooltip: { ...tooltipBase(col), displayColors: false,
            callbacks: { title: (it) => it[0].label, label: (c) => `Running variance ${usd(values[c.dataIndex])}`,
              afterLabel: (c) => c.dataIndex ? `Change that day ${usd(values[c.dataIndex] - values[c.dataIndex - 1])}` : "" } } },
      },
      plugins: [zeroLine, crosshair, halo],
    });
    const axis = document.createElement("div"); axis.className = "cj-var-ax";
    const mk = (t, cls) => { const sp = document.createElement("span"); if (cls) sp.className = cls; sp.textContent = t; return sp; };
    axis.append(mk(fmtDate(dates[0])), lo < 0 ? mk(`Lowest ${usd(lo)} · ${fmtDate(dates[loI])}`, "lo") : mk(""), mk(fmtDate(dates[last])));
    wrap.append(box, axis);
    return wrap;
  }

  window.LSCharts = { flow, donut, variance };

  // Theme switch: charts read colours at draw time, so redraw the overview when the theme changes.
  new MutationObserver(() => {
    const home = document.getElementById("p-home");
    if (home && home.classList.contains("active") && typeof renderOverview === "function") { windowUntil = 0; renderOverview(); }
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
})();
