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
const authConfig = fetch("/v1/auth-config").then((r) => r.json()).then((c) => {
  S.mfa = !!c.mfa; $("lg-code-row").classList.toggle("hidden", !S.mfa);
  $("lg-hint").textContent = S.mfa ? "Invitation-only. Password plus authenticator code." : "Invitation-only access.";
  S.noLogin = c.login_required === false; S.demoPeople = c.demo_people || [];
  S.samples = !!c.samples_enabled;
  $("sample-box").classList.toggle("hidden", !S.samples);
  if (c.version) $("app-version").textContent = `LedgerSight v${c.version}`;
}).catch(() => {});
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
  put("ws-select", S.wsList.map((w) => el("option", { value: w.id }, w.name)));
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
  S.run = null; S.upload = null; S.agents = null; S.anim = null; renderPipelines();
  const w = await api(W());
  S.wsMeta = w;
  renderStatus(w); renderHealth(w.uploads); renderDocs(w.documents); renderHeld(w.held);
  $("mapping").classList.add("hidden");
  if (w.runs.length) S.run = await api(W(`/runs/${w.runs[w.runs.length - 1].id}`));
  renderRun(); renderExceptions(); renderControls(); await renderEvidence(); await renderTrust();
  S.asks = S.asks.filter((x) => x.ws === S.ws); renderAskLog(); if (S.askOpen) renderAsk(); if (isActive("reports")) renderReports();
  if (S.me.role === "owner_reviewer" && !S.noLogin) renderTeam();
  fillProfile(w);
  await progress();
}

// ---------------- progress: stepper + overview ----------------
S.exported = {};
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
  kids.push(el("span", { class: "pill runtag" }, "Last run ", el("b", {}, st.last_run || "none")));
  put("stepper", kids);
  const nr = $("nav-review"); nr.textContent = st.open_exceptions || ""; nr.classList.toggle("hidden", !st.open_exceptions);
  $("page-sub").textContent = [w.period, w.industry].filter(Boolean).join(" · ");
  const opt = [...$("ws-select").options].find((o) => o.value === w.id); if (opt) opt.textContent = w.name;
  S.status = st;
  renderReadiness();
  renderOverview();
}
["exp-csv", "exp-pack", "exp-print"].forEach((id) => $(id).addEventListener("click", () => { if (S.run) { S.exported[S.ws + S.run.id] = true; setTimeout(progress, 300); } }));

// ---------------- overview dashboard ----------------
function greeting() { const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; }
async function renderOverview() {
  const w = S.wsList.find((x) => x.id === S.ws); if (!w) return;
  const st = w.status;
  const state = !st.uploads ? ["Awaiting files", ""] : !st.runs ? ["Files imported", "SIMULATED"] : st.open_exceptions ? [`${st.open_exceptions} to review`, "OPEN"] : ["Ready to report", "ACCEPTED"];
  const next = !st.uploads ? ["Upload this period's files", "import"] : !(st.bank && st.ledger) ? ["Add the bank statement and ledger", "import"]
    : !st.runs ? ["Run reconciliation", "recon"] : st.open_exceptions ? [`Review ${st.open_exceptions} item${st.open_exceptions > 1 ? "s" : ""}`, "exceptions"] : ["Download the reports", "reports"];
  put("ov-hero", el("div", {}, el("div", { class: "greet" }, `${greeting()}, ${S.me.display_name}`),
      el("h2", {}, w.name, el("span", { class: "badge " + state[1] }, state[0])),
      el("div", { class: "muted small" }, [w.industry, w.period].filter(Boolean).join(" · "))),
    el("div", { class: "next" }, el("span", { class: "muted small" }, "Next step"), el("button", { class: "primary", onclick: () => go(next[1]) }, next[0] + " →")));
  const r = S.run && S.run.result;
  const bankLines = S.run ? Object.values(S.run.lines).filter((l) => l.kind === "bank").length : 0;
  const pct = r && bankLines ? Math.round(r.matches.length / bankLines * 100) : null;
  put("ov-kpis",
    tile("Sources imported", `${SOURCE_KINDS.filter((k) => st[k]).length} / ${SOURCE_KINDS.length}`, "", st.bank && st.ledger ? "bank and ledger present" : "bank and ledger needed"),
    tile("Matched automatically", r ? `${r.matches.length}` : "–", r ? "good" : "", r ? `${pct}% of ${bankLines} bank transactions` : "no run yet"),
    tile("Awaiting review", r ? st.open_exceptions : "–", r && st.open_exceptions ? "amb" : r ? "good" : "", r ? `${r.exceptions.length} raised in total` : ""),
    tile("Variance", r ? usd(r.variance_cents) : "–", "", r ? "bank net minus ledger net" : ""),
    tile("Unexplained", r ? usd(r.unexplained_variance_cents) : "–", r ? (r.unexplained_variance_cents ? "bad" : "good") : "", r ? "after review items" : ""));
  if (isActive("home")) { try { await renderEvidence(); } catch (_) { /* counts fall back to last loaded */ } }
  const ev = S.events || [];
  const cnt = (k) => ev.filter((e) => String(e.policy_decision).startsWith(k)).length;
  put("ov-gov", el("div", { class: "kpis" }, [["ALLOW", cnt("ALLOW")], ["ESCALATE", cnt("ESCALATE")], ["DENY", cnt("DENY")], ["Evidence", S.verify || "–"]]
      .map(([k, v]) => el("div", { class: "kpi" }, el("div", { class: "k" }, k), el("div", { class: "v" }, el("span", { class: "badge " + (k === "Evidence" ? v : k) }, String(v)))))),
    el("div", { class: "muted small" }, "Every request by a person or an agent is checked by TrustOps first and signed into the audit trail."));
  const ROUTINE = new Set(["workspace.read", "export.read", "query.ask"]);
  put("ov-activity", ev.filter((e) => !ROUTINE.has(e.action)).slice(-7).reverse().map((e) => el("li", {}, el("span", { class: "t" }, e.timestamp_utc.slice(11, 19)),
    el("span", {}, el("b", {}, e.agent_code || (e.actor_type === "human" ? "Person" : niceActor(e.actor_id))), ` ${nice(e.action)} `,
      el("span", { class: "badge " + String(e.policy_decision).split(":")[0] }, String(e.policy_decision).split(":")[0]),
      e.policy_id ? el("span", { class: "muted" }, ` ${e.policy_id}`) : null))));
  if (!ev.some((e) => !ROUTINE.has(e.action))) put("ov-activity", el("li", { class: "muted" }, "No activity yet. Upload files to start."));
  await loadAgents();
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
      el("ul", { class: "plain" }, (l.review ? [`${l.review.decision === "CONFIRM" ? "Confirmed" : "Rejected"} by ${l.review.by}: “${l.review.note}”`] : []).concat(l.signals).map((x) => el("li", {}, x)))),
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
      d.exceptions.map((e) => el("div", { class: "sugg" }, el("b", {}, e.id), ` · ${nice(e.type)}: ${e.why} `, el("span", { class: "badge " + (e.status === "OPEN" ? "OPEN" : "") }, nice(e.status))))) : null);
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
  const when = (x) => x ? new Date(x).toLocaleString() : "";
  put("trace-view",
    el("div", { class: "client-head" }, el("div", {}, el("div", { class: "eyebrow2" }, "TRANSACTION " + id),
      el("div", { class: "client-name" }, `${t.line.description || t.line.customer_alias || id}`),
      el("div", { class: "client-meta" }, `${t.line.date || ""} · ${usd(+t.line.amount_cents)} · from the ${t.kind} file`)),
      el("span", { class: "badge " + t.chain.state }, t.chain.state === "PASS" ? "Evidence verified" : t.chain.state)),
    el("ol", { class: "timeline" }, t.steps.map((s, i) => el("li", {},
      el("div", { class: "tl-dot" }, String(i + 1)),
      el("div", { class: "tl-body" }, el("div", { class: "tl-stage" }, s.stage), el("div", { class: "tl-title" }, s.title),
        el("div", { class: "small muted" }, [s.who ? "by " + s.who : null, when(s.when)].filter(Boolean).join(" · ")),
        Object.keys(s.detail || {}).length ? el("table", { class: "kv" }, Object.entries(s.detail).filter(([, v]) => v != null && v !== "")
          .map(([k, v]) => el("tr", {}, el("td", { class: "muted small" }, nice(k)), el("td", { class: "small" }, (k.endsWith("cents") || k === "still_owed" || k === "amount") && v !== "" && !isNaN(+v) ? usd(+v) : fmt(v))))) : null,
        s.proof ? el("div", { class: "proof" }, "Proof: " + s.proof) : null)))),
    t.related.length ? el("div", {}, el("div", { class: "card-title gap" }, "Linked records"), el("div", { class: "side" }, t.related.map((r) => srcBox(r.id, KIND_SHORT[r.kind] || labelFor(r.id))))) : null,
    el("div", { class: "card-title gap" }, `Signed evidence for this transaction (${t.events.length} records)`),
    el("div", { class: "muted small" }, `Each record is chained to the one before and signed. Whole chain: ${t.chain.reason}.`),
    el("table", {}, el("tr", {}, ["#", "Time (UTC)", "Who", "Action", "Decision", "Record hash"].map((h) => el("th", {}, h))),
      t.events.map((e) => el("tr", {}, el("td", {}, e.seq), el("td", { class: "small" }, e.timestamp_utc.slice(11, 19)), el("td", {}, niceActor(e.actor_id)), el("td", {}, nice(e.action)),
        el("td", {}, el("span", { class: "badge " + String(e.policy_decision).split(":")[0] }, String(e.policy_decision).split(":")[0])), el("td", { class: "mono" }, e.event_hash.slice(0, 16) + "…")))));
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
  if (location.hash.slice(1) !== t.dataset.tab) history.replaceState(null, "", "#" + t.dataset.tab);
  document.title = `${(t.querySelector(".lbl-t") || t).textContent.trim()} · LedgerSight`;
  window.scrollTo(0, 0);
  if (t.dataset.tab === "home") renderOverview();
  if (t.dataset.tab === "evidence") renderEvidence();
  if (t.dataset.tab === "trace") renderTraceList();
  if (t.dataset.tab === "parties") renderParties();
  if (t.dataset.tab === "reports") renderReports();
  if (t.dataset.tab === "rules") renderRules();
  if (t.dataset.tab === "agents") startAgentMonitor(); else stopAgentMonitor();
});
function isActive(tab) { return document.getElementById("p-" + tab).classList.contains("active"); }

// ---------------- import ----------------
const KIND = { bank: "Bank statement", ledger: "General ledger", ar: "Open invoices", ap: "Vendor bills", documents: "Supporting docs", parties: "Customer & vendor list" };
function renderStatus() { renderSources(); }
async function renderSources() {
  let s; try { s = await api(W("/sources")); } catch (_) { return; }
  s.kinds.forEach((k) => { KIND[k.kind] = k.label; });
  SOURCE_KINDS = s.kinds.map((k) => k.kind);
  put("src-status", s.kinds.map((k) => el("div", {}, el("span", {}, k.label, k.required ? null : el("span", { class: "muted small" }, " · optional")),
    el("span", { class: "src-right" }, k.rows ? el("span", { class: "small muted" }, `${k.rows} rows `) : null,
      el("span", { class: "badge " + k.status, title: s.status_labels[k.status] }, k.status === "NOT_CONNECTED" ? "Not yet" : nice(k.status)),
      k.status !== "NOT_CONNECTED" ? el("button", { class: "linkbtn", title: "Remove this file", onclick: () => clearSource(k.kind) }, "Clear") : null))));
  put("connectors", el("table", {}, el("tr", {}, ["Connection", "Status", "What the label means"].map((h) => el("th", {}, h))),
    s.connectors.map((c) => el("tr", {}, el("td", {}, c.name), el("td", {}, el("span", { class: "badge " + c.status }, nice(c.status))),
      el("td", { class: "small muted" }, s.status_labels[c.status])))));
  const sel = $("up-kind"), cur = sel.value;
  put(sel, el("option", { value: "auto" }, "Detect file type automatically"),
    s.kinds.map((k) => el("option", { value: k.kind }, `${k.label} · ${k.formats.filter((f) => f !== "PDF (text)").join(", ")}`)),
    el("option", { value: "pdf" }, "Invoice / receipt · PDF"));
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "auto";
}
async function clearSource(k) {
  try { const r = await api(W(`/sources/${k}/clear`), { method: "POST" });
    msg("clear-msg", `${KIND[k]} cleared` + (r.runs_withdrawn ? "; previous match results withdrawn, run Match again." : "."), "good");
    S.run = null; await refreshWs(); renderRun(); renderExceptions(); renderResults(); }
  catch (e) { msg("clear-msg", e.message, "err"); }
}
async function clearClient(btn) {
  const name = (S.wsMeta && S.wsMeta.name) || "this client";
  if (!(await confirmDialog(`Clear all data for ${name}?`,
    "This removes the imported files, reconciliation runs and review decisions. The audit trail is archived, not deleted, and the clear itself is recorded.",
    "Clear data", true))) return;
  await withBusy(btn, async () => {
    try { await api(W("/reset"), { method: "POST" }); toast(`${name}: data cleared. Upload new files to start again.`, "good");
      $("mapping").classList.add("hidden"); await switchWs(); }
    catch (e) { toast(e.message, "err"); }
  }, "Clearing…");
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
      done.flatMap((u) => u.health.rejected.map((r) => `row ${r.row}: ${r.reason}`)).join("; ")) : null,
    done.filter((u) => u.health.note).map((u) => el("div", { class: "msg" }, u.health.note)));
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
  S.queue = files; S.autoKind = true; S.queueTotal = files.length;
  msg("up-msg", `${files.length} file${files.length === 1 ? "" : "s"} from ${from}. Checking each one…`); nextInQueue();
}

const b64file = (a) => { const bin = atob(a.data_b64), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return new File([u], a.name); };
async function openEmail(f) {
  const fd = new FormData(); fd.append("file", f);
  let r; try { r = await api(W("/email"), { method: "POST", form: fd }); } catch (e) { return msg("up-msg", "Refused: " + e.message, "err"); }
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
  if (!f) { if (S.queueTotal > 1) msg("up-msg", `All ${S.queueTotal} files done.`, "good"); S.queueTotal = 0; $("up-file").value = ""; return; }
  uploadOne(f);
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
// Files the importer can read (bank files, ledgers, spreadsheets, text PDFs). Anything else is accepted too:
// it is stored unopened and held until conversion to JSON is built, and is not used by reconciliation.
const IMPORTABLE = /\.(csv|xlsx|tsv|txt|json|ofx|qfx|qbo|pdf)$/i;
async function holdOne(f) {
  const fd = new FormData(); fd.append("file", f);
  msg("up-msg", `Storing ${f.name} for conversion…`);
  try {
    const r = await api(W("/held-files"), { method: "POST", form: fd });
    toast(`${f.name}: held for conversion to JSON${r.duplicate_of ? ` (same file as ${r.duplicate_of})` : ""}. Not opened, and not used in reconciliation.`, "good");
    msg("up-msg", ""); await refreshWs();
  } catch (e) { msg("up-msg", `${f.name} could not be stored: ${e.message}`, "err"); }
  nextInQueue();
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
async function uploadOne(f) {
  if (!IMPORTABLE.test(f.name)) return holdOne(f);
  const kind = S.autoKind ? guessKind(f.name) : $("up-kind").value;
  const left = S.queue.length ? ` (${S.queue.length} more after this)` : "";
  const fd = new FormData(); fd.append("file", f);
  if (kind === "pdf") {
    msg("up-msg", `Reading ${f.name} in an isolated parser…`);
    try { S.upload = await agentsBusy({ "AG-01": "working" }, `Reading ${f.name}`, api(W("/documents"), { method: "POST", form: fd })); msg("up-msg", `${f.name}: fields extracted. Check them, then confirm${left}.`, "good"); renderDocConfirm(); }
    catch (e) { msg("up-msg", `${f.name} refused: ${e.message}`, "err"); nextInQueue(); }
    return;
  }
  fd.append("kind", kind);
  msg("up-msg", `Checking ${f.name} as ${KIND[kind]}…`);
  try { S.upload = await agentsBusy({ "AG-01": "working", "AG-02": "queued" }, `Checking ${f.name}`, api(W("/uploads"), { method: "POST", form: fd })); msg("up-msg", `${f.name} passed checks as ${KIND[kind]}. Confirm the columns below${left}.`, "good"); renderMapping(); }
  catch (e) { msg("up-msg", `${f.name} refused: ${e.message}`, "err"); nextInQueue(); }
}
function renderMapping() {
  const u = S.upload, box = $("mapping");
  box.classList.remove("hidden");
  const fields = Object.keys(u.proposed_mapping);
  const selects = {};
  put(box,
    el("div", { class: "card-title" }, `Confirm column mapping · ${u.filename} (${u.rows} rows)`),
    u.duplicate_of ? el("div", { class: "msg err" }, `This exact file was already imported (${u.duplicate_of}). Confirming will not add rows twice.`) : null,
    el("div", { class: "muted small" }, `Suggested by ${u.mapping_method}. Change anything that is wrong, then confirm.`),
    el("table", {}, el("tr", {}, el("th", {}, "LedgerSight field"), el("th", {}, "Your column")),
      fields.map((f) => { const s = el("select", {}, el("option", { value: "" }, "— not mapped —"), u.header.map((h) => el("option", { value: h, selected: h === u.proposed_mapping[f] }, h))); selects[f] = s;
        return el("tr", {}, el("td", {}, nice(f)), el("td", {}, s)); })),
    el("div", { class: "card-title gap" }, "Preview"),
    el("table", {}, el("tr", {}, u.header.map((h) => el("th", {}, h))), u.preview.map((r) => el("tr", {}, r.map((c) => el("td", {}, c))))),
    el("div", { class: "row gap" }, el("button", { class: "primary", onclick: (ev) => withBusy(ev.currentTarget, async () => {
      const mapping = {}; for (const f of fields) mapping[f] = selects[f].value || null;
      try { const r = await api(W(`/uploads/${u.id}/mapping`), { method: "POST", json: { mapping } }); box.classList.add("hidden"); msg("up-msg", "");
        toast(`${u.filename}: ${r.health ? r.health.accepted + " rows imported" : "imported"}${r.health && r.health.rejected.length ? `, ${r.health.rejected.length} rejected` : ""}.`, r.health && r.health.rejected.length ? "err" : "good");
        await refreshWs(); nextInQueue(); }
      catch (e) { msg("up-msg", e.message, "err"); }
    }, "Importing…") }, "Confirm mapping & import")));
}
$("sample-btn").onclick = (ev) => withBusy(ev.currentTarget, async () => {
  msg("up-msg", "");
  try { const r = await agentsBusy({ "AG-01": "working", "AG-02": "queued" }, "Loading all files", api(W("/samples/load"), { method: "POST" }));
    toast(`${r.length} files loaded: bank statement, ledger, invoices, vendor bills, documents and customer & vendor list. You can now run reconciliation.`, "good");
    await refreshWs(); await progress(); }
  catch (e) { msg("up-msg", e.message, "err"); }
}, "Loading…");
async function refreshWs() { const w = await api(W()); renderStatus(w); renderHealth(w.uploads); renderDocs(w.documents); renderHeld(w.held); }

function renderDocConfirm() {
  const u = S.upload, box = $("mapping"), inputs = {};
  box.classList.remove("hidden");
  const type = el("select", {}, ["receipt", "vendor_invoice", "other"].map((t) => el("option", { value: t }, nice(t))));
  put(box, el("div", { class: "card-title" }, `Confirm document fields · ${u.filename} (${u.pages} page${u.pages > 1 ? "s" : ""})`),
    u.duplicate_of ? el("div", { class: "msg err" }, `This exact PDF was already uploaded (${u.duplicate_of}). Confirming again replaces the same document; nothing is counted twice.`) : null,
    u.unsafe_content.length ? el("div", { class: "exc REJECTED" }, el("div", { class: "type" }, "⚠ Instruction-like text found in this document"),
      el("div", { class: "small" }, "Treated as data only. No action was taken and none can be triggered from document text. Flagged in the evidence log:"),
      u.unsafe_content.map((f) => el("div", { class: "mono" }, `p${f.page}: “${f.source_text}”`))) : null,
    el("div", { class: "muted small" }, u.method),
    el("table", {}, el("tr", {}, ["Field", "Value (edit if wrong)", "Read from (page · exact text)", "Confidence"].map((h) => el("th", {}, h))),
      ["vendor", "reference", "date", "amount"].map((k) => { const f = u.fields[k]; const i = el("input", { value: f.value }); inputs[k] = i;
        return el("tr", {}, el("td", {}, nice(k)), el("td", {}, i), el("td", { class: "small" }, f.page ? `p${f.page} · “${f.source_text}”` : f.source_text),
          el("td", {}, el("span", { class: "badge " + (f.confidence === "high" ? "PASS" : f.confidence === "none" ? "FAIL" : "UNKNOWN") }, f.confidence))); })),
    el("div", { class: "row gap" }, el("span", { class: "small muted" }, "Document type"), type,
      el("button", { class: "primary", onclick: async () => {
        const fields = {}; for (const k in inputs) fields[k] = inputs[k].value;
        try { await api(W(`/documents/${u.id}/confirm`), { method: "POST", json: { fields, doc_type: type.value } }); box.classList.add("hidden");
          msg("up-msg", `Document linked to reference ${fields.reference}. Re-run Match to use it.`, "good"); await refreshWs(); nextInQueue(); }
        catch (e) { msg("up-msg", e.message, "err"); } } }, "Confirm & link document")));
}
function renderDocs(docs) {
  if (!docs || !docs.length) return put("docs", el("div", { class: "muted" }, "No supporting documents yet."));
  put("docs", el("table", {}, el("tr", {}, ["Doc", "Type", "Reference", "Vendor", "Amount", "File", "Confirmed by"].map((h) => el("th", {}, h))),
    docs.map((d) => el("tr", {}, el("td", {}, d.id), el("td", {}, nice(d.doc_type)), el("td", {}, d.reference), el("td", {}, d.vendor_alias || ""),
      el("td", { class: "num" }, d.amount_cents != null ? usd(d.amount_cents) : ""), el("td", { class: "small" }, d.file), el("td", { class: "small" }, d.confirmed_by || "index file")))));
}

// ---------------- start here / trial home ----------------
function go(tab) { document.querySelector(`[data-tab="${tab}"]`).click(); }
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
$("run-btn").onclick = (ev) => withBusy(ev.currentTarget, async () => {
  msg("run-msg", "");
  try { S.run = await animateRun(api(W("/reconciliation-runs"), { method: "POST" })); toast(`Reconciliation ${S.run.id} complete: ${S.run.result.matches.length} matched, ${S.run.result.exceptions.length} to review.`, "good");
    renderRun(); renderExceptions(); renderResults(); }
  catch (e) { msg("run-msg", e.message, "err"); }
}, "Running…");
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
  put("workflow", el("div", { class: "muted small" }, `Run ${S.run.id} · trace ${S.run.trace_id} · policy set ${w[0].policy_version}. Each agent asked TrustOps before it acted; every answer is a signed record in the audit trail.`),
    el("ol", { class: "wf" }, w.map((s, i) => el("li", {},
      el("div", { class: "tl-dot" }, String(i + 1)),
      el("div", {}, el("div", { class: "code" }, `${s.agent} · ${niceActor(s.actor_id)}`), el("div", { class: "tl-title" }, `${s.name}: ${s.title}`),
        el("div", {}, s.summary || ""),
        el("div", { class: "meta" }, `TrustOps ${s.decision} · ${s.policy_id} ${nice(s.policy_name)} · tool ${nice(s.action)} · step ${s.step_id}`)),
      el("div", { class: "nums" }, el("span", { class: "badge " + s.decision }, s.decision), el("div", {}, `${s.rows_in} in → ${s.rows_out} out`), el("div", {}, `${s.duration_ms} ms`))))));
}
function renderRun() {
  renderWorkflow(); renderReadiness();
  if (!S.run) { put("tiles"); put("matches", empty("No run yet", "Matched bank and ledger lines appear here after a run.")); return; }
  const r = S.run.result, open = r.exceptions.filter((e) => e.status === "OPEN").length;
  put("tiles",
    tile("Bank net movement", usd(r.bank_net_cents)), tile("Ledger net movement", usd(r.ledger_net_cents)),
    tile("Variance", usd(r.variance_cents), r.variance_cents ? "amb" : "good"),
    tile("Variance unexplained", usd(r.unexplained_variance_cents), r.unexplained_variance_cents ? "bad" : "good"),
    tile("Matched automatically", r.matches.length, "good"), tile("Exceptions to review", open, open ? "amb" : "good"));
  put("matches", el("table", {}, el("tr", {}, ["Bank", "Ledger", "Amount", "Rule · why"].map((h) => el("th", {}, h))),
    r.matches.map((m) => el("tr", { class: "click", ondblclick: () => openTrace(m.bank_id), onclick: () => put("detail", el("div", { class: "row" }, el("button", { class: "secondary", onclick: () => openTrace(m.bank_id) }, "Trace " + m.bank_id + " end to end →")), el("div", { class: "side" }, srcBox(m.bank_id, "Bank"), srcBox(m.ledger_id, "Ledger")), el("div", { class: "sugg" }, `${m.rule_id}: ${m.why}`)) },
      el("td", {}, m.bank_id), el("td", {}, m.ledger_id), el("td", { class: "num" }, usd(m.amount_cents)),
      el("td", { class: "small" }, el("span", { class: "rule-id" }, m.rule_id), m.why)))));
}
const tile = (k, v, cls, sub) => el("div", { class: "tile " + (cls || "") }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), sub ? el("div", { class: "s" }, sub) : null);

// ---------------- exceptions ----------------
const TYPE_LABEL = { POSSIBLE_DUPLICATE: "Possible duplicate charge", PARTIAL_RECEIPT: "Partial payment received", MISSING_GL: "Bank debit not in ledger",
  MISSING_RECEIPT: "Expense missing receipt", UNMATCHED_DEPOSIT: "Deposit not in ledger", MISSING_BANK: "Ledger entry not in bank", AMBIGUOUS: "Ambiguous — engine abstained", OVERPAYMENT: "Overpayment" };
const DENY_TEXT = {
  MAKER_CHECKER_SELF_APPROVAL: "Maker-checker: you proposed this resolution, so you cannot approve it yourself.",
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
  put("exc-list", S.run.result.exceptions.map((x) => {
    const note = el("textarea", { placeholder: "Review note (recorded with your name)" });
    const out = el("div", { class: "msg" });
    const fail = (e) => { out.textContent = denyText(e); out.className = "msg err"; renderEvidence(); };
    const act = (d) => async () => {
      try { const res = await api(W(`/runs/${S.run.id}/exceptions/${x.id}/decisions`), { method: "POST", json: { decision: d, note: note.value } });
        Object.assign(x, res); renderExceptions(); renderRun(); renderResults(); }
      catch (e) { fail(e); }
    };
    const propose = async () => {
      try { const res = await api(W(`/runs/${S.run.id}/exceptions/${x.id}/proposals`), { method: "POST", json: { note: note.value } }); Object.assign(x, res); renderExceptions(); }
      catch (e) { fail(e); }
    };
    const actionable = x.status === "OPEN" || x.status === "RETURNED";
    return el("div", { class: "exc " + x.status },
      el("div", { class: "exc-head" }, el("div", {}, el("div", { class: "type" }, `${x.id} · ${TYPE_LABEL[x.type] || x.type}`), el("div", {}, x.why)),
        el("div", {}, el("span", { class: "amt" }, usd(x.amount_cents)), " ", el("span", { class: "badge " + x.status }, x.status))),
      govLine(x.trustops),
      x.residual_cents != null ? el("div", { class: "msg" }, `Still owed on invoice: ${usd(x.residual_cents)}`) : null,
      el("div", { class: "side" }, x.evidence_refs.map((id) => srcBox(id, labelFor(id)))),
      x.suggestion ? el("div", { class: "sugg" }, "Suggested next step: " + x.suggestion) : null,
      el("div", { class: "row" }, el("span", { class: "muted small" }, `Rule ${x.rule_id} · raised by AG-03 · proposed by ${x.proposed_by || "agent:recon (deterministic)"}`),
        x.bank_id ? el("button", { class: "secondary", onclick: () => openTrace(x.bank_id) }, "Trace " + x.bank_id + " →") : null),
      x.proposal_note ? el("div", { class: "sugg" }, `Preparer proposal by ${x.proposed_by}: “${x.proposal_note}”`) : null,
      x.status === "RETURNED" ? el("div", { class: "msg" }, `Returned by ${x.reviewer}${x.review_note ? ": “" + x.review_note + "”" : ""}. Waiting for the preparer to propose again.`) : null,
      actionable ? el("div", {}, note, el("div", { class: "row" },
          !reviewer ? el("button", { class: "secondary", onclick: propose }, "Propose resolution for reviewer") : null,
          el("button", { class: "ok", onclick: act("ACCEPT") }, "Approve"), el("button", { class: "no", onclick: act("REJECT") }, "Reject"),
          el("button", { class: "secondary", onclick: act("RETURN") }, "Return to preparer")),
          el("div", { class: "muted small" }, reviewer ? "You are a CPA reviewer. TrustOps will refuse an approval of a resolution you proposed yourself (maker-checker)."
            : "Preparers propose. TrustOps checks every approval on the server: only a CPA reviewer who did not propose it can decide."), out)
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
}
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
  $("exp-print").href = S.run ? `/static/pack.html?ws=${encodeURIComponent(S.ws)}&run=${encodeURIComponent(S.run.id)}` : "#";
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
      el("li", {}, `Each client book is kept separate: its own data and its own evidence chain (${t.workspaces.length} book${t.workspaces.length === 1 ? "" : "s"} here)`),
      el("li", {}, `Region declared: ${t.region_declared}. ${t.region_note}`), el("li", {}, "Data allowed: bank statements, ledgers, invoices and receipts only"),
      el("li", {}, t.hard_boundary), el("li", {}, `Storage: ${t.storage}`), el("li", {}, t.retention))),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "AI use"), el("div", { class: "sugg" }, t.ai_note),
      el("table", {}, el("tr", {}, el("th", {}, "Worker"), el("th", {}, "Does"), el("th", {}, "Model")), t.agents.map((a) => el("tr", {}, el("td", {}, el("b", {}, a.code || ""), " " + niceActor(a.id)), el("td", {}, a.does), el("td", {}, a.model)))),
      el("div", { class: "muted small gap" }, `Allowlisted routes: ${t.model_routes_allowed.map(nice).join(", ")} · in use: ${t.model_routes_in_use.map(nice).join(", ")}`)),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Never permitted (no grant exists)"), el("ul", { class: "plain" }, t.forbidden_actions.map((f) => el("li", {}, nice(f))))),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Role permissions"), el("table", {}, Object.entries(t.roles).map(([r, a]) => el("tr", {}, el("td", {}, el("b", {}, ROLE_NAME[r] || (String(r).startsWith("agent:") ? niceActor(r) : nice(r)))), el("td", { class: "small" }, a.map(nice).join(", ")))))),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Evidence signing"), el("div", {}, `${t.evidence.algorithm} · key: ${t.evidence.key_source} · policy ${t.policy_version}`),
      el("pre", {}, t.evidence.public_key_pem), el("div", { class: "muted small" }, `Security events outside client scope: ${t.security_events}`))));
}

// ---------------- live agent monitor ----------------
const AG_STATUS = { WORKING: "Working now", IDLE: "Idle · done", BLOCKED: "Blocked by TrustOps", NOT_USED_YET: "Not used yet" };
let agTimer = null;
function startAgentMonitor() { stopAgentMonitor(); renderAgents(); agTimer = setInterval(renderAgents, 2000); }
function stopAgentMonitor() { if (agTimer) clearInterval(agTimer); agTimer = null; }
const ago = (s) => s == null ? "" : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
function readiness() {
  const st = S.status || {};
  const need = [["bank", "Bank statement"], ["ledger", "General ledger"]].filter(([k]) => !st[k]).map(([, l]) => l);
  return need.length ? el("div", { class: "callout gap" }, el("div", {},
    el("b", {}, `Before you can run: upload ${need.join(" and ")}.`),
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
  $("ag-runid").textContent = d.run_id || "none"; $("ag-trace").textContent = d.trace_id || "–";
  put("ag-grid", d.agents.map((a) => {
    const s = a.last_run_step;
    return el("div", { class: "card agent " + a.status },
      el("div", { class: "head" }, el("div", {}, el("div", { class: "code" }, a.code), el("div", { class: "card-title" }, a.name)),
        el("span", { class: "badge " + a.status }, a.status === "WORKING" ? el("span", { class: "pulse" }) : null, AG_STATUS[a.status] || a.status)),
      el("div", { class: "muted small" }, a.does),
      el("div", { class: "counts" }, ["ALLOW", "ESCALATE", "DENY"].map((k) => el("span", { class: "badge " + (a.counts[k] ? k : "") }, `${k} ${a.counts[k]}`)),
        el("span", { class: "small muted" }, ` ${a.actions} request${a.actions === 1 ? "" : "s"} to TrustOps`)),
      el("div", { class: "small" }, a.last ? [el("b", {}, "Last: "), `${nice(a.last.action)} · ${niceDecision(a.last.decision)} · ${a.last.policy_id || ""} · ${ago(a.last.seconds_ago)}`] : el("span", { class: "muted" }, a.status_note)),
      el("details", {}, el("summary", {}, "Details: run step, allowed tools"),
        el("div", { class: "sugg" }, a.status_note),
        s ? el("div", {}, el("b", {}, `In run ${d.run_id}: `), `${s.title}. ${s.summary || ""}`,
          el("div", { class: "muted small" }, `${s.rows_in} rows in → ${s.rows_out} out · ${s.duration_ms} ms · ${s.status} · step ${s.step_id}`)) : null,
        el("div", { class: "tools gap" }, "Allowed tools: " + a.tools.map(nice).join(", ") + " · model: " + a.model)));
  }));
  put("ag-feed", d.feed.length ? el("div", { class: "tablewrap" }, el("table", {},
    el("tr", {}, ["#", "Time (UTC)", "Agent", "Action", "TrustOps decision", "Policy", "Run", "On behalf of"].map((h) => el("th", {}, h))),
    d.feed.map((e) => el("tr", {}, el("td", {}, e.seq), el("td", { class: "small mono" }, e.at.slice(11, 19)), el("td", {}, el("b", {}, e.agent), " " + niceActor(e.actor_id)),
      el("td", {}, nice(e.action)), el("td", {}, el("span", { class: "badge " + String(e.decision).split(":")[0] }, niceDecision(e.decision))),
      el("td", {}, e.policy_id || ""), el("td", { class: "small" }, e.run_id || "—"), el("td", { class: "small" }, e.delegated_by || "")))))
    : empty("No agent activity yet", "Import files or press “Run reconciliation now”."));
}
$("ag-run").onclick = (ev) => withBusy(ev.currentTarget, async () => {
  msg("ag-msg", "");
  try { S.run = await animateRun(api(W("/reconciliation-runs"), { method: "POST" })); toast(`Reconciliation ${S.run.id} complete. Each agent's card shows what it did.`, "good");
    renderRun(); renderExceptions(); renderResults(); renderAgents(); }
  catch (e) { msg("ag-msg", e.message, "err"); }
}, "Running…");

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
    data = stats([["Tried", nice(a.last.action)], ["Decision", decision("DENY", a.last.policy_id)], ["When", ago(a.last.seconds_ago)]]);
  else if (step) data = stats([["Rows", `${step.rows_in} → ${step.rows_out}`], ["Decision", decision(step.decision, step.policy_id)], ["Time", `${step.duration_ms} ms`]]);
  else if (a.last) data = stats([["Last", nice(a.last.action)], ["Decision", decision(a.last.decision, a.last.policy_id)], ["When", ago(a.last.seconds_ago)]]);
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
  const skeleton = () => el("div", {}, el("span", { class: "skel", style: "width:60%" }), el("span", { class: "skel", style: "width:90%" }), el("span", { class: "skel", style: "width:40%" }));
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
  else if (d && d.run_id) foot = `Last run ${d.run_id} · trace ${d.trace_id}. Every step was checked by TrustOps and signed into the audit trail.`;
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
function empty(title, text) { return el("div", { class: "empty" }, el("b", {}, title), text ? el("span", {}, text) : null); }

// ---------------- reports (finance + governance, same run id) ----------------
const FMT_LABEL = { pdf: "PDF", xlsx: "Excel", csv: "CSV", json: "JSON" };
function renderReports() {
  $("rep-run").textContent = S.run ? S.run.id : "none";
  $("rep-trace").textContent = S.run ? S.run.trace_id || "–" : "–";
  if (!S.run) { put("rep-cards"); put("rep-preview", el("div", { class: "muted" }, "Run a reconciliation first.")); return; }
  const cards = [
    ["finance", "Finance Reconciliation Report", "LEDGERSIGHT", "FIN-", "What happened to the financial records: totals, matches, exceptions, outstanding balances and reviewer status."],
    ["governance", "TrustOps Governance Report", "TRUSTOPS", "GOV-", "How it happened: every agent step, ALLOW / ESCALATE / DENY decisions with policy versions, approvals, blocked actions and evidence verification."]];
  put("rep-cards", cards.map(([k, t, by, pre, d]) => el("div", { class: "card" },
    el("div", { class: "eyebrow2" }, `${by} · ${pre}${S.run.id}`), el("div", { class: "card-title" }, t), el("div", { class: "muted small" }, d),
    el("div", { class: "fmt" }, el("button", { class: "secondary", onclick: () => previewReport(k) }, "Preview"),
      ["pdf", "xlsx", "csv", "json"].map((f) => el("a", { class: (f === "pdf" ? "primary" : "secondary") + " btnlink", href: W(`/runs/${S.run.id}/reports/${k}.${f}`), download: "" }, (f === "pdf" ? "Download " : "") + FMT_LABEL[f]))))));
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
      el("div", { class: "a" }, a.answer),
      a.rows.length ? el("div", { class: "tablewrap" }, el("table", {}, el("tr", {}, a.columns.map((c) => el("th", {}, c))), a.rows.map((r) => el("tr", {}, r.map((c) => el("td", {}, String(c))))))) : null,
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
      el("div", { class: "card" }, el("div", { class: "card-title" }, "People: role permissions"),
        el("table", {}, r.roles.map((x) => el("tr", {}, el("td", {}, el("b", {}, x.label)), el("td", { class: "small" }, x.actions.map(nice).join(", "))))),
        el("div", { class: "card-title gap" }, "Never permitted to anyone"), el("div", { class: "small" }, r.forbidden_actions.map(nice).join(" · ")))));
}

// ---------------- team ----------------
async function renderTeam() {
  put("inv-ws", S.wsList.map((w) => el("label", { class: "small" }, el("input", { type: "checkbox", value: w.id, checked: w.id === S.ws }), " " + w.name)));
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
