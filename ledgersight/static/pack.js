"use strict";
const q = new URLSearchParams(location.search);
const ws = q.get("ws"), run = q.get("run");
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) { if (v == null) continue; if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v); }
  for (const c of kids.flat()) if (c != null) e.append(c instanceof Node ? c : String(c));
  return e;
}
const usd = (c) => (c < 0 ? "-" : "") + "$" + (Math.abs(c) / 100).toLocaleString("en-US", { minimumFractionDigits: 2 });
const tbl = (head, rows) => el("table", {}, el("tr", {}, head.map((h) => el("th", {}, h))), rows.map((r) => el("tr", {}, r.map((c) => el("td", {}, c ?? "")))));

fetch(`/v1/workspaces/${encodeURIComponent(ws)}/runs/${encodeURIComponent(run)}/review-pack.json`, { credentials: "same-origin" })
  .then(async (r) => {
    const root = document.getElementById("pack");
    if (!r.ok) { root.replaceChildren(el("p", { class: "msg err" }, r.status === 401 ? "Sign in first." : "Not available.")); return; }
    const p = await r.json(), R = p.run, v = p.evidence.verification;
    root.replaceChildren(
      el("div", { class: "pack-head" },
        el("div", {}, el("img", { src: "/static/logo.svg", alt: "CloudSeals", class: "logo-lg" }), el("div", { class: "eyebrow" }, "LEDGERSIGHT · RECONCILIATION REVIEW PACK"), el("h1", {}, `${p.client.name} · ${p.client.period}`),
          el("div", { class: "muted small" }, `Run ${R.id} · engine ${R.engine_version} · generated ${new Date(p.generated_at).toLocaleString()} by ${p.generated_by}`)),
        el("button", { class: "primary print-btn", onclick: () => window.print() }, "Print / Save as PDF")),
      el("section", {}, el("h3", {}, "Control totals"), tbl(["Bank net", "Ledger net", "Variance", "Unexplained", "Matched", "Exceptions"],
        [[usd(R.bank_net_cents), usd(R.ledger_net_cents), usd(R.variance_cents), usd(R.unexplained_variance_cents), R.matches.length, R.exceptions.length]])),
      el("section", {}, el("h3", {}, "Inputs (source digests)"), tbl(["Source", "Rows", "SHA-256"], Object.entries(p.inputs).map(([k, x]) => [k, x.rows, el("span", { class: "mono" }, x.sha256)]))),
      el("section", {}, el("h3", {}, "Exceptions and reviewer decisions"), tbl(["ID", "Type", "Bank", "Amount", "Reason", "Status", "Reviewer", "Approval", "Note"],
        R.exceptions.map((e) => [e.id, e.type, e.bank_id, usd(e.amount_cents), e.why, e.status, e.reviewer, e.approval_id, e.review_note]))),
      el("section", {}, el("h3", {}, "Automatic matches"), tbl(["Bank", "Ledger", "Amount", "Rule", "Why"], R.matches.map((m) => [m.bank_id, m.ledger_id, usd(m.amount_cents), m.rule_id, m.why]))),
      el("section", {}, el("h3", {}, "Evidence verification"),
        el("p", {}, el("span", { class: "badge " + v.state }, v.state === "PASS" ? "VERIFIED" : v.state === "UNKNOWN" ? "NOT VERIFIED" : "FAILED"), ` ${v.reason} · ${v.checked} Ed25519-signed, hash-chained events.`),
        el("p", { class: "muted small" }, "Verify independently: download the signed review pack (JSON) and run ", el("span", { class: "mono" }, p.evidence.verify_offline), " with the public key below."),
        el("pre", {}, p.evidence.public_key_pem)),
      el("section", {}, el("h3", {}, "Boundary"), el("p", {}, p.boundary)));
  });
