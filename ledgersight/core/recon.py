"""Deterministic reconciliation engine (no LLM in the financial path).

Passes (plan 8.2):
  1. exact      - same currency + signed amount + normalised reference, date within 3 days
  2. tolerance  - same currency + signed amount, date within 3 days, UNIQUE candidate both ways
                  (ambiguous candidates -> AMBIGUOUS exception, abstain)
  3. AR         - bank credits vs open invoices: full or PARTIAL_RECEIPT with residual
  4. exceptions - POSSIBLE_DUPLICATE, MISSING_GL, UNMATCHED_DEPOSIT, MISSING_BANK
  5. support    - matched non-bank-fee debits > USD 100 need a linked document -> MISSING_RECEIPT
  6. tax        - interstate sale with missing taxability / nexus / registration / date facts -> TAX_REVIEW
                  (a flag for a CPA; no tax is determined, computed, filed or remitted)
  FX (in pass 4) - a payment in one currency against a bill in another, with no approved rate source
                  -> FX_REVIEW, reason APPROVED_FX_RATE_MISSING (no conversion, no auto-match)

Every exception carries a case id (bank/ledger line id, or e.g. CA-TAX-01 / UK-FX-01 for review cases)
and a client-facing status (MATCHED / PARTIAL / UNMATCHED / DUPLICATE / TAX_REVIEW / FX_REVIEW /
APPROVAL_REQUIRED); an unresolved case is never shown as completed.

Invariants: one source line consumed at most once; nothing is ever "posted".
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date

from ..config import load

_M = load("matching.json")  # thresholds are configuration, not code
ENGINE_VERSION = _M["engine_version"]
DATE_TOLERANCE_DAYS = int(_M["date_tolerance_days"])
DUPLICATE_WINDOW_DAYS = int(_M["duplicate_window_days"])
RECEIPT_THRESHOLD_CENTS = int(_M["receipt_threshold_cents"])
BANK_FEE_HINTS = tuple(h.upper() for h in _M["bank_fee_hints"])
TAX_INPUTS: dict[str, str] = (_M.get("tax_review") or {}).get("required_inputs", {})
APPROVED_FX = {(s["from"].upper(), s["to"].upper()) for s in (_M.get("fx") or {}).get("approved_rate_sources", [])}

# exception type -> what the client-facing status says (never "completed" while a case is open)
CLIENT_STATUS = {"POSSIBLE_DUPLICATE": "DUPLICATE", "PARTIAL_RECEIPT": "PARTIAL", "MISSING_GL": "UNMATCHED",
                 "UNMATCHED_DEPOSIT": "UNMATCHED", "MISSING_BANK": "UNMATCHED", "TAX_REVIEW": "TAX_REVIEW",
                 "FX_REVIEW": "FX_REVIEW", "AMBIGUOUS": "APPROVAL_REQUIRED", "OVERPAYMENT": "APPROVAL_REQUIRED",
                 "MISSING_RECEIPT": "APPROVAL_REQUIRED"}
# types whose amount is part of the bank-vs-ledger variance (TAX_REVIEW is on a matched line, so it is not)
VARIANCE_TYPES = ("POSSIBLE_DUPLICATE", "MISSING_GL", "UNMATCHED_DEPOSIT", "FX_REVIEW")


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
    case_id: str = ""
    client_status: str = ""
    reason_code: str | None = None
    missing_inputs: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)  # source facts shown to the CPA (states, currencies...), never computed values


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
              invoices: list[dict] | None = None, documents: list[dict] | None = None,
              bills: list[dict] | None = None) -> RunResult:
    invoices = invoices or []
    documents = documents or []
    bill_by_ref = {x["ref_norm"]: x for x in (bills or [])}  # AP: open vendor bills
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
                         and _days(x["date"], b["date"]) <= DUPLICATE_WINDOW_DAYS and x["id"] in used_b), None)
            if twin:
                add_exc("POSSIBLE_DUPLICATE", b=b, rule="R4_DUP",
                        why=f"same amount, payee and reference as {twin['id']} within {DUPLICATE_WINDOW_DAYS} days; ledger has only one entry",
                        evidence_refs=[b["id"], twin["id"]],
                        suggestion="Confirm with bank/vendor whether charged twice; request refund if so. Do not book.")
            else:
                bill = bill_by_ref.get(b["ref_norm"])
                if bill and bill["currency"] != b["currency"]:
                    pair = (bill["currency"], b["currency"])
                    approved = pair in APPROVED_FX
                    add_exc("FX_REVIEW", b=b, rule="R4_FX",
                            why=f"bank paid {b['currency']} {-b['amount_cents']/100:,.2f} against vendor bill {bill['id']} "
                                f"of {bill['currency']} {bill['open_cents']/100:,.2f}; "
                                + ("an approved rate source exists, but conversion is not automated"
                                   if approved else f"no approved {bill['currency']}->{b['currency']} exchange-rate source in policy")
                                + "; amounts are not converted and the payment is not matched",
                            evidence_refs=[b["id"], bill["id"]],
                            reason_code="FX_CONVERSION_NOT_AUTOMATED" if approved else "APPROVED_FX_RATE_MISSING",
                            facts={"bill_currency": bill["currency"], "bill_amount": f"{bill['open_cents']/100:.2f}",
                                   "paid_currency": b["currency"], "paid_amount": f"{-b['amount_cents']/100:.2f}",
                                   "vendor_country": bill.get("vendor_country", ""), "vendor": bill.get("vendor_alias", "")},
                            suggestion="CPA confirms the rate source and the realised FX difference, then the preparer books the "
                                       "entry in the accounting system. LedgerSight invents no rate and posts nothing.")
                elif bill and bill["currency"] == b["currency"]:
                    add_exc("MISSING_GL", b=b, rule="R4_MISSING_GL",
                            why=f"bank debit has no ledger entry; open vendor bill {bill['id']} "
                                f"({bill['open_cents']/100:.2f}) is on file for this reference",
                            evidence_refs=[b["id"], bill["id"]],
                            suggestion=f"Vendor bill {bill['id']} supports the payment. After CPA review, the preparer "
                                       "books the entry in the accounting system. LedgerSight posts nothing.")
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

    # Pass 6: state tax readiness. Facts are organised for a CPA; nothing is decided or calculated.
    bank_by_ref = {}
    for b in bank_sorted:
        if b["amount_cents"] > 0 and b["ref_norm"]:
            bank_by_ref.setdefault(b["ref_norm"], b)
    for inv in sorted(invoices, key=lambda i: i["id"]):
        frm, to = inv.get("ship_from_state") or "", inv.get("ship_to_state") or ""
        if not to or not frm or frm == to:
            continue
        missing = [k for k in TAX_INPUTS if not str(inv.get(k) or "").strip()]
        if not missing:
            continue
        b = bank_by_ref.get(inv["ref_norm"])
        excs.append(Exception_(
            f"EX-{len(excs) + 1:03d}", "TAX_REVIEW", b and b["id"], None, inv["amount_cents"], "R6_STATE_TAX_REVIEW",
            f"sale on invoice {inv['id']} ships from {frm} to {to}; missing: " + ", ".join(TAX_INPUTS[k] for k in missing)
            + ". Potential tax/nexus review; no tax liability or amount is asserted",
            [x for x in ((b or {}).get("id"), inv["id"]) if x],
            suggestion="CPA reviews nexus, registration and taxability for the destination state and records the "
                       "determination outside LedgerSight. No tax is computed, filed or paid here.",
            reason_code="TAX_INPUTS_MISSING", missing_inputs=missing,
            facts={"invoice": inv["id"], "customer": inv.get("customer_alias", ""), "ship_from_state": frm,
                   "ship_to_state": to, "invoice_date": inv.get("issue_date", ""), "invoice_amount": f"{inv['amount_cents']/100:.2f}",
                   "currency": inv["currency"]}))

    # case ids and client-facing status, derived from the data (never typed in)
    seq: dict[str, int] = {}
    for e in excs:
        if e.type == "TAX_REVIEW":
            key = f"{e.facts['ship_to_state']}-TAX"
        elif e.type == "FX_REVIEW":
            key = f"{e.facts.get('vendor_country') or e.facts['bill_currency'][:2]}-FX"
        else:
            key = None
        if key:
            seq[key] = seq.get(key, 0) + 1
            e.case_id = f"{key}-{seq[key]:02d}"
        else:
            e.case_id = e.bank_id or e.ledger_id or e.id
        e.client_status = CLIENT_STATUS.get(e.type, "APPROVAL_REQUIRED")

    bank_net = sum(r["amount_cents"] for r in bank)
    ledger_net = sum(r["amount_cents"] for r in ledger)
    explained = sum(e.amount_cents for e in excs if e.type in VARIANCE_TYPES) \
        - sum(e.amount_cents for e in excs if e.type == "MISSING_BANK")
    return RunResult(ENGINE_VERSION, workspace_id, bank_net, ledger_net, bank_net - ledger_net,
                     matches, excs, (bank_net - ledger_net) - explained)
