"""Ask LedgerSight (AG-06): read-only, intent-limited questions over one client book.

No language model and no free-form database access. A question is classified,
TrustOps decides whether it may be answered, and only then is it mapped to one of
a fixed set of intents over the current run. Every answer cites the source rows,
run and rule it came from. Nothing here can change data.

Supported: unmatched / exception lists (by type, status, date), why X was flagged,
what is outstanding on X, who approved X, matches, run summary.
"""
from __future__ import annotations

import re

SQL = re.compile(r"\b(select\s+.+\s+from|insert\s+into|delete\s+from|drop\s+table|update\s+\w+\s+set|union\s+select|alter\s+table|truncate)\b|;\s*--|\bor\s+1\s*=\s*1\b", re.I | re.S)
WRITE = re.compile(r"\b(post|approve|accept|reject|return|pay|transfer|delete|remove|update|change|modify|email|send|mark|"
                   r"create|insert|reset|book\s+(the|an|a|this)\s+(entry|journal|payment)|write\s+off|close\s+the\s+invoice)\b", re.I)
CROSS = re.compile(r"\b(other|another|all|every|different|any\s+other)\s+(clients?|customers?|books?|firms?|compan(y|ies)|workspaces?)\b|"
                   r"\bacross\s+(clients|books|workspaces)\b", re.I)
ISO_DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
ID_LIKE = re.compile(r"\b([A-Z]{2,5}(?:-[A-Z]{1,3})?-?\d{2,})\b")

TYPE_WORDS = [
    (re.compile(r"partial", re.I), "PARTIAL_RECEIPT"),
    (re.compile(r"duplicate", re.I), "POSSIBLE_DUPLICATE"),
    (re.compile(r"missing\s+(gl|ledger)|not\s+in\s+(the\s+)?ledger|no\s+ledger", re.I), "MISSING_GL"),
    (re.compile(r"missing\s+(receipt|document|support)|no\s+receipt|without\s+(a\s+)?receipt", re.I), "MISSING_RECEIPT"),
    (re.compile(r"deposit", re.I), "UNMATCHED_DEPOSIT"),
    (re.compile(r"ambiguous", re.I), "AMBIGUOUS"),
    (re.compile(r"overpa", re.I), "OVERPAYMENT"),
    (re.compile(r"missing\s+bank|not\s+in\s+(the\s+)?bank", re.I), "MISSING_BANK"),
]
UNMATCHED_TYPES = {"MISSING_GL", "UNMATCHED_DEPOSIT", "POSSIBLE_DUPLICATE", "AMBIGUOUS", "MISSING_BANK"}

REFUSAL_TEXT = {
    "WORKSPACE_SCOPE": "I can only answer about the client book you have open. I can't search, list or confirm anything outside it.",
    "SQL_NOT_ALLOWED": "I don't run database queries. Ask a question in plain words, for example “show unmatched deposits”.",
    "READ_ONLY_QUERY": "I'm read-only. I can explain and list items, but I can't approve, post, pay, email or change anything. "
                       "Use the Review queue for decisions; nothing is ever posted to the books.",
}


def usd(c) -> str:
    return "" if c is None else ("-" if c < 0 else "") + f"${abs(c) / 100:,.2f}"


# Name-shaped phrases ("for Acme Corp", "Acme's deposits"). Any such name that is not this book's
# client or one of its own customers/vendors is treated as another client. Real and made-up names
# are handled identically, so the answer never reveals whether some other client exists.
NAMED = re.compile(r"\b(?:for|of|at|from|about|regarding)\s+((?:[A-Z][\w&.-]*)(?:\s+[A-Z][\w&.-]*)*)|\b([A-Z][\w&.-]*(?:\s+[A-Z][\w&.-]*)*)'s\b")
STOP = {"ledgersight", "trustops", "cpa", "usd", "gl", "ar", "ap", "i", "me", "the", "this", "run", "show", "list", "give",
        "tell", "what", "why", "who", "how", "find", "display", "explain", "please", "can", "is", "are", "was", "january", "february",
        "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"}


def _foreign_names(q: str, allowed_terms: list[str]) -> list[str]:
    allowed = [t.lower() for t in allowed_terms if t]
    out = []
    for m in NAMED.finditer(q):
        name = (m.group(1) or m.group(2) or "").strip()
        words = [w for w in re.split(r"\s+", name.lower()) if w and w not in STOP and not re.search(r"\d", w)]
        if words and not any(all(w in a for w in words) for a in allowed):
            out.append(name)
    return out


def classify(question: str, allowed_terms: list[str]) -> str:
    """read | sql | write | cross_client. TrustOps turns anything but read into DENY."""
    q = question or ""
    if SQL.search(q):
        return "sql"
    if CROSS.search(q) or _foreign_names(q, allowed_terms):
        return "cross_client"
    if WRITE.search(q):
        return "write"
    return "read"


def refusal(code: str) -> dict:
    return {"status": "REFUSED", "intent": "refused", "answer": REFUSAL_TEXT.get(code, REFUSAL_TEXT["READ_ONLY_QUERY"]),
            "rows": [], "columns": [], "citations": []}


def _type_label(t):
    return t.replace("_", " ").lower()


class _Ctx:
    def __init__(self, ctx: dict):
        self.client = ctx["client"]
        self.run = ctx["run"]
        self.lines = ctx["lines"]            # id -> canonical row (all kinds)
        self.files = ctx["files"]            # sha256 -> filename
        self.res = self.run["result"]
        self.exc = self.res["exceptions"]

    def cite_line(self, lid):
        r = self.lines.get(lid)
        if not r:
            return None
        return {"ref": lid, "kind": "source row", "source": f"{self.files.get(r.get('source_sha256'), 'source file')}, row {r.get('source_row')}",
                "sha256": r.get("source_sha256")}

    def cite_exc(self, e):
        return {"ref": e["id"], "kind": "exception", "source": f"run {self.run['id']} · rule {e['rule_id']}"}

    def cites(self, excs=(), line_ids=()):
        out, seen = [], set()
        for e in excs:
            if e["id"] not in seen:
                seen.add(e["id"]); out.append(self.cite_exc(e))
            line_ids = list(line_ids) + list(e["evidence_refs"])
        for lid in line_ids:
            if lid and lid not in seen and (c := self.cite_line(lid)):
                seen.add(lid); out.append(c)
        return out

    def bank_date(self, e):
        r = self.lines.get(e.get("bank_id") or "") or self.lines.get(e.get("ledger_id") or "") or {}
        return str(r.get("date") or "")[:10]

    def related(self, rid):
        return [e for e in self.exc if rid in (e["id"], e.get("bank_id"), e.get("ledger_id"), e.get("approval_id")) or rid in e["evidence_refs"]]


def _ok(intent, answer, rows=None, columns=None, citations=None):
    return {"status": "ANSWERED", "intent": intent, "answer": answer, "rows": rows or [], "columns": columns or [],
            "citations": citations or []}


def _date_filter(q):
    ds = ISO_DATE.findall(q)
    low = q.lower()
    if len(ds) >= 2:
        a, b = sorted(ds[:2]); return lambda d: a <= d <= b, f"between {a} and {b}"
    if len(ds) == 1:
        d0 = ds[0]
        if re.search(r"\bafter\b", low):
            return lambda d: d > d0, f"after {d0}"
        if re.search(r"\b(since|from)\b", low):
            return lambda d: d >= d0, f"on or after {d0}"
        if re.search(r"\bbefore\b", low):
            return lambda d: bool(d) and d < d0, f"before {d0}"
        if re.search(r"\b(until|up to)\b", low):
            return lambda d: bool(d) and d <= d0, f"on or before {d0}"
        return lambda d: d == d0, f"on {d0}"
    return None, ""


def answer(question: str, ctx: dict) -> dict:
    q = (question or "").strip()
    if not q:
        return help_(ctx)
    if not ctx.get("run"):
        return {"status": "NO_DATA", "intent": "no_run", "answer": "There is no reconciliation run for this book yet. Run Match first.",
                "rows": [], "columns": [], "citations": []}
    c = _Ctx(ctx)
    up = q.upper()
    known = set(c.lines) | {e["id"] for e in c.exc} | {e.get("approval_id") for e in c.exc if e.get("approval_id")}
    hits = sorted((k for k in known if k and re.search(rf"(?<![A-Z0-9-]){re.escape(k.upper())}(?![A-Z0-9])", up)), key=len, reverse=True)
    low = q.lower()
    if hits:
        rid = hits[0]
        if re.search(r"\bwho\b|approv|decid|review(ed|er)|sign(ed)?\s+off", low):
            return _who(c, rid)
        if re.search(r"outstanding|owed|owe|residual|balance|remaining|left\s+to\s+pay|still\s+open", low):
            return _outstanding(c, rid)
        return _why(c, rid)
    unknown = [t for t in ID_LIKE.findall(up) if t not in known and not ISO_DATE.match(t)]
    if unknown:
        return {"status": "NO_DATA", "intent": "lookup", "answer": f"I can't find {unknown[0]} in this book's latest run.",
                "rows": [], "columns": [], "citations": []}
    if re.search(r"\bsummary\b|overview|how\s+many|totals?\b|variance|how\s+did\s+the\s+run", low):
        return _summary(c)
    if re.search(r"\bwho\b|approv|decision|decided|signed\s+off", low):
        return _decisions(c)
    types = {t for rx, t in TYPE_WORDS if rx.search(q)}
    if re.search(r"\bunmatched\b|not\s+matched|unreconciled", low) and not types:
        types = set(UNMATCHED_TYPES)
    if types or re.search(r"exception|flag|issue|problem|review|open\s+items?|pending|discrepanc|outstanding", low):
        if not types and re.search(r"outstanding|owed", low):
            return _outstanding_all(c)
        return _list(c, q, types)
    if re.search(r"\bmatch(ed|es)?\b", low):
        return _matches(c, q)
    return help_(ctx)


def _list(c: _Ctx, q: str, types: set[str]):
    low = q.lower()
    status = None
    if re.search(r"\b(open|pending|awaiting|unresolved)\b", low):
        status = {"OPEN", "RETURNED"}
    elif re.search(r"\baccepted\b|\bapproved\b", low):
        status = {"ACCEPTED"}
    elif re.search(r"\brejected\b", low):
        status = {"REJECTED"}
    elif re.search(r"\breturned\b", low):
        status = {"RETURNED"}
    keep, dtext = _date_filter(q)
    rows = [e for e in c.exc if (not types or e["type"] in types) and (not status or e["status"] in status)
            and (not keep or keep(c.bank_date(e)))]
    what = ", ".join(sorted(_type_label(t) for t in types)) if types else "exceptions"
    scope = f"{c.client['name']} · {c.client['period']}" + (f" · {dtext}" if dtext else "")
    if not rows:
        return _ok("list_exceptions", f"No {what} found for {scope} in run {c.run['id']}.")
    total = sum(e["amount_cents"] for e in rows)
    return _ok("list_exceptions", f"{len(rows)} item{'s' if len(rows) != 1 else ''} ({what}) for {scope}, totalling {usd(total)}.",
               [[e["id"], e.get("bank_id") or e.get("ledger_id"), c.bank_date(e), usd(e["amount_cents"]), _type_label(e["type"]),
                 e["status"], (e.get("trustops") or {}).get("policy_id", "")] for e in rows],
               ["Exception", "Line", "Date", "Amount", "Type", "Status", "Policy"], c.cites(rows))


def _why(c: _Ctx, rid: str):
    ex = c.related(rid)
    m = next((m for m in c.res["matches"] if rid in (m["bank_id"], m["ledger_id"])), None)
    parts = []
    if m:
        parts.append(f"{m['bank_id']} was matched to {m['ledger_id']} by rule {m['rule_id']}: {m['why']}.")
    for e in ex:
        t = e.get("trustops") or {}
        s = f"{e['id']} was flagged as {_type_label(e['type'])} by rule {e['rule_id']} because {e['why']}."
        if t:
            s += f" TrustOps: {t['decision']} under {t['policy_id']} {t.get('policy_name', '')}".rstrip() + \
                 (f", needs a {t.get('required_role_label') or t.get('required_role')}" if t.get("required_role") else "") + "."
        s += f" Status: {e['status']}" + (f" (by {e['reviewer']})" if e.get("reviewer") else "") + "."
        if e.get("suggestion"):
            s += f" Suggested next step: {e['suggestion']}"
        parts.append(s)
    if not parts:
        if rid in c.lines:
            return _ok("explain", f"{rid} is in the imported files but was not matched or flagged in run {c.run['id']}.",
                       citations=c.cites(line_ids=[rid]))
        return {"status": "NO_DATA", "intent": "explain", "answer": f"I can't find {rid} in this book's latest run.",
                "rows": [], "columns": [], "citations": []}
    return _ok("explain", " ".join(parts), citations=c.cites(ex, [rid] + ([m["ledger_id"], m["bank_id"]] if m else [])))


def _who(c: _Ctx, rid: str):
    ex = c.related(rid)
    if not ex:
        return _ok("who_approved", f"No review item involves {rid}, so there is no approval to report.", citations=c.cites(line_ids=[rid]))
    parts, rows = [], []
    for e in ex:
        if e["status"] in ("OPEN",):
            parts.append(f"{e['id']}: no approval recorded. It is waiting for a CPA reviewer.")
        else:
            parts.append(f"{e['id']}: {e['status'].lower()} by {e['reviewer']} on {str(e.get('reviewed_at'))[:19].replace('T', ' ')} UTC "
                         f"(approval {e['approval_id']}).")
        rows.append([e["id"], e["status"], e.get("reviewer") or "—", e.get("approval_id") or "—", e.get("proposed_by") or "agent:recon"])
    return _ok("who_approved", " ".join(parts), rows, ["Exception", "Status", "Reviewer", "Approval ID", "Proposed by"], c.cites(ex))


def _outstanding(c: _Ctx, rid: str):
    ex = [e for e in c.related(rid) if e["type"] in ("PARTIAL_RECEIPT", "OVERPAYMENT")]
    line = c.lines.get(rid) or {}
    if ex:
        e = ex[0]
        return _ok("outstanding", f"{usd(e['residual_cents'])} is still outstanding on {e['evidence_refs'][-1]}: {e['why']}.",
                   citations=c.cites(ex))
    if line.get("open_cents") is not None:
        return _ok("outstanding", f"{rid} shows an open balance of {usd(line['open_cents'])} in the imported file. "
                                  "No partial payment against it was found in this run.", citations=c.cites(line_ids=[rid]))
    open_ex = [e for e in c.related(rid) if e["status"] in ("OPEN", "RETURNED")]
    if open_ex:
        return _ok("outstanding", f"No unpaid balance is recorded for {rid}, but {len(open_ex)} review item(s) are still open: "
                                  + ", ".join(f"{e['id']} ({_type_label(e['type'])})" for e in open_ex) + ".", citations=c.cites(open_ex))
    return _ok("outstanding", f"Nothing is outstanding on {rid} in run {c.run['id']}.", citations=c.cites(line_ids=[rid]))


def _outstanding_all(c: _Ctx):
    ex = [e for e in c.exc if e.get("residual_cents")]
    if not ex:
        return _ok("outstanding", "No partial receipts with an outstanding balance in this run.")
    return _ok("outstanding", f"{usd(sum(e['residual_cents'] for e in ex))} outstanding across {len(ex)} partial receipt(s).",
               [[e["id"], e["bank_id"], e["evidence_refs"][-1], usd(e["residual_cents"]), e["status"]] for e in ex],
               ["Exception", "Bank line", "Invoice", "Still owed", "Status"], c.cites(ex))


def _decisions(c: _Ctx):
    ex = [e for e in c.exc if e["status"] != "OPEN"]
    if not ex:
        return _ok("who_approved", f"No reviewer decisions have been recorded yet in run {c.run['id']}. "
                                   f"{len(c.exc)} item(s) are waiting for a CPA reviewer.")
    return _ok("who_approved", f"{len(ex)} decision(s) recorded in run {c.run['id']}.",
               [[e["id"], e["status"], e["reviewer"], e["approval_id"], str(e.get("reviewed_at"))[:19]] for e in ex],
               ["Exception", "Decision", "Reviewer", "Approval ID", "When (UTC)"], c.cites(ex))


def _matches(c: _Ctx, q: str):
    keep, dtext = _date_filter(q)
    ms = [m for m in c.res["matches"] if not keep or keep(str((c.lines.get(m["bank_id"]) or {}).get("date", ""))[:10])]
    return _ok("list_matches", f"{len(ms)} automatic match(es)" + (f" {dtext}" if dtext else "") + f" in run {c.run['id']}.",
               [[m["bank_id"], m["ledger_id"], usd(m["amount_cents"]), m["rule_id"]] for m in ms],
               ["Bank line", "Ledger line", "Amount", "Rule"], c.cites(line_ids=[x for m in ms for x in (m["bank_id"], m["ledger_id"])]))


def _summary(c: _Ctx):
    r = c.res
    open_ = sum(e["status"] in ("OPEN", "RETURNED") for e in c.exc)
    return _ok("summary", f"Run {c.run['id']} for {c.client['name']} ({c.client['period']}): {len(r['matches'])} matched automatically, "
                          f"{len(c.exc)} exception(s), {open_} awaiting review. Bank net {usd(r['bank_net_cents'])}, ledger net "
                          f"{usd(r['ledger_net_cents'])}, variance {usd(r['variance_cents'])}, unexplained {usd(r['unexplained_variance_cents'])}.",
               citations=[{"ref": c.run["id"], "kind": "run", "source": f"engine {r['engine_version']}"}])


def suggestions(ctx: dict) -> list[str]:
    run = ctx.get("run")
    if not run:
        return ["Give me a summary of this run"]
    exc = run["result"]["exceptions"]
    out = ["Show unmatched transactions", "Show open exceptions"]
    for t in ("POSSIBLE_DUPLICATE", "MISSING_GL"):
        e = next((e for e in exc if e["type"] == t), None)
        if e:
            out.append(f"Why was {e['bank_id'] or e['id']} flagged?")
    e = next((e for e in exc if e["type"] == "PARTIAL_RECEIPT"), None)
    if e:
        out.append(f"What is outstanding on {e['evidence_refs'][-1]}?")
    if exc:
        out.append(f"Who approved {exc[0]['bank_id'] or exc[0]['id']}?")
    out.append("Give me a summary of this run")
    return out


def help_(ctx: dict):
    return {"status": "HELP", "intent": "help",
            "answer": "I answer read-only questions about this client book: unmatched items, why a line was flagged, "
                      "what is outstanding, who approved something, matches and a run summary. You can filter by date (YYYY-MM-DD).",
            "rows": [], "columns": [], "citations": [], "suggestions": suggestions(ctx)}
