"use strict";
const $ = (id) => document.getElementById(id);
const S = { ws: null, run: null, me: null, wsList: [], clicks: 0, upload: null, asks: [] };

function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c instanceof Node ? c : String(c));
  return e;
}
const put = (id, ...kids) => { const n = typeof id === "string" ? $(id) : id; n.replaceChildren(...kids.flat().filter((k) => k != null)); };
const usd = (c) => (c < 0 ? "-" : "") + "$" + (Math.abs(c) / 100).toLocaleString("en-US", { minimumFractionDigits: 2 });
// Readable names on screen. Display only: the technical codes stay unchanged in config, the audit trail and exports.
const ACRONYM = new Set(["GL", "AR", "AP", "CPA", "CSV", "JSON", "PDF", "OK", "ID", "AI", "LLM", "SHA", "URL", "MFA", "KYC", "OCR",
  "QBO", "OFX", "QFX", "XLSX", "TSV", "EML", "UTC", "API", "SSO", "US", "SAP"]);
const titleCase = (v) => String(v ?? "").replace(/[._:\-]+/g, " ").trim().split(/\s+/).filter(Boolean)
  .map((w) => ACRONYM.has(w.toUpperCase()) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(" ");
const nice = (v) => (v == null || v === "" ? "" : titleCase(String(v).replace(/_cents$/, "")));  // amounts are shown in dollars, so drop "cents"
// Typography for server sentences (display only; the text itself is unchanged):
// sentence case, amounts as bold $1,234.56, record IDs (HB005, GL-H-104, INV-1005, EX-001, R3_AR_PARTIAL) in semibold.
const RICH_RE = /(-?\$[\d,]+\.\d{2})|((?<![\w.$\/-])-?\d+(?:,\d{3})*\.\d{2}(?![\d.%]|\s?(?:ms|s)\b))|(\b[A-Z]{2,5}(?:-[A-Z]{1,3})*-?\d{2,}[A-Za-z0-9]*\b|\b[A-Z]{2,4}-[a-f0-9]{6,}\b|\bR\d+_[A-Z_]+\b)/g;
function rich(text, sentence = true) {
  let t = String(text ?? "");
  if (sentence) t = t.replace(/^(\s*)([a-z])/, (_, a, c) => a + c.toUpperCase());
  const out = []; let i = 0;
  for (const m of t.matchAll(RICH_RE)) {
    if (m.index > i) out.push(t.slice(i, m.index));
    if (m[1]) out.push(el("b", { class: "money" }, m[1]));
    else if (m[2]) out.push(el("b", { class: "money" }, usd(Math.round(parseFloat(m[2].replace(/,/g, "")) * 100))));
    else out.push(el("span", { class: "rid" }, m[3]));
    i = m.index + m[0].length;
  }
  if (i < t.length) out.push(t.slice(i));
  return out;
}
// Names typed all in lowercase ("cloud seals", "ai tech") are shown capitalised ("Cloud Seals", "AI Tech").
// Anything typed with its own capitals ("eBay", "McKinsey") is shown exactly as typed. The saved value never changes.
const proper = (v) => { const t = String(v ?? ""); if (!/[a-z]/.test(t) || t !== t.toLowerCase()) return t;
  return t.replace(/[a-z0-9]+/g, (w) => ACRONYM.has(w.toUpperCase()) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1)); };
const niceList = (v) => /^[a-z0-9_.]+(, ?[a-z0-9_.]+)*$/.test(String(v || "")) ? String(v).split(/, ?/).map(nice).join(", ") : v;
function niceActor(id) {
  const a = S.agents && S.agents.agents.find((x) => x.actor_id === id);
  if (a) return `${a.name} Agent`;
  return String(id ?? "").startsWith("agent:") ? `${nice(String(id).slice(6))} Agent` : id;
}
function niceDecision(d) { const [k, ...r] = String(d ?? "").split(":"); return r.length ? `${k} · ${nice(r.join(":"))}` : k; }
const msg = (id, text, kind) => { const n = $(id); n.textContent = text || ""; n.className = "msg " + (kind || ""); };

async function api(path, opts = {}) {
  const o = { method: opts.method || "GET", headers: { "x-ledgersight": "1" }, credentials: "same-origin" };
  if (opts.json !== undefined) { o.headers["content-type"] = "application/json"; o.body = JSON.stringify(opts.json); }
  if (opts.form) o.body = opts.form;
  let r;
  try { r = await fetch(path, o); }
  catch (_) {
    if (!S.offlineShown) { S.offlineShown = true; toast("Can't reach the LedgerSight server. Check that it is running, then try again.", "err"); setTimeout(() => { S.offlineShown = false; }, 8000); }
    const err = new Error("server unreachable"); err.status = 0; throw err;
  }
  let body = null;
  try { body = await r.json(); } catch (_) {}
  if (r.status === 401 && path !== "/v1/login") { showLogin(); }
  if (!r.ok) {
    const d = body && body.detail;
    const text = typeof d === "string" ? d : d && d.reason_code ? `DENY · ${nice(d.reason_code)}${d.policy_id ? " · " + d.policy_id : ""}` : `HTTP ${r.status}`;
    const err = new Error(text); err.status = r.status; err.body = body; throw err;
  }
  if (o.method === "POST" && S.ws && path.startsWith("/v1/workspaces")) setTimeout(progress, 0);
  return body;
}
const W = (p = "") => `/v1/workspaces/${S.ws}${p}`;

// ---------------- auth ----------------
function showLogin() { $("app").classList.add("hidden"); $("login").classList.remove("hidden"); }
const authConfig = fetch("/v1/auth-config").then((r) => r.json()).then(async (c) => {
  S.mfa = !!c.mfa; $("lg-code-row").classList.toggle("hidden", !S.mfa);
  const onlyGoogle = !!(c.google_login && c.google_client_id);
  $("lg-pwform").classList.toggle("hidden", onlyGoogle);
  $("lg-divider").classList.toggle("hidden", onlyGoogle);
  $("lg-hint").textContent = onlyGoogle ? "Sign in with your work Google account." :
    S.mfa ? "Invitation-only. Password plus authenticator code." : "Invitation-only access.";
  S.noLogin = c.login_required === false; S.demoPeople = c.demo_people || [];
  if (c.version) $("app-version").textContent = `LedgerSight v${c.version}`;
  if (onlyGoogle) await setUpGoogleSignIn(c.google_client_id);
}).catch(() => {});

async function onGoogleCredential(resp) {
  msg("lg-msg", "Signing in…");
  try {
    await api("/v1/login/google", { method: "POST", json: { credential: resp.credential } });
    await boot();
  } catch (e) { msg("lg-msg", e.message, "err"); }
}
async function setUpGoogleSignIn(clientId) {
  try {
    await loadScript("https://accounts.google.com/gsi/client");
    google.accounts.id.initialize({ client_id: clientId, callback: onGoogleCredential });
    google.accounts.id.renderButton($("g_id_button"), { theme: "outline", size: "large", shape: "rectangular", logo_alignment: "center", width: Math.max(220, Math.min(400, ($("lg-card").clientWidth || 440) - 62)) });
    $("lg-google").classList.remove("hidden");
  } catch (_) { /* password sign-in still works without this */ }
}
$("act-as").onchange = async (e) => {
  await api("/v1/demo/act-as", { method: "POST", json: { as: e.target.value } });
  await boot();
};
$("lg-btn").onclick = async () => {
  msg("lg-msg", "Signing in…");
  try {
    await api("/v1/login", { method: "POST", json: { user_id: $("lg-user").value.trim(), password: $("lg-pw").value, code: $("lg-code").value.trim() } });
    $("lg-pw").value = ""; $("lg-code").value = "";
    await boot();
  } catch (e) { msg("lg-msg", e.message, "err"); }
};
["lg-user", "lg-pw", "lg-code"].forEach((id) => $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") $("lg-btn").click(); }));
$("logout").onclick = async () => { await api("/v1/logout", { method: "POST" }); location.reload(); };

async function boot() {
  await authConfig;
  try { S.me = await api("/v1/me"); } catch (_) { showLogin(); return; }
  $("login").classList.add("hidden"); $("app").classList.remove("hidden");
  $("logout").classList.toggle("hidden", !!S.noLogin);
  $("me-name").parentElement.classList.toggle("hidden", !!S.noLogin);
  $("act-as-wrap").classList.toggle("hidden", !S.noLogin);
  if (S.noLogin) {
    const key = (S.demoPeople.find((p) => p.role === S.me.role) || {}).key;
    put("act-as", S.demoPeople.map((p) => el("option", { value: p.key, selected: p.key === key }, p.name)));
  }
  const role = ROLE_NAME[S.me.role] || S.me.role;
  $("me-name").textContent = S.me.display_name; $("me-name").parentElement.title = `${S.me.user_id} · ${role}`;
  $("avatar").title = `${S.me.display_name} · ${role}`;
  $("avatar").textContent = S.me.display_name.split(/[\s@.()]+/).filter(Boolean).slice(0, 2).map((x) => x[0].toUpperCase()).join("");
  S.wsList = await api("/v1/workspaces");
  put("ws-select", S.wsList.map((w) => el("option", { value: w.id }, proper(w.name))));
  S.ws = S.wsList[0] && S.wsList[0].id;
  document.querySelector('[data-tab="team"]').classList.toggle("hidden", S.me.role !== "owner_reviewer" || !!S.noLogin);
  await switchWs();
  const fromHash = location.hash.slice(1);
  if (fromHash === "ask") openAsk();
  else if (fromHash && document.getElementById("p-" + fromHash)) go(fromHash);
}
const ROLE_NAME = { owner_reviewer: "CPA reviewer", preparer: "Staff preparer", support: "Support" };
$("ws-select").onchange = async (e) => { S.ws = e.target.value; await switchWs(); };

async function switchWs() {
  S.run = null; S.agents = null; S.anim = null; renderPipelines(); resetIntakeUi();
  const w = await api(W());
  S.wsMeta = w;
  renderStatus(w); renderHealth(w.uploads); renderDocs(w.documents); renderHeld(w.held);
  if (w.runs.length) S.run = await api(W(`/runs/${w.runs[w.runs.length - 1].id}`));
  renderRun(); renderExceptions(); renderControls(); await renderEvidence(); await renderTrust();
  S.asks = S.asks.filter((x) => x.ws === S.ws); renderAskLog(); if (S.askOpen) renderAsk(); if (isActive("reports")) renderReports();
  if (S.me.role === "owner_reviewer" && !S.noLogin) renderTeam();
  fillProfile(w);
  await progress();
}

// ---------------- progress: stepper + overview ----------------
S.exported = (() => { try { return JSON.parse(localStorage.getItem("ls-exported") || "{}"); } catch (_) { return {}; } })();
function markExported() {  // the Report step is done once this run's report has been downloaded (remembered in this browser)
  if (!S.run) return;
  S.exported[S.ws + S.run.id] = true;
  try { localStorage.setItem("ls-exported", JSON.stringify(S.exported)); } catch (_) {}
  setTimeout(progress, 300);
}
const STEPS = [["Import", "import"], ["Confirm columns", "import"], ["Reconcile", "recon"], ["Review", "exceptions"], ["Report", "reports"]];
async function progress() {
  S.wsList = await api("/v1/workspaces");
  const w = S.wsList.find((x) => x.id === S.ws); if (!w) return;
  const st = w.status;
  const done = [st.uploads > 0, st.bank && st.ledger, st.runs > 0, st.runs > 0 && st.open_exceptions === 0, !!S.exported[S.ws + st.last_run]];
  const cur = done.indexOf(false);
  const kids = [];
  STEPS.forEach(([name, tab], i) => {
    if (i) kids.push(el("span", { class: "bar" + (done[i - 1] ? " done" : "") }));
    kids.push(el("button", { class: "step" + (done[i] ? " done" : i === cur ? " cur" : ""), onclick: () => go(tab) },
      el("span", { class: "dot" }, done[i] ? "✓" : String(i + 1)), name));
  });
  put("stepper", kids);
  const nr = $("nav-review"); nr.textContent = st.open_exceptions || ""; nr.classList.toggle("hidden", !st.open_exceptions);
  // Nothing to clear -> the clear buttons are off (they come back as soon as a file is imported or a run exists).
  const hasData = st.uploads > 0 || st.runs > 0;
  $("clear-all").disabled = !hasData; $("clear-all").title = hasData ? "" : "Nothing imported yet";
  $("reset-btn").disabled = S.me.role !== "owner_reviewer" || !hasData; $("reset-btn").title = hasData ? "" : "Nothing to clear yet";
  $("page-sub").textContent = proper(w.period || "");  // top bar: client + period only
  const opt = [...$("ws-select").options].find((o) => o.value === w.id); if (opt) opt.textContent = proper(w.name);
  S.status = st;
  renderReadiness();
  renderOverview();
}
["exp-csv", "exp-pack", "exp-print"].forEach((id) => $(id).addEventListener("click", markExported));

// ---------------- dashboard visuals ----------------
// Presentation only. Every figure is read from the run and status already loaded (S.run, S.wsList, S.events);
// nothing here calculates money differently or invents data. Charts are plain SVG (no external libraries: CSP).
const REDUCE = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
const SVG_NS = "http://www.w3.org/2000/svg";
function sv(tag, attrs, ...kids) {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, v);
  for (const c of kids.flat()) if (c != null) e.append(c);
  return e;
}
// Numbers count up from their previous value (only when they change), ending on the exact formatted value.
S.kv = S.kv || {};
function counter(key, value, fmt) {
  const span = el("span", { class: "num" }, fmt(value));
  const from = S.kv[key]; S.kv[key] = value;
  if (REDUCE || from === value || typeof value !== "number") return span;
  const start = typeof from === "number" ? from : 0, t0 = performance.now(), dur = 750;
  span.textContent = fmt(start);
  (function step(t) {
    if (!span.isConnected && t - t0 > 50) { span.textContent = fmt(value); return; }
    const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
    span.textContent = k < 1 ? fmt(Math.round(start + (value - start) * e)) : fmt(value);
    if (k < 1) requestAnimationFrame(step);
  })(t0);
  return span;
}
const compactUsd = (c) => {
  const v = Math.abs(c) / 100, s = c < 0 ? "-" : "";
  return v >= 1e6 ? `${s}$${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${s}$${(v / 1e3).toFixed(v >= 1e4 ? 0 : 1)}k` : `${s}$${Math.round(v)}`;
};
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const shortDate = (d) => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d || ""); return m ? `${MON[+m[2] - 1]} ${+m[3]}` : d; };
const KPI_ICON = {
  sources: "M4 7c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 7v5c0 1.7 3.6 3 8 3s8-1.3 8-3V7M4 12v5c0 1.7 3.6 3 8 3s8-1.3 8-3v-5",
  matched: "M9 12.5l2 2 4-4.5M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z",
  review: "M3 13h5l2 3h4l2-3h5M5.5 5h13l2.5 8v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6z",
  variance: "M12 3v18M5 8l-3 6h6zM19 8l-3 6h6zM5 8h14M8 21h8",
  unexplained: "M12 3l9 16H3zM12 10v4M12 17h.01",
};
function kpiIcon(name) {
  return sv("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
    sv("path", { d: KPI_ICON[name] }));
}
function kpiCard({ icon, label, value, tone, sub, viz, extra, wide, cls }) {
  return el("div", { class: "kw" + (tone ? " " + tone : "") + (wide ? " kw-wide" : "") + (cls ? " " + cls : "") },
    el("div", { class: "kw-top" }, el("span", { class: "kw-k" }, label), el("span", { class: "kw-ico" }, kpiIcon(icon))),
    el("div", { class: "kw-mid" }, el("div", { class: "kw-v" }, value), viz ? el("div", { class: "kw-viz" }, viz) : null),
    extra || null,
    sub ? el("div", { class: "kw-s" }, sub) : null);
}
// small radial progress (0..1)
function ring(frac, cls) {
  const r = 15, c = 2 * Math.PI * r, f = Math.max(0, Math.min(1, frac || 0));
  const arc = sv("circle", { cx: 18, cy: 18, r, class: "rg-v " + (cls || ""), "stroke-dasharray": `${c * f} ${c}`, transform: "rotate(-90 18 18)" });
  if (!REDUCE) arc.style.setProperty("--len", String(c));
  return sv("svg", { class: "ring", viewBox: "0 0 36 36", "aria-hidden": "true" }, sv("circle", { cx: 18, cy: 18, r, class: "rg-t" }), arc);
}
function sparkline(values, cls) {
  if (!values || values.length < 2) return null;
  const W = 120, H = 34, min = Math.min(...values, 0), max = Math.max(...values, 0), span = max - min || 1;
  const pts = values.map((v, i) => [i / (values.length - 1) * W, H - 3 - (v - min) / span * (H - 6)]);
  const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const zero = H - 3 - (0 - min) / span * (H - 6);
  return sv("svg", { class: "spark " + (cls || ""), viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" },
    sv("line", { x1: 0, x2: W, y1: zero, y2: zero, class: "sp-zero" }),
    sv("path", { d: d + ` L${W} ${zero} L0 ${zero} Z`, class: "sp-area" }),
    sv("path", { d, class: "sp-line", pathLength: "1" }),
    sv("circle", { cx: pts[pts.length - 1][0], cy: pts[pts.length - 1][1], r: 2.6, class: "sp-dot" }));
}
// Running (bank minus ledger) by date, but only if it adds up to the run's own variance; otherwise no sparkline.
function varianceSeries(run) {
  const r = run.result, lines = Object.values(run.lines);
  const sum = (k) => lines.filter((l) => l.kind === k).reduce((s, l) => s + l.amount_cents, 0);
  if (sum("bank") !== r.bank_net_cents || sum("ledger") !== r.ledger_net_cents) return null;
  const byDate = {};
  lines.forEach((l) => { if (l.kind === "bank" || l.kind === "ledger") byDate[l.date] = (byDate[l.date] || 0) + (l.kind === "bank" ? l.amount_cents : -l.amount_cents); });
  const dates = Object.keys(byDate).sort();
  let cum = 0; const out = dates.map((d) => (cum += byDate[d]));
  out.dates = dates;
  return out.length && out[out.length - 1] === r.variance_cents ? out : null;
}

// Cash movement: money in and money out on the bank statement, per day (from the imported bank lines).
function cashFlowChart(run) {
  const bank = Object.values(run.lines).filter((l) => l.kind === "bank");
  if (!bank.length) return empty("No bank transactions in this run", "");
  const days = {};
  bank.forEach((l) => { const d = days[l.date] = days[l.date] || { in: 0, out: 0, n: 0 }; if (l.amount_cents >= 0) d.in += l.amount_cents; else d.out += -l.amount_cents; d.n++; });
  const keys = Object.keys(days).sort();
  const totIn = keys.reduce((s, k) => s + days[k].in, 0), totOut = keys.reduce((s, k) => s + days[k].out, 0);
  const W = 720, H = 230, L = 54, R = 12, T = 14, B = 30, pw = W - L - R, ph = H - T - B;
  let acc = 0; const cum = keys.map((k) => (acc += days[k].in - days[k].out));  // running net on the statement; ends at bank net
  const maxIn = Math.max(...keys.map((k) => days[k].in), ...cum, 0), maxOut = Math.max(...keys.map((k) => days[k].out), ...cum.map((v) => -v), 0);
  const tot = (maxIn + maxOut) || 1, y0 = T + ph * (maxIn / tot), sc = ph / tot;
  const slot = pw / keys.length, bw = Math.max(4, Math.min(16, slot * 0.32));
  const g = sv("g", {});
  [maxIn, -maxOut].forEach((v) => { if (!v) return; const y = y0 - v * sc;
    g.append(sv("line", { x1: L, x2: W - R, y1: y, y2: y, class: "cf-grid" }), sv("text", { x: L - 8, y: y + 4, class: "cf-ax", "text-anchor": "end" }, compactUsd(v))); });
  g.append(sv("line", { x1: L, x2: W - R, y1: y0, y2: y0, class: "cf-zero" }), sv("text", { x: L - 8, y: y0 + 4, class: "cf-ax", "text-anchor": "end" }, "$0"));
  const ticks = new Set([0, keys.length - 1, Math.round((keys.length - 1) / 2)]);
  keys.forEach((k, i) => {
    const cx = L + slot * i + slot / 2, d = days[k];
    if (d.in) { const b = sv("rect", { x: cx - bw - 1, y: y0 - d.in * sc, width: bw, height: Math.max(1, d.in * sc), rx: 3, class: "cf-in" }); b.style.animationDelay = `${i * 35}ms`; g.append(b); }
    if (d.out) { const b = sv("rect", { x: cx + 1, y: y0, width: bw, height: Math.max(1, d.out * sc), rx: 3, class: "cf-out" }); b.style.animationDelay = `${i * 35 + 60}ms`; g.append(b); }
    if (ticks.has(i)) g.append(sv("text", { x: cx, y: H - 8, class: "cf-ax", "text-anchor": "middle" }, shortDate(k)));
    const hit = sv("rect", { x: L + slot * i, y: T, width: slot, height: ph, class: "cf-hit" });
    hit.dataset.i = i; g.append(hit);
  });
  const pts = keys.map((k, i) => [L + slot * i + slot / 2, y0 - cum[i] * sc]);
  g.append(sv("path", { d: pts.map((q, i) => (i ? "L" : "M") + q[0].toFixed(1) + " " + q[1].toFixed(1)).join(" "), class: "cf-net", pathLength: "1" }));
  pts.forEach((q) => g.append(sv("circle", { cx: q[0].toFixed(1), cy: q[1].toFixed(1), r: 2.6, class: "cf-net-dot" })));
  g.querySelectorAll(".cf-hit").forEach((h) => g.append(h));  // hover targets stay on top
  const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, class: "cf-svg", role: "img", "aria-label": `Bank money in ${usd(totIn)} and money out ${usd(totOut)} by day` }, g);
  const tip = el("div", { class: "cf-tip", "aria-hidden": "true" });
  const wrap = el("div", { class: "cf-wrap" }, svg, tip);
  svg.addEventListener("pointermove", (ev) => {
    const h = ev.target.closest && ev.target.closest(".cf-hit"); if (!h) { tip.classList.remove("on"); return; }
    const k = keys[+h.dataset.i], d = days[k];
    put(tip, el("b", {}, shortDate(k)), el("span", { class: "in" }, `In ${usd(d.in)}`), el("span", { class: "out" }, `Out ${usd(d.out)}`),
      el("span", {}, `Running net ${usd(cum[+h.dataset.i])}`), el("span", {}, `${d.n} transaction${d.n === 1 ? "" : "s"}`));
    const box = wrap.getBoundingClientRect(), hb = h.getBoundingClientRect();
    tip.style.left = `${Math.min(box.width - 150, Math.max(0, hb.left - box.left + hb.width / 2 - 70))}px`;
    tip.classList.add("on");
    g.querySelectorAll(".cf-hit.on").forEach((x) => x.classList.remove("on")); h.classList.add("on");
  });
  svg.addEventListener("pointerleave", () => { tip.classList.remove("on"); g.querySelectorAll(".cf-hit.on").forEach((x) => x.classList.remove("on")); });
  const viz = window.LSCharts ? LSCharts.flow({ keys, days, cum, label: svg.getAttribute("aria-label"), fmtAxis: compactUsd, fmtDate: shortDate, usd }) : wrap;
  return el("div", {},
    el("div", { class: "cf-stats" },
      el("div", {}, el("span", { class: "lg in" }), el("span", { class: "k" }, "Money in"), el("b", {}, usd(totIn))),
      el("div", {}, el("span", { class: "lg out" }), el("span", { class: "k" }, "Money out"), el("b", {}, usd(totOut))),
      el("div", {}, el("span", { class: "lg net" }), el("span", { class: "k" }, "Running net"), el("b", {}, usd(run.result.bank_net_cents))),
      el("div", {}, el("span", { class: "k" }, "Days with activity"), el("b", {}, String(keys.length)))),
    viz);
}

// Match coverage: bank transactions matched, raised for review, or neither; plus bank vs ledger totals.
function coverageChart(run) {
  const r = run.result;
  const bankIds = Object.values(run.lines).filter((l) => l.kind === "bank").map((l) => l.id);
  if (!bankIds.length) return empty("No bank transactions in this run", "");
  const matchedIds = new Set(r.matches.map((m) => m.bank_id));
  const matched = r.matches.length;
  const review = Math.min(Math.max(0, bankIds.length - matched), new Set(r.exceptions.map((x) => x.bank_id).filter((id) => id && !matchedIds.has(id))).size);
  const other = Math.max(0, bankIds.length - matched - review);
  const segs = [["Matched", matched, "seg-m"], ["Raised for review", review, "seg-r"], ["Not matched", other, "seg-o"]].filter((s) => s[1] > 0);
  const total = bankIds.length, R = 52, C = 2 * Math.PI * R;
  let acc = 0;
  const arcs = segs.map(([, n, cls], i) => {
    const len = n / total * C, a = sv("circle", { cx: 64, cy: 64, r: R, class: "dn " + cls, "stroke-dasharray": `${Math.max(0, len - 2)} ${C}`, "stroke-dashoffset": String(-acc), transform: "rotate(-90 64 64)" });
    a.style.animationDelay = `${i * 120}ms`; acc += len; return a;
  });
  const pct = Math.round(matched / total * 100);
  const donut = el("div", { class: "dn-wrap" },
    window.LSCharts ? LSCharts.donut({ segs, total, label: `${matched} of ${total} bank transactions matched` }) :
    sv("svg", { viewBox: "0 0 128 128", class: "dn-svg", role: "img", "aria-label": `${matched} of ${total} bank transactions matched` },
      sv("circle", { cx: 64, cy: 64, r: R, class: "dn-track" }), arcs),
    el("div", { class: "dn-mid" }, el("b", {}, counter("cov-pct", pct, (n) => `${n}%`)), el("span", {}, "matched")));
  const legend = el("ul", { class: "dn-legend" }, segs.map(([k, n, cls]) =>
    el("li", {}, el("span", { class: "lg " + cls }), el("span", { class: "k" }, k), el("b", {}, String(n)))),
    el("li", { class: "tot" }, el("span", { class: "k" }, "Bank transactions"), el("b", {}, String(total))));
  const mx = Math.max(Math.abs(r.bank_net_cents), Math.abs(r.ledger_net_cents), 1);
  const bar = (k, v, cls) => el("div", { class: "nb" }, el("div", { class: "nb-k" }, el("span", {}, k), el("b", {}, usd(v))),
    el("div", { class: "nb-t" }, (() => { const f = el("i", { class: cls }); f.style.width = `${Math.abs(v) / mx * 100}%`; return f; })()));
  return el("div", {},
    el("div", { class: "dn-row" }, donut, legend),
    el("div", { class: "nb-box" }, bar("Bank net", r.bank_net_cents, "b"), bar("Ledger net", r.ledger_net_cents, "l"),
      el("div", { class: "nb-sum" }, el("span", {}, "Variance ", el("b", {}, usd(r.variance_cents))),
        el("span", {}, "Unexplained ", el("b", { class: r.unexplained_variance_cents ? "bad" : "good" }, usd(r.unexplained_variance_cents))))));
}

// ---------------- overview dashboard ----------------
function greeting() { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; }
async function renderOverview() {
  const w = S.wsList.find((x) => x.id === S.ws); if (!w) return;
  const st = w.status;
  const state = !st.uploads ? ["Awaiting files", ""] : !st.runs ? ["Files imported", "SIMULATED"] : st.open_exceptions ? [`${st.open_exceptions} to review`, "OPEN"] : ["Ready to report", "ACCEPTED"];
  const items = (n) => `${n} item${n === 1 ? "" : "s"}`;
  let reviewStep = [`Review ${items(st.open_exceptions)}`, "exceptions"];
  const exc = S.run && S.run.id === st.last_run ? S.run.result.exceptions.filter((e) => e.status === "OPEN" || e.status === "RETURNED") : null;
  if (exc && exc.length) {
    if (S.me.role === "owner_reviewer") {
      const ready = exc.filter((e) => e.status === "OPEN").length;
      reviewStep = ready ? [`Review ${items(ready)}`, "exceptions"] : [`${items(exc.length)} returned to the preparer`, "exceptions"];
    } else {
      const todo = exc.filter((e) => e.status === "RETURNED" || !e.proposal_note).length;
      reviewStep = todo ? [`Propose resolutions for ${items(todo)}`, "exceptions"] : [`Waiting for a CPA reviewer (${items(exc.length)})`, "exceptions"];
    }
  }
  const next = !st.uploads ? ["Upload this period's files", "import"] : !(st.bank && st.ledger) ? ["Add the bank statement and ledger", "import"]
    : !st.runs ? ["Run reconciliation", "recon"] : st.open_exceptions ? reviewStep : ["Download the reports", "reports"];
  const done = st.runs && !st.open_exceptions;
  const r = S.run && S.run.result;
  const bankLines = S.run ? Object.values(S.run.lines).filter((l) => l.kind === "bank").length : 0;
  const pct = r && bankLines ? Math.round(r.matches.length / bankLines * 100) : null;
  // Hero: client, period, status, next step; ring = share of bank transactions matched automatically (same figure as the KPI).
  const R = 54, C = 2 * Math.PI * R, f = pct == null ? 0 : Math.min(1, pct / 100);
  put("ov-hero",
    el("div", { class: "hero-l" },
      el("div", { class: "greet" }, `${greeting()}, ${S.me.display_name}`),
      el("h2", {}, proper(w.name), el("span", { class: "badge " + state[1] }, state[0])),
      el("div", { class: "hero-meta" }, [w.industry, w.period].filter(Boolean).map(proper).join(" · ")),
      el("div", { class: "next" }, el("span", { class: "next-k" }, "Next step"),
        el("div", { class: "next-btns" },
          el("button", { class: "primary", id: "next-step", onclick: () => next[1] === "exceptions" ? openReview() : go(next[1]) }, next[0] + " →"),
          done && S.me.role === "owner_reviewer" ? el("button", { class: "secondary", id: "next-period", onclick: (e) => clearClient(e.currentTarget, true) }, "Start next period") : null))),
    el("div", { class: "hero-r" },
      el("div", { class: "hero-ring" },
        sv("svg", { viewBox: "0 0 128 128", "aria-hidden": "true" }, sv("circle", { cx: 64, cy: 64, r: R, class: "ht" }),
          pct ? sv("circle", { cx: 64, cy: 64, r: R, class: "hv", "stroke-dasharray": `${C * f} ${C}`, transform: "rotate(-90 64 64)" }) : null),
        el("div", { class: "hm" }, el("b", {}, pct == null ? "–" : counter("h-pct", pct, (n) => `${n}%`)), el("span", {}, pct == null ? "no run yet" : "matched automatically"))),
      el("ul", { class: "hero-stats" },
        el("li", {}, el("span", {}, "Bank transactions"), el("b", {}, r ? String(bankLines) : "–")),
        el("li", {}, el("span", {}, "Matched"), el("b", {}, r ? String(r.matches.length) : "–")),
        el("li", {}, el("span", {}, "Open for review"), el("b", {}, r ? String(st.open_exceptions) : "–")))));
  const nSrc = SOURCE_KINDS.filter((k) => st[k]).length;
  const decided = r ? r.exceptions.length - st.open_exceptions : 0;
  const vSeries = r && S.run.id === st.last_run ? varianceSeries(S.run) : null;
  const progBar = () => { const t = r.exceptions.length || 1, pt = el("div", { class: "pt" }), d = el("i", { class: "d" }), o = el("i", { class: "o" });
    d.style.width = `${decided / t * 100}%`; o.style.width = `${st.open_exceptions / t * 100}%`; if (decided) pt.append(d); if (st.open_exceptions) pt.append(o);
    return el("div", { class: "prog" }, pt, el("div", { class: "pl" }, el("span", {}, el("b", {}, String(decided)), " decided"), el("span", {}, el("b", {}, String(st.open_exceptions)), " open"))); };
  const unx = r ? r.unexplained_variance_cents : null;
  put("ov-kpis",
    kpiCard({ icon: "matched", label: "Matched automatically", tone: r ? "good" : "",
      value: r ? counter("k-m", r.matches.length, String) : "–",
      sub: r ? `${pct}% of ${bankLines} bank transactions` : "no run yet", viz: r ? ring(pct / 100, "ok") : null }),
    kpiCard({ icon: "review", label: "Awaiting review", tone: r && st.open_exceptions ? "amb" : r ? "good" : "",
      value: r ? counter("k-o", st.open_exceptions, String) : "–", sub: r ? `${r.exceptions.length} raised in total` : "",
      extra: r && r.exceptions.length ? progBar() : null }),
    kpiCard({ icon: "variance", label: "Variance", wide: true, value: r ? counter("k-v", r.variance_cents, usd) : "–",
      sub: r ? "bank net minus ledger net" : "",
      extra: r ? el("div", {}, vSeries ? (window.LSCharts ? LSCharts.variance({ values: vSeries, dates: vSeries.dates, fmtDate: shortDate, usd })
          : (() => { const sp = sparkline(vSeries, ""); sp.setAttribute("class", "spark spark-lg"); return sp; })()) : null,
        el("div", { class: "kw-pair" }, el("span", {}, "Bank net", el("b", {}, usd(r.bank_net_cents))), el("span", {}, "Ledger net", el("b", {}, usd(r.ledger_net_cents))))) : null }),
    kpiCard({ icon: "unexplained", label: "Unexplained", tone: r ? (unx ? "bad" : "good") : "", cls: r ? "kw-status " + (unx ? "bad" : "good") : "",
      value: r ? counter("k-u", unx, usd) : "–", sub: r ? "after review items" : "",
      viz: r ? el("span", { class: "chip " + (unx ? "bad" : "good") }, unx ? "Needs attention" : "Fully explained") : null }),
    kpiCard({ icon: "sources", label: "Sources imported", tone: st.bank && st.ledger ? "good" : "",
      value: el("span", {}, counter("k-src", nSrc, String), ` / ${SOURCE_KINDS.length}`),
      sub: st.bank && st.ledger ? "bank and ledger present" : "bank and ledger needed",
      extra: el("div", { class: "segs lg", "aria-hidden": "true" }, SOURCE_KINDS.map((k) => el("i", { class: st[k] ? "on" : "", title: KIND[k] || k }))) }));
  const cur = r && S.run.id === st.last_run;
  $("ov-charts").classList.toggle("hidden", !cur);
  if (cur) { put("ov-flow", cashFlowChart(S.run)); put("ov-cover", coverageChart(S.run)); }
  renderCaseCards(r && S.run.id === st.last_run ? r : null);
  if (isActive("home")) { try { await renderEvidence(); } catch (_) { /* counts fall back to last loaded */ } }
  const ev = S.events || [];
  const cnt = (k) => ev.filter((e) => String(e.policy_decision).startsWith(k)).length;
  const govN = [["ALLOW", cnt("ALLOW"), "Allowed"], ["ESCALATE", cnt("ESCALATE"), "Sent to a person"], ["DENY", cnt("DENY"), "Blocked"]];
  const govT = govN.reduce((a, x) => a + x[1], 0);
  put("ov-gov",
    el("div", { class: "gv-bar", "aria-hidden": "true" }, govN.map(([k, n]) => { const i = el("i", { class: k }); i.style.flexGrow = String(n); return n ? i : null; })),
    el("ul", { class: "gv-list" }, govN.map(([k, n, label]) =>
      el("li", {}, el("span", { class: "badge " + k }, k), " ", el("b", { class: "gv-n" }, String(n)), el("span", { class: "gv-l" }, label),
        el("span", { class: "gv-p" }, govT ? `${Math.round(n / govT * 100)}%` : "–")))),
    el("div", { class: "gv-ev" }, el("span", {}, "Evidence chain"), el("span", { class: "badge " + (S.verify || "") }, S.verify || "–"),
      el("span", { class: "muted small" }, `${ev.length} signed event${ev.length === 1 ? "" : "s"}`)),
    el("div", { class: "muted small gv-note" }, "Every request by a person or an agent is checked by TrustOps first and signed into the audit trail."));
  const ROUTINE = new Set(["workspace.read", "export.read", "query.ask"]);
  put("ov-activity", ev.filter((e) => !ROUTINE.has(e.action)).slice(-7).reverse().map((e) => { const dec = String(e.policy_decision).split(":")[0];
    return el("li", { class: "tl " + dec }, el("span", { class: "tl-dot" }),
      el("div", { class: "tl-b" },
        el("div", { class: "tl-h" }, el("b", {}, e.agent_code || (e.actor_type === "human" ? "Person" : niceActor(e.actor_id))), ` ${nice(e.action)}`),
        el("div", { class: "tl-m" }, el("span", { class: "badge " + dec }, dec), e.policy_id ? el("span", { class: "rid" }, e.policy_id) : null, el("span", { class: "t" }, e.timestamp_utc.slice(11, 19)))));
  }));
  if (!ev.some((e) => !ROUTINE.has(e.action))) put("ov-activity", el("li", { class: "muted" }, "No activity yet. Upload files to start."));
  await loadAgents();
}
// Exceptions grouped by what the client sees. Every case is a link to its detail in the review queue.
const CASE_ORDER = ["UNMATCHED", "DUPLICATE", "PARTIAL", "TAX_REVIEW", "FX_REVIEW", "APPROVAL_REQUIRED"];
function renderCaseCards(r) {
  const box = $("ov-cases");
  if (!r || !r.exceptions.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const groups = {};
  r.exceptions.forEach((x) => { (groups[x.client_status || "APPROVAL_REQUIRED"] = groups[x.client_status || "APPROVAL_REQUIRED"] || []).push(x); });
  const keys = CASE_ORDER.filter((k) => groups[k]).concat(Object.keys(groups).filter((k) => !CASE_ORDER.includes(k)));
  const isOpen = (x) => x.status === "OPEN" || x.status === "RETURNED";
  put(box, el("div", { class: "card-title" }, "Cases to review ", el("span", { class: "muted small" }, "click a case to open its detail and source rows"),
      el("button", { class: "linkbtn", onclick: openReview }, "Review queue →")),
    el("div", { class: "case-grid" }, keys.map((k) => {
      const xs = groups[k], open = xs.filter(isOpen).length;
      return el("div", { class: "case-card" + (open ? " open" : ""), role: "button", tabindex: "0", title: "Open the first case in this group",
          onclick: (ev) => { if (!ev.target.closest(".case-link")) openCase((xs.find(isOpen) || xs[0]).id); },
          onkeydown: (ev) => { if (ev.key === "Enter") openCase((xs.find(isOpen) || xs[0]).id); } },
        el("div", { class: "cc-k" }, CASE_STATUS[k] || nice(k), el("span", { class: "badge " + (open ? "OPEN" : "ACCEPTED") }, open ? `${open} open` : "decided")),
        el("div", { class: "cc-v" }, usd(xs.reduce((s, x) => s + Math.abs(x.amount_cents), 0))),
        (() => { const all = r.exceptions.reduce((s, x) => s + Math.abs(x.amount_cents), 0) || 1, b = el("div", { class: "cc-bar" }), f = el("i", {});
          f.style.width = `${xs.reduce((s, x) => s + Math.abs(x.amount_cents), 0) / all * 100}%`; b.append(f); return b; })(),
        el("div", { class: "cc-list" }, xs.map((x) => caseLink(x.id, `${x.case_id || x.id} · ${TYPE_LABEL[x.type] || nice(x.type)}`))));
    })));
}
let SOURCE_KINDS = ["bank", "ledger", "ar", "ap", "documents", "parties"];
function fillProfile(w) {
  const reviewer = S.me.role === "owner_reviewer";
  [["pf-name", w.name], ["pf-period", w.period], ["pf-industry", w.industry || ""]].forEach(([id, v]) => { $(id).value = v; $(id).disabled = !reviewer; });
  $("pf-save").disabled = !reviewer; $("reset-btn").disabled = !reviewer;
  msg("pf-msg", reviewer ? "" : "Only a CPA reviewer can rename the client or clear its data.");
}
$("pf-save").onclick = (ev) => withBusy(ev.currentTarget, async () => {
  try {
    const r = await api(W("/profile"), { method: "POST", json: { name: $("pf-name").value, period: $("pf-period").value, industry: $("pf-industry").value } });
    msg("pf-msg", ""); toast("Client profile saved. The change is recorded in the audit trail.", "good"); S.wsMeta = r; await renderEvidence(); await progress();
  } catch (e) { msg("pf-msg", e.message, "err"); }
}, "Saving…");
document.addEventListener("click", (e) => { const a = e.target.closest("[data-go]"); if (a) { e.preventDefault(); go(a.dataset.go); } });

// ---------------- counterparties ----------------
const LINK_BADGE = { CONFIRMED: ["Confirmed", "ACCEPTED"], LINKED: ["Linked", "PASS"], SUGGESTED: ["Check this", "OPEN"],
  AMBIGUOUS: ["Ambiguous", "OPEN"], UNLINKED: ["No evidence", ""], REJECTED: ["Rejected", "REJECTED"] };
const KIND_LABEL = { bank: "Bank", ledger: "Ledger", ar: "Invoice", documents: "Document" };
S.party = null; S.pfilter = "all"; S.pq = "";
const signed = (c) => el("span", { class: "amt " + (c > 0 ? "in" : c < 0 ? "out" : "") }, (c > 0 ? "+" : "") + usd(c));
async function renderParties() {
  let d; try { d = await api(W("/parties")); } catch (_) { return; }
  if (!d.register_loaded) {
    put("party-list", el("div", { class: "muted" }, "No customer & vendor list yet. Import it under Sources."));
    put("party-unlinked", "–"); return;
  }
  const counts = { all: d.parties.length, customer: d.parties.filter((p) => p.type === "customer").length,
    vendor: d.parties.filter((p) => p.type === "vendor").length, check: d.parties.filter((p) => p.needs_check).length };
  const shown = d.parties.filter((p) => (S.pfilter === "all" || (S.pfilter === "check" ? p.needs_check : p.type === S.pfilter))
    && (!S.pq || p.name.toLowerCase().includes(S.pq.toLowerCase())));
  const search = el("input", { class: "pl-search", type: "search", placeholder: "Search customers & vendors", value: S.pq, "aria-label": "Search customers and vendors",
    oninput: (e) => { S.pq = e.target.value; renderParties().then(() => { const s = document.querySelector(".pl-search"); if (s) { s.focus(); s.setSelectionRange(S.pq.length, S.pq.length); } }); } });
  const chip = (k, label) => el("button", { class: "pl-chip" + (S.pfilter === k ? " on" : ""), "aria-pressed": S.pfilter === k ? "true" : "false",
    onclick: () => { S.pfilter = k; renderParties(); } }, label, el("span", { class: "pl-n" }, counts[k]));
  put("party-list", search, el("div", { class: "pl-chips" }, chip("all", "All"), chip("customer", "Customers"), chip("vendor", "Vendors"), chip("check", "To check")),
    shown.length ? el("table", { class: "tl pl" }, shown.map((p) => el("tr", { class: "click" + (p.id === S.party ? " sel-row" : ""), onclick: () => openParty(p.id) },
      el("td", { class: "tl-main" }, el("b", {}, p.name),
        el("div", { class: "small muted" }, `${nice(p.type)} · ${p.transactions} bank txn${p.transactions === 1 ? "" : "s"}`)),
      el("td", { class: "tl-side" },
        p.money_in_cents ? el("div", {}, signed(p.money_in_cents)) : null, p.money_out_cents ? el("div", {}, signed(-p.money_out_cents)) : null,
        p.needs_check ? el("span", { class: "badge OPEN" }, p.needs_check + " to check") : p.confirmed ? el("span", { class: "badge ACCEPTED" }, "Confirmed") : null))))
      : el("div", { class: "muted small pl-empty" }, "No matches."));
  put("party-unlinked", d.unlinked.length ? el("div", { class: "pt-list" }, d.unlinked.map((l) => el("div", { class: "pt-card" },
    el("div", { class: "pt-top" }, el("div", { class: "grow" }, el("b", {}, l.line_id), el("div", { class: "small muted" }, l.description)),
      signed(l.amount_cents)),
    el("div", { class: "pt-meta" }, el("span", { class: "badge " + LINK_BADGE[l.state][1] }, LINK_BADGE[l.state][0]),
      el("span", { class: "small muted" }, l.state === "AMBIGUOUS" ? "Points at: " + l.candidates.join(", ") : "Nothing identifies the payer. Not guessed.")),
    assignBox(l, d.parties)))) : el("div", { class: "muted" }, "Every bank transaction is linked to someone."));
}
function assignBox(l, parties) {
  const sel = el("select", { "aria-label": "Assign to" }, el("option", { value: "" }, "Assign to…"), parties.map((p) => el("option", { value: p.id }, p.name)));
  const note = el("input", { placeholder: "How do you know? (recorded)", "aria-label": "Reason" });
  return el("div", { class: "pt-assign" }, sel, note, el("button", { class: "secondary", onclick: async () => {
    if (!sel.value) return;
    await api(W(`/parties/${sel.value}/links/${l.line_id}`), { method: "POST", json: { decision: "CONFIRM", note: note.value } });
    S.party = sel.value; renderParties(); openParty(sel.value);
  } }, "Confirm"));
}
async function openParty(pid) {
  S.party = pid;
  if (!document.getElementById("p-parties").classList.contains("active")) go("parties");
  renderParties();
  let d; try { d = await api(W(`/parties/${encodeURIComponent(pid)}`)); } catch (e) { return put("party-view", el("div", { class: "msg err" }, e.message)); }
  const p = d.party, bank = d.links.filter((l) => l.kind === "bank");
  const act = (l, decision) => async () => {
    const note = decision === "REJECT" ? "Not this party" : "Confirmed by reviewer";
    await api(W(`/parties/${pid}/links/${l.line_id}`), { method: "POST", json: { decision, note } }); openParty(pid);
  };
  const open = d.transactions.filter((l) => l.state !== "CONFIRMED").length;
  const txn = (l) => el("article", { class: "pt-card" + (l.state === "SUGGESTED" || l.state === "AMBIGUOUS" ? " attn" : "") },
    el("div", { class: "pt-top" },
      el("div", { class: "grow" }, el("div", { class: "pt-id" }, el("b", {}, l.line_id), el("span", { class: "pt-kind" }, KIND_LABEL[l.kind] || nice(l.kind)),
        el("span", { class: "small muted" }, l.date)), el("div", { class: "pt-desc" }, l.description)),
      el("div", { class: "pt-right" }, l.amount_cents != null ? signed(l.amount_cents) : null,
        el("span", { class: "badge " + LINK_BADGE[l.state][1] }, LINK_BADGE[l.state][0]))),
    l.attached.length ? el("div", { class: "pt-attached" }, el("span", { class: "pt-lbl" }, "Linked to"),
      l.attached.map((x) => el("span", { class: "pt-tag" }, `${KIND_LABEL[x.kind] || nice(x.kind)} ${x.line_id}` + (x.kind === "ar" && x.open_cents != null ? ` · open ${usd(x.open_cents)}` : "")))) : null,
    el("details", { class: "pt-why" }, el("summary", {}, "Why it's theirs", el("span", { class: "pl-n" }, l.signals.length + (l.review ? 1 : 0))),
      el("ul", { class: "plain" }, (l.review ? [`${l.review.decision === "CONFIRM" ? "Confirmed" : "Rejected"} by ${l.review.by}: “${l.review.note}”`] : []).concat(l.signals).map((x) => el("li", {}, rich(x))))),
    el("div", { class: "pt-actions" },
      ["bank", "ledger", "ar"].includes(l.kind) ? el("button", { class: "secondary", onclick: () => openTrace(l.line_id) }, "Trace →") : null,
      el("span", { class: "grow" }),
      l.state !== "CONFIRMED" ? el("button", { class: "no", onclick: act(l, "REJECT") }, "Not them") : null,
      l.state !== "CONFIRMED" ? el("button", { class: "ok", onclick: act(l, "CONFIRM") }, "Confirm") : null));
  put("party-view",
    el("div", { class: "client-head" }, el("div", {}, el("div", { class: "eyebrow2" }, nice(p.type) + " · " + p.id), el("div", { class: "client-name" }, p.name),
      el("div", { class: "client-meta" }, "Known as " + p.aliases.join(", ") + (p.ref_prefixes.length ? " · references " + p.ref_prefixes.join(", ") : ""))),
      el("span", { class: "badge" + (open ? " OPEN" : " ACCEPTED") }, open ? `${open} to review` : "All confirmed")),
    el("div", { class: "pt-stats" }, [["Money in", usd(bank.filter((l) => l.amount_cents > 0).reduce((a, l) => a + l.amount_cents, 0))],
      ["Money out", usd(-bank.filter((l) => l.amount_cents < 0).reduce((a, l) => a + l.amount_cents, 0))],
      ["Open invoices", usd(d.owed_cents)], ["Still owed", d.residual_cents ? usd(d.residual_cents) : "–"]]
      .map(([k, v]) => el("div", { class: "pt-stat" }, el("div", { class: "k" }, k), el("div", { class: "v" }, v)))),
    el("div", { class: "pt-head" }, el("div", { class: "card-title" }, "Transactions with " + p.name, el("span", { class: "pl-n" }, d.transactions.length)),
      el("div", { class: "small muted" }, "Confirm or reject each link. Decisions are signed into the audit trail.")),
    el("div", { class: "pt-list" }, d.transactions.map(txn)),
    d.exceptions.length ? el("div", {}, el("div", { class: "card-title gap" }, "Open questions"),
      d.exceptions.map((e) => el("div", { class: "sugg" }, el("b", {}, e.id), ` · ${nice(e.type)}: `, rich(e.why, false), " ", el("span", { class: "badge " + (e.status === "OPEN" ? "OPEN" : "") }, nice(e.status))))) : null);
}

// ---------------- trace ----------------
async function renderTraceList(selected) {
  let d; try { d = await api(W("/trace")); } catch (_) { return; }
  if (!d.lines.length) return put("trace-list", el("div", { class: "muted" }, "Import a bank statement first."));
  put("trace-list", el("div", { class: "muted small" }, d.run_id ? `Status from reconciliation run ${d.run_id}` : el("span", {}, "Not reconciled yet. ", el("a", { href: "#recon", "data-go": "recon" }, "Run reconciliation"), " to see each outcome.")),
    el("table", { class: "tl" }, d.lines.map((l) => el("tr", { class: "click" + (l.id === selected ? " sel-row" : ""), onclick: () => openTrace(l.id), title: l.description },
      el("td", { class: "tl-main" }, el("b", {}, l.id), el("div", { class: "tl-desc small muted" }, l.description),
        el("span", { class: "badge " + ({ MATCHED: "ACCEPTED", OPEN: "OPEN", ACCEPTED: "ACCEPTED", REJECTED: "REJECTED" }[l.status] || "") }, l.label)),
      el("td", { class: "tl-side" }, el("div", { class: "num tl-amt" }, usd(l.amount_cents)))))));
}
async function openTrace(id) {
  if (!document.getElementById("p-trace").classList.contains("active")) go("trace");
  renderTraceList(id);
  put("trace-view", el("div", { class: "muted" }, "Loading…"));
  let t; try { t = await api(W(`/trace/${encodeURIComponent(id)}`)); } catch (e) { return put("trace-view", el("div", { class: "msg err" }, e.message)); }
  const fmt = (v) => v == null || v === "" ? "—" : typeof v === "object" ? Object.entries(v).filter(([, c]) => c).map(([f, c]) => f === c ? f : `${f} ← “${c}”`).join(", ") : String(v);
  const when = (x) => x ? new Date(x).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
  const who = (w) => { if (!w) return null; w = String(w).replace(/\s*\(deterministic\)$/, "");
    return w.toLowerCase() === "trustops" ? "TrustOps" : w.startsWith("agent:") ? niceActor(w) : w.includes("@") ? w : nice(w); };
  const tidy = (x) => String(x || "").replace(/\b[A-Z]+(?:_[A-Z]+)+\b/g, (m) => nice(m));
  const val = (k, v) => (k.endsWith("cents") || k === "still_owed" || k === "amount") && v !== "" && !isNaN(+v) ? usd(+v) : fmt(v);
  const amt = +t.line.amount_cents;
  put("trace-view",
    el("header", { class: "th" },
      el("div", { class: "grow" }, el("div", { class: "xh-id" }, `${nice(t.kind)} transaction · ${id}`),
        el("h3", { class: "xh-title" }, t.line.description || t.line.customer_alias || id),
        el("div", { class: "th-meta" }, [t.line.date, t.line.reference ? "Ref " + t.line.reference : null, `from the ${t.kind} file`].filter(Boolean).join(" · "))),
      el("div", { class: "xh-right" }, el("div", { class: "xh-amt" + (amt > 0 ? " pos" : "") }, (amt > 0 ? "+" : "") + usd(amt)),
        el("span", { class: "badge " + t.chain.state }, t.chain.state === "PASS" ? "✓ Evidence verified" : t.chain.state))),
    el("div", { class: "th-sec" }, "Journey", el("span", { class: "pl-n" }, t.steps.length)),
    el("ol", { class: "timeline tj" }, t.steps.map((s, i) => {
      const rows = Object.entries(s.detail || {}).filter(([, v]) => v != null && v !== "");
      return el("li", {},
        el("div", { class: "tl-dot" }, String(i + 1)),
        el("div", { class: "tl-body" },
          el("div", { class: "tj-top" }, el("span", { class: "tj-stage" }, nice(s.stage)),
            el("span", { class: "tj-when" }, [who(s.who), when(s.when)].filter(Boolean).join(" · "))),
          el("div", { class: "tl-title" }, rich(tidy(s.title))),
          rows.length || s.proof ? el("details", { class: "xd" }, el("summary", {}, "Details"),
            rows.length ? el("dl", {}, rows.flatMap(([k, v]) => [el("dt", {}, nice(k)), el("dd", {}, rich(val(k, v), false))])) : null,
            s.proof ? proofEl(s.proof) : null) : null));
    })),
    t.related.length ? [el("div", { class: "th-sec" }, "Linked records", el("span", { class: "pl-n" }, t.related.length)),
      el("div", { class: "xrs" + (t.related.length > 1 ? " grid2" : "") }, t.related.map((r) => recCard(r.id, KIND_SHORT[r.kind] || labelFor(r.id))))] : null,
    el("details", { class: "xd th-ev" }, el("summary", {}, `Signed audit records (${t.events.length})`),
      el("div", { class: "muted small my8" }, `Each record is chained to the one before and signed. Whole chain: ${t.chain.reason}.`),
      el("div", { class: "tablewrap" }, el("table", {}, el("tr", {}, ["#", "Time (UTC)", "Who", "Action", "Decision", "Record hash"].map((h) => el("th", {}, h))),
        t.events.map((e) => el("tr", {}, el("td", {}, e.seq), el("td", { class: "small" }, e.timestamp_utc.slice(11, 19)), el("td", {}, niceActor(e.actor_id)), el("td", {}, nice(e.action)),
          el("td", {}, el("span", { class: "badge " + String(e.policy_decision).split(":")[0] }, String(e.policy_decision).split(":")[0])), el("td", { class: "mono" }, e.event_hash.slice(0, 16) + "…")))))));
}
// Proof line in plain words. A file fingerprint (SHA-256) is shortened; hover shows it in full and Copy gives auditors the whole value.
function proofEl(p) {
  p = String(p);
  const h = p.match(/\b[a-f0-9]{64}\b/);
  if (h) {
    const full = h[0], btn = el("button", { class: "secondary proof-copy", type: "button", "aria-label": "Copy full file fingerprint" }, "Copy");
    btn.onclick = async () => { try { await navigator.clipboard.writeText(full); btn.textContent = "Copied"; } catch (_) { btn.textContent = "Copy failed"; }
      setTimeout(() => { btn.textContent = "Copy"; }, 1500); };
    return el("div", { class: "proof ok" }, el("span", { class: "proof-ico", "aria-hidden": "true" }, "✓"), "File fingerprint verified · ",
      el("code", { title: "SHA-256 " + full }, full.slice(0, 6) + "…" + full.slice(-4)), btn);
  }
  const [k, ...rest] = p.split(" "), v = rest.join(" ");
  const LABEL = { run: "Reconciliation run", trace: "Trace", approval: "Approval", doc: "Document" };
  if (LABEL[k] && v) return el("div", { class: "proof" }, `${LABEL[k]} `, el("span", { class: "rid" }, v));
  if (/balance/.test(p)) return el("div", { class: "proof ok" }, el("span", { class: "proof-ico", "aria-hidden": "true" }, "✓"), rich(p));
  return el("div", { class: "proof" }, rich(p));
}
// One compact record box (bank line, ledger entry, invoice…): what, how much, where from.
function recCard(id, kind) {
  const l = line(id); kind = kind || labelFor(id);
  if (!l) return el("div", { class: "xr" }, el("div", { class: "xr-k" }, kind), el("div", { class: "xr-desc" }, id || "—"));
  const a = l.open_cents != null ? l.open_cents : l.amount_cents;
  return el("div", { class: "xr" },
    el("div", { class: "xr-top" }, el("span", { class: "xr-k" }, `${kind} · `, el("span", { class: "rid" }, id)), el("span", { class: "xr-amt" }, usd(a))),
    el("div", { class: "xr-desc" }, l.description || l.customer_alias || ""),
    el("div", { class: "xr-meta" }, [l.date, (l.reference || (l.open_cents != null ? l.id : "")) ? "Ref " + (l.reference || l.id) : null,
      l.open_cents != null ? "open balance" : null].filter(Boolean).join(" · ")));
}

// ---------------- theme ----------------
(function theme() {
  let t = null; try { t = localStorage.getItem("ls-theme-v2"); } catch (_) {}
  if (t) document.documentElement.dataset.theme = t;
  $("theme").onclick = () => {
    const set = document.documentElement.dataset.theme;
    const dark = set ? set === "dark" : !!(window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("ls-theme-v2", document.documentElement.dataset.theme); } catch (_) {}
  };
})();

// ---------------- tabs (each page has its own address, e.g. #reports) ----------------
document.querySelectorAll(".tab").forEach((t) => t.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "p-" + t.dataset.tab));
  if (location.hash.slice(1) !== t.dataset.tab) history.pushState(null, "", "#" + t.dataset.tab);
  document.title = `${(t.querySelector(".lbl-t") || t).textContent.trim()} · LedgerSight`;
  window.scrollTo(0, 0);
  if (t.dataset.tab === "home") renderOverview();
  if (t.dataset.tab === "evidence") renderEvidence();
  if (t.dataset.tab === "trace") renderTraceList();
  if (t.dataset.tab === "parties") renderParties();
  if (t.dataset.tab === "reports") renderReports();
  if (t.dataset.tab === "rules") renderRules();
  if (t.dataset.tab === "controls") renderProof();
  if (t.dataset.tab === "recon") progress();  // never judge "ready to run" from stale status
  if (t.dataset.tab === "agents") startAgentMonitor(); else stopAgentMonitor();
});
// ---------------- shell: breadcrumb, collapsible sidebar, period chip (presentation only) ----------------
function setCrumb(t) {
  let g = t.previousElementSibling; while (g && !g.classList.contains("navlabel")) g = g.previousElementSibling;
  $("crumb-group").textContent = g ? proper(g.textContent.trim().toLowerCase()).replace(/^\w/, (c) => c.toUpperCase()) : "";
  $("crumb-page").textContent = (t.querySelector(".lbl-t") || t).textContent.trim();
}
document.querySelectorAll(".tab").forEach((t) => {
  t.title = (t.querySelector(".lbl-t") || t).textContent.trim();
  t.addEventListener("click", () => setCrumb(t));
});
setCrumb(document.querySelector(".tab.active") || document.querySelector(".tab"));
(function sideMini() {
  let on = false; try { on = localStorage.getItem("ls-side-mini") === "1"; } catch (_) {}
  const apply = () => { document.body.classList.toggle("side-mini", on); const b = $("side-toggle");
    b.title = b.ariaLabel = on ? "Expand menu" : "Collapse menu"; b.setAttribute("aria-expanded", String(!on)); };
  apply();
  const side = document.querySelector(".sidebar"), wide = matchMedia("(min-width:821px)");
  let t = 0;
  const peek = (v, ms) => { clearTimeout(t); t = setTimeout(() => side.classList.toggle("peek", v && on && wide.matches), ms); };
  side.addEventListener("mouseenter", () => peek(true, 160));
  side.addEventListener("mouseleave", () => peek(false, 120));
  side.addEventListener("focusin", () => peek(true, 0));
  side.addEventListener("focusout", (e) => { if (!side.contains(e.relatedTarget) && !side.matches(":hover")) peek(false, 0); });
  side.addEventListener("click", (e) => { if (e.target.closest(".tab")) peek(false, 0); });
  $("side-toggle").onclick = () => { on = !on; peek(false, 0); apply(); try { localStorage.setItem("ls-side-mini", on ? "1" : "0"); } catch (_) {} };
})();
// Overview sections (Summary / Agents & decisions / Setup & guide): presentation only, every block still renders.
function ovSec(name, focus) {
  document.querySelectorAll(".ov-tab").forEach((b) => { const on = b.dataset.sec === name;
    b.classList.toggle("active", on); b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1; if (on && focus) b.focus(); });
  document.querySelectorAll(".ov-sec").forEach((x) => x.classList.toggle("hidden", x.dataset.sec !== name));
  const sec = $("ovs-" + name); if (window.LSMotion && sec) LSMotion.enter(sec);
  try { sessionStorage.setItem("ls-ov-sec", name); } catch (_) {}
}
document.querySelectorAll(".ov-tab").forEach((b, i, all) => {
  b.onclick = () => ovSec(b.dataset.sec);
  b.onkeydown = (e) => { const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0; if (!d) return;
    e.preventDefault(); ovSec(all[(i + d + all.length) % all.length].dataset.sec, true); };
});
(() => { let s = null; try { s = sessionStorage.getItem("ls-ov-sec"); } catch (_) {} if (s && $("ovs-" + s)) ovSec(s); })();
$("period-chip").onclick = () => {
  go("home"); ovSec("setup"); const d = $("ov-profile"); d.open = true;
  setTimeout(() => { d.scrollIntoView({ behavior: REDUCE ? "auto" : "smooth", block: "center" }); $("pf-period").focus({ preventScroll: true }); }, 120);
};
function isActive(tab) { return document.getElementById("p-" + tab).classList.contains("active"); }

// ---------------- import ----------------
const KIND = { bank: "Bank statement", ledger: "General ledger", ar: "Open invoices", ap: "Vendor bills", documents: "Supporting docs", parties: "Customer & vendor list" };
function renderStatus() { renderSources(); }
async function renderSources() {
  let s; try { s = await api(W("/sources")); } catch (_) { return; }
  s.kinds.forEach((k) => { KIND[k.kind] = k.label; });
  SOURCE_KINDS = s.kinds.map((k) => k.kind);
  put("src-status", s.kinds.map((k) => {
    const on = k.status !== "NOT_CONNECTED";
    return el("div", { class: "ss-row" + (on ? "" : " ss-empty") },
      el("span", { class: "ss-dot " + (on ? "on" : "") }),
      el("div", { class: "ss-main" },
        el("div", { class: "ss-name" }, k.label, k.required ? null : el("span", { class: "ss-opt" }, "Optional")),
        on && k.envelope ? srcEnvelope(k.envelope) : el("div", { class: "ss-meta" }, on ? `${k.rows || 0} records` : (k.required ? "Needed to run reconciliation" : "Not imported"))),
      el("div", { class: "ss-side" },
        on ? el("span", { class: "ss-count" }, String(k.rows || 0), el("small", {}, " records")) : null,
        el("span", { class: "badge " + k.status, title: s.status_labels[k.status] }, on ? nice(k.status) : "Not yet"),
        on ? el("button", { class: "linkbtn ss-clear", title: "Remove this file", onclick: () => clearSource(k.kind) }, "Clear") : null));
  }), el("div", { class: "ss-foot" }, "All sources are read-only. Hover a file name for its fingerprint."));
  put("connectors", el("table", {}, el("tr", {}, ["Connection", "Status", "What the label means"].map((h) => el("th", {}, h))),
    s.connectors.map((c) => el("tr", {}, el("td", {}, c.name), el("td", {}, el("span", { class: "badge " + c.status }, nice(c.status))),
      el("td", { class: "small muted" }, s.status_labels[c.status])))));
  const sel = $("up-kind"), cur = sel.value;
  put(sel, el("option", { value: "auto" }, "Detect file type automatically"),
    s.kinds.map((k) => el("option", { value: k.kind }, `${k.label} · ${k.formats.filter((f) => f !== "PDF (text)").join(", ")}`)),
    el("option", { value: "pdf" }, "Invoice / receipt · PDF"));
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "auto";
}
// Where these rows came from, in this client's context: file, fingerprint, when, currency, period, read-only.
function srcEnvelope(e) {
  const fresh = e.freshness_seconds == null ? "" : ago(e.freshness_seconds);
  const tip = `${e.source_file || ""}\nSHA-256 ${e.source_digest}\nTenant ${e.tenant_id} · client ${e.client_id}` +
    (e.period ? `\nPeriod ${e.period}` : "") + `\nAccess ${e.access === "READ_ONLY" ? "read-only" : nice(e.access)}`;
  return el("div", { class: "src-env" },
    el("div", { class: "ss-file", title: tip }, e.source_file || "—"),
    el("div", { class: "ss-meta" }, [e.date_range ? spanDates(e.date_range) : null, e.currency && e.currency.length ? e.currency.join(" / ") : null,
      fresh ? `Imported ${fresh}` : null].filter(Boolean).join("  ·  ")));
}
// 2026-10-01 → 2026-10-31 reads as "Oct 1 – 31, 2026"
function spanDates([a, b]) {
  const M = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const p = (d) => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(d || ""); return m ? { y: +m[1], m: +m[2] - 1, d: +m[3] } : null; };
  const x = p(a), y = p(b);
  if (!x || !y) return `${a} – ${b}`;
  if (x.y === y.y && x.m === y.m) return x.d === y.d ? `${M[x.m]} ${x.d}, ${x.y}` : `${M[x.m]} ${x.d} – ${y.d}, ${x.y}`;
  if (x.y === y.y) return `${M[x.m]} ${x.d} – ${M[y.m]} ${y.d}, ${x.y}`;
  return `${M[x.m]} ${x.d}, ${x.y} – ${M[y.m]} ${y.d}, ${y.y}`;
}
async function clearSource(k) {
  try { const r = await api(W(`/sources/${k}/clear`), { method: "POST" });
    msg("clear-msg", `${KIND[k]} cleared` + (r.runs_withdrawn ? "; previous match results withdrawn, run Match again." : "."), "good");
    if (S.upload && (S.upload.kind === k || (k === "documents" && S.upload.kind === "pdf"))) resetIntakeUi();
    put("up-next"); S.run = null; await refreshWs(); renderRun(); renderExceptions(); renderResults(); }
  catch (e) { msg("clear-msg", e.message, "err"); }
}
async function clearClient(btn, nextPeriod = false) {
  const name = proper((S.wsMeta && S.wsMeta.name) || "this client");
  const ask = nextPeriod
    ? [`Start the next period for ${name}?`, "This closes the current period. Its audit trail is archived (never deleted) and the files, runs and decisions are cleared so you can upload next period's files. Download the reports first if you still need them.", "Start next period", false]
    : [`Clear all data for ${name}?`, "This removes the imported files, reconciliation runs and review decisions. The audit trail is archived, not deleted, and the clear itself is recorded.", "Clear data", true];
  if (!(await confirmDialog(...ask))) return;
  await withBusy(btn, async () => {
    try { await api(W("/reset"), { method: "POST" }); toast(nextPeriod ? `${name}: new period started. Upload this period's files.` : `${name}: data cleared. Upload new files to start again.`, "good");
      resetIntakeUi(); await switchWs(); }
    catch (e) { toast(e.message, "err"); }
  }, "Clearing…");
  await progress();  // withBusy re-enables the button when it finishes; re-apply "nothing to clear"
}
$("clear-all").onclick = (e) => clearClient(e.currentTarget);
function renderHealth(uploads) {
  const done = uploads.filter((u) => u.health);
  if (!done.length) return put("health", el("div", { class: "muted" }, "Nothing imported yet."));
  put("health", el("table", {},
    el("tr", {}, ["Source", "File", "State", "Rows received", "Accepted", "Rejected", "Debits", "Credits", "Control total", "SHA-256"].map((h) => el("th", {}, h))),
    done.map((u) => el("tr", {},
      el("td", {}, KIND[u.kind]), el("td", {}, u.filename), el("td", {}, el("span", { class: "badge " + (u.state === "VALIDATED" ? "ACCEPTED" : "OPEN") }, nice(u.state))),
      el("td", { class: "num" }, u.health.received), el("td", { class: "num" }, u.health.accepted),
      el("td", { class: "num" + (u.health.rejected.length ? " neg" : "") }, u.health.rejected.length),
      el("td", { class: "num" }, usd(u.health.debit_cents)), el("td", { class: "num" }, usd(u.health.credit_cents)),
      el("td", {}, el("span", { class: "badge " + (u.health.control_total_ok ? "PASS" : "FAIL") }, u.health.control_total_ok ? "Balances" : "Mismatch")),
      el("td", { class: "mono" }, u.sha256.slice(0, 12) + "…")))),
    done.some((u) => u.health.rejected.length) ? el("div", { class: "msg err" }, "Rejected rows: " +
      done.flatMap((u) => u.health.rejected.map((r) => `row ${r.row}: ${r.reason}`)).join("; ")) : null);
}
// Several files can be chosen at once; each one is checked and confirmed in turn.
S.queue = [];
const isEml = (f) => /\.eml$/i.test(f.name) || f.type === "message/rfc822";
function intakeFiles(files, from) {
  put("drive-view");
  const emails = files.filter(isEml), normal = files.filter((f) => !isEml(f));
  if (normal.length) queueFiles(normal, from);
  emails.forEach((f) => openEmail(f));
}
$("choose-btn").onclick = () => $("up-file").click();
$("up-file").onchange = () => {
  const files = [...$("up-file").files];
  $("up-file").value = "";
  if (files.length) intakeFiles(files, "this computer");
};
// Drag and drop onto the dropzone.
const dzTarget = $("dz-target");
["dragenter", "dragover"].forEach((ev) => dzTarget.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); dzTarget.classList.add("drag"); }));
["dragleave", "drop"].forEach((ev) => dzTarget.addEventListener(ev, (e) => { e.preventDefault(); e.stopPropagation(); dzTarget.classList.remove("drag"); }));
dzTarget.addEventListener("drop", (e) => {
  const files = [...(e.dataTransfer?.files || [])];
  if (files.length) intakeFiles(files, "drag and drop");
});
$("dz-drive-btn").onclick = () => openGoogleConnectModal();
function queueFiles(files, from) {
  S.queue = files; S.autoKind = true; S.queueTotal = files.length; S.refusals = []; put("up-refused"); put("up-next");
  msg("up-msg", `${files.length} file${files.length === 1 ? "" : "s"} from ${from}. Checking each one…`); nextInQueue();
}

const b64file = (a) => { const bin = atob(a.data_b64), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return new File([u], a.name); };
async function openEmail(f) {
  const fd = new FormData(); fd.append("file", f);
  let r; try { r = await api(W("/email"), { method: "POST", form: fd }); } catch (e) { return msg("up-msg", "Refused. " + e.message, "err"); }
  const boxes = r.attachments.map((a) => el("input", { type: "checkbox", checked: true }));
  put("em-view", el("div", { class: "card gap" },
    el("div", { class: "small muted" }, "From"), el("div", {}, r.from), el("div", { class: "small muted gap" }, "Subject"), el("div", { class: "card-title" }, r.subject),
    r.unsafe_content.length ? el("div", { class: "msg err" }, "Instruction-like text in the email body was flagged and ignored: " + r.unsafe_content.join(" | ")) : null,
    r.attachments.length ? el("div", {}, el("div", { class: "small muted" }, "Attachments"),
      r.attachments.map((a, i) => el("label", { class: "row small" }, boxes[i], `${a.name} · ${(a.size / 1024).toFixed(1)} KB`))) : el("div", { class: "muted" }, "No usable attachments."),
    r.skipped.map((s) => el("div", { class: "small muted" }, `Skipped ${s.name}: ${s.reason}`)),
    r.attachments.length ? el("button", { class: "primary gap", onclick: () => { put("em-view"); queueFiles(r.attachments.filter((_, i) => boxes[i].checked).map(b64file), "the email"); } }, "Import selected attachments") : null));
}
let driveCfg = null;
const loadScript = (src) => new Promise((ok, bad) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = bad; document.head.append(s); });
async function renderDrive(open) {
  if (!driveCfg) driveCfg = (await api("/v1/integrations")).google_drive;
  if (driveCfg.enabled && open) return openDrivePicker();
  if (!driveCfg.enabled) return put("drive-view", el("div", { class: "card gap" },
    el("div", { class: "card-title" }, "Google Drive is not connected yet"),
    el("ol", { class: "plain small" }, el("li", {}, "In Google Cloud Console, enable the Google Drive API."),
      el("li", {}, "Create an OAuth client (Web application) with origin http://127.0.0.1:8090, and an API key."),
      el("li", {}, "Add GOOGLE_CLIENT_ID=… and GOOGLE_API_KEY=… to the .env file, then restart LedgerSight.")),
    el("div", { class: "small muted" }, "Access is read-only and limited to the files you pick (drive.file scope). Nothing else in your Drive is visible.")));

}
async function openDrivePicker() {
  try {
    await Promise.all([loadScript("https://apis.google.com/js/api.js"), loadScript("https://accounts.google.com/gsi/client")]);
    await new Promise((ok) => gapi.load("picker", ok));
    const tc = google.accounts.oauth2.initTokenClient({ client_id: driveCfg.client_id, scope: "https://www.googleapis.com/auth/drive.file", callback: (t) => {
      if (t.error) return msg("up-msg", "Google sign-in cancelled.", "err");
      const view = new google.picker.DocsView(google.picker.ViewId.DOCS).setIncludeFolders(true).setSelectFolderEnabled(false);
      new google.picker.PickerBuilder().addView(view).enableFeature(google.picker.Feature.MULTISELECT_ENABLED)
        .setOAuthToken(t.access_token).setDeveloperKey(driveCfg.api_key).setAppId(driveCfg.app_id)
        .setOrigin(window.location.protocol + "//" + window.location.host)
        .setCallback(async (d) => {
          if (d.action !== google.picker.Action.PICKED) return;
          const files = [];
          const EXPORT = { "application/vnd.google-apps.spreadsheet": ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"],
            "application/vnd.google-apps.document": ["application/pdf", ".pdf"], "application/vnd.google-apps.presentation": ["application/pdf", ".pdf"] };
          for (const doc of d.docs) {
            const exp = EXPORT[doc.mimeType];
            const url = exp ? `https://www.googleapis.com/drive/v3/files/${doc.id}/export?mimeType=${encodeURIComponent(exp[0])}`
                            : `https://www.googleapis.com/drive/v3/files/${doc.id}?alt=media`;
            const r = await fetch(url, { headers: { Authorization: "Bearer " + t.access_token } });
            if (!r.ok) { msg("up-msg", `Could not read ${doc.name} from Drive.`, "err"); continue; }
            files.push(new File([await r.blob()], exp && !doc.name.endsWith(exp[1]) ? doc.name + exp[1] : doc.name));
          }
          if (files.length) queueFiles(files, "Google Drive");
        }).build().setVisible(true);
    } });
    tc.requestAccessToken();
  } catch (e) { msg("up-msg", "Google Drive could not load: " + e.message, "err"); }
}
const fromHTML = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const GOOGLE_ICON = {
  drive: '<svg width="22" height="22" viewBox="0 0 16 16"><path d="M5.6 1h4.8l4.8 8.3h-4.8z" fill="#ffba00"/><path d="M5.6 1L.8 9.3l2.4 4.1 4.8-8.3z" fill="#00ac47"/><path d="M3.2 13.4h9.6l2.4-4.1h-9.6z" fill="#2684fc"/></svg>',
  docs: '<svg width="22" height="22" viewBox="0 0 16 16"><path d="M3 1h6l4 4v10a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1z" fill="#4285f4"/><path d="M9 1v4h4" fill="#a1c2fa"/><rect x="4" y="8" width="6" height="1" fill="#fff"/><rect x="4" y="10" width="6" height="1" fill="#fff"/><rect x="4" y="12" width="4" height="1" fill="#fff"/></svg>',
  sheets: '<svg width="22" height="22" viewBox="0 0 16 16"><path d="M3 1h6l4 4v10a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1z" fill="#0f9d58"/><path d="M9 1v4h4" fill="#87d3ab"/><rect x="4" y="8" width="2" height="1.3" fill="#fff"/><rect x="7" y="8" width="2" height="1.3" fill="#fff"/><rect x="10" y="8" width="2" height="1.3" fill="#fff"/><rect x="4" y="10.3" width="2" height="1.3" fill="#fff"/><rect x="7" y="10.3" width="2" height="1.3" fill="#fff"/><rect x="10" y="10.3" width="2" height="1.3" fill="#fff"/></svg>',
  slides: '<svg width="22" height="22" viewBox="0 0 16 16"><path d="M3 1h6l4 4v10a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V2a1 1 0 0 1 1-1z" fill="#f4b400"/><path d="M9 1v4h4" fill="#fbdc8d"/><rect x="4" y="8.3" width="8" height="4.3" rx=".5" fill="#fff"/></svg>',
};
async function openGoogleConnectModal() {
  if (!driveCfg) driveCfg = (await api("/v1/integrations")).google_drive;
  const close = () => { overlay.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const go = () => { close(); put("em-view"); renderDrive(true); };
  const btnLabel = driveCfg.enabled ? "Sign in" : "Connect";
  const row = (icon, title, desc) => el("div", { class: "connect-row" },
    el("div", { class: "connect-row-icon" }, fromHTML(GOOGLE_ICON[icon])),
    el("div", { class: "connect-row-text" }, el("div", { class: "connect-row-title" }, title), el("div", { class: "small muted" }, desc)),
    el("button", { class: "primary", onclick: go }, btnLabel));
  const overlay = el("div", { class: "overlay", onclick: (e) => { if (e.target === overlay) close(); } },
    el("div", { class: "dialog connect-dialog", role: "dialog", "aria-modal": "true", "aria-label": "Connect Google Drive" },
      el("div", { class: "connect-head" }, el("div", { class: "dialog-title" }, "Connect Google Drive"),
        el("button", { class: "iconbtn", "aria-label": "Close", onclick: close }, "✕")),
      el("div", { class: "dialog-body gap" }, "Pick reconciliation files from Google Drive. Access is read-only and limited to the files you select (drive.file scope) — nothing else in your Drive is visible."),
      el("div", { class: "connect-rows gap" },
        row("drive", "Google Drive", driveCfg.enabled ? "Sign in and pick files" : "Not connected yet"),
        row("sheets", "Google Sheets", "Spreadsheets picked from Drive import as Excel"),
        row("docs", "Google Docs", "Documents picked from Drive import as PDF"),
        row("slides", "Google Slides", "Presentations picked from Drive import as PDF")),
      el("div", { class: "connect-foot" }, el("span", { class: "small muted" }, "Sign-in opens in a separate window."),
        el("button", { class: "secondary", onclick: close }, "Not now"))));
  document.body.append(overlay); document.addEventListener("keydown", onKey);
}
function nextInQueue() {
  const f = S.queue.shift();
  if (!f) { if (S.queueTotal > 1) msg("up-msg", `All ${S.queueTotal} files done.`, "good"); S.queueTotal = 0; $("up-file").value = ""; offerRun(); return; }
  uploadOne(f);
}
// Forget every half-finished upload on this page: pending mapping, messages, refusals, queue. Used when the
// client's data is cleared (here or by a colleague) or another client is opened, so nothing stale remains.
function resetIntakeUi() {
  S.queue = []; S.queueTotal = 0; S.upload = null; S.refusals = [];
  $("mapping").classList.add("hidden"); put("mapping");
  msg("up-msg", ""); put("up-refused"); put("up-next"); put("em-view"); put("drive-view"); $("up-file").value = "";
}
// Once the bank statement and general ledger are both in, take the user straight to the next step.
async function offerRun() {
  await progress();
  const st = S.status || {};
  if (!(st.bank && st.ledger)) {
    const need = [!st.bank && "a bank statement", !st.ledger && "a general ledger"].filter(Boolean).join(" and ");
    return put("up-next", st.uploads ? el("div", { class: "callout gap" }, el("b", {}, `Add ${need} to run reconciliation.`),
      el("div", { class: "small" }, "Reconciliation compares the bank statement with the general ledger, so both are needed.")) : null);
  }
  put("up-next", el("div", { class: "callout gap next-run" },
    el("div", {}, el("b", {}, "Files imported. Bank statement and general ledger are ready."),
      el("div", { class: "small" }, S.status.runs ? "Run again to include the files you just added." : "Next: match the bank statement to the ledger.")),
    el("button", { class: "primary", onclick: () => { put("up-next"); go("recon"); setTimeout(() => $("run-btn").click(), 250); } }, "Run reconciliation →")));
}
function guessKind(name) {
  const n = name.toLowerCase();
  if (n.endsWith(".pdf")) return /bank|statement/.test(n) ? "bank" : /ledger|journal/.test(n) ? "ledger" : "pdf";
  if (/\.(ofx|qfx|qbo)$/.test(n) || /bank|statement/.test(n)) return "bank";
  if (/receipt|invoice_|bill/.test(n) && n.endsWith(".pdf")) return "pdf";
  if (/ledger|journal|\bgl\b/.test(n)) return "ledger";
  if (/(^|[_\-\s.])(ap|bills?)([_\-\s.]|$)|payable/.test(n)) return "ap";
  if (/\bar\b|_ar\.|invoice|receivable/.test(n)) return "ar";
  if (/vendor|customer|parties|contacts/.test(n)) return "parties";
  if (/document|receipt/.test(n)) return "documents";
  return "bank";
}
// Formats the importer can read. Anything else (Word, PowerPoint, ZIP, macro workbooks, images...) is refused.
const IMPORTABLE = /\.(csv|xlsx|tsv|txt|json|ofx|qfx|qbo|pdf)$/i;
const ACCEPTED_TEXT = "CSV, Excel (.xlsx), TSV, JSON, OFX/QFX/QBO bank downloads, PDF, or a saved email (.eml)";
// Every refusal is listed in one place, so a batch of files shows exactly what was not accepted and why.
function refused(f, reason) {
  S.refusals = (S.refusals || []).concat([{ name: f.name, reason }]);
  put("up-refused", el("div", { class: "msg err" }, el("b", {}, "Not accepted"),
    el("ul", { class: "plain small" }, S.refusals.map((x) => el("li", {}, el("b", {}, x.name), ` — ${x.reason}`)))));
}
const kb = (n) => n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
function renderHeld(held) {
  const list = held || [];
  $("held-card").classList.toggle("hidden", !list.length);
  if (!list.length) return put("held");
  put("held", el("div", { class: "tablewrap" }, el("table", {},
    el("tr", {}, ["File", "Type", "Size", "Status", "Added by", "Added", "SHA-256"].map((h) => el("th", {}, h))),
    list.slice().reverse().map((h) => el("tr", {}, el("td", {}, h.filename), el("td", {}, h.extension ? h.extension.slice(1).toUpperCase() : "—"),
      el("td", { class: "num" }, kb(h.size)), el("td", {}, el("span", { class: "badge SIMULATED", title: h.note }, "Held · JSON conversion in development")),
      el("td", { class: "small" }, h.uploaded_by), el("td", { class: "small" }, new Date(h.uploaded_at).toLocaleString()),
      el("td", { class: "mono" }, h.sha256.slice(0, 12) + "…"))))));
}
// One file from the device, Google Drive or an email. The SERVER decides what the file is from its content
// (columns and rows); the name is only sent as a tie-break hint. Wrong files are refused with the reason.
async function uploadOne(f) {
  if (!IMPORTABLE.test(f.name)) {
    const ext = (f.name.match(/\.[^.]+$/) || ["(no extension)"])[0].toLowerCase();
    refused(f, `${ext} files are not supported. Accepted: ${ACCEPTED_TEXT}.`);
    msg("up-msg", ""); return nextInQueue();
  }
  const left = S.queue.length ? ` (${S.queue.length} more after this)` : "";
  const fd = new FormData(); fd.append("file", f); fd.append("kind", "auto"); fd.append("hint", guessKind(f.name));
  msg("up-msg", `Reading ${f.name} to work out what it is…`);
  try {
    const r = await agentsBusy({ "AG-01": "working", "AG-02": "queued" }, `Checking ${f.name}`, api(W("/uploads"), { method: "POST", form: fd }));
    S.upload = r;
    if (r.routed === "document") { msg("up-msg", `${f.name} is a receipt or invoice. Fields extracted; check them, then confirm${left}.`, "good"); return renderDocConfirm(); }
    msg("up-msg", `${f.name} recognised as ${KIND[r.kind] || r.kind_label} from its columns. Confirm the mapping below${left}.`, "good"); renderMapping();
  } catch (e) { refused(f, e.message); msg("up-msg", ""); nextInQueue(); }
}
// "File 2 of 7" while a batch is being confirmed one by one.
function batchPos() {
  const n = S.queueTotal || 0;
  return n > 1 ? `File ${n - S.queue.length} of ${n}` : "";
}
// The confirm action sits in a bar at the top of the card that stays on screen while scrolling the mapping,
// and the card scrolls into view as soon as a file is read, so nobody has to hunt for the button.
function mapBar(title, sub, btn) {
  const pos = batchPos();
  return el("div", { class: "map-bar" },
    el("div", { class: "map-bar-l" }, el("div", { class: "map-title" }, title),
      el("div", { class: "map-sub" }, [pos, sub].filter(Boolean).join("  ·  "))),
    el("div", { class: "map-bar-r" }, btn));
}
function showMappingCard(box) {
  box.classList.remove("hidden");
  requestAnimationFrame(() => {
    const top = box.getBoundingClientRect().top;
    if (top < 60 || top > window.innerHeight * 0.55) box.scrollIntoView({ behavior: "smooth", block: "start" });
    const b = box.querySelector(".map-bar .primary"); if (b) b.focus({ preventScroll: true });
  });
}
function renderMapping() {
  const u = S.upload, box = $("mapping");
  const fields = Object.keys(u.proposed_mapping);
  const selects = {};
  const confirm = (label) => el("button", { class: "primary", onclick: (ev) => withBusy(ev.currentTarget, async () => {
    const mapping = {}; for (const f of fields) mapping[f] = selects[f].value || null;
    try { const r = await api(W(`/uploads/${u.id}/mapping`), { method: "POST", json: { mapping } }); box.classList.add("hidden"); msg("up-msg", "");
      toast(`${u.filename}: ${r.health ? r.health.accepted + " rows imported" : "imported"}${r.health && r.health.rejected.length ? `, ${r.health.rejected.length} rejected` : ""}.`, r.health && r.health.rejected.length ? "err" : "good");
      await refreshWs(); nextInQueue(); }
    catch (e) { if (e.status === 404) { staleUpload(); await refreshWs(); nextInQueue(); } else msg("up-msg", e.message, "err"); }
  }, "Importing…") }, label);
  put(box,
    mapBar(`Confirm columns · ${u.filename}`, `${KIND[u.kind] || u.kind_label} · ${u.rows} records`,
      confirm(S.queue.length ? "Confirm & next file →" : "Confirm & import")),
    u.detection ? el("div", { class: "row small gap" }, el("span", { class: "badge PASS" }, `Recognised as ${KIND[u.kind] || u.kind_label}`),
      el("span", { class: "muted" }, u.detection.method)) : null,
    u.duplicate_of ? el("div", { class: "msg err" }, `This exact file was already imported (${u.duplicate_of}). Confirming will not add rows twice.`) : null,
    el("div", { class: "muted small gap" }, `Suggested by ${u.mapping_method}. Change anything that is wrong, then confirm.`),
    el("table", {}, el("tr", {}, el("th", {}, "LedgerSight field"), el("th", {}, "Your column")),
      fields.map((f) => { const s = el("select", {}, el("option", { value: "" }, "— not mapped —"), u.header.map((h) => el("option", { value: h, selected: h === u.proposed_mapping[f] }, h))); selects[f] = s;
        return el("tr", {}, el("td", {}, nice(f)), el("td", {}, s)); })),
    el("details", { class: "gap map-prev" }, el("summary", {}, `Preview first ${u.preview.length} rows`),
      el("div", { class: "tablewrap" }, el("table", {}, el("tr", {}, u.header.map((h) => el("th", {}, h))), u.preview.map((r) => el("tr", {}, r.map((c) => el("td", {}, c))))))),
    el("div", { class: "row gap" }, confirm(S.queue.length ? "Confirm & next file →" : "Confirm & import")));
  showMappingCard(box);
}
// After any upload, confirmation or clear: redraw the Sources page AND refresh the book's status, so the
// stepper, Overview and the Run button know the bank statement / ledger are now present.
async function refreshWs() {
  const w = await api(W()); renderStatus(w); renderHealth(w.uploads); renderDocs(w.documents); renderHeld(w.held);
  // A file waiting for confirmation that is no longer on the client (its data was cleared, perhaps by a
  // colleague) must not stay on screen: it can no longer be imported.
  if (S.upload && !$("mapping").classList.contains("hidden") && !w.uploads.some((u) => u.id === S.upload.id)) staleUpload();
  await progress();
}
function staleUpload() {
  const name = S.upload ? S.upload.filename : "The file";
  resetIntakeUi();
  msg("up-msg", `${name} is no longer on this client: its data was cleared. Upload it again if you still need it.`, "err");
}
// Several people can work on the same client. When this tab comes back into view, re-read the client so a
// clear, upload or run done elsewhere is reflected (at most every 3 seconds).
let lastSync = 0;
function syncIfStale() {
  if (!S.ws || !S.me || document.visibilityState === "hidden" || Date.now() - lastSync < 3000) return;
  lastSync = Date.now(); refreshWs().catch(() => {});
}
window.addEventListener("focus", syncIfStale);
document.addEventListener("visibilitychange", syncIfStale);

function renderDocConfirm() {
  const u = S.upload, box = $("mapping"), inputs = {};
  const type = el("select", {}, ["receipt", "vendor_invoice", "other"].map((t) => el("option", { value: t }, nice(t))));
  const go = () => el("button", { class: "primary", onclick: async () => {
        const fields = {}; for (const k in inputs) fields[k] = inputs[k].value;
        try { await api(W(`/documents/${u.id}/confirm`), { method: "POST", json: { fields, doc_type: type.value } }); box.classList.add("hidden");
          msg("up-msg", `Document linked to reference ${fields.reference}.`, "good"); await refreshWs(); nextInQueue(); }
        catch (e) { if (e.status === 404) { staleUpload(); await refreshWs(); nextInQueue(); } else msg("up-msg", e.message, "err"); } } },
    S.queue.length ? "Confirm & next file →" : "Confirm & link document");
  put(box, mapBar(`Confirm document · ${u.filename}`, `${u.pages} page${u.pages > 1 ? "s" : ""}`, go()),
    u.duplicate_of ? el("div", { class: "msg err" }, `This exact PDF was already uploaded (${u.duplicate_of}). Confirming again replaces the same document. Nothing is counted twice.`) : null,
    u.unsafe_content.length ? el("div", { class: "exc REJECTED" }, el("div", { class: "type" }, "⚠ Instruction-like text found in this document"),
      el("div", { class: "small" }, "Treated as data only. No action was taken and none can be triggered from document text. Flagged in the evidence log:"),
      u.unsafe_content.map((f) => el("div", { class: "mono" }, `p${f.page}: “${f.source_text}”`))) : null,
    el("div", { class: "muted small" }, u.method),
    el("table", {}, el("tr", {}, ["Field", "Value (edit if wrong)", "Read from (page · exact text)", "Confidence"].map((h) => el("th", {}, h))),
      ["vendor", "reference", "date", "amount"].map((k) => { const f = u.fields[k]; const i = el("input", { value: f.value }); inputs[k] = i;
        return el("tr", {}, el("td", {}, nice(k)), el("td", {}, i), el("td", { class: "small" }, f.page ? `p${f.page} · “${f.source_text}”` : f.source_text),
          el("td", {}, el("span", { class: "badge " + (f.confidence === "high" ? "PASS" : f.confidence === "none" ? "FAIL" : "UNKNOWN") }, f.confidence))); })),
    el("div", { class: "row gap" }, el("span", { class: "small muted" }, "Document type"), type, go()));
  showMappingCard(box);
}
function renderDocs(docs) {
  if (!docs || !docs.length) return put("docs", el("div", { class: "muted" }, "No supporting documents yet."));
  put("docs", el("table", {}, el("tr", {}, ["Doc", "Type", "Reference", "Vendor", "Amount", "File", "Confirmed by"].map((h) => el("th", {}, h))),
    docs.map((d) => el("tr", {}, el("td", {}, d.id), el("td", {}, nice(d.doc_type)), el("td", {}, d.reference), el("td", {}, d.vendor_alias || ""),
      el("td", { class: "num" }, d.amount_cents != null ? usd(d.amount_cents) : ""), el("td", { class: "small" }, d.file), el("td", { class: "small" }, d.confirmed_by || "index file")))));
}

// ---------------- start here / trial home ----------------
function go(tab) { document.querySelector(`[data-tab="${tab}"]`).click(); }
window.addEventListener("popstate", () => {  // browser Back / Forward
  const h = location.hash.slice(1) || "home";
  if (h === "ask") return openAsk();
  if (S.askOpen) closeAsk();
  if (document.getElementById("p-" + h)) go(h);
});
$("reset-btn").onclick = (e) => clearClient(e.currentTarget);

// ---------------- toasts, confirm dialog, busy buttons ----------------
function toast(text, kind) {
  const box = $("toasts");
  const t = el("div", { class: "toast " + (kind || ""), role: kind === "err" ? "alert" : "status" }, text);
  box.append(t);
  while (box.children.length > 3) box.firstElementChild.remove(); // keep the newest three, never cover the page
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, kind === "err" ? 7000 : 4000);
}
function confirmDialog(title, body, okText, danger) {
  return new Promise((resolve) => {
    const close = (v) => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
    const onKey = (e) => { if (e.key === "Escape") close(false); };
    const ok = el("button", { class: danger ? "danger solid" : "primary", onclick: () => close(true) }, okText || "Confirm");
    const overlay = el("div", { class: "overlay", onclick: (e) => { if (e.target === overlay) close(false); } },
      el("div", { class: "dialog", role: "dialog", "aria-modal": "true", "aria-label": title },
        el("div", { class: "dialog-title" }, title), el("div", { class: "dialog-body" }, body),
        el("div", { class: "dialog-actions" }, el("button", { class: "secondary", onclick: () => close(false) }, "Cancel"), ok)));
    document.body.append(overlay); document.addEventListener("keydown", onKey); ok.focus();
  });
}
async function withBusy(btn, fn, label) {
  if (!btn || btn.disabled) return fn();
  const text = btn.textContent; btn.disabled = true; btn.classList.add("busy"); btn.textContent = label || "Working…";
  try { return await fn(); } finally { btn.disabled = false; btn.classList.remove("busy"); btn.textContent = text; }
}

// ---------------- reconcile ----------------
$("run-btn").onclick = async (ev) => { await withBusy(ev.currentTarget, async () => {
  msg("run-msg", "");
  await progress();  // re-read the client first: a colleague may have cleared it since this page loaded
  if (readiness()) { renderReadiness(); return msg("run-msg", "The bank statement or general ledger is missing now. Upload it on the Sources page.", "err"); }
  try { S.run = await animateRun(api(W("/reconciliation-runs"), { method: "POST" })); toast(`Reconciliation ${S.run.id} complete: ${S.run.result.matches.length} matched, ${S.run.result.exceptions.length} to review.`, "good");
    renderRun(); renderExceptions(); renderResults(); }
  catch (e) { msg("run-msg", e.message, "err"); }
}, "Running…"); renderReadiness(); };  // withBusy re-enables the button; keep it off if files are missing
function line(id) { return S.run && S.run.lines[id]; }
const KIND_SHORT = { bank: "Bank", ledger: "Ledger", ar: "Invoice", ap: "Vendor bill" };
function labelFor(id) { const l = line(id); return l && KIND_SHORT[l.kind] ? KIND_SHORT[l.kind] : id.startsWith("GL") ? "Ledger" : id.includes("INV") ? "Invoice" : "Bank"; }
function srcBox(id, label) {
  const l = line(id);
  if (!l) return el("div", { class: "srcbox" }, el("b", {}, label), id || "—");
  return el("div", { class: "srcbox" }, el("b", {}, `${label} · ${id}`),
    l.date ? el("div", {}, l.date) : null, el("div", {}, l.description || l.customer_alias || ""),
    el("div", {}, "Ref: " + (l.reference || (l.open_cents != null ? l.id : "") || "—")), el("div", { class: "amt" }, usd(l.open_cents != null ? l.open_cents : l.amount_cents)),
    l.open_cents != null ? el("div", { class: "muted small" }, "open balance") : null,
    el("div", { class: "muted small" }, `source row ${l.source_row}`));
}
function renderWorkflow() {
  renderPipelines();
  if (!S.run || !S.run.workflow) return put("workflow", el("div", { class: "muted small" }, "Run a reconciliation to see each agent step, the TrustOps decision behind it, rows in and out, and timing."));
  const w = S.run.workflow;
  put("workflow", el("div", { class: "muted small" }, "Each agent asked TrustOps before it acted. Every answer is a signed record in the audit trail."),
    el("ol", { class: "wf" }, w.map((s, i) => el("li", {},
      el("div", { class: "tl-dot" }, String(i + 1)),
      el("div", {}, el("div", { class: "code" }, `${s.agent} · ${niceActor(s.actor_id)}`), el("div", { class: "tl-title" }, `${s.name}: `, rich(s.title, false)),
        el("div", {}, rich(s.summary || "")),
        el("div", { class: "meta" }, `TrustOps ${s.decision} · ${s.policy_id} ${nice(s.policy_name)} · tool ${nice(s.action)} · step ${s.step_id}`)),
      el("div", { class: "nums" }, el("span", { class: "badge " + s.decision }, s.decision), el("div", {}, `${s.rows_in} in → ${s.rows_out} out`), el("div", {}, `${s.duration_ms} ms`))))));
}
// The six customer-facing stages of the run, from the server (never drawn from fixed text).
const STAGE_BADGE = { DONE: "ACCEPTED", BLOCKED: "BLOCKED", NOT_RUN: "IDLE" };
function renderStages() {
  const st = S.run && S.run.stages;
  if (!st || !st.length) return put("stages", el("div", { class: "muted small" }, S.run ? "This run was created before stage tracking; run reconciliation again to see each stage."
    : "Run a reconciliation to see Intake, Validate, Map, Reconcile, Exception and Complete with their real outputs."));
  const open = S.run.result.exceptions.filter((x) => x.status === "OPEN" || x.status === "RETURNED").length;
  put("stages", el("ol", { class: "stages" }, st.map((s, i) => el("li", { class: "stage " + s.status },
      el("div", { class: "sg-head" }, el("span", { class: "sg-n" }, String(i + 1)), el("b", {}, s.stage),
        el("span", { class: "badge " + (s.stage === "Complete" && open ? "OPEN" : STAGE_BADGE[s.status] || "IDLE") },
          s.stage === "Complete" && open ? `${open} open` : nice(s.status))),
      el("div", { class: "sg-sum" }, rich(s.stage !== "Complete" ? s.summary || "" : open
        ? `Reports ready; ${open} case(s) still awaiting a CPA decision, so they are not complete` : "Reports ready; every case has a CPA decision", false)),
      s.case_ids && s.case_ids.length ? el("div", { class: "sg-cases" }, s.case_ids.map((c) => caseLink(c))) : null,
      el("div", { class: "sg-meta muted" }, `${s.agents.join(" + ")} · ${(s.step_ids || []).join(", ")}`)))),
    el("div", { class: "muted small" }, `Run ${S.run.id} · trace ${S.run.trace_id} · rulebook ${S.run.rulebook_version || "–"}`));
}
function renderRun() {
  renderWorkflow(); renderReadiness(); renderStages();
  if (!S.run) { put("tiles"); put("matches", empty("No run yet", "Matched bank and ledger lines appear here after a run.")); return; }
  const r = S.run.result, open = r.exceptions.filter((e) => e.status === "OPEN").length;
  put("tiles",
    tile("Bank net movement", usd(r.bank_net_cents)), tile("Ledger net movement", usd(r.ledger_net_cents)),
    tile("Variance", usd(r.variance_cents), r.variance_cents ? "amb" : "good"),
    tile("Variance unexplained", usd(r.unexplained_variance_cents), r.unexplained_variance_cents ? "bad" : "good"),
    tile("Matched automatically", r.matches.length, "good"), tile("Exceptions to review", open, open ? "amb" : "good"));
  put("matches", el("table", {}, el("tr", {}, ["Bank", "Ledger", "Amount", "Rule · why"].map((h) => el("th", {}, h))),
    r.matches.map((m) => el("tr", { class: "click", ondblclick: () => openTrace(m.bank_id), onclick: () => put("detail", el("div", { class: "row" }, el("button", { class: "secondary", onclick: () => openTrace(m.bank_id) }, "Trace " + m.bank_id + " end to end →")), el("div", { class: "side" }, srcBox(m.bank_id, "Bank"), srcBox(m.ledger_id, "Ledger")), el("div", { class: "sugg" }, el("span", { class: "rid" }, m.rule_id), ": ", rich(m.why, false))) },
      el("td", {}, el("span", { class: "rid" }, m.bank_id)), el("td", {}, el("span", { class: "rid" }, m.ledger_id)), el("td", { class: "num" }, usd(m.amount_cents)),
      el("td", { class: "small" }, el("span", { class: "rule-id" }, m.rule_id), rich(m.why))))));
}
const tile = (k, v, cls, sub) => el("div", { class: "tile " + (cls || "") }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), sub ? el("div", { class: "s" }, sub) : null);

// ---------------- exceptions ----------------
const TYPE_LABEL = { POSSIBLE_DUPLICATE: "Possible duplicate charge", PARTIAL_RECEIPT: "Partial payment received", MISSING_GL: "Bank debit not in ledger",
  MISSING_RECEIPT: "Expense missing receipt", UNMATCHED_DEPOSIT: "Deposit not in ledger", MISSING_BANK: "Ledger entry not in bank", AMBIGUOUS: "Ambiguous — engine abstained", OVERPAYMENT: "Overpayment",
  TAX_REVIEW: "Potential state tax / nexus review", FX_REVIEW: "Foreign currency payment — approved FX rate missing" };
// Client-facing case status (an open case is never shown as completed)
const CASE_STATUS = { DUPLICATE: "Duplicate", PARTIAL: "Partial", UNMATCHED: "Unmatched", TAX_REVIEW: "Tax review", FX_REVIEW: "FX review", APPROVAL_REQUIRED: "Approval required" };
const caseStatus = (x) => x.status === "ACCEPTED" || x.status === "REJECTED" ? `${nice(x.status)} by ${x.reviewer}` : (CASE_STATUS[x.client_status] || nice(x.client_status || x.type));
const FACT_LABEL = { invoice: "Invoice", customer: "Customer", ship_from_state: "Ships from", ship_to_state: "Ships to", invoice_date: "Invoice date",
  invoice_amount: "Invoice amount", currency: "Currency", bill_currency: "Bill currency", bill_amount: "Bill amount", paid_currency: "Paid in",
  paid_amount: "Amount paid", vendor_country: "Supplier country", vendor: "Supplier" };
const INPUT_LABEL = { taxability: "Taxability in destination state", nexus_status: "Nexus status in destination state",
  dest_registration: "Seller registration in destination state", issue_date: "Effective (tax point) date" };
const DENY_TEXT = {
  MAKER_CHECKER_SELF_APPROVAL: "You proposed this resolution, so you cannot approve it yourself (maker-checker rule).",
  ROLE_NOT_AUTHORISED: "Your role cannot decide review items. A CPA reviewer must decide.",
  REQUIRED_ROLE_MISSING: "TrustOps escalated this item to a CPA reviewer.", AGENT_CANNOT_APPROVE: "Agents can never approve.",
};
function denyText(e) {
  const d = e.body && e.body.detail;
  return d && d.reason_code ? `TrustOps DENY · ${nice(d.reason_code)} · ${d.policy_id || ""}. ${DENY_TEXT[d.reason_code] || ""} The refusal is recorded in the audit trail.` : e.message;
}
function govLine(t) {
  if (!t) return null;
  return el("div", { class: "gov" }, el("span", { class: "badge " + t.decision }, "TrustOps " + t.decision),
    el("span", {}, `${t.policy_id} ${nice(t.policy_name)}`), t.required_role ? el("span", {}, `· needs a ${t.required_role_label || t.required_role}`) : null,
    el("span", { class: "muted small" }, `· policy set ${t.policy_version}`));
}
function renderExceptions() {
  if (!S.run) return put("exc-list", empty("Nothing to review yet", "Run a reconciliation first. Anything the rules can't settle on their own appears here for a person to decide."));
  if (!S.run.result.exceptions.length) return put("exc-list", empty("Nothing needs a person", `Run ${S.run.id} raised no review items.`));
  const reviewer = S.me.role === "owner_reviewer";
  if (S.excRun !== S.run.id) { S.excRun = S.run.id; S.excSeen = new Set(); S.excFilter = "open"; }
  const all = S.run.result.exceptions, isOpen = (x) => x.status === "OPEN" || x.status === "RETURNED";
  const nOpen = all.filter(isOpen).length;
  const shown = all.filter((x) => S.excFilter === "all" || (S.excFilter === "open" ? isOpen(x) || S.excSeen.has(x.id) : !isOpen(x)));
  const chip = (k, label, n) => el("button", { class: "pl-chip" + (S.excFilter === k ? " on" : ""), "aria-pressed": String(S.excFilter === k),
    onclick: () => { S.excFilter = k; S.excSeen = new Set(); renderExceptions(); } }, label, el("span", { class: "pl-n" }, n));
  const bar = el("div", { class: "pl-chips exc-filter" }, chip("open", "Open", nOpen), chip("decided", "Decided", all.length - nOpen), chip("all", "All", all.length));
  if (!shown.length) return put("exc-list", bar, S.excFilter === "open"
    ? el("div", { class: "empty" }, el("b", {}, "Every item has a decision"), el("span", {}, "This period is ready to report."),
        el("button", { class: "primary mt10", onclick: () => go("reports") }, "Download the reports →"))
    : empty("No decided items yet", "Decisions appear here once a reviewer approves, rejects or returns an item."));
  put("exc-list", bar, shown.map((x) => {
    const note = el("textarea", { rows: 2, placeholder: "Add a note (saved with your name in the audit trail)", "aria-label": "Review note" });
    const out = el("div", { class: "msg" });
    const fail = (e) => { out.textContent = denyText(e); out.className = "msg err"; renderEvidence(); };
    const act = (d) => async () => {
      try { const res = await api(W(`/runs/${S.run.id}/exceptions/${x.id}/decisions`), { method: "POST", json: { decision: d, note: note.value } });
        Object.assign(x, res); S.excSeen.add(x.id); renderExceptions(); renderRun(); renderResults(); }
      catch (e) { fail(e); }
    };
    const propose = async () => {
      try { const res = await api(W(`/runs/${S.run.id}/exceptions/${x.id}/proposals`), { method: "POST", json: { note: note.value } }); Object.assign(x, res); renderExceptions(); }
      catch (e) { fail(e); }
    };
    const actionable = x.status === "OPEN" || x.status === "RETURNED";
    const t = x.trustops;
    const rec = (id) => recCard(id);
    const refs = x.evidence_refs || [];
    return el("article", { class: "exc " + x.status, "data-exc": x.id },
      el("header", { class: "xh" },
        el("div", { class: "grow" }, el("div", { class: "xh-id" }, x.id, x.case_id && x.case_id !== x.id ? ` · Case ${x.case_id}` : "",
            x.client_status ? el("span", { class: "case-st" }, caseStatus(x)) : null),
          el("h3", { class: "xh-title" }, TYPE_LABEL[x.type] || nice(x.type)), el("p", { class: "xh-why" }, rich(x.why))),
        el("div", { class: "xh-right" }, el("div", { class: "xh-amt" }, usd(x.amount_cents)),
          el("span", { class: "badge " + x.status }, actionable ? nice(x.status) : x.status))),
      t ? el("div", { class: "xg" }, el("span", { class: "badge " + t.decision }, "TrustOps " + nice(t.decision)),
        el("span", {}, t.required_role ? `Needs a ${t.required_role_label || t.required_role}` : nice(t.policy_name))) : null,
      refs.length ? el("div", { class: "xrs" + (refs.length === 2 ? " two" : "") },
        refs.flatMap((id, i) => [i ? el("div", { class: "xr-link", "aria-hidden": "true" }, "⇄") : null, rec(id)])) : null,
      x.residual_cents != null ? el("div", { class: "xres" }, el("span", {}, "Still owed on invoice"), el("b", {}, usd(x.residual_cents))) : null,
      x.facts && Object.keys(x.facts).length ? el("div", { class: "xfacts" },
        el("div", { class: "xs-k" }, x.type === "TAX_REVIEW" ? "Facts from the source files · CPA determines nexus and taxability" : "Facts from the source files · no rate applied"),
        el("dl", {}, Object.entries(x.facts).filter(([, v]) => v).flatMap(([k, v]) => [el("dt", {}, FACT_LABEL[k] || nice(k)), el("dd", {}, String(v))])),
        x.missing_inputs && x.missing_inputs.length ? el("div", { class: "xmiss" }, el("b", {}, "Missing for CPA review: "),
          x.missing_inputs.map((k) => INPUT_LABEL[k] || nice(k)).join(" · ")) : null,
        x.reason_code ? el("div", { class: "xmiss" }, el("b", {}, "Reason: "), x.reason_code) : null,
        el("div", { class: "muted small" }, x.type === "TAX_REVIEW" ? "Review required. No tax liability or amount is asserted; nothing is filed or paid."
          : "Human review. No exchange rate is invented and the payment is not auto-matched.")) : null,
      x.suggestion ? el("div", { class: "xs" }, el("div", { class: "xs-k" }, "Suggested next step"), el("div", {}, rich(x.suggestion))) : null,
      x.proposal_note ? el("div", { class: "xs prop" }, el("div", { class: "xs-k" }, `Preparer proposal · ${x.proposed_by}`), el("div", {}, `“${x.proposal_note}”`)) : null,
      x.status === "RETURNED" ? el("div", { class: "msg" }, `Returned by ${x.reviewer}${x.review_note ? ": “" + x.review_note + "”" : ""}. Waiting for the preparer to propose again.`) : null,
      el("details", { class: "xd" }, el("summary", {}, "Details"),
        el("dl", {},
          el("dt", {}, "Rule"), el("dd", {}, el("span", { class: "rid" }, x.rule_id)),
          el("dt", {}, "Raised by"), el("dd", {}, "Reconciliation Agent (AG-03)"),
          el("dt", {}, "Proposed by"), el("dd", {}, x.proposed_by ? niceActor(x.proposed_by) : "Reconciliation Agent (deterministic rules)"),
          t ? [el("dt", {}, "Policy"), el("dd", {}, `${t.policy_id} ${nice(t.policy_name)} · policy set ${t.policy_version}`)] : null,
          refs.length ? [el("dt", {}, "Source rows"), el("dd", {}, rich(refs.map((id) => { const l = line(id); return l ? `${id}: row ${l.source_row}` : id; }).join(" · "), false))] : null,
          x.source_record_refs && x.source_record_refs.length ? [el("dt", {}, "Source files"), el("dd", {}, x.source_record_refs.join(" · "))] : null,
          x.run_id ? [el("dt", {}, "Run linkage"), el("dd", { class: "small" }, `Case ${x.case_id} · run ${x.run_id} · trace ${x.trace_id} · raised by ${x.agent_id}, routed by ${x.routed_by || "AG-04"} · rulebook ${x.rulebook_version}`)] : null)),
      actionable ? el("div", { class: "xa" }, note,
          el("div", { class: "xa-row" },
            x.bank_id ? el("button", { class: "linkbtn", onclick: () => openTrace(x.bank_id) }, "Trace " + x.bank_id + " →") : null,
            el("span", { class: "grow" }),
            el("button", { class: "secondary", onclick: act("RETURN") }, "Return to preparer"),
            el("button", { class: "no", onclick: act("REJECT") }, "Reject"),
            !reviewer ? el("button", { class: "primary", onclick: propose }, "Propose resolution for reviewer") : null,
            el("button", { class: "ok", onclick: act("ACCEPT") }, "Approve")),
          el("div", { class: "xa-hint" }, reviewer ? "You can't approve a resolution you proposed yourself (maker-checker rule)."
            : "You propose. A CPA reviewer who didn't propose it decides."), out)
        : el("div", { class: "msg good" }, `${x.status} by ${x.reviewer} · ${new Date(x.reviewed_at).toLocaleString()} · ${x.approval_id} · ${x.approval_policy || ""}${x.review_note ? " · “" + x.review_note + "”" : ""} · ${x.effect}`));
  }));
}

// ---------------- controls ----------------
const ATTACKS = [
  { t: "Agent posts a journal entry", d: "AG-03 Reconciliation asks to write the missing ledger entry into the client's books.", b: { action: "ledger.post", as_agent: "AG-03" } },
  { t: "Agent approves a review item", d: "AG-06 Query Assistant tries to approve an exception.", b: { action: "exception.decide", as_agent: "AG-06" } },
  { t: "Agent uses a tool it wasn't given", d: "AG-01 Intake tries to run the matching engine itself.", b: { action: "run.compute", as_agent: "AG-01" } },
  { t: "Post adjustment to the books", d: "You ask for the missing ledger entry to be written into the client's ledger.", b: { action: "ledger.post" } },
  { t: "Release a vendor payment", d: "You ask for the vendor on the missing-ledger item to be paid.", b: { action: "bank.payment" } },
  { t: "Email the client", d: "You ask for an email to the client about the missing receipt.", b: { action: "email.send" } },
  { t: "Send data to a public chatbot", d: "Route this run through an unapproved public LLM.", b: { action: "run.create", model_route: "public-llm" } },
  { t: "Upload a real tax return", d: "Declare an upload as tax-return data.", b: { action: "upload.create", data_class: "tax_return" } },
  { t: "Change the model route", d: "Try to re-point AI processing elsewhere.", b: { action: "model_route.change" } },
];
function renderControls() {
  put("attack-grid", ATTACKS.map((a) => el("div", { class: "card" }, el("div", { class: "card-title" }, a.t), el("div", { class: "muted small" }, a.d),
    el("div", { class: "row" }, el("button", { class: "danger", onclick: () => attempt(a) }, "Attempt")))));
  renderProof();
}
// ---------------- TrustOps proof: ALLOW / ESCALATE / DENY / DENY, read from the signed audit trail ----------------
async function renderProof() {
  if (!S.ws) return;
  let p; try { p = await api(W("/trustops/proof")); } catch (e) { return put("proof", el("div", { class: "msg err" }, e.message)); }
  const isPrep = S.me.role === "preparer";
  const firstOpen = S.run && S.run.result.exceptions.find((x) => x.status === "OPEN" || x.status === "RETURNED");
  const tryBtn = (c) => {
    if (c.key === "deny_preparer_approval" && isPrep && firstOpen) return el("button", { class: "danger", onclick: async () => {
      try { await api(W(`/runs/${S.run.id}/exceptions/${firstOpen.id}/decisions`), { method: "POST", json: { decision: "ACCEPT", note: "" } }); }
      catch (e) { toast(denyText(e), "bad"); }
      renderProof(); renderEvidence(); } }, `Try: approve ${firstOpen.case_id || firstOpen.id} as preparer`);
    if (c.key === "deny_preparer_approval" && !isPrep) return el("span", { class: "muted small" }, S.noLogin ? "Switch “Acting as” to Staff Preparer to try it." : "Sign in as a staff preparer to try it.");
    if (c.key === "deny_cross_client") return el("button", { class: "danger", onclick: async () => {
      const q = "Show bank transactions for another client";
      try { const a = await api(W("/ask"), { method: "POST", json: { question: q } }); S.asks.push({ q, a, ws: S.ws }); renderAskLog(); } catch (_) { /* refusal is the point */ }
      renderProof(); renderEvidence(); } }, "Try: ask for another client's data");
    return null;
  };
  const row = (k, v, cls) => v == null || v === "" ? null : [el("dt", {}, k), el("dd", cls ? { class: cls } : {}, v)];
  const when = (t) => new Date(t).toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
  const actor = (c) => `${niceActor(c.actor_id)}${c.agent_code ? ` (${c.agent_code})` : ""}${c.actor_role_label && !c.agent_code ? ` · ${c.actor_role_label}` : ""}`;
  put("proof",
    el("div", { class: "pf-sum" },
      el("span", { class: "badge " + p.chain.state }, `Audit chain ${p.chain.state}`),
      el("span", { class: "pf-ver" }, "Policy set ", el("span", { class: "rid" }, p.policy_version)),
      el("span", { class: "pf-note" }, p.all_shown ? "All decisions below are on record for this client." : "Any decision not yet on record shows how to produce it live.")),
    el("div", { class: "pfc-grid" }, p.cards.map((c) => {
      const t = tryBtn(c);
      return el("article", { class: "pfc " + (c.shown ? c.decision : "pending") },
        el("header", { class: "pfc-head" },
          el("span", { class: "badge " + (c.shown ? c.decision : "IDLE") }, c.decision),
          c.shown ? el("span", { class: "pfc-when" }, when(c.time_utc)) : el("span", { class: "pfc-when" }, "Not on record yet")),
        el("h3", { class: "pfc-title" }, c.title),
        el("p", { class: "pfc-what" }, c.what),
        c.shown ? el("dl", { class: "pfc-dl" },
          row("Who", actor(c)),
          row("Action", nice(c.action)),
          row("Rule", el("span", {}, el("span", { class: "rid" }, c.policy_id), ` ${nice(c.policy_name)}`)),
          row("Decided by", c.required_role_label),
          row("Outcome", String(c.result || "").replace(/;\s*(\w)/g, (_, ch) => ". " + ch.toUpperCase())),
          row("Reason", c.reason_code && c.reason_code !== "OK" ? nice(c.reason_code) : null))
          : el("div", { class: "pfc-how" }, c.how_to_show),
        c.shown ? el("div", { class: "pfc-ev", title: `Event hash ${c.event_hash}
Policy set ${c.policy_version}${c.policy_rule_version ? ` · rule v${c.policy_rule_version}` : ""}${c.delegated_by ? `
Acting for ${c.delegated_by}` : ""}${c.run_id ? `
Run ${c.run_id}` : ""}${c.trace_id ? ` · ${c.trace_id}` : ""}` },
          el("span", { class: "pfc-sig" + (c.signature === "PASS" ? " is-ok" : "") }, c.signature === "PASS" ? "✓ Signed" : `Signature ${c.signature}`),
          el("span", {}, `Event #${c.evidence_seq}`),
          c.run_id ? el("span", { class: "rid" }, c.run_id) : null) : null,
        t ? el("footer", { class: "pfc-foot" }, t) : null);
    })));
}
$("proof-refresh").onclick = renderProof;
const attempts = [];
async function attempt(a) {
  let r;
  try { r = await api(W("/actions/attempt"), { method: "POST", json: a.b }); }
  catch (e) { r = e.body || { decision: "DENY", reason_code: e.message }; }
  attempts.unshift({ a, r, at: new Date() });
  put("attack-out", el("table", {}, el("tr", {}, ["Time", "Who", "Attempt", "Decision", "Reason", "Policy", "Executed"].map((h) => el("th", {}, h))),
    attempts.map((x) => el("tr", {}, el("td", {}, x.at.toLocaleTimeString()), el("td", {}, x.r.actor ? niceActor(x.r.actor) : "You"), el("td", {}, x.a.t),
      el("td", {}, el("span", { class: "badge " + x.r.decision }, x.r.decision)),
      el("td", {}, nice(x.r.reason_code)), el("td", {}, x.r.policy_id || ""), el("td", {}, String(!!x.r.executed))))),
    el("div", { class: "msg" }, "Each refusal is now a signed record in the Audit trail and appears in the Governance report."));
}

// ---------------- evidence ----------------
async function renderEvidence() {
  if (!S.ws) return;
  const ev = await api(W("/evidence"));
  const v = ev.verification;
  S.events = ev.events; S.verify = v.state;
  const b = $("verify-badge"); b.className = "badge " + v.state;
  b.textContent = v.state === "PASS" ? `VERIFIED · ${v.checked} signed events` : `${v.state === "UNKNOWN" ? "NOT VERIFIED" : "FAILED"} · ${v.reason}`;
  for (const [id, p] of [["exp-csv", "export.csv"], ["exp-pack", "review-pack.json"]]) {
    $(id).classList.toggle("disabled", !S.run); $(id).href = S.run ? W(`/runs/${S.run.id}/${p}`) : "#";
  }
  $("exp-print").classList.toggle("disabled", !S.run);
  $("exp-audit-pdf").href = W("/audit-trail.pdf"); $("exp-audit-csv").href = W("/audit-trail.csv");
  $("exp-print").href = S.run ?`/static/pack.html?ws=${encodeURIComponent(S.ws)}&run=${encodeURIComponent(S.run.id)}` : "#";
  put("events", el("div", { class: "muted small" }, `Signing key ${ev.key_id} (${ev.key_source}). Newest first.`),
    el("table", {}, el("tr", {}, ["Time (UTC)", "Actor", "Action", "Decision", "Policy", "Source refs", "Hash"].map((h) => el("th", {}, h))),
      ev.events.slice().reverse().map((e) => el("tr", {}, el("td", { class: "small" }, e.timestamp_utc.slice(11, 19)),
        el("td", {}, e.agent_code ? el("span", {}, el("b", {}, e.agent_code), " " + niceActor(e.actor_id)) : niceActor(e.actor_id)),
        el("td", {}, nice(e.action)), el("td", {}, el("span", { class: "badge " + String(e.policy_decision).split(":")[0] }, niceDecision(e.policy_decision))),
        el("td", { class: "small" }, e.policy_id || ""),
        el("td", { class: "small" }, (e.source_line_refs || []).slice(0, 6).join(", ")), el("td", { class: "mono" }, e.event_hash.slice(0, 10) + "…")))));
}
$("ev-refresh").onclick = renderEvidence;
$("tamper-btn").onclick = async () => {
  try { const t = await api(W("/evidence/tamper-check"), { method: "POST" });
    msg("tamper-out", `Copy with event #${t.tampered_index} edited → ${t.verification.state}: ${t.verification.reason}. Real chain still ${t.original_chain.state}.`, t.verification.state === "FAIL" ? "good" : "err"); }
  catch (e) { msg("tamper-out", e.message, "err"); }
};

function renderResults() { progress(); }

// ---------------- trust ----------------
async function renderTrust() {
  const t = await api("/v1/trust");
  put("trust", el("div", { class: "grid two" },
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Data boundary"), el("ul", { class: "plain" },
      el("li", {}, `Each client book is kept separate, with its own data and its own evidence chain (${t.workspaces.length} book${t.workspaces.length === 1 ? "" : "s"} here)`),
      el("li", {}, `Declared region is ${t.region_declared}. ${t.region_note}`), el("li", {}, "Only bank statements, ledgers, invoices and receipts are allowed"),
      el("li", {}, t.hard_boundary), el("li", {}, `Data is kept in ${/^memory/.test(t.storage) ? "" : "a "}${t.storage}`), el("li", {}, t.retention))),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "AI use"), el("div", { class: "sugg" }, t.ai_note),
      el("table", {}, el("tr", {}, el("th", {}, "Worker"), el("th", {}, "Does"), el("th", {}, "Model")), t.agents.map((a) => el("tr", {}, el("td", {}, el("b", {}, a.code || ""), " " + niceActor(a.id)), el("td", {}, a.does), el("td", {}, a.model)))),
      el("div", { class: "muted small gap" }, `Allowlisted routes: ${t.model_routes_allowed.map(nice).join(", ")} · in use: ${t.model_routes_in_use.map(nice).join(", ")}`)),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Never permitted (no grant exists)"), el("ul", { class: "plain" }, t.forbidden_actions.map((f) => el("li", {}, nice(f))))),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Role permissions"), el("table", {}, Object.entries(t.roles).map(([r, a]) => el("tr", {}, el("td", {}, el("b", {}, ROLE_NAME[r] || (String(r).startsWith("agent:") ? niceActor(r) : nice(r)))), el("td", { class: "small" }, a.map(nice).join(", ")))))),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Evidence signing"), el("div", {}, `${t.evidence.algorithm} · key: ${t.evidence.key_source} · policy ${t.policy_version}`),
      el("pre", {}, t.evidence.public_key_pem), el("div", { class: "muted small" }, `Security events outside client scope: ${t.security_events}`))));
}

// ---------------- live agent monitor ----------------
const AG_STATUS = { WORKING: "Working now", IDLE: "Done", BLOCKED: "Blocked by TrustOps", NOT_USED_YET: "Not used yet" };
let agTimer = null;
function startAgentMonitor() { stopAgentMonitor(); renderAgents(); agTimer = setInterval(renderAgents, 2000); }
function stopAgentMonitor() { if (agTimer) clearInterval(agTimer); agTimer = null; }
const ago = (s) => s == null ? "" : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
function readiness() {
  const st = S.status || {};
  const need = [["bank", "Bank statement"], ["ledger", "General ledger"]].filter(([k]) => !st[k]).map(([, l]) => l);
  return need.length ? el("div", { class: "callout gap" }, el("div", {},
    el("b", {}, `Upload ${need.join(" and ")} before you can run reconciliation.`),
    el("div", { class: "small" }, "Reconciliation compares the bank statement with the general ledger, so both are required. Invoices, bills and documents are optional extras. "
      + "Test files are in the project's demo_files folder (for example demo_files/1_standard_month)."),
    el("div", { class: "row" }, el("button", { class: "primary", onclick: () => go("import") }, "Go to Sources →")))) : null;
}
function renderReadiness() {
  const box = readiness();
  put("ag-ready", box); put("recon-ready", readiness());
  $("ag-run").disabled = !!box; $("run-btn").disabled = !!box;
}
async function renderAgents() {
  renderReadiness();
  const d = await loadAgents();
  if (!d) { $("ag-live").textContent = "○ offline"; $("ag-live").classList.remove("live"); return; }
  $("ag-live").textContent = "● live · updated " + new Date().toLocaleTimeString(); $("ag-live").classList.add("live");

  put("ag-grid", d.agents.map((a) => {
    const stat =(k, label, cls) => el("div", { class: "ag-stat" + (a.counts[k] ? " " + cls : "") }, el("b", {}, a.counts[k]), el("span", {}, label));
    return el("div", { class: "card agent " + a.status },
      el("div", { class: "head" }, el("div", {}, el("div", { class: "code" }, a.code), el("div", { class: "card-title" }, a.name)),
        el("span", { class: "badge " + a.status }, a.status === "WORKING" ? el("span", { class: "pulse" }) : null, AG_STATUS[a.status] || a.status)),
      el("p", { class: "ag-does" }, a.does),
      el("div", { class: "ag-stats", title: `${a.actions} request${a.actions === 1 ? "" : "s"} checked by TrustOps` },
        stat("ALLOW", "Allowed", "is-ok"), stat("ESCALATE", "Escalated", "is-warn"), stat("DENY", "Blocked", "is-bad")),
      el("div", { class: "ag-last" }, a.last ? [el("span", { class: "muted" }, "Last action "), el("b", {}, nice(a.last.action)), el("span", { class: "muted" }, ` · ${ago(a.last.seconds_ago)}`)]
        : el("span", { class: "muted" }, a.status_note)));
  }));
  put("ag-feed", d.feed.length ? el("div", { class: "tablewrap" }, el("table", {},
    el("tr", {}, ["#", "Time (UTC)", "Agent", "Action", "TrustOps decision", "Policy", "Run", "On behalf of"].map((h) => el("th", {}, h))),
    d.feed.map((e) => el("tr", {}, el("td", {}, e.seq), el("td", { class: "small mono" }, e.at.slice(11, 19)), el("td", {}, el("b", {}, e.agent), " " + niceActor(e.actor_id)),
      el("td", {}, nice(e.action)), el("td", {}, el("span", { class: "badge " + String(e.decision).split(":")[0] }, niceDecision(e.decision))),
      el("td", {}, e.policy_id || ""), el("td", { class: "small" }, e.run_id || "—"), el("td", { class: "small" }, e.delegated_by || "")))))
    : empty("No agent activity yet", "Import files or press “Run reconciliation now”."));
}
$("ag-run").onclick = async (ev) => { await withBusy(ev.currentTarget, async () => {
  msg("ag-msg", "");
  await progress();
  if (readiness()) { renderReadiness(); return msg("ag-msg", "The bank statement or general ledger is missing now. Upload it on the Sources page.", "err"); }
  try { S.run = await animateRun(api(W("/reconciliation-runs"), { method: "POST" })); toast(`Reconciliation ${S.run.id} complete. Each agent's card shows what it did.`, "good");
    renderRun(); renderExceptions(); renderResults(); renderAgents(); }
  catch (e) { msg("ag-msg", e.message, "err"); }
}, "Running…"); renderReadiness(); };

// ---------------- agent pipeline ----------------
// Drawn only from GET /agents and the run's own `workflow` steps. Nothing here is invented:
// "working" means a request is really in flight, or a recorded step is being replayed in order;
// every number shown (rows, decision, policy, ms) is copied from the server's response.
const PIPE_IDS = ["ov-agents", "ag-pipeline", "rc-pipeline"];
const STEP_MS = 420;
const reducedMotion = () => !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
S.agents = null; S.anim = null;
async function loadAgents() {
  if (!S.ws) return null;
  try { S.agents = await api(W("/agents")); } catch (_) { return null; }
  renderPipelines(); return S.agents;
}
const escCount = (s) => (s && s.escalations ? Object.values(s.escalations).reduce((x, y) => x + y, 0) : 0);
function outcome(s) {
  if (s.status === "BLOCKED" || String(s.decision).startsWith("DENY")) return "blocked";
  return escCount(s) || String(s.decision).startsWith("ESCALATE") ? "escalated" : "done";
}
const STATE_LABEL = { idle: "Not used yet", queued: "Queued", working: "Working…", done: "Done", escalated: "Escalated",
  blocked: "Blocked by TrustOps", stopped: "Not run · stopped" };
const SVGNS = "http://www.w3.org/2000/svg";
function icon(d) {
  const s = document.createElementNS(SVGNS, "svg"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "2.6"); s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round");
  const p = document.createElementNS(SVGNS, "path"); p.setAttribute("d", d); s.append(p); return s;
}
const ICON = { done: "M5 12.5l4.5 4.5L19 7.5", escalated: "M12 7v6M12 17h.01", blocked: "M7 7l10 10M17 7L7 17", stopped: "M7 12h10" };
// What each agent achieved, in business words, read from its own recorded step summary (nothing invented).
function outcomeLine(code, s) {
  const t = String(s.summary || ""), n = (re) => { const m = t.match(re); return m ? m.slice(1) : null; };
  let m;
  if (code === "AG-01" && (m = n(/(\d+) sources present/))) return [el("b", {}, m[0]), ` files checked${/unchanged/.test(t) ? " · unchanged" : ""}`];
  if (code === "AG-02" && (m = n(/(\d+) bank and ledger rows/))) return [el("b", {}, m[0]), " lines standardised"];
  if (code === "AG-03" && (m = n(/(\d+) matched automatically, (\d+) exception/))) return [el("b", {}, m[0]), " matched · ", el("b", {}, m[1]), " for review"];
  if (code === "AG-04" && escCount(s)) return "Routed to a CPA reviewer";
  if (code === "AG-04") return "Nothing needed a person";
  if (code === "AG-05" && /ready/.test(t)) return [el("b", {}, "2"), " reports ready"];
  if (code === "AG-06" && /available/.test(t)) return "Ready for questions";
  return [el("b", {}, `${s.rows_in} → ${s.rows_out}`), " rows"];
}
function nodeFor(a, i) {
  const an = S.anim, forced = an && an.states[a.code];
  let state, step = null;
  if (forced) { state = forced; step = an.shown[a.code] || null; }
  else if (a.status === "BLOCKED") state = "blocked";
  else if (a.last_run_step) { step = a.last_run_step; state = outcome(step); }
  else state = a.status === "NOT_USED_YET" ? "idle" : "done";
  const live = !forced && a.status === "WORKING";
  let label = STATE_LABEL[state];
  if (state === "escalated" && escCount(step)) label = `${escCount(step)} sent to a person`;
  const dec = (d) => String(d).split(":")[0];
  // a decision row shows the ALLOW/ESCALATE/DENY chip on the left and the policy id on the right, so nothing is cut off
  const stats = (rows) => el("dl", { class: "pstats" }, rows.map(([k, v]) => k === "Decision"
    ? el("div", { class: "pdec" }, el("dt", {}, el("span", { class: "dchip " + dec(v[0]) }, dec(v[0]))), el("dd", {}, v[1] || ""))
    : el("div", {}, el("dt", {}, k), el("dd", {}, v))));
  const decision = (d, pol) => [d, pol];
  let data;
  if (state === "working") data = el("div", { class: "pnote" }, an.titles[a.code] || an.note || "");
  else if (state === "queued") data = el("div", { class: "pnote" }, "Waiting for the previous step");
  else if (state === "stopped") data = el("div", { class: "pnote" }, "The workflow stopped at the blocked step");
  else if (state === "blocked" && a.status === "BLOCKED" && a.last && !forced)
    data = el("div", { class: "pline" }, `Tried ${nice(a.last.action)} · ${ago(a.last.seconds_ago)}`);
  else if (step) data = el("div", { class: "pline",
    title: `${step.rows_in} → ${step.rows_out} rows · TrustOps ${dec(step.decision)} · ${step.policy_id} · ${step.duration_ms} ms` }, outcomeLine(a.code, step));
  else if (a.last) data = el("div", { class: "pline" }, `${nice(a.last.action)} · ${ago(a.last.seconds_ago)}`);
  else data = el("div", { class: "pnote" }, "No requests yet");
  const ico = ICON[state] ? icon(ICON[state]) : el("span", {}, String(i + 1));
  const cls = `pnode s-${state}` + (live ? " live" : "") + (an && an.fresh === a.code && forced !== "working" ? " fresh" : "");
  return { live, el: el("li", { class: cls, title: `${a.code} ${a.name}: ${a.does}`, "aria-label": `${a.code} ${a.name}: ${label}` },
    el("div", { class: "prail" }, el("span", { class: "pico" }, ico)),
    el("div", { class: "pbody" },
      el("div", { class: "pcode" }, a.code),
      el("div", { class: "pname" }, a.name),
      el("span", { class: "pstate" }, el("i"), label),
      data)) };
}
function renderPipelines() {
  const d = S.agents, an = S.anim;
  const skeleton = () => el("div", {}, el("span", { class: "skel w60" }), el("span", { class: "skel w90" }), el("span", { class: "skel w40" }));
  let active = 0, total = d ? d.agents.length : 6, track = null;
  if (d) {
    const nodes = d.agents.map(nodeFor);
    active = an ? Object.values(an.states).filter((s) => s === "working").length : nodes.filter((n) => n.live).length;
    track = el("ol", { class: "pipe-track", "aria-live": "polite" }, nodes.map((n) => n.el));
  }
  let foot;
  if (an && an.mode === "running") foot = "Run in progress on the server…";
  else if (an && an.mode === "replay") foot = `Replaying ${an.run} step by step from its signed record. Rows, decisions and times are the real values.`;
  else if (an && an.mode === "upload") foot = `${an.note}. Each agent asks TrustOps before it acts.`;
  else if (d && d.run_id) foot = "Every step was checked by TrustOps and signed into the audit trail.";
  else foot = "No run yet. Upload a bank statement and a ledger, then run reconciliation.";
  for (const id of PIPE_IDS) { const n = $(id); if (n) put(n, track ? track.cloneNode(true) : skeleton(), el("div", { class: "pipe-foot" }, foot)); }
  document.querySelectorAll('[data-pipe="count"]').forEach((c) => {
    c.classList.toggle("on", active > 0 || !!(an && an.mode === "running"));
    c.replaceChildren(el("i"), an && an.mode === "running" ? "Run in progress" : `${active} of ${total} agents active`);
  });
  document.querySelectorAll(".pipe").forEach((p) => p.classList.toggle("running", !!(an && an.mode === "running")));
}
// Show agents as busy while a real request is in flight; afterwards the server's own record takes over.
async function agentsBusy(states, note, promise) {
  if (S.anim) return promise;
  S.anim = { mode: "upload", states, shown: {}, titles: {}, note }; renderPipelines();
  try { return await promise; } finally { S.anim = null; loadAgents(); }
}
// A run is one synchronous request. While it is in flight every agent is queued; when the result arrives its
// recorded workflow is replayed in order. A BLOCKED/DENY step stops the pipeline exactly as the server did.
async function animateRun(promise) {
  if (!S.agents) await loadAgents();
  const codes = S.agents ? S.agents.agents.map((a) => a.code) : [];
  S.anim = { mode: "running", states: Object.fromEntries(codes.map((c) => [c, "queued"])), shown: {}, titles: {} }; renderPipelines();
  let run;
  try { run = await promise; } catch (e) { S.anim = null; loadAgents(); throw e; }
  replay(run);
  return run;
}
async function replay(run) {
  const an = S.anim; if (!an) return;
  an.mode = "replay"; an.run = run.id;
  const wf = run.workflow || [];
  const fast = reducedMotion();
  let stopped = false;
  for (const s of wf) {
    if (stopped) { an.states[s.agent] = "stopped"; continue; }
    an.states[s.agent] = "working"; an.titles[s.agent] = s.title; renderPipelines();
    if (!fast) await sleep(STEP_MS);
    an.states[s.agent] = outcome(s); an.shown[s.agent] = s; an.fresh = s.agent; renderPipelines(); an.fresh = null;
    if (an.states[s.agent] === "blocked") stopped = true;
  }
  for (const c of Object.keys(an.states)) if (an.states[c] === "queued") an.states[c] = stopped ? "stopped" : "idle";
  renderPipelines();
  if (!fast) await sleep(900);
  if (S.anim === an) { S.anim = null; await loadAgents(); }
}
// Next step "Review": open the queue on the Open filter and bring the first item that needs this person into view.
function openReview() {
  S.excFilter = "open"; S.excSeen = new Set();
  go("exceptions"); renderExceptions();
  const reviewer = S.me.role === "owner_reviewer";
  const pick = [...document.querySelectorAll("#exc-list .exc.OPEN, #exc-list .exc.RETURNED")]
    .find((n) => reviewer ? n.classList.contains("OPEN") : true) || document.querySelector("#exc-list .exc");
  if (!pick) return;
  requestAnimationFrame(() => {
    pick.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" });
    pick.classList.remove("flash"); void pick.offsetWidth; pick.classList.add("flash");
    const t = pick.querySelector("textarea"); if (t) t.focus({ preventScroll: true });
  });
}
// Open one case in the review queue: from an Overview card, a run stage or an Ask answer. Accepts the
// exception id (EX-003), the case id (CA-TAX-01) or the bank/ledger line id (HB005).
function openCase(ref) {
  if (!S.run) return go("exceptions");
  const all = S.run.result.exceptions;
  const x = all.find((e) => e.id === ref) || all.find((e) => e.case_id === ref) || all.find((e) => e.bank_id === ref || e.ledger_id === ref);
  if (!x) return toast(`${ref} is not a review item in run ${S.run.id}.`);
  const open = x.status === "OPEN" || x.status === "RETURNED";
  S.excRun = S.run.id; S.excFilter = open ? "open" : "decided"; S.excSeen = new Set();
  go("exceptions"); renderExceptions();
  const n = document.querySelector(`#exc-list [data-exc="${x.id}"]`);
  if (!n) return;
  const d = n.querySelector("details.xd"); if (d) d.open = true;
  requestAnimationFrame(() => {
    n.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "center" });
    n.classList.remove("flash"); void n.offsetWidth; n.classList.add("flash");
  });
}
const caseLink = (ref, label) => el("button", { class: "linkbtn case-link", title: "Open this case in the review queue", onclick: () => openCase(ref) }, label || ref);
function empty(title, text) { return el("div", { class: "empty" }, el("b", {}, title), text ? el("span", {}, text) : null); }

// ---------------- reports (finance + governance, same run id) ----------------
const FMT_LABEL = { pdf: "PDF", xlsx: "Excel", csv: "CSV", json: "JSON" };
function renderReports() {
  $("rep-run").textContent = S.run ? S.run.id : "none";
  $("rep-trace").textContent = S.run ? S.run.trace_id || "–" : "–";
  if (!S.run) { put("rep-cards"); put("rep-preview", el("div", { class: "muted" }, "Run a reconciliation first.")); return; }
  const cards = [
    ["finance", "Finance Reconciliation Report", "LEDGERSIGHT", "FIN-", "What happened to the financial records. Totals, matches, exceptions, outstanding balances and reviewer status."],
    ["governance", "TrustOps Governance Report", "TRUSTOPS", "GOV-", "How it happened. Every agent step, each ALLOW, ESCALATE or DENY decision with its policy version, approvals, blocked actions and evidence verification."]];
  put("rep-cards", cards.map(([k, t, by, pre, d]) => el("div", { class: "card" },
    el("div", { class: "eyebrow2" }, `${by} · ${pre}${S.run.id}`), el("div", { class: "card-title" }, t), el("div", { class: "muted small" }, d),
    el("div", { class: "fmt" }, el("button", { class: "secondary", onclick: () => previewReport(k) }, "Preview"),
      ["pdf", "xlsx", "csv", "json"].map((f) => el("a", { class: (f === "pdf" ? "primary" : "secondary") + " btnlink", href: W(`/runs/${S.run.id}/reports/${k}.${f}`), download: "", onclick: markExported }, (f === "pdf" ? "Download " : "") + FMT_LABEL[f]))))));
  put("rep-preview", el("div", { class: "muted" }, "Choose Preview on either report. Generating a report is itself recorded in the audit trail."));
  $("rep-prev-title").textContent = "Preview";
}
async function previewReport(kind) {
  let r; try { r = await api(W(`/runs/${S.run.id}/reports/${kind}.json`)); } catch (e) { return put("rep-preview", el("div", { class: "msg err" }, e.message)); }
  $("rep-prev-title").textContent = `Preview · ${r.title} · ${r.report_id}`;
  put("rep-preview", el("table", { class: "kv" }, r.summary.map(([k, v]) => el("tr", {}, el("td", { class: "muted small" }, k), el("td", { class: "small" }, String(v))))),
    r.sections.map((s) => el("div", {}, el("div", { class: "card-title gap" }, `${s.title} (${s.rows.length})`),
      s.rows.length ? el("div", { class: "tablewrap" }, el("table", {}, el("tr", {}, s.columns.map((c) => el("th", {}, c))),
        s.rows.slice(0, 60).map((row) => el("tr", {}, row.map((c) => el("td", { class: "small" }, String(c)))))))
        : el("div", { class: "muted small" }, "None."))));
}

// ---------------- Ask LedgerSight (read-only, cited) ----------------
async function renderAsk() {
  let s; try { s = await api(W("/ask/suggestions")); } catch (_) { return; }
  put("ask-chips", s.suggestions.map((q) => el("button", { class: "chip", onclick: () => { $("ask-q").value = q; askNow(); } }, q)));
}
async function askNow() {
  const q = $("ask-q").value.trim(); if (!q) return;
  $("ask-q").value = "";
  S.asks.push({ q, a: null, ws: S.ws }); renderAskLog();
  const item = S.asks[S.asks.length - 1];
  try { item.a = await api(W("/ask"), { method: "POST", json: { question: q } }); }
  catch (e) { item.a = { status: "ERROR", answer: e.message, rows: [], columns: [], citations: [] }; }
  renderAskLog();
}
// The conversation, oldest first like a chat. Answers come only from the server; nothing is generated here.
function renderAskLog() {
  const mine = S.asks.filter((x) => x.ws === S.ws);
  $("ask-empty").classList.toggle("hidden", mine.length > 0);
  const chip = (t) => el("button", { class: "chip", onclick: () => { $("ask-q").value = t; askNow(); } }, t);
  put("ask-out", mine.map(({ q, a }) => el("div", { class: "qa" },
    el("div", { class: "q-bubble" }, q),
    !a ? el("div", { class: "a-bubble" }, el("span", { class: "typing" }, el("i"), el("i"), el("i"))) : el("div", { class: "a-bubble" },
      el("div", { class: "row" },
        el("span", { class: "badge " + ({ ANSWERED: "PASS", REFUSED: "DENY" }[a.status] || "UNKNOWN") }, nice(a.status)),
        a.trustops ? el("span", { class: "badge " + a.trustops.decision, title: `${a.trustops.policy_id} ${nice(a.trustops.policy_name)}` }, `TrustOps ${a.trustops.decision} · ${a.trustops.policy_id}`) : null),
      el("div", { class: "a" }, rich(a.answer)),
      a.rows.length ? el("div", { class: "tablewrap" }, el("table", {}, el("tr", {}, a.columns.map((c) => el("th", {}, c))), a.rows.map((r) => el("tr", {}, r.map((c, i) =>
        el("td", {}, (a.columns[i] === "Exception" || a.columns[i] === "Case") && c && c !== "—"
          ? el("button", { class: "linkbtn", title: "Open this case in the review queue", onclick: () => { closeAsk(); openCase(String(c)); } }, String(c)) : String(c))))))) : null,
      a.suggestions ? el("div", { class: "chips" }, a.suggestions.map(chip)) : null,
      a.citations.length ? el("div", { class: "cites" }, "Sources", el("ul", { class: "plain" }, a.citations.map((c) => el("li", {}, el("b", {}, c.ref), ` · ${c.kind} · ${c.source}`,
        c.sha256 ? el("span", { class: "mono" }, ` · sha256 ${c.sha256.slice(0, 12)}…`) : null,
        c.kind === "source row" ? el("button", { class: "linkbtn", onclick: () => { closeAsk(); openTrace(c.ref); } }, " Trace →") : null)))) : null))));
  const sc = $("ask-scroll"); sc.scrollTop = sc.scrollHeight;
}
S.askOpen = false;
function setAsk(open) {
  S.askOpen = open;
  const d = $("ask-drawer");
  d.classList.toggle("open", open); d.setAttribute("aria-hidden", String(!open)); d.inert = !open;
  $("ask-open").setAttribute("aria-expanded", String(open)); $("ask-open").classList.toggle("on", open);
  document.body.classList.toggle("ask-open", open);
}
function openAsk() { setAsk(true); renderAsk(); renderAskLog(); setTimeout(() => $("ask-q").focus(), 120); }
function closeAsk() { if (!S.askOpen) return; setAsk(false); $("ask-open").focus(); }
$("ask-open").onclick = () => (S.askOpen ? closeAsk() : openAsk());
$("ask-close").onclick = closeAsk;
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && S.askOpen && !document.querySelector(".overlay")) closeAsk(); });
$("ask-drawer").inert = true;
$("ask-btn").onclick = askNow;
$("ask-q").addEventListener("keydown", (e) => { if (e.key === "Enter") askNow(); });

// ---------------- rulebooks (read-only policy set) ----------------
async function renderRules() {
  let r; try { r = await api("/v1/rulebooks"); } catch (e) { return put("rules", el("div", { class: "msg err" }, e.message)); }
  const roleLabel = (id) => (r.roles.find((x) => x.role === id) || {}).label || id;
  put("rules", el("div", { class: "row" }, el("span", { class: "badge" }, `Policy set ${r.version}`), el("span", { class: "badge" }, "Read-only")),
    el("div", { class: "card gap" }, el("div", { class: "card-title" }, "Policies"), el("div", { class: "tablewrap" }, el("table", {},
      el("tr", {}, ["Policy", "Name", "Version", "Effect", "Applies to", "Required role", "Rule"].map((h) => el("th", {}, h))),
      r.policies.map((p) => el("tr", {}, el("td", {}, el("b", {}, p.id)), el("td", {}, nice(p.name)), el("td", {}, p.version),
        el("td", {}, el("span", { class: "badge " + p.effect }, p.effect)),
        el("td", { class: "small" }, p.exception_types ? p.exception_types.map((t) => t === "*" ? "All other exceptions" : nice(t)).join(", ") : niceList(p.applies_to)),
        el("td", { class: "small" }, p.required_role ? roleLabel(p.required_role) : "—"), el("td", { class: "small" }, p.rule)))))),
    el("div", { class: "grid two gap" },
      el("div", { class: "card" }, el("div", { class: "card-title" }, "Registered agents and the only tools they may use"),
        el("table", {}, r.agents.map((a) => el("tr", {}, el("td", {}, el("b", {}, a.code), el("div", { class: "small muted" }, a.name)),
          el("td", { class: "small" }, a.tools.map(nice).join(", ")), el("td", { class: "small muted" }, a.model))))),
      el("div", { class: "card" }, el("div", { class: "card-title" }, "Role permissions"),
        el("table", {}, r.roles.map((x) => el("tr", {}, el("td", {}, el("b", {}, x.label)), el("td", { class: "small" }, x.actions.map(nice).join(", "))))),
        el("div", { class: "card-title gap" }, "Never permitted to anyone"), el("div", { class: "small" }, r.forbidden_actions.map(nice).join(" · ")))));
}

// ---------------- team ----------------
async function renderTeam() {
  put("inv-ws", S.wsList.map((w) => el("label", { class: "small" }, el("input", { type: "checkbox", value: w.id, checked: w.id === S.ws }), " " + proper(w.name))));
  const us = await api("/v1/users");
  put("users", el("table", {}, el("tr", {}, ["User", "Role", "Clients", "Expires", "Status", ""].map((h) => el("th", {}, h))),
    us.map((u) => el("tr", {}, el("td", {}, u.user_id), el("td", {}, u.role), el("td", { class: "small" }, u.workspaces.join(", ")), el("td", { class: "small" }, new Date(u.expires_at).toLocaleDateString()),
      el("td", {}, el("span", { class: "badge " + (u.revoked ? "DENY" : u.active ? "ALLOW" : "OPEN") }, u.revoked ? "revoked" : u.active ? "active" : "invited")),
      el("td", {}, u.user_id !== S.me.user_id && !u.revoked ? el("button", { class: "no", onclick: async () => { await api(`/v1/users/${encodeURIComponent(u.user_id)}/revoke`, { method: "POST" }); renderTeam(); } }, "Revoke") : null)))));
}
$("inv-btn").onclick = async () => {
  const ws = [...document.querySelectorAll("#inv-ws input:checked")].map((i) => i.value);
  try { const r = await api("/v1/invites", { method: "POST", json: { user_id: $("inv-user").value.trim(), display_name: $("inv-name").value.trim(), role: $("inv-role").value, workspaces: ws } });
    put("inv-out", el("div", { class: "msg good" }, "Invitation link (single use, expires " + new Date(r.expires_at).toLocaleString() + "):"), el("div", { class: "mono" }, r.invite_url)); renderTeam(); }
  catch (e) { msg("inv-out", e.message, "err"); }
};

boot();
