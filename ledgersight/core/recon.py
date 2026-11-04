"""Deterministic reconciliation engine (no LLM in the financial path).

Passes (plan 8.2):
  1. exact      - same currency + signed amount + normalised reference, date within 3 days
  2. tolerance  - same currency + signed amount, date within 3 days, UNIQUE candidate both ways
                  (ambiguous candidates -> AMBIGUOUS exception, abstain)
  3. AR         - bank credits vs open invoices: full or PARTIAL_RECEIPT with residual
  4. exceptions - POSSIBLE_DUPLICATE, MISSING_GL, UNMATCHED_DEPOSIT, MISSING_BANK
  5. support    - matched non-bank-fee debits > USD 100 need a linked document -> MISSING_RECEIPT

Invariants: one source line consumed at most once; nothing is ever "posted".
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date

ENGINE_VERSION = "recon-1.0.0"
DATE_TOLERANCE_DAYS = 3
RECEIPT_THRESHOLD_CENTS = 10_000
BANK_FEE_HINTS = ("SERVICE FEE", "BANK FEE", "WIRE FEE")


@dataclass
class Match:
    bank_id: str
    ledger_id: str
    rule_id: str
    amount_cents: int
    why: str


@dataclass
class Exception_:
    id: str
    type: str
    bank_id: str | None
    ledger_id: str | None
    amount_cents: int
    rule_id: str
    why: str
    evidence_refs: list[str] = field(default_factory=list)
    suggestion: str | None = None
    alternatives: list[str] = field(default_factory=list)
    residual_cents: int | None = None
    status: str = "OPEN"  # OPEN -> ACCEPTED / REJECTED by a named human (never POSTED)


@dataclass
class RunResult:
    engine_version: str
    workspace_id: str
    bank_net_cents: int
    ledger_net_cents: int
    variance_cents: int
    matches: list[Match]
    exceptions: list[Exception_]
    unexplained_variance_cents: int

    def to_dict(self):
        return asdict(self)


def _days(a: date, b: date) -> int:
    return abs((a - b).days)


def reconcile(workspace_id: str, bank: list[dict], ledger: list[dict],
              invoices: list[dict] | None = None, documents: list[dict] | None = None) -> RunResult:
    invoices = invoices or []
    documents = documents or []
    used_b: set[str] = set()
    used_l: set[str] = set()
    held_l: set[str] = set()  # ledger lines held by an ambiguous proposal (not reported missing)
    matches: list[Match] = []
    excs: list[Exception_] = []

    def add_exc(type_, b=None, l=None, rule="", why="", **kw):
        eid = f"EX-{len(excs) + 1:03d}"
        amt = (b or l)["amount_cents"]
        refs = kw.pop("evidence_refs", None) or [x for x in ((b or {}).get("id"), (l or {}).get("id")) if x]
        excs.append(Exception_(eid, type_, b and b["id"], l and l["id"], amt, rule, why, refs, **kw))

    bank_sorted = sorted(bank, key=lambda r: (r["date"], r["id"]))
    ledger_sorted = sorted(ledger, key=lambda r: (r["date"], r["id"]))

    # Pass 1: exact reference
    for b in bank_sorted:
        if not b["ref_norm"]:
            continue
        cands = [l for l in ledger_sorted if l["id"] not in used_l and l["currency"] == b["currency"]
                 and l["amount_cents"] == b["amount_cents"] and l["ref_norm"] == b["ref_norm"]
                 and _days(l["date"], b["date"]) <= DATE_TOLERANCE_DAYS]
        if cands:
            l = cands[0]
            used_b.add(b["id"]); used_l.add(l["id"])
            matches.append(Match(b["id"], l["id"], "R1_EXACT_REF", b["amount_cents"],
                                 f"amount, currency and reference {b['reference']!r} agree; "
                                 f"date gap {_days(l['date'], b['date'])}d"))

    # Pass 2: amount + date tolerance, unique both ways, else abstain
    for b in bank_sorted:
        if b["id"] in used_b:
            continue
        cands = [l for l in ledger_sorted if l["id"] not in used_l and l["currency"] == b["currency"]
                 and l["amount_cents"] == b["amount_cents"] and _days(l["date"], b["date"]) <= DATE_TOLERANCE_DAYS]
        if len(cands) == 1:
            l = cands[0]
            back = [x for x in bank_sorted if x["id"] not in used_b and x["currency"] == l["currency"]
                    and x["amount_cents"] == l["amount_cents"] and _days(x["date"], l["date"]) <= DATE_TOLERANCE_DAYS]
            if len(back) == 1:
                used_b.add(b["id"]); used_l.add(l["id"])
                matches.append(Match(b["id"], l["id"], "R2_AMOUNT_DATE", b["amount_cents"],
                                     f"unique amount within ±{DATE_TOLERANCE_DAYS}d; reference differs"))
                continue
        if len(cands) > 1:
            add_exc("AMBIGUOUS", b=b, rule="R2_AMOUNT_DATE",
                    why=f"{len(cands)} ledger candidates; engine abstains",
                    alternatives=[c["id"] for c in cands])
            used_b.add(b["id"]); held_l.update(c["id"] for c in cands)

    # Pass 3: AR open-invoice check for matched or unmatched credits carrying an invoice ref
    inv_by_ref = {i["ref_norm"]: i for i in invoices}
    for b in bank_sorted:
        if b["amount_cents"] <= 0:
            continue
        inv = inv_by_ref.get(b["ref_norm"])
        if not inv or inv["currency"] != b["currency"]:
            continue
        if b["amount_cents"] < inv["open_cents"]:
            residual = inv["open_cents"] - b["amount_cents"]
            add_exc("PARTIAL_RECEIPT", b=b, rule="R3_AR_PARTIAL",
                    why=f"received {b['amount_cents']/100:.2f} against invoice {inv['id']} "
                        f"open {inv['open_cents']/100:.2f}; residual {residual/100:.2f} remains open",
                    residual_cents=residual, evidence_refs=[b["id"], inv["id"]],
                    suggestion="Accept as partial receipt; keep invoice open for residual. No invoice is closed automatically.")
        elif b["amount_cents"] > inv["open_cents"]:
            add_exc("OVERPAYMENT", b=b, rule="R3_AR_OVER",
                    why=f"received more than open balance on {inv['id']}", evidence_refs=[b["id"], inv["id"]])

    # Pass 4: residual classification
    for b in bank_sorted:
        if b["id"] in used_b:
            continue
        if b["amount_cents"] < 0:
            twin = next((x for x in bank_sorted if x["id"] != b["id"] and x["amount_cents"] == b["amount_cents"]
                         and x["ref_norm"] == b["ref_norm"] and x["description"] == b["description"]
                         and _days(x["date"], b["date"]) <= 5 and x["id"] in used_b), None)
            if twin:
                add_exc("POSSIBLE_DUPLICATE", b=b, rule="R4_DUP",
                        why=f"same amount, payee and reference as {twin['id']} within 5 days; ledger has only one entry",
                        evidence_refs=[b["id"], twin["id"]],
                        suggestion="Confirm with bank/vendor whether charged twice; request refund if so. Do not book.")
            else:
                add_exc("MISSING_GL", b=b, rule="R4_MISSING_GL",
                        why="bank debit has no ledger entry",
                        suggestion="Obtain vendor document, then preparer books entry in the accounting system.")
        else:
            inv = inv_by_ref.get(b["ref_norm"])
            add_exc("UNMATCHED_DEPOSIT", b=b, rule="R4_UNMATCHED_DEP",
                    why="bank credit has no ledger entry" + (" and no reference" if not b["ref_norm"] else ""),
                    suggestion=(f"Reference matches open invoice {inv['id']}; confirm and book receipt."
                                if inv else "Identify payer; do not assume customer."))
        used_b.add(b["id"])
    for l in ledger_sorted:
        if l["id"] not in used_l and l["id"] not in held_l:
            add_exc("MISSING_BANK", l=l, rule="R4_MISSING_BANK", why="ledger entry has no bank transaction")

    # Pass 5: supporting documents for matched expenses
    docs_by_ref = {d["ref_norm"]: d for d in documents}
    bank_by_id = {b["id"]: b for b in bank}
    for m in matches:
        b = bank_by_id[m.bank_id]
        if b["amount_cents"] >= 0 or -b["amount_cents"] <= RECEIPT_THRESHOLD_CENTS:
            continue
        if any(h in b["description"].upper() for h in BANK_FEE_HINTS):
            continue
        if b["ref_norm"] not in docs_by_ref:
            add_exc("MISSING_RECEIPT", b=b, rule="R5_SUPPORT_DOC",
                    why=f"expense over USD {RECEIPT_THRESHOLD_CENTS/100:.0f} with no linked receipt/invoice",
                    evidence_refs=[b["id"], m.ledger_id],
                    suggestion="Request receipt from client before sign-off.")

    bank_net = sum(r["amount_cents"] for r in bank)
    ledger_net = sum(r["amount_cents"] for r in ledger)
    explained = sum(e.amount_cents for e in excs if e.type in ("POSSIBLE_DUPLICATE", "MISSING_GL", "UNMATCHED_DEPOSIT")) \
        - sum(e.amount_cents for e in excs if e.type == "MISSING_BANK")
    return RunResult(ENGINE_VERSION, workspace_id, bank_net, ledger_net, bank_net - ledger_net,
                     matches, excs, (bank_net - ledger_net) - explained)
