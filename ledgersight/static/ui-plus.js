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
    const clear = () => els.forEach((e) => { e.style.transform = ""; e.style.opacity = ""; e.style.filter = ""; });
    const t = setTimeout(clear, 1400);
    if (anim && anim.finished) anim.finished.then(() => { clearTimeout(t); clear(); }, () => {});
    else if (anim && anim.then) anim.then(() => { clearTimeout(t); clear(); }, () => {});
  };

  /* ---------- 1. sliding active pill in the sidebar (Framer "layoutId" style) ---------- */
  const nav = document.querySelector(".sidebar nav");
  let pill = null, pillAnims = [];
  const side = nav && nav.closest(".sidebar");
  // While the rail is opening or closing the menu geometry is changing under the pill; a glide started then would
  // finish on stale coordinates, so during that ~0.3s the pill snaps instead.
  const railMoving = () => !!(side && side.dataset.railMoving);
  // Liquid glide: the pill's top and bottom edges are separate springs. The edge in the direction of travel leads,
  // the other follows, so the pill stretches toward the new item and settles into it (iOS / Linear style).
  const cur = { x: 0, top: 0, bottom: 0, w: 0 };
  const paint = () => {
    pill.style.transform = `translate(${cur.x}px, ${cur.top}px)`;
    pill.style.width = `${cur.w}px`; pill.style.height = `${Math.max(0, cur.bottom - cur.top)}px`;
  };
  const stopPill = () => { pillAnims.forEach((a) => { try { a.stop(); } catch (_) {} }); pillAnims = []; };
  function placePill(instant) {
    if (!pill) return;
    const a = nav.querySelector(".tab.active");
    if (!a || !a.offsetParent) { pill.style.opacity = "0"; return; }
    pill.style.opacity = "1";
    const box = { x: a.offsetLeft, y: a.offsetTop, width: a.offsetWidth, height: a.offsetHeight };
    pill.style.setProperty("--pl", `${a.offsetLeft}px`);
    const same = Math.abs(cur.top - box.y) < 0.5 && Math.abs(cur.bottom - (box.y + box.height)) < 0.5 && Math.abs(cur.x - box.x) < 0.5 && Math.abs(cur.w - box.width) < 0.5;
    if (!motionOn || instant || railMoving() || !cur.w) {
      stopPill(); Object.assign(cur, { x: box.x, top: box.y, bottom: box.y + box.height, w: box.width }); paint();
      return;
    }
    if (same) return;
    stopPill();
    const down = box.y >= cur.top, lead = spring(0.3, 0.12), trail = spring(0.5, 0.16);
    const go = (k, to, opts) => pillAnims.push(M.animate(cur[k], to, { ...opts, onUpdate: (v) => { cur[k] = v; paint(); } }));
    go("top", box.y, down ? trail : lead);
    go("bottom", box.y + box.height, down ? lead : trail);
    go("x", box.x, lead); go("w", box.width, lead);
  }
  if (nav) {
    pill = document.createElement("span");
    pill.className = "nav-pill"; pill.setAttribute("aria-hidden", "true");
    nav.prepend(pill); nav.classList.add("has-pill");
    placePill(true);
    let raf = 0;
    if (window.ResizeObserver) new ResizeObserver(() => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => placePill(true)); }).observe(nav);
    new MutationObserver(() => placePill(false)).observe(nav, { subtree: true, attributes: true, attributeFilter: ["class"] });
    // Rail opens/closes (hover peek, collapse toggle): snap the pill to the new layout, again once the width transition ends.
    if (side) {
      let settleT = 0;
      const snap = () => { side.dataset.railMoving = "1"; placePill(true); clearTimeout(settleT);
        settleT = setTimeout(() => { delete side.dataset.railMoving; placePill(true); }, 320); };
      new MutationObserver(snap).observe(side, { attributes: true, attributeFilter: ["class"] });
      let mini = document.body.classList.contains("side-mini");
      new MutationObserver(() => { const m = document.body.classList.contains("side-mini"); if (m !== mini) { mini = m; snap(); } })
        .observe(document.body, { attributes: true, attributeFilter: ["class"] });
      side.addEventListener("transitionend", (e) => { if (e.target === side) placePill(true); });
    }
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
  // Page entrance, in three beats (about 0.5s in all, so it never slows work down):
  //  1. the title, then the subtitle, rise word by word, each word from under its own clip line,
  //  2. the header actions rise with a short blur-to-sharp,
  //  3. the content blocks, then the cards inside them, rise one after another.
  const HEAD = ".page-head, .ov-head";
  const TEXT = "h2, .sub, .greet, .hero-meta";
  const HEADLINE = "h2, .greet";   // these rise one letter at a time
  const UNIT = ".badge, code, kbd, svg, button, input, select, img, .mono, .pill, .chip";  // never split inside these
  // Wrap every word in <span.ls-w><span.ls-wi>word</span></span> (bold/links are split too; badges stay whole).
  function splitWords(node, chars) {
    if (node.dataset.lsSplit) unsplit(node);
    const inners = [];
    const flexLike = /flex|grid/.test(getComputedStyle(node).display);
    const wrapUnit = (n) => {
      const w = document.createElement("span"), i = document.createElement("span");
      w.className = "ls-w"; i.className = "ls-wi"; n.replaceWith(w); i.appendChild(n); w.appendChild(i); inners.push(i);
    };
    const walk = (parent) => {
      for (const n of [...parent.childNodes]) {
        if (n.nodeType === 3) {
          const parts = n.textContent.split(/(\s+)/);
          if (parts.every((s) => !s.trim())) continue;
          const frag = document.createDocumentFragment();
          for (const s of parts) {
            if (!s) continue;
            if (!s.trim()) { frag.appendChild(document.createTextNode(s)); continue; }
            const w = document.createElement("span");
            if (chars) {
              // headline: the word stays one unbreakable clip box; each letter inside it rises on its own
              w.className = "ls-w ls-wc";
              for (const ch of Array.from(s)) { const i = document.createElement("span"); i.className = "ls-wi"; i.textContent = ch; w.appendChild(i); inners.push(i); }
            } else {
              const i = document.createElement("span");
              w.className = "ls-w"; i.className = "ls-wi"; i.textContent = s; w.appendChild(i); inners.push(i);
            }
            frag.appendChild(w);
          }
          n.replaceWith(frag);
        } else if (n.nodeType === 1) {
          if (n.matches(UNIT)) wrapUnit(n); else walk(n);
        }
      }
    };
    if (flexLike) {
      // In a flex/grid heading, loose text runs become one inline item each so spaces and wrapping stay as they were.
      for (const n of [...node.childNodes]) if (n.nodeType === 3 && n.textContent.trim()) {
        const run = document.createElement("span"); run.className = "ls-wrun"; n.replaceWith(run); run.appendChild(n);
      }
    }
    walk(node);
    node.dataset.lsSplit = "1";
    return inners;
  }
  function unsplit(node) {
    if (!node || !node.dataset.lsSplit) return;
    node.querySelectorAll(".ls-w").forEach((w) => {
      if (w.classList.contains("ls-wc")) return w.replaceWith(document.createTextNode(w.textContent));
      const i = w.firstChild; w.replaceWith(...(i ? i.childNodes : []));
    });
    node.querySelectorAll(".ls-wrun").forEach((r) => r.replaceWith(...r.childNodes));
    delete node.dataset.lsSplit; node.normalize();
  }
  function revealWords(nodes, startDelay) {
    let t = startDelay;
    nodes.forEach((node) => {
      // Headlines rise letter by letter; subtitles and other header text rise word by word.
      const chars = node.matches(HEADLINE);
      const words = splitWords(node, chars);
      if (!words.length) return;
      const step = chars ? Math.min(0.032, 0.5 / words.length) : Math.min(0.045, 0.42 / words.length);   // long text still finishes quickly
      words.forEach((w) => { w.style.transform = "translateY(110%)"; if (chars) w.style.opacity = "0"; });
      const anim = chars
        ? M.animate(words, { y: ["110%", "0%"], opacity: [0, 1] }, { duration: 0.6, ease: [0.22, 1, 0.36, 1], delay: M.stagger(step, { startDelay: t }) })
        : M.animate(words, { y: ["110%", "0%"] }, { duration: 0.55, ease: [0.22, 1, 0.36, 1], delay: M.stagger(step, { startDelay: t }) });
      const done = () => unsplit(node);
      (anim.finished || anim).then(done, done);
      setTimeout(() => { if (node.dataset.lsSplit) done(); }, (t + step * words.length + 0.6) * 1000 + 400);
      t += Math.min(0.18, step * words.length * 0.5);
    });
  }
  function enterPanel(p) {
    if (!motionOn || !p) return;
    windowUntil = performance.now() + 1500;
    const head = p.querySelector(HEAD);
    const texts = head ? [...head.querySelectorAll(TEXT)].filter((e) => e.offsetParent && !e.parentElement.closest(TEXT)) : [];
    const headRest = head ? [...head.querySelectorAll(".mprog, .row, .next-btns, .next")]
      .filter((e) => e.offsetParent && !e.parentElement.closest(".next, .row, .next-btns")) : [];
    const blocks = [...p.children].filter((e) => e.offsetParent && !e.matches(HEAD));
    const cards = [...p.querySelectorAll(".card .card, .grid > .card, .tiles > .tile, #ov-kpis > *, #ov-charts > *, .exc, .pfc, .ag-card, .agent, #rep-cards > .card, #ev-summary > .card, #attack-grid > .card")]
      .filter((e) => e.offsetParent).slice(0, 18);
    const nested = new Set(cards);
    const outer = blocks.filter((b) => !nested.has(b));
    const all = [...headRest, ...outer, ...cards];
    revealWords(texts, 0);                                      // 1. title, then subtitle, word by word
    headRest.forEach((e) => { e.style.opacity = "0"; });          // 2. header actions
    settle(headRest, M.animate(headRest, { opacity: [0, 1], y: [14, 0], filter: ["blur(6px)", "blur(0px)"] },
      { duration: 0.5, ease: [0.22, 1, 0.36, 1], delay: M.stagger(0.06, { startDelay: 0.22 }) }));
    [...outer, ...cards].forEach((e) => { e.style.opacity = "0"; });   // 3. blocks, then the cards inside them
    settle(outer, M.animate(outer, { opacity: [0, 1], y: [28, 0] }, { ...spring(0.55, 0.1), delay: M.stagger(0.06, { startDelay: 0.24 }) }));
    if (cards.length) settle(cards, M.animate(cards, { opacity: [0, 1], y: [22, 0], scale: [0.985, 1] }, { ...spring(0.55, 0.1), delay: M.stagger(0.04, { startDelay: 0.3 }) }));
    p.querySelectorAll(ENTER).forEach((e) => { e.dataset.entered = "1"; });
    settle(all.filter((e) => e.style), null);
  }
  // Animate whenever a page becomes active, however it was opened (sidebar, links, Back/Forward, Ctrl+K results).
  // A MutationObserver fires after the click handler has finished rendering the page, before the browser paints.
  document.querySelectorAll(".panel").forEach((pnl) => {
    let was = pnl.classList.contains("active");
    new MutationObserver(() => { const now = pnl.classList.contains("active"); if (now && !was) enterPanel(pnl); was = now; })
      .observe(pnl, { attributes: true, attributeFilter: ["class"] });
  });
  window.LSMotion = { enter: enterPanel };
  // UI-8: hide the floating Ask button while the page scrolls down; bring it back on scroll up or at the end
  (function () {
    let lastY = window.scrollY;
    window.addEventListener("scroll", () => {
      const y = window.scrollY, atEnd = window.innerHeight + y >= document.documentElement.scrollHeight - 8;
      document.body.classList.toggle("fab-away", y > lastY + 4 && y > 120 && !atEnd);
      if (y < lastY - 4 || atEnd) document.body.classList.remove("fab-away");
      lastY = y;
    }, { passive: true });
  })();
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
    if (!area || !(area.bottom > area.top)) return color;
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
            borderRadius: 20, borderSkipped: false, stack: "s", barPercentage: 0.62, categoryPercentage: 0.7, order: 1, cents: inC },
          { type: "bar", label: "Money out", data: outC.map((v) => -v / 100), backgroundColor: vgrad(col.bad, 0.85, 0.4), hoverBackgroundColor: col.bad,
            borderRadius: 20, borderSkipped: false, stack: "s", barPercentage: 0.62, categoryPercentage: 0.7, order: 2, cents: outC.map((v) => -v) },
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
      const { chart } = ctx, a = chart.chartArea; if (!a || !(a.bottom > a.top)) return color;
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
          const { chart } = ctx, a = chart.chartArea; if (!a || !(a.bottom > a.top)) return col.accent;
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
