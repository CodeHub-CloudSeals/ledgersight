// Login page motion: spring entrances and word reveal (2026-11-12: the card tilt was removed; the page follows the app's calm style).
// Plain Web Animations API (the CSP allows only same-origin scripts). Honours prefers-reduced-motion.
(function () {
  "use strict";
  const root = document.getElementById("login");
  if (!root || !root.animate) return;
  const reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  // Spring curve (stiffness ~170, damping ~18) sampled into a CSS linear() easing, like Framer Motion's default.
  const SPRING = CSS.supports && CSS.supports("animation-timing-function", "linear(0, 1)")
    ? "linear(0, 0.0234, 0.0874 3.1%, 0.3445 7%, 0.6327 11.4%, 0.8628 15.8%, 1.0082 20.3%, 1.0835 24.8%, 1.1003 28.6%, 1.0905 32.6%, 1.0251 41.4%, 0.9971 49.4%, 0.9926 55.4%, 1.0004 74%, 1)"
    : "cubic-bezier(.2,.9,.25,1.15)";
  let started = false;
  if (!reduce) root.classList.add("m-pre");  // hide animated parts until their entrance runs (no flash)
  const settle = (a) => a.finished.then(() => a.cancel()).catch(() => {});  // hand control back to CSS / tilt

  // "Hello world" -> <span class=w>Hello</span> <span class=w>world</span>, once.
  root.querySelectorAll(".m-words").forEach((h) => {
    if (h.dataset.split) return;
    h.dataset.split = "1";
    const lines = h.querySelectorAll(".ln").length ? [...h.querySelectorAll(".ln")] : [h];
    lines.forEach((ln) => {
      const words = ln.textContent.trim().split(/\s+/);
      ln.textContent = "";
      words.forEach((w, i) => {
        const s = document.createElement("span"); s.className = "w"; s.textContent = w;
        ln.appendChild(s); if (i < words.length - 1) ln.appendChild(document.createTextNode(" "));
      });
    });
  });

  function enter() {
    if (started) return; started = true;
    if (reduce) { root.classList.remove("m-pre"); root.classList.add("m-done"); countUp(true); feed(true); return; }
    const card = document.getElementById("lg-card");
    settle(card.animate([{ opacity: 0, transform: "translateY(28px) scale(.96)", filter: "blur(6px)" },
      { opacity: 1, transform: "none", filter: "blur(0)" }], { duration: 900, easing: SPRING, fill: "both" }));
    const items = [...root.querySelectorAll(".m-in, .m-words .w")];
    items.forEach((n, i) => settle(n.animate([{ opacity: 0, transform: "translateY(14px)" }, { opacity: 1, transform: "none" }],
      { duration: 800, delay: 120 + i * 45, easing: SPRING, fill: "both" })));
    root.classList.remove("m-pre");
    setTimeout(() => root.classList.add("m-done"), 400);
    setTimeout(() => countUp(false), 700);
    setTimeout(() => feed(false), 1100);
  }

  function countUp(now) {
    root.querySelectorAll("[data-count]").forEach((b) => {
      const to = +b.dataset.count;
      if (now) { b.textContent = to; return; }
      const t0 = performance.now(), dur = 1200;
      (function step(t) {
        const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
        b.textContent = Math.round(to * e);
        if (k < 1) requestAnimationFrame(step);
      })(t0);
    });
  }

  // A rolling feed of TrustOps decisions, the newest sliding in on top.
  const EVENTS = [
    ["ALLOW", "Agent reads the bank statement", "POL-02"],
    ["ESCALATE", "Missing GL sent to CPA review", "POL-05"],
    ["DENY", "Agent tried to post to the GL", "POL-03"],
    ["ALLOW", "10 transactions matched", "POL-02"],
    ["DENY", "Self-approval blocked", "POL-06"],
    ["ESCALATE", "Sales tax item sent to review", "POL-11"],
  ];
  let fi = 0, feedTimer = null;
  function chip(e) {
    const d = document.createElement("div"); d.className = "lv-ev";
    const b = document.createElement("span"); b.className = "lv-dec " + e[0]; b.textContent = e[0];
    const t = document.createElement("span"); t.className = "lv-txt"; t.textContent = e[1];
    const p = document.createElement("span"); p.className = "lv-pol"; p.textContent = e[2] + " · signed";
    d.append(b, t, p); return d;
  }
  function feed(now) {
    const box = document.getElementById("lv-feed");
    if (!box) return;
    const push = () => {
      if (root.classList.contains("hidden")) return;
      const n = chip(EVENTS[fi++ % EVENTS.length]);
      box.prepend(n);
      if (!reduce) n.animate([{ opacity: 0, transform: "translateY(-16px) scale(.97)" }, { opacity: 1, transform: "none" }], { duration: 700, easing: SPRING });
      const keep = box.classList.contains("one") ? 1 : 3;
      while (box.children.length > keep) box.lastElementChild.remove();
    };
    push(); if (!box.classList.contains("one")) { push(); push(); }
    if (!now && !feedTimer) feedTimer = setInterval(push, 2600);
  }

  // Cursor spotlight and depth parallax on the brand panel; gentle 3D tilt on the sign-in card.
  if (!reduce) {
    const spot = document.getElementById("lv-spot");
    const layers = [...root.querySelectorAll("[data-depth]")];
    let raf = 0, mx = 0.5, my = 0.5;
    root.addEventListener("pointermove", (ev) => {
      const r = root.getBoundingClientRect(); mx = (ev.clientX - r.left) / r.width; my = (ev.clientY - r.top) / r.height;
      if (raf) return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        if (spot) { spot.style.transform = `translate(${mx * r.width - 300}px, ${my * r.height - 300}px)`; spot.style.opacity = "1"; }
        layers.forEach((l) => { const d = +l.dataset.depth; l.style.translate = `${(mx - 0.5) * d}px ${(my - 0.5) * d}px`; });
      });
    });
    root.addEventListener("pointerleave", () => { if (spot) spot.style.opacity = "0"; layers.forEach((l) => { l.style.translate = "0 0"; }); });
  }

  // Headline: the last word changes every 3 s (explainable. / audit-ready. / governed. / on time.). Letter by letter
  // and never overlapping: the old word's letters lift out one after another, then the line's width glides to the
  // new word, then its letters rise in one by one (like the app's page headlines). Fixed word under reduced motion;
  // paused while the login screen or the tab is hidden. Screen readers get one full sentence (.sr-only) instead.
  const rot = document.getElementById("lg-rot");
  if (rot && !reduce) {
    const words = rot.dataset.words.split("|");
    const make = (w) => { const s = document.createElement("span"); s.className = "lg-rw";
      for (const ch of w) { const c = document.createElement("span"); c.className = "lg-ch"; c.textContent = ch; s.appendChild(c); } return s; };
    const first = rot.querySelector(".lg-rw"); if (first) first.replaceWith(make(first.textContent));
    let wi = 0, busy = false;
    const OUT = "cubic-bezier(.55,0,.75,.2)", GLIDE = "cubic-bezier(.22,1,.36,1)";
    async function swap() {
      if (busy || root.classList.contains("hidden") || document.hidden) return;
      busy = true;
      try {
        const old = rot.querySelector(".lg-rw"); wi = (wi + 1) % words.length;
        const letters = old ? [...old.children] : [];
        await Promise.all(letters.map((c, i) => c.animate([{ opacity: 1, transform: "none" }, { opacity: 0, transform: "translateY(-.4em)" }],
          { duration: 240, delay: i * 22, easing: OUT, fill: "forwards" }).finished));
        const from = rot.getBoundingClientRect().width, nw = make(words[wi]);
        if (old) old.replaceWith(nw); else rot.appendChild(nw);
        const to = rot.getBoundingClientRect().width;
        rot.animate([{ width: from + "px" }, { width: to + "px" }], { duration: 380, easing: GLIDE });
        [...nw.children].forEach((c, i) => c.animate([{ opacity: 0, transform: "translateY(.5em)" }, { opacity: 1, transform: "none" }],
          { duration: 560, delay: 90 + i * 38, easing: SPRING, fill: "backwards" }));
      } catch (_) { /* an animation was cancelled; the next tick starts clean */ }
      busy = false;
    }
    setInterval(swap, 3000);
  }

  // The login screen is revealed by avocet.js removing "hidden"; start the show the moment it appears.
  if (!root.classList.contains("hidden")) enter();
  new MutationObserver(() => { if (!root.classList.contains("hidden")) enter(); })
    .observe(root, { attributes: true, attributeFilter: ["class"] });
})();
