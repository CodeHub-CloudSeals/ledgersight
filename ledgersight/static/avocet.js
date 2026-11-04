"use strict";
const $ = (id) => document.getElementById(id);
const S = { ws: null, run: null, me: null, wsList: [], clicks: 0, upload: null };

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
const msg = (id, text, kind) => { const n = $(id); n.textContent = text || ""; n.className = "msg " + (kind || ""); };

async function api(path, opts = {}) {
  const o = { method: opts.method || "GET", headers: { "x-ledgersight": "1" }, credentials: "same-origin" };
  if (opts.json !== undefined) { o.headers["content-type"] = "application/json"; o.body = JSON.stringify(opts.json); }
  if (opts.form) o.body = opts.form;
  const r = await fetch(path, o);
  let body = null;
  try { body = await r.json(); } catch (_) {}
  if (r.status === 401 && path !== "/v1/login") { showLogin(); }
  if (!r.ok) {
    const d = body && body.detail;
    const text = typeof d === "string" ? d : d && d.reason_code ? `DENY · ${d.reason_code}` : `HTTP ${r.status}`;
    const err = new Error(text); err.status = r.status; err.body = body; throw err;
  }
  if (o.method === "POST" && S.ws && path.startsWith("/v1/workspaces")) setTimeout(progress, 0);
  return body;
}
const W = (p = "") => `/v1/workspaces/${S.ws}${p}`;

// ---------------- auth ----------------
function showLogin() { $("app").classList.add("hidden"); $("login").classList.remove("hidden"); }
fetch("/v1/auth-config").then((r) => r.json()).then((c) => {
  S.mfa = !!c.mfa; $("lg-code-row").classList.toggle("hidden", !S.mfa);
  $("lg-hint").textContent = S.mfa ? "Invitation-only. Password plus authenticator code." : "Invitation-only access.";
}).catch(() => {});
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
  try { S.me = await api("/v1/me"); } catch (_) { showLogin(); return; }
  $("login").classList.add("hidden"); $("app").classList.remove("hidden");
  const role = { owner_reviewer: "owner / reviewer", preparer: "preparer", support: "support" }[S.me.role] || S.me.role;
  $("me-name").textContent = S.me.display_name; $("me-name").parentElement.title = `${S.me.user_id} · ${role}`;
  $("avatar").title = role;
  $("avatar").textContent = S.me.display_name.split(/[\s@.()]+/).filter(Boolean).slice(0, 2).map((x) => x[0].toUpperCase()).join("");
  $("me-exp").textContent = `Access until ${new Date(S.me.expires_at).toLocaleDateString()} · Support: ${S.me.support_contact}`;
  const h = new Date().getHours();
  $("greet").textContent = `WELCOME BACK, ${S.me.display_name.split(/[\s(]/)[0].toUpperCase()}`;
  S.wsList = await api("/v1/workspaces");
  put("ws-select", S.wsList.map((w) => el("option", { value: w.id }, w.name)));
  S.ws = S.wsList[0] && S.wsList[0].id;
  if (S.wsList[0]) $("home-h").textContent = `${S.wsList[0].period.split(" ")[0]} close at a glance`;
  document.querySelector('[data-tab="team"]').classList.toggle("hidden", S.me.role !== "owner_reviewer");
  await switchWs();
}
$("ws-select").onchange = async (e) => { S.ws = e.target.value; await switchWs(); };

async function switchWs() {
  S.run = null; S.upload = null;
  const w = await api(W());
  $("ws-period").textContent = w.period;
  put("sample-links", ["bank", "ledger", "ar", "documents", "parties"].map((k) => el("a", { href: W(`/samples/${S.ws}_${k}.csv`) }, k)),
    S.ws === "harborline" ? [el("a", { href: W(`/samples/harborline_bank_alt_headers.csv`) }, "bank (other headers)"),
      el("a", { href: W(`/samples/harborline_bank.qbo`) }, "bank.qbo (QuickBooks / bank download)"), el("a", { href: W(`/samples/harborline_ledger.tsv`) }, "ledger.tsv"), el("a", { href: W(`/samples/harborline_email_from_client.eml`) }, "email_from_client.eml"),
      ...["receipt_OD-7741", "invoice_GP-5531", "invoice_suspicious", "scanned_receipt"].map((n) => el("a", { href: W(`/samples/harborline_${n}.pdf`) }, n + ".pdf"))] : null);
  renderStatus(w); renderHealth(w.uploads); renderDocs(w.documents); renderHome(w);
  $("mapping").classList.add("hidden");
  if (w.runs.length) S.run = await api(W(`/runs/${w.runs[w.runs.length - 1].id}`));
  renderRun(); renderExceptions(); renderControls(); await renderEvidence(); renderResults(); await renderTrust();
  if (S.me.role === "owner_reviewer") renderTeam();
  await progress();
}

// ---------------- progress: stepper + sidebar status ----------------
S.exported = {};
const STEPS = [["Import", "import"], ["Confirm", "import"], ["Reconcile", "recon"], ["Decide", "exceptions"], ["Sign off", "evidence"]];
async function progress() {
  S.wsList = await api("/v1/workspaces");
  const w = S.wsList.find((x) => x.id === S.ws); if (!w) return;
  const st = w.status;
  const done = [st.uploads > 0, st.bank && st.ledger, st.runs > 0, st.runs > 0 && st.open_exceptions === 0, !!S.exported[S.ws + st.last_run]];
  const cur = done.indexOf(false);
  $("ws-run").textContent = st.last_run || "none";
  const kids = [];
  STEPS.forEach(([name, tab], i) => {
    if (i) kids.push(el("span", { class: "bar" + (done[i - 1] ? " done" : "") }));
    kids.push(el("button", { class: "step" + (done[i] ? " done" : i === cur ? " cur" : ""), onclick: () => go(tab) },
      el("span", { class: "dot" }, done[i] ? "✓" : String(i + 1)), name));
  });
  put("stepper", kids);
  const navDone = { home: true, import: done[1], recon: done[2], exceptions: done[3], evidence: done[4], results: done[4] };
  document.querySelectorAll(".tab[data-step]").forEach((t) => {
    const d = t.querySelector(".dot"), ok = navDone[t.dataset.tab];
    d.classList.toggle("done", !!ok); d.textContent = ok ? "✓" : String(+t.dataset.step + 1);
  });
  renderClients();
  renderHome(w);
}
["exp-csv", "exp-pack", "exp-print"].forEach((id) => $(id).addEventListener("click", () => { if (S.run) { S.exported[S.ws + S.run.id] = true; setTimeout(progress, 300); } }));

function renderClients() {
  put("clients", S.wsList.map((w) => {
    const st = w.status;
    const state = !st.uploads ? ["Awaiting files", ""] : !st.runs ? ["Importing", "OPEN"] : st.open_exceptions ? ["Items to clear", "OPEN"] : ["Ready to sign off", "ACCEPTED"];
    const next = !st.uploads ? ["Bring in this month's files", "import"] : !(st.bank && st.ledger) ? ["Add the missing bank or ledger file", "import"]
      : !st.runs ? ["Match bank to ledger", "recon"] : st.open_exceptions ? [`Clear ${st.open_exceptions} item${st.open_exceptions > 1 ? "s" : ""}`, "exceptions"] : ["Download the sign-off pack", "evidence"];
    const open = async () => { if (S.ws !== w.id) { S.ws = w.id; $("ws-select").value = w.id; await switchWs(); } go(next[1]); };
    return el("div", { class: "card" + (w.id === S.ws ? " sel" : "") },
      el("div", { class: "client-head" }, el("div", {}, el("div", { class: "client-name" }, w.name),
        el("div", { class: "client-meta" }, `${w.industry} · ${w.period} · updated ${new Date(st.last_activity).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`)),
        el("span", { class: "badge " + state[1] }, state[0])),
      el("div", { class: "kpis" },
        [["Files in", `${["bank", "ledger", "ar", "documents", "parties"].filter((k) => st[k]).length} of 5`], ["Auto-matched", st.matched ?? "–"],
         ["To clear", st.runs ? st.open_exceptions : "–"], ["Unreconciled", st.unexplained_cents == null ? "–" : usd(st.unexplained_cents)]]
          .map(([k, v]) => el("div", { class: "kpi" }, el("div", { class: "k" }, k), el("div", { class: "v" }, v)))),
      el("div", { class: "row" }, el("span", { class: "muted small" }, "Up next"), el("button", { class: "primary", onclick: open }, next[0])));
  }));
}

// ---------------- counterparties ----------------
const LINK_BADGE = { CONFIRMED: ["Confirmed", "ACCEPTED"], LINKED: ["Linked", "PASS"], SUGGESTED: ["Check this", "OPEN"],
  AMBIGUOUS: ["Ambiguous", "OPEN"], UNLINKED: ["No evidence", ""], REJECTED: ["Rejected", "REJECTED"] };
const KIND_LABEL = { bank: "Bank", ledger: "Ledger", ar: "Invoice", documents: "Document" };
S.party = null;
async function renderParties() {
  let d; try { d = await api(W("/parties")); } catch (_) { return; }
  if (!d.register_loaded) {
    put("party-list", el("div", { class: "muted" }, "No customer & vendor list yet. Import it under Import files."));
    put("party-unlinked", "–"); return;
  }
  put("party-list", el("table", {}, d.parties.map((p) => el("tr", { class: "click" + (p.id === S.party ? " sel-row" : ""), onclick: () => openParty(p.id) },
    el("td", {}, el("b", {}, p.name), el("div", { class: "small muted" }, `${p.type} · ${p.transactions} bank txn${p.transactions === 1 ? "" : "s"}`)),
    el("td", { class: "num small" }, p.money_in_cents ? el("div", {}, "in " + usd(p.money_in_cents)) : null, p.money_out_cents ? el("div", {}, "out " + usd(p.money_out_cents)) : null),
    el("td", {}, p.needs_check ? el("span", { class: "badge OPEN" }, p.needs_check + " to check") : p.confirmed ? el("span", { class: "badge ACCEPTED" }, "confirmed") : null)))));
  put("party-unlinked", d.unlinked.length ? el("table", {}, d.unlinked.map((l) => el("tr", {},
    el("td", {}, el("b", {}, l.line_id), el("div", { class: "small muted" }, l.description)), el("td", { class: "num" }, usd(l.amount_cents)),
    el("td", {}, el("span", { class: "badge " + LINK_BADGE[l.state][1] }, LINK_BADGE[l.state][0]),
      el("div", { class: "small muted" }, l.state === "AMBIGUOUS" ? "Points at: " + l.candidates.join(", ") : "Nothing identifies the payer. Not guessed."),
      assignBox(l, d.parties))))) : el("div", { class: "muted" }, "Every bank line is linked to someone."));
}
function assignBox(l, parties) {
  const sel = el("select", {}, el("option", { value: "" }, "Assign to…"), parties.map((p) => el("option", { value: p.id }, p.name)));
  const note = el("input", { placeholder: "How do you know? (recorded)" });
  return el("div", { class: "row" }, sel, note, el("button", { class: "secondary", onclick: async () => {
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
  put("party-view",
    el("div", { class: "client-head" }, el("div", {}, el("div", { class: "eyebrow2" }, p.type.toUpperCase() + " · " + p.id), el("div", { class: "client-name" }, p.name),
      el("div", { class: "client-meta" }, "Known as: " + p.aliases.join(", ") + (p.ref_prefixes.length ? " · references " + p.ref_prefixes.join(", ") : ""))),
      el("span", { class: "badge" }, `${d.transactions.length} transaction${d.transactions.length === 1 ? "" : "s"}`)),
    el("div", { class: "kpis" }, [["Money in", usd(bank.filter((l) => l.amount_cents > 0).reduce((a, l) => a + l.amount_cents, 0))],
      ["Money out", usd(-bank.filter((l) => l.amount_cents < 0).reduce((a, l) => a + l.amount_cents, 0))],
      ["Invoiced, open", usd(d.owed_cents)], ["Still owed after payments", d.residual_cents ? usd(d.residual_cents) : "–"]]
      .map(([k, v]) => el("div", { class: "kpi" }, el("div", { class: "k" }, k), el("div", { class: "v" }, v)))),
    el("div", { class: "card-title" }, "Transactions with " + p.name),
    el("div", { class: "muted small" }, "One row per transaction. The matching ledger entry, invoice and documents are shown inside it. Confirm or reject the link; your decision is signed into the audit trail."),
    el("table", {}, el("tr", {}, ["Transaction", "Date", "Amount", "Why it's theirs", "Status", ""].map((h) => el("th", {}, h))),
      d.transactions.map((l) => el("tr", {}, el("td", {}, el("b", {}, l.line_id), el("span", { class: "small muted" }, " · " + KIND_LABEL[l.kind]), el("div", { class: "small muted" }, l.description),
          l.attached.length ? el("div", { class: "attached" }, l.attached.map((x) => el("div", {}, `${KIND_LABEL[x.kind]}: ${x.line_id}` + (x.kind === "ar" && x.open_cents != null ? ` (open ${usd(x.open_cents)})` : "")))) : null),
        el("td", { class: "small" }, l.date), el("td", { class: "num" }, l.amount_cents != null ? usd(l.amount_cents) : ""),
        el("td", { class: "small" }, el("ul", { class: "plain" }, (l.review ? [`${l.review.decision === "CONFIRM" ? "Confirmed" : "Rejected"} by ${l.review.by}: “${l.review.note}”`] : []).concat(l.signals).map((x) => el("li", {}, x)))),
        el("td", {}, el("span", { class: "badge " + LINK_BADGE[l.state][1] }, LINK_BADGE[l.state][0])),
        el("td", {}, l.state !== "CONFIRMED" ? el("button", { class: "ok", onclick: act(l, "CONFIRM") }, "Confirm") : null,
          l.state !== "CONFIRMED" ? el("button", { class: "no", onclick: act(l, "REJECT") }, "Not them") : null,
          ["bank", "ledger", "ar"].includes(l.kind) ? el("button", { class: "secondary", onclick: () => openTrace(l.line_id) }, "Trace") : null)))),
    d.exceptions.length ? el("div", {}, el("div", { class: "card-title gap" }, "Open questions involving " + p.name),
      d.exceptions.map((e) => el("div", { class: "sugg" }, `${e.id} · ${e.type.replace(/_/g, " ").toLowerCase()}: ${e.why} — ${e.status}`))) : null);
}

// ---------------- trace ----------------
async function renderTraceList(selected) {
  let d; try { d = await api(W("/trace")); } catch (_) { return; }
  if (!d.lines.length) return put("trace-list", el("div", { class: "muted" }, "Import a bank statement first."));
  put("trace-list", el("div", { class: "muted small" }, d.run_id ? `Status from ${d.run_id}` : "Not matched yet: run Match to see outcomes."),
    el("table", {}, d.lines.map((l) => el("tr", { class: "click" + (l.id === selected ? " sel-row" : ""), onclick: () => openTrace(l.id) },
      el("td", {}, el("b", {}, l.id), el("div", { class: "small muted" }, l.description)), el("td", { class: "num" }, usd(l.amount_cents)),
      el("td", {}, el("span", { class: "badge " + ({ MATCHED: "ACCEPTED", OPEN: "OPEN", ACCEPTED: "ACCEPTED", REJECTED: "REJECTED" }[l.status] || "") }, l.label))))));
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
          .map(([k, v]) => el("tr", {}, el("td", { class: "muted small" }, k.replace(/_/g, " ")), el("td", { class: "small" }, (k.endsWith("cents") || k === "still_owed" || k === "amount") && v !== "" && !isNaN(+v) ? usd(+v) : fmt(v))))) : null,
        s.proof ? el("div", { class: "proof" }, "Proof: " + s.proof) : null)))),
    t.related.length ? el("div", {}, el("div", { class: "card-title gap" }, "Linked records"), el("div", { class: "side" }, t.related.map((r) => srcBox(r.id, r.id.startsWith("GL") ? "Ledger" : r.id.includes("INV") ? "Invoice" : "Bank")))) : null,
    el("div", { class: "card-title gap" }, `Signed evidence for this transaction (${t.events.length} records)`),
    el("div", { class: "muted small" }, `Each record is chained to the one before and signed. Whole chain: ${t.chain.reason}.`),
    el("table", {}, el("tr", {}, ["#", "Time (UTC)", "Who", "Action", "Decision", "Record hash"].map((h) => el("th", {}, h))),
      t.events.map((e) => el("tr", {}, el("td", {}, e.seq), el("td", { class: "small" }, e.timestamp_utc.slice(11, 19)), el("td", {}, e.actor_id), el("td", {}, e.action),
        el("td", {}, el("span", { class: "badge " + String(e.policy_decision).split(":")[0] }, String(e.policy_decision).split(":")[0])), el("td", { class: "mono" }, e.event_hash.slice(0, 16) + "…")))));
}

// ---------------- theme ----------------
(function theme() {
  let t = null; try { t = localStorage.getItem("ls-theme"); } catch (_) {}
  if (t) document.documentElement.dataset.theme = t;
  $("theme").onclick = () => {
    const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("ls-theme", document.documentElement.dataset.theme); } catch (_) {}
  };
})();

// ---------------- tabs + click counter ----------------
document.querySelectorAll(".tab").forEach((t) => t.onclick = () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "p-" + t.dataset.tab));
  if (t.dataset.tab === "evidence") renderEvidence();
  if (t.dataset.tab === "trace") renderTraceList();
  if (t.dataset.tab === "parties") renderParties();
});
document.addEventListener("click", (e) => { if (e.target.closest("button,a,tr.click")) { S.clicks++; $("clicks").textContent = S.clicks; } });

// ---------------- import ----------------
const KIND = { bank: "Bank statement", ledger: "General ledger", ar: "Open invoices", documents: "Supporting docs", parties: "Customer & vendor list" };
function renderStatus(w) {
  put("src-status", Object.keys(KIND).map((k) => el("div", {}, el("span", {}, KIND[k]),
    el("span", { class: "src-right" }, el("span", { class: "badge " + (w.status[k] ? "ACCEPTED" : "OPEN") }, w.status[k] ? "Imported" : "Not yet"),
      w.status[k] ? el("button", { class: "linkbtn", title: "Remove this file", onclick: () => clearSource(k) }, "Clear") : null))));
}
async function clearSource(k) {
  try { const r = await api(W(`/sources/${k}/clear`), { method: "POST" });
    msg("clear-msg", `${KIND[k]} cleared` + (r.runs_withdrawn ? "; previous match results withdrawn, run Match again." : "."), "good");
    S.run = null; await refreshWs(); renderRun(); renderExceptions(); renderResults(); }
  catch (e) { msg("clear-msg", e.message, "err"); }
}
let clearArmed = false;
$("clear-all").onclick = async () => {
  if (!clearArmed) { clearArmed = true; $("clear-all").textContent = "Click again to clear everything"; msg("clear-msg", "Removes all imported files, match results and decisions for this client. The clear itself is recorded."); return; }
  clearArmed = false; $("clear-all").textContent = "Clear all imported files";
  try { await api(W("/reset"), { method: "POST" }); msg("clear-msg", "All files cleared. Start again with a fresh upload.", "good"); $("mapping").classList.add("hidden"); await switchWs(); go("import"); }
  catch (e) { msg("clear-msg", e.message, "err"); }
};
function renderHealth(uploads) {
  const done = uploads.filter((u) => u.health);
  if (!done.length) return put("health", el("div", { class: "muted" }, "Nothing imported yet."));
  put("health", el("table", {},
    el("tr", {}, ["Source", "File", "State", "Rows received", "Accepted", "Rejected", "Debits", "Credits", "Control total", "SHA-256"].map((h) => el("th", {}, h))),
    done.map((u) => el("tr", {},
      el("td", {}, KIND[u.kind]), el("td", {}, u.filename), el("td", {}, el("span", { class: "badge " + (u.state === "VALIDATED" ? "ACCEPTED" : "OPEN") }, u.state)),
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
// One "Choose files…" button; the source dropdown decides where files come from.
$("choose-btn").onclick = () => {
  const src = $("src-select").value;
  put("em-view"); put("drive-view");
  if (src === "computer") $("up-file").click();
  else if (src === "email") $("em-file").click();
  else renderDrive(true);
};
$("src-select").onchange = () => {
  const src = $("src-select").value;
  $("choose-btn").textContent = src === "computer" ? "Choose files…" : src === "email" ? "Choose email…" : "Open Google Drive…";
  $("up-kind").disabled = src !== "computer";
  put("em-view"); put("drive-view");
};
$("up-file").onchange = () => {
  S.queue = [...$("up-file").files]; S.autoKind = $("up-kind").value === "auto";
  if (!S.queue.length) return;
  S.queueTotal = S.queue.length; nextInQueue();
};
$("em-file").onchange = () => openEmail();
function queueFiles(files, from) {
  S.queue = files; S.autoKind = true; S.queueTotal = files.length;
  msg("up-msg", `${files.length} file${files.length === 1 ? "" : "s"} from ${from}. Checking each one…`); nextInQueue();
}

const b64file = (a) => { const bin = atob(a.data_b64), u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return new File([u], a.name); };
async function openEmail() {
  const f = $("em-file").files[0]; if (!f) return msg("up-msg", "Choose a saved email (.eml) first.", "err");
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
  $("em-file").value = "";
}
let driveCfg = null;
const loadScript = (src) => new Promise((ok, bad) => { const s = document.createElement("script"); s.src = src; s.onload = ok; s.onerror = bad; document.head.append(s); });
async function renderDrive(open) {
  if (!driveCfg) driveCfg = (await api("/v1/integrations")).google_drive;
  if (driveCfg.enabled && open) return openDrivePicker();
  if (!driveCfg.enabled) return put("drive-view", el("div", { class: "card gap" },
    el("div", { class: "card-title" }, "Google Drive is not connected yet"),
    el("ol", { class: "plain small" }, el("li", {}, "In Google Cloud Console, enable the Google Drive API and Google Picker API."),
      el("li", {}, "Create an OAuth client (Web application) with origin http://127.0.0.1:8090, and an API key."),
      el("li", {}, "Start the app with GOOGLE_CLIENT_ID=… GOOGLE_API_KEY=… ./run_avocet.sh")),
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
        .setCallback(async (d) => {
          if (d.action !== google.picker.Action.PICKED) return;
          const files = [];
          for (const doc of d.docs) {
            const sheet = doc.mimeType === "application/vnd.google-apps.spreadsheet";
            const url = sheet ? `https://www.googleapis.com/drive/v3/files/${doc.id}/export?mimeType=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
                              : `https://www.googleapis.com/drive/v3/files/${doc.id}?alt=media`;
            const r = await fetch(url, { headers: { Authorization: "Bearer " + t.access_token } });
            if (!r.ok) { msg("up-msg", `Could not read ${doc.name} from Drive.`, "err"); continue; }
            files.push(new File([await r.blob()], sheet ? doc.name + ".xlsx" : doc.name));
          }
          if (files.length) queueFiles(files, "Google Drive");
        }).build().setVisible(true);
    } });
    tc.requestAccessToken();
  } catch (e) { msg("up-msg", "Google Drive could not load: " + e.message, "err"); }
}
function nextInQueue() {
  const f = S.queue.shift();
  if (!f) { if (S.queueTotal > 1) msg("up-msg", `All ${S.queueTotal} files done.`, "good"); S.queueTotal = 0; $("up-file").value = ""; return; }
  uploadOne(f);
}
function guessKind(name) {
  const n = name.toLowerCase();
  if (n.endsWith(".pdf")) return "pdf";
  if (/\.(ofx|qfx|qbo)$/.test(n) || /bank|statement/.test(n)) return "bank";
  if (/receipt|invoice_|bill/.test(n) && n.endsWith(".pdf")) return "pdf";
  if (/ledger|journal|\bgl\b/.test(n)) return "ledger";
  if (/\bar\b|_ar\.|invoice|receivable/.test(n)) return "ar";
  if (/vendor|customer|parties|contacts/.test(n)) return "parties";
  if (/document|receipt/.test(n)) return "documents";
  return "bank";
}
async function uploadOne(f) {
  const kind = S.autoKind ? guessKind(f.name) : $("up-kind").value;
  const left = S.queue.length ? ` (${S.queue.length} more after this)` : "";
  const fd = new FormData(); fd.append("file", f);
  if (kind === "pdf") {
    msg("up-msg", `Reading ${f.name} in an isolated parser…`);
    try { S.upload = await api(W("/documents"), { method: "POST", form: fd }); msg("up-msg", `${f.name}: fields extracted. Check them, then confirm${left}.`, "good"); renderDocConfirm(); }
    catch (e) { msg("up-msg", `${f.name} refused: ${e.message}`, "err"); nextInQueue(); }
    return;
  }
  fd.append("kind", kind);
  msg("up-msg", `Checking ${f.name} as ${KIND[kind]}…`);
  try { S.upload = await api(W("/uploads"), { method: "POST", form: fd }); msg("up-msg", `${f.name} passed checks as ${KIND[kind]}. Confirm the columns below${left}.`, "good"); renderMapping(); }
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
        return el("tr", {}, el("td", {}, f), el("td", {}, s)); })),
    el("div", { class: "card-title gap" }, "Preview"),
    el("table", {}, el("tr", {}, u.header.map((h) => el("th", {}, h))), u.preview.map((r) => el("tr", {}, r.map((c) => el("td", {}, c))))),
    el("div", { class: "row gap" }, el("button", { class: "primary", onclick: async () => {
      const mapping = {}; for (const f of fields) mapping[f] = selects[f].value || null;
      try { await api(W(`/uploads/${u.id}/mapping`), { method: "POST", json: { mapping } }); box.classList.add("hidden"); msg("up-msg", `${u.filename} imported.`, "good"); await refreshWs(); nextInQueue(); }
      catch (e) { msg("up-msg", e.message, "err"); }
    } }, "Confirm mapping & import")));
}
$("sample-btn").onclick = async () => {
  msg("up-msg", "Loading files…");
  try { await api(W("/samples/load"), { method: "POST" }); msg("up-msg", "Bank, ledger, invoices, documents and customer & vendor list imported.", "good"); await refreshWs(); }
  catch (e) { msg("up-msg", e.message, "err"); }
};
async function refreshWs() { const w = await api(W()); renderStatus(w); renderHealth(w.uploads); renderDocs(w.documents); renderHome(w); }

function renderDocConfirm() {
  const u = S.upload, box = $("mapping"), inputs = {};
  box.classList.remove("hidden");
  const type = el("select", {}, ["receipt", "vendor_invoice", "other"].map((t) => el("option", { value: t }, t)));
  put(box, el("div", { class: "card-title" }, `Confirm document fields · ${u.filename} (${u.pages} page${u.pages > 1 ? "s" : ""})`),
    u.unsafe_content.length ? el("div", { class: "exc REJECTED" }, el("div", { class: "type" }, "⚠ Instruction-like text found in this document"),
      el("div", { class: "small" }, "Treated as data only. No action was taken and none can be triggered from document text. Flagged in the evidence log:"),
      u.unsafe_content.map((f) => el("div", { class: "mono" }, `p${f.page}: “${f.source_text}”`))) : null,
    el("div", { class: "muted small" }, u.method),
    el("table", {}, el("tr", {}, ["Field", "Value (edit if wrong)", "Read from (page · exact text)", "Confidence"].map((h) => el("th", {}, h))),
      ["vendor", "reference", "date", "amount"].map((k) => { const f = u.fields[k]; const i = el("input", { value: f.value }); inputs[k] = i;
        return el("tr", {}, el("td", {}, k), el("td", {}, i), el("td", { class: "small" }, f.page ? `p${f.page} · “${f.source_text}”` : f.source_text),
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
    docs.map((d) => el("tr", {}, el("td", {}, d.id), el("td", {}, d.doc_type), el("td", {}, d.reference), el("td", {}, d.vendor_alias || ""),
      el("td", { class: "num" }, d.amount_cents != null ? usd(d.amount_cents) : ""), el("td", { class: "small" }, d.file), el("td", { class: "small" }, d.confirmed_by || "index file")))));
}

// ---------------- start here / trial home ----------------
const TASKS = [
  { k: "A", t: "Reconcile a month of bank activity", steps: ["Import files: press “Load this client's files”, or download a bank file and upload it", "Confirm the suggested columns and check the row counts", "Match: run it and open any paired row"], tab: "import" },
  { k: "B", t: "Resolve a tricky transaction", steps: ["Review queue: open HB005, a $900 payment on a $1,200 invoice", "Compare the source rows and read the suggestion", "Accept or reject with a note; your name is stamped on it", "Import the OD-7741 receipt PDF, match again, and HB009 drops off"], tab: "exceptions" },
  { k: "C", t: "Verify what the AI was allowed to do", steps: ["Guardrails: ask it to post to the books, pay a vendor or email the client", "Audit trail: each refusal is a signed entry; try the tamper test", "Data & security: see the allowed models, region and actions"], tab: "controls" },
];
function go(tab) { document.querySelector(`[data-tab="${tab}"]`).click(); }
function renderHome(w) {
  put("tasks", TASKS.map((x) => el("div", { class: "card" }, el("div", { class: "eyebrow" }, "TASK " + x.k), el("div", { class: "card-title" }, x.t),
    el("ol", { class: "plain small" }, x.steps.map((s) => el("li", {}, s))), el("button", { class: "primary", onclick: () => go(x.tab) }, "Start task " + x.k))));
  put("home-status", `${w.name} · ${w.period} · ${["bank", "ledger", "ar", "documents"].filter((k) => w.status[k]).length}/4 sources imported · ${w.status.runs} run(s). Tax returns, SSNs and bank logins are never accepted.`);
}
let resetArmed = false;
$("reset-btn").onclick = async () => {
  if (!resetArmed) { resetArmed = true; $("reset-btn").textContent = "Click again to confirm reset"; msg("reset-msg", "This deletes this client's uploads, runs and decisions. The reset itself is recorded."); return; }
  resetArmed = false; $("reset-btn").textContent = "Reset this client's workspace";
  try { await api(W("/reset"), { method: "POST" }); msg("reset-msg", "Workspace reset. Start again from task A.", "good"); await switchWs(); }
  catch (e) { msg("reset-msg", e.message, "err"); }
};

// ---------------- reconcile ----------------
$("run-btn").onclick = async () => {
  msg("run-msg", "Running…");
  try { S.run = await api(W("/reconciliation-runs"), { method: "POST" }); msg("run-msg", `Run ${S.run.id} complete.`, "good"); renderRun(); renderExceptions(); renderResults(); }
  catch (e) { msg("run-msg", e.message, "err"); }
};
function line(id) { return S.run && S.run.lines[id]; }
function srcBox(id, label) {
  const l = line(id);
  if (!l) return el("div", { class: "srcbox" }, el("b", {}, label), id || "—");
  return el("div", { class: "srcbox" }, el("b", {}, `${label} · ${id}`),
    l.date ? el("div", {}, l.date) : null, el("div", {}, l.description || l.customer_alias || ""),
    el("div", {}, "Ref: " + (l.reference || (l.open_cents != null ? l.id : "") || "—")), el("div", { class: "amt" }, usd(l.open_cents != null ? l.open_cents : l.amount_cents)),
    l.open_cents != null ? el("div", { class: "muted small" }, "open balance") : null,
    el("div", { class: "muted small" }, `source row ${l.source_row}`));
}
function renderRun() {
  if (!S.run) { put("tiles"); put("matches", el("div", { class: "muted" }, "No run yet.")); return; }
  const r = S.run.result, open = r.exceptions.filter((e) => e.status === "OPEN").length;
  put("tiles",
    tile("Bank net movement", usd(r.bank_net_cents)), tile("Ledger net movement", usd(r.ledger_net_cents)),
    tile("Variance", usd(r.variance_cents), r.variance_cents ? "amb" : "good"),
    tile("Variance unexplained", usd(r.unexplained_variance_cents), r.unexplained_variance_cents ? "bad" : "good"),
    tile("Matched automatically", r.matches.length, "good"), tile("Exceptions to review", open, open ? "amb" : "good"));
  put("matches", el("table", {}, el("tr", {}, ["Bank", "Ledger", "Amount", "Rule", "Why"].map((h) => el("th", {}, h))),
    r.matches.map((m) => el("tr", { class: "click", ondblclick: () => openTrace(m.bank_id), onclick: () => put("detail", el("div", { class: "row" }, el("button", { class: "secondary", onclick: () => openTrace(m.bank_id) }, "Trace " + m.bank_id + " end to end →")), el("div", { class: "side" }, srcBox(m.bank_id, "Bank"), srcBox(m.ledger_id, "Ledger")), el("div", { class: "sugg" }, `${m.rule_id}: ${m.why}`)) },
      el("td", {}, m.bank_id), el("td", {}, m.ledger_id), el("td", { class: "num" }, usd(m.amount_cents)), el("td", {}, m.rule_id), el("td", { class: "small" }, m.why)))));
}
const tile = (k, v, cls) => el("div", { class: "tile " + (cls || "") }, el("div", { class: "k" }, k), el("div", { class: "v" }, v));

// ---------------- exceptions ----------------
const TYPE_LABEL = { POSSIBLE_DUPLICATE: "Possible duplicate charge", PARTIAL_RECEIPT: "Partial payment received", MISSING_GL: "Bank debit not in ledger",
  MISSING_RECEIPT: "Expense missing receipt", UNMATCHED_DEPOSIT: "Deposit not in ledger", MISSING_BANK: "Ledger entry not in bank", AMBIGUOUS: "Ambiguous — engine abstained", OVERPAYMENT: "Overpayment" };
function renderExceptions() {
  if (!S.run) return put("exc-list", el("div", { class: "muted" }, "Run a reconciliation first."));
  const can = S.me.role === "owner_reviewer";
  put("exc-list", S.run.result.exceptions.map((x) => {
    const note = el("textarea", { placeholder: "Review note (recorded with your name)" });
    const out = el("div", { class: "msg" });
    const act = (d) => async () => {
      try { const res = await api(W(`/runs/${S.run.id}/exceptions/${x.id}/decisions`), { method: "POST", json: { decision: d, note: note.value } });
        Object.assign(x, res); renderExceptions(); renderRun(); renderResults(); }
      catch (e) { out.textContent = e.message; out.className = "msg err"; }
    };
    return el("div", { class: "exc " + x.status },
      el("div", { class: "exc-head" }, el("div", {}, el("div", { class: "type" }, `${x.id} · ${TYPE_LABEL[x.type] || x.type}`), el("div", {}, x.why)),
        el("div", {}, el("span", { class: "amt" }, usd(x.amount_cents)), " ", el("span", { class: "badge " + x.status }, x.status))),
      x.residual_cents != null ? el("div", { class: "msg" }, `Still owed on invoice: ${usd(x.residual_cents)}`) : null,
      el("div", { class: "side" }, x.evidence_refs.map((id) => srcBox(id, id.startsWith("GL") ? "Ledger" : id.startsWith("INV") || id.includes("INV") ? "Invoice" : "Bank"))),
      x.suggestion ? el("div", { class: "sugg" }, "Suggested next step: " + x.suggestion) : null,
      el("div", { class: "row" }, el("span", { class: "muted small" }, `Rule ${x.rule_id} · proposed by ${x.proposed_by || "agent:recon (deterministic)"}`),
        x.bank_id ? el("button", { class: "secondary", onclick: () => openTrace(x.bank_id) }, "Trace " + x.bank_id + " →") : null),
      x.proposal_note ? el("div", { class: "sugg" }, `Preparer proposal by ${x.proposed_by}: “${x.proposal_note}”`) : null,
      x.status === "OPEN" ? (can ? el("div", {}, note, el("div", { class: "row" }, el("button", { class: "ok", onclick: act("ACCEPT") }, "Accept"), el("button", { class: "no", onclick: act("REJECT") }, "Reject")), out)
        : el("div", {}, note, el("div", { class: "row" }, el("button", { class: "secondary", onclick: async () => {
            try { const res = await api(W(`/runs/${S.run.id}/exceptions/${x.id}/proposals`), { method: "POST", json: { note: note.value } }); Object.assign(x, res); renderExceptions(); }
            catch (e) { out.textContent = e.message; out.className = "msg err"; } } }, "Propose resolution for reviewer")),
            el("div", { class: "muted small" }, "Preparers propose; an owner/reviewer decides. The proposer can never approve their own proposal."), out))
        : el("div", { class: "msg good" }, `${x.status} by ${x.reviewer} · ${new Date(x.reviewed_at).toLocaleString()} · ${x.approval_id}${x.review_note ? " · “" + x.review_note + "”" : ""} · ${x.effect}`));
  }));
}

// ---------------- controls ----------------
const ATTACKS = [
  { t: "Post adjustment to the books", d: "Ask the agent to write the HB006 entry into the client's ledger.", b: { action: "ledger.post" } },
  { t: "Release a vendor payment", d: "Ask the agent to pay Gulfprint $315.", b: { action: "bank.payment" } },
  { t: "Email the client", d: "Ask the agent to email Harborline about the missing receipt.", b: { action: "email.send" } },
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
  put("attack-out", el("table", {}, el("tr", {}, ["Time", "Attempt", "Decision", "Reason", "Executed"].map((h) => el("th", {}, h))),
    attempts.map((x) => el("tr", {}, el("td", {}, x.at.toLocaleTimeString()), el("td", {}, x.a.t), el("td", {}, el("span", { class: "badge " + x.r.decision }, x.r.decision)),
      el("td", {}, x.r.reason_code || ""), el("td", {}, String(!!x.r.executed))))),
    el("div", { class: "msg" }, "Each refusal is now a signed record in tab 5."));
}

// ---------------- evidence ----------------
async function renderEvidence() {
  if (!S.ws) return;
  const ev = await api(W("/evidence"));
  const v = ev.verification;
  const b = $("verify-badge"); b.className = "badge " + v.state;
  b.textContent = v.state === "PASS" ? `VERIFIED · ${v.checked} signed events` : `${v.state === "UNKNOWN" ? "NOT VERIFIED" : "FAILED"} · ${v.reason}`;
  for (const [id, p] of [["exp-csv", "export.csv"], ["exp-pack", "review-pack.json"]]) {
    $(id).classList.toggle("disabled", !S.run); $(id).href = S.run ? W(`/runs/${S.run.id}/${p}`) : "#";
  }
  $("exp-print").classList.toggle("disabled", !S.run);
  $("exp-print").href = S.run ? `/static/pack.html?ws=${encodeURIComponent(S.ws)}&run=${encodeURIComponent(S.run.id)}` : "#";
  put("events", el("div", { class: "muted small" }, `Signing key ${ev.key_id} (${ev.key_source}). Newest first.`),
    el("table", {}, el("tr", {}, ["Time (UTC)", "Actor", "Action", "Decision", "Source refs", "Hash"].map((h) => el("th", {}, h))),
      ev.events.slice().reverse().map((e) => el("tr", {}, el("td", { class: "small" }, e.timestamp_utc.slice(11, 19)), el("td", {}, e.actor_id),
        el("td", {}, e.action), el("td", {}, el("span", { class: "badge " + String(e.policy_decision).split(":")[0] }, e.policy_decision)),
        el("td", { class: "small" }, (e.source_line_refs || []).slice(0, 6).join(", ")), el("td", { class: "mono" }, e.event_hash.slice(0, 10) + "…")))));
}
$("ev-refresh").onclick = renderEvidence;
$("tamper-btn").onclick = async () => {
  try { const t = await api(W("/evidence/tamper-check"), { method: "POST" });
    msg("tamper-out", `Copy with event #${t.tampered_index} edited → ${t.verification.state}: ${t.verification.reason}. Real chain still ${t.original_chain.state}.`, t.verification.state === "FAIL" ? "good" : "err"); }
  catch (e) { msg("tamper-out", e.message, "err"); }
};

// ---------------- results ----------------
function renderResults() {
  if (!S.run) { put("measured", el("div", { class: "muted" }, "Run a reconciliation first.")); put("roi-out"); return; }
  const r = S.run.result, total = r.matches.length + r.exceptions.filter((e) => e.bank_id && !["MISSING_RECEIPT", "PARTIAL_RECEIPT"].includes(e.type)).length;
  const reviewed = r.exceptions.filter((e) => e.status !== "OPEN").length;
  put("measured", tile("Bank lines imported (no re-keying)", Object.keys(S.run.lines).filter((k) => !k.startsWith("GL") && !k.includes("INV")).length),
    tile("Matched automatically", `${r.matches.length} of ${total}`, "good"), tile("Exceptions raised", r.exceptions.length, "amb"),
    tile("Exceptions reviewed", `${reviewed} / ${r.exceptions.length}`), tile("Clicks this session", S.clicks),
    tile("Unexplained variance", usd(r.unexplained_variance_cents), r.unexplained_variance_cents ? "bad" : "good"));
  roi();
}
function roi() {
  if (!S.run) return;
  const r = S.run.result, lines = +$("roi-lines").value, books = +$("roi-books").value, mm = +$("roi-min").value, em = +$("roi-exc").value;
  const rate = r.exceptions.length / Math.max(1, r.matches.length + r.exceptions.length);
  const manual = lines * books * mm / 60, after = lines * books * rate * em / 60;
  put("roi-out", el("div", { class: "tiles" }, tile("Manual hours / month (assumed)", manual.toFixed(1)), tile("Review hours / month (estimated)", after.toFixed(1)),
    tile("Hours potentially freed", (manual - after).toFixed(1), "amb")),
    el("div", { class: "muted small gap" }, `Uses this run's exception rate (${(rate * 100).toFixed(0)}%), Every input is an assumption to replace with your measured baseline during the pilot.`));
}
["roi-lines", "roi-books", "roi-min", "roi-exc"].forEach((id) => $(id).addEventListener("input", roi));

// ---------------- trust ----------------
async function renderTrust() {
  const t = await api("/v1/trust");
  put("trust", el("div", { class: "grid two" },
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Data boundary"), el("ul", { class: "plain" },
      el("li", {}, `Each client book is kept separate: its own data and its own evidence chain (${t.workspaces.length} book${t.workspaces.length === 1 ? "" : "s"} here)`),
      el("li", {}, `Region declared: ${t.region_declared}. ${t.region_note}`), el("li", {}, "Data allowed: bank statements, ledgers, invoices and receipts only"),
      el("li", {}, t.hard_boundary), el("li", {}, t.retention))),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "AI use"), el("div", { class: "sugg" }, t.ai_note),
      el("table", {}, el("tr", {}, el("th", {}, "Worker"), el("th", {}, "Does"), el("th", {}, "Model")), t.agents.map((a) => el("tr", {}, el("td", {}, a.id), el("td", {}, a.does), el("td", {}, a.model)))),
      el("div", { class: "muted small gap" }, `Allowlisted routes: ${t.model_routes_allowed.join(", ")} · in use: ${t.model_routes_in_use.join(", ")}`)),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Never permitted (no grant exists)"), el("ul", { class: "plain" }, t.forbidden_actions.map((f) => el("li", {}, f)))),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Role permissions"), el("table", {}, Object.entries(t.roles).map(([r, a]) => el("tr", {}, el("td", {}, el("b", {}, r)), el("td", { class: "small" }, a.join(", ")))))),
    el("div", { class: "card" }, el("div", { class: "card-title" }, "Evidence signing"), el("div", {}, `${t.evidence.algorithm} · key: ${t.evidence.key_source} · policy ${t.policy_version}`),
      el("pre", {}, t.evidence.public_key_pem), el("div", { class: "muted small" }, `Security events outside client scope: ${t.security_events}`))));
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
