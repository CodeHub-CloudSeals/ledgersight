"""Safe source ingestion: CSV/TSV/XLSX/JSON/OFX -> canonical lines in integer cents.

Rules (plan 8.1): never evaluate formulas or external links, reject macro/zip
formats, bound size/rows, keep raw + normalised reference, preserve source
line refs, compute a batch digest so re-imports are idempotent.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 5000
ALLOWED_EXT = {".csv", ".xlsx", ".tsv", ".txt", ".json", ".ofx", ".qfx", ".qbo"}
BANK_FILE_EXT = {".ofx", ".qfx", ".qbo"}  # Open Financial Exchange: US bank / QuickBooks downloads
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0"  # legacy .xls / macro containers

CANONICAL = {
    # Bank: one signed amount, OR separate money-in / money-out columns (Paid in / Paid out, Credit / Debit).
    # Ledger: separate debit / credit, OR one signed amount (cash side: positive = money in).
    "bank": ["line_id", "date", "description", "reference", "amount", "currency", "paid_in", "paid_out"],
    "ledger": ["entry_id", "posted_date", "account", "description", "reference", "debit", "credit", "currency", "amount"],
    # The trailing AR/AP fields are optional facts for CPA review (state tax readiness, foreign supplier).
    # Files without them import exactly as before; nothing is ever computed from them, only flagged.
    "ar": ["invoice_id", "customer_alias", "issue_date", "amount", "open_balance", "currency",
           "ship_from_state", "ship_to_state", "taxability", "nexus_status", "dest_registration"],
    "ap": ["bill_id", "vendor_alias", "issue_date", "amount", "open_balance", "currency", "vendor_country"],
    "documents": ["doc_id", "doc_type", "reference", "vendor_alias", "amount", "file"],
    "parties": ["party_id", "name", "type", "aliases", "ref_prefixes"],
}

# Deterministic header synonyms. An optional AI mapping assistant may *propose*
# beyond this, but the user must confirm (state MAPPED_PENDING_CONFIRMATION).
SYNONYMS = {
    "line_id": ["txn id", "transaction id", "id", "line id", "fitid"],
    "entry_id": ["entry id", "journal id", "je id", "id"],
    "date": ["posting date", "transaction date", "txn date", "date", "dtposted"],
    "posted_date": ["posted date", "posting date", "date"],
    "description": ["memo", "narrative", "details", "description", "name"],
    "reference": ["ref #", "ref", "reference", "check number", "doc ref", "refnum"],
    "amount": ["amount (usd)", "amount", "value", "net amount", "trnamt"],
    "currency": ["ccy", "currency", "cur", "curdef"],
    "account": ["account", "gl account", "account name"],
    "debit": ["debit", "dr"],
    "credit": ["credit", "cr"],
    # bank statements are written from the account holder's side: a credit is money in, a debit money out
    "paid_in": ["paid in", "money in", "credit", "credits", "credit amount", "deposit", "deposits", "receipts"],
    "paid_out": ["paid out", "money out", "debit", "debits", "debit amount", "withdrawal", "withdrawals", "payments"],
    "bill_id": ["bill id", "bill number", "bill no", "vendor invoice", "invoice number", "invoice id", "reference"],
    "vendor_alias": ["vendor", "vendor name", "supplier", "payee"],
    "customer_alias": ["customer", "customer name", "client"],
    "issue_date": ["issue date", "bill date", "invoice date", "date"],
    "open_balance": ["open balance", "balance due", "amount due", "open amount"],
    "ship_from_state": ["ship from state", "origin state", "from state", "seller state"],
    "ship_to_state": ["ship to state", "destination state", "to state", "customer state"],
    "taxability": ["taxable", "tax status", "taxability code"],
    "nexus_status": ["nexus", "economic nexus"],
    "dest_registration": ["registered in destination", "destination registration", "sales tax registration"],
    "vendor_country": ["country", "supplier country", "vendor country code"],
}


class IngestError(Exception):
    pass


@dataclass
class ImportResult:
    kind: str
    source_sha256: str
    rows_received: int
    accepted: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)  # {"row": n, "reason": str, "raw": {...}}
    debit_cents: int = 0
    credit_cents: int = 0

    @property
    def net_cents(self) -> int:
        return self.credit_cents - self.debit_cents


MAX_AMOUNT = Decimal("1000000000000")  # 1 trillion: anything larger is a data error, not a transaction
_EU_DECIMAL = re.compile(r"[-+]?\d{1,3}(?:\.\d{3})*,\d{1,2}|[-+]?\d+,\d{1,2}")   # 1.234,56 or 12,5
_US_GROUPED = re.compile(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")                  # 1,234.56


def to_cents(raw) -> int:
    if raw is None or str(raw).strip() == "":
        raise ValueError("empty amount")
    s = str(raw).strip().replace("$", "").replace(" ", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    if "," in s:
        # Never guess silently: "1,50" must not become 150.00. Comma as decimal (European) or comma as
        # thousands separator in groups of three (US); anything else is refused with a reason.
        if _EU_DECIMAL.fullmatch(s):
            s = s.replace(".", "").replace(",", ".")
        elif _US_GROUPED.fullmatch(s):
            s = s.replace(",", "")
        else:
            raise ValueError(f"ambiguous amount {raw!r} (use 1234.56 or 1,234.56)")
    try:
        d = Decimal(s)
        if not d.is_finite():
            raise ValueError(f"amount is not a finite number: {raw!r}")
        if abs(d) >= MAX_AMOUNT:
            raise ValueError(f"amount out of range: {raw!r}")
        if d != d.quantize(Decimal("0.01")):
            raise ValueError(f"more than 2 decimal places: {raw!r}")
    except InvalidOperation as e:
        raise ValueError(f"unparseable amount {raw!r}") from e
    cents = int(d * 100)
    return -cents if neg else cents


def norm_ref(ref: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (ref or "").upper())


_SLASH_DATE = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")


def date_order(values) -> str:
    """Day/month order for a file's non-ISO dates (e.g. 10/05/2026): 'DMY' if any first part is > 12,
    otherwise 'MDY' (US default, also used when every date in the file is ambiguous)."""
    for v in values:
        m = _SLASH_DATE.fullmatch(str(v or "").strip()[:10])
        if m and int(m[1]) > 12:
            return "DMY"
    return "MDY"


def describe_dates(values) -> dict | None:
    """What the mapping step shows a person about a file's dates, so the day/month order is confirmed, never assumed.
    order: ISO | DMY | MDY.  certain: the file itself proves the order (a part above 12, or ISO dates).
    reason: one plain sentence. None when the column has no dates at all."""
    vals = [str(v or "").strip() for v in values if str(v or "").strip()]
    if not vals:
        return None
    slash = [m for m in (_SLASH_DATE.fullmatch(v[:10]) for v in vals) if m]
    if not slash:
        return {"order": "ISO", "certain": True, "reason": "Dates are in year-month-day form, so there is no day/month doubt.",
                "example": vals[0][:10]}
    day_first = next((m[0] for m in slash if int(m[1]) > 12), None)
    month_first = next((m[0] for m in slash if int(m[2]) > 12), None)
    if day_first and not month_first:
        return {"order": "DMY", "certain": True, "reason": f"{day_first} can only be day/month, so dates are read as DD/MM/YYYY.",
                "example": day_first}
    if month_first and not day_first:
        return {"order": "MDY", "certain": True, "reason": f"{month_first} can only be month/day, so dates are read as MM/DD/YYYY.",
                "example": month_first}
    if day_first and month_first:
        return {"order": "MDY", "certain": False, "reason": "This file mixes day/month and month/day dates; check the format "
                "before importing.", "example": slash[0][0]}
    return {"order": "MDY", "certain": False, "reason": f"Every date (e.g. {slash[0][0]}) could be either way round. They will be "
            "read as MM/DD/YYYY (US) unless you choose DD/MM/YYYY.", "example": slash[0][0]}


def parse_date(raw, order: str = "MDY") -> date:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    m = _SLASH_DATE.fullmatch(s[:10])
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        mm, dd = (b, a) if order == "DMY" else (a, b)
        try:
            return date(y, mm, dd)
        except ValueError as e:
            raise ValueError(f"invalid date {s!r} ({'DD/MM/YYYY' if order == 'DMY' else 'MM/DD/YYYY'})") from e
    try:
        return date.fromisoformat(s[:10])
    except ValueError as e:
        raise ValueError(f"unreadable date {s!r} (use YYYY-MM-DD or MM/DD/YYYY)") from e


def sniff_and_check(filename: str, blob: bytes) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise IngestError(f"unsupported file type {ext or '(none)'}; CSV, TSV, Excel (.xlsx), JSON or OFX/QFX/QBO only")
    if len(blob) > MAX_BYTES:
        raise IngestError("file exceeds 10 MB limit")
    if blob.startswith(OLE_MAGIC):
        raise IngestError("legacy/macro container rejected")
    if ext != ".xlsx" and blob.startswith(ZIP_MAGIC):
        raise IngestError("archive disguised as CSV rejected")
    if ext == ".xlsx":
        if not blob.startswith(ZIP_MAGIC):
            raise IngestError("not a valid XLSX container")
        import zipfile

        try:
            z = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile as e:
            raise IngestError("not a valid XLSX file (the workbook is damaged)") from e
        with z:
            names = z.namelist()
            if "xl/workbook.xml" not in names:
                raise IngestError("not a valid XLSX file (no workbook inside)")
            if any(n.endswith("vbaProject.bin") for n in names):
                raise IngestError("macro-enabled workbook rejected")
            if sum(i.file_size for i in z.infolist()) > 50 * MAX_BYTES:
                raise IngestError("decompressed size too large (zip bomb guard)")
            if any(n.startswith("xl/externalLinks/") for n in names):
                raise IngestError("workbook with external links rejected")
    return ext


def read_table(filename: str, blob: bytes) -> tuple[list[str], list[list]]:
    ext = sniff_and_check(filename, blob)
    if ext in BANK_FILE_EXT:
        rows = _read_ofx(blob)
    elif ext == ".json":
        rows = _read_json(blob)
    elif ext in (".csv", ".tsv", ".txt"):
        if blob.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = blob.decode("utf-16")
        else:
            try:
                text = blob.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = blob.decode("latin-1")
        first = text.splitlines()[0] if text else ""
        counts = {d: first.count(d) for d in ("\t", "|", ";", ",")}
        delim = "\t" if ext == ".tsv" else max(counts, key=lambda d: (counts[d], d == ","))
        if counts[delim] == 0:
            delim = ","
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        except csv.Error as e:
            raise IngestError(f"CSV could not be read: {e}") from e
    else:
        from openpyxl import load_workbook

        # data_only=False so formulas come back as strings and are rejected, never evaluated
        try:
            wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=False)
            ws = wb.worksheets[0]
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
        except Exception as e:  # damaged or non-Excel content inside a zip
            raise IngestError("Excel file could not be read (damaged or not a workbook)") from e
    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        raise IngestError("no data")
    if len(rows) - 1 > MAX_ROWS:
        raise IngestError(f"more than {MAX_ROWS} rows")
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    return header, rows[1:]


def _read_ofx(blob: bytes) -> list[list]:
    """OFX / QFX / QBO bank download (SGML or XML). Parsed as text only; nothing is executed."""
    text = blob.decode("latin-1")
    if "<OFX" not in text.upper():
        raise IngestError("not an OFX/QFX/QBO bank file")
    import re
    cur = (re.search(r"<CURDEF>\s*([A-Z]{3})", text, re.I) or [None, "USD"])[1].upper()
    rows = [["FITID", "DTPOSTED", "NAME", "REFNUM", "TRNAMT", "CURDEF"]]
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|</BANKTRANLIST>)", text, re.S | re.I):
        tag = lambda t: (re.search(rf"<{t}>\s*([^<\r\n]*)", block, re.I) or [None, ""])[1].strip()
        d = tag("DTPOSTED")[:8]
        rows.append([tag("FITID"), f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d,
                     tag("NAME") or tag("MEMO"), tag("REFNUM") or tag("CHECKNUM"), tag("TRNAMT"), cur])
    if len(rows) == 1:
        raise IngestError("no transactions found in bank file")
    return rows


def _read_json(blob: bytes) -> list[list]:
    """JSON export: a list of flat records, or {"rows"|"data"|"transactions"|"records": [...]}.
    Parsed as data only; nested values are kept as text and never interpreted."""
    import json
    try:
        doc = json.loads(blob.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise IngestError("not valid JSON") from e
    if isinstance(doc, dict):
        doc = next((doc[k] for k in ("rows", "data", "transactions", "records", "items") if isinstance(doc.get(k), list)), None)
    if not isinstance(doc, list) or not all(isinstance(r, dict) for r in doc):
        raise IngestError("JSON must be a list of records (objects with the same fields)")
    header: list[str] = []
    for r in doc:
        header += [k for k in r if k not in header]
    cell = lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v
    return [header] + [[cell(r.get(h)) for h in header] for r in doc]


def propose_mapping(kind: str, header: list[str]) -> dict[str, str | None]:
    """canonical field -> source column. Unknowns are None (user must map)."""
    lower = {h.lower(): h for h in header}
    out = {}
    for f in CANONICAL[kind]:
        hit = lower.get(f)
        if not hit:
            for syn in SYNONYMS.get(f, []):
                if syn in lower:
                    hit = lower[syn]
                    break
        out[f] = hit
    return out


# A required field may instead be covered by these columns (e.g. a bank statement with Paid in / Paid out columns
# has no single Amount). Either way every row must give exactly one non-zero amount.
ALTERNATIVES = {"bank": {"amount": ("paid_in", "paid_out")}}


def missing_required(kind: str, mapping: dict) -> list[str]:
    alt = ALTERNATIVES.get(kind, {})
    return [f for f in REQUIRED[kind] if not mapping.get(f) and not any(mapping.get(a) for a in alt.get(f, ()))]


def required_for(kind: str, mapping: dict) -> list[str]:
    """Required fields as shown in the mapping step, after alternatives are taken into account."""
    out = []
    for f in REQUIRED[kind]:
        alts = [a for a in ALTERNATIVES.get(kind, {}).get(f, ()) if mapping.get(a)]
        out += alts if (alts and not mapping.get(f)) else [f]
    return out


REQUIRED = {
    "bank": ["line_id", "date", "amount", "currency"],
    "ledger": ["entry_id", "posted_date", "currency"],
    "ar": ["invoice_id", "amount", "open_balance", "currency"],
    "ap": ["bill_id", "amount", "open_balance", "currency"],
    "documents": ["doc_id", "reference"],
    "parties": ["party_id", "name"],
}


KIND_LABEL = {"bank": "bank statement", "ledger": "general ledger", "ar": "open invoices (AR)",
              "ap": "vendor bills (AP)", "documents": "supporting documents list", "parties": "customer & vendor list"}


def detect_kind(header: list[str], rows: list[list], hint: str | None = None) -> tuple[str, dict]:
    """Work out what a file is from its CONTENT, not its name.

    A type fits only if every one of its required fields maps to a column AND the rows actually read
    as that type (a dry run of the real import). Among types that fit, the one that reads the most
    rows cleanly and maps the most columns wins; the filename hint only breaks an exact tie.
    Raises IngestError, with the columns found and what was missing, when nothing fits."""
    fits, near = [], []
    for kind in CANONICAL:
        mapping = propose_mapping(kind, header)
        missing = missing_required(kind, mapping)
        if missing:
            near.append((len(missing), kind, missing))
            continue
        res = import_table(kind, "", b"", mapping, table=(header, rows))
        if not res.accepted:
            first = res.rejected[0]["reason"] if res.rejected else "no rows"
            near.append((0.5, kind, [f"rows could not be read ({first})"]))
            continue
        mapped = sum(1 for v in mapping.values() if v)
        fits.append((len(res.accepted) / max(1, len(rows)), mapped / len(CANONICAL[kind]), kind == hint, kind, mapping))
    if not fits:
        near.sort()
        best = "; ".join((f"as a {KIND_LABEL[k]}, {miss[0]}" if miss and miss[0].startswith("rows could not") else
                          f"as a {KIND_LABEL[k]} it is missing {', '.join(m.replace('_', ' ') for m in miss)}")
                         for _, k, miss in near[:2])
        cols = ", ".join(h for h in header if h)[:200] or "(no column names)"
        raise IngestError(f"Not accepted: this file doesn't look like a bank statement, ledger or other finance record LedgerSight can read. "
                          f"Columns found: {cols}. Closest match: {best}. "
                          f"Download a sample template on the Sources page to see the columns expected.")
    fits.sort(reverse=True)
    top = fits[0]
    return top[3], {"method": "detected from the file's columns and rows (file name not used)" if not top[2] or len(fits) == 1
                    else "detected from the file's columns and rows; file name used only to break a tie",
                    "candidates": [f[3] for f in fits]}


def import_table(kind: str, filename: str, blob: bytes, mapping: dict[str, str | None],
                 table: tuple[list[str], list[list]] | None = None, confirmed_order: str | None = None) -> ImportResult:
    """`table` is an already-extracted (header, rows), e.g. from a PDF statement read in the isolated parser.
    `confirmed_order` ("DMY" / "MDY") is the order a person confirmed in the mapping step; otherwise it is detected."""
    if confirmed_order not in (None, "DMY", "MDY"):
        raise IngestError("date order must be DMY or MDY")
    missing = missing_required(kind, mapping)
    if missing:
        raise IngestError(f"mapping incomplete: {missing}")
    header, rows = table if table else read_table(filename, blob)
    idx = {f: header.index(col) for f, col in mapping.items() if col and col in header}
    res = ImportResult(kind=kind, source_sha256=hashlib.sha256(blob).hexdigest(), rows_received=len(rows))
    seen_ids = set()
    dcol = idx.get("date", idx.get("posted_date"))
    order = confirmed_order or (date_order(r[dcol] for r in rows if dcol < len(r)) if dcol is not None else "MDY")
    for n, r in enumerate(rows, start=2):
        raw = {f: (r[i] if i < len(r) else None) for f, i in idx.items()}
        try:
            for v in raw.values():
                if isinstance(v, str) and v.startswith("="):
                    raise ValueError("formula cell rejected (not evaluated)")
            rec = _canon(kind, raw, order)
            rid = rec["id"]
            if rid in seen_ids:
                raise ValueError(f"duplicate source id {rid} in batch")
            seen_ids.add(rid)
            rec["source_row"] = n
            rec["source_sha256"] = res.source_sha256
        except (ValueError, TypeError, ArithmeticError) as e:
            res.rejected.append({"row": n, "reason": str(e), "raw": {k: str(v) for k, v in raw.items()}})
            continue
        res.accepted.append(rec)
        if kind in ("bank", "ledger"):
            if rec["amount_cents"] < 0:
                res.debit_cents += -rec["amount_cents"]
            else:
                res.credit_cents += rec["amount_cents"]
    return res


def _nonzero(v: str) -> str:
    """'' for an empty or zero amount cell (exports often write 0.00 in the unused debit/credit column)."""
    try:
        return "" if not v or to_cents(v) == 0 else v
    except ValueError:
        return v  # unreadable: keep it, so the row is rejected with the real reason


def _canon(kind: str, raw: dict, order: str = "MDY") -> dict:
    s = lambda k: ("" if raw.get(k) is None else str(raw.get(k)).strip())
    if kind == "bank":
        cur = s("currency").upper()
        if not re.fullmatch(r"[A-Z]{3}", cur):
            raise ValueError(f"bad currency {cur!r}")
        if s("amount") or not ("paid_in" in raw or "paid_out" in raw):
            amt = to_cents(raw.get("amount"))  # a blank cell is rejected as "empty amount", as before
        else:  # separate money-in / money-out columns; a 0.00 in the unused column counts as empty
            pin, pout = _nonzero(s("paid_in")), _nonzero(s("paid_out"))
            if bool(pin) == bool(pout):
                raise ValueError("exactly one of paid in / paid out required")
            amt = abs(to_cents(pin)) if pin else -abs(to_cents(pout))
        return {"id": s("line_id"), "date": parse_date(raw["date"], order), "description": s("description"),
                "reference": s("reference"), "ref_norm": norm_ref(s("reference")),
                "amount_cents": amt, "currency": cur}
    if kind == "ledger":
        dr, cr = _nonzero(s("debit")), _nonzero(s("credit"))
        # Ledger lines here are the cash-side postings: debit to income/AR receipt = cash in.
        if not dr and not cr and s("amount"):  # one signed amount column (positive = cash in)
            amt = to_cents(raw["amount"])
        elif bool(dr) == bool(cr):
            raise ValueError("exactly one of debit/credit required")
        else:
            amt = to_cents(dr) if dr else -to_cents(cr)
        return {"id": s("entry_id"), "date": parse_date(raw["posted_date"], order), "account": s("account"),
                "description": s("description"), "reference": s("reference"),
                "ref_norm": norm_ref(s("reference")), "amount_cents": amt, "currency": s("currency").upper()}
    if kind == "ar":
        rec = {"id": s("invoice_id"), "ref_norm": norm_ref(s("invoice_id")),
               "customer_alias": s("customer_alias"), "amount_cents": to_cents(raw["amount"]),
               "open_cents": to_cents(raw["open_balance"]), "currency": s("currency").upper()}
        if s("ship_to_state") or s("ship_from_state"):  # only carried when the file supplies them
            rec.update(issue_date=s("issue_date"), ship_from_state=s("ship_from_state").upper(),
                       ship_to_state=s("ship_to_state").upper(), taxability=s("taxability"),
                       nexus_status=s("nexus_status"), dest_registration=s("dest_registration"))
        return rec
    if kind == "ap":
        rec = {"id": s("bill_id"), "ref_norm": norm_ref(s("bill_id")),
               "vendor_alias": s("vendor_alias"), "amount_cents": to_cents(raw["amount"]),
               "open_cents": to_cents(raw["open_balance"]), "currency": s("currency").upper()}
        if s("vendor_country"):
            rec["vendor_country"] = s("vendor_country").upper()
        return rec
    if kind == "documents":
        return {"id": s("doc_id"), "doc_type": s("doc_type"), "reference": s("reference"),
                "ref_norm": norm_ref(s("reference")),
                "amount_cents": to_cents(raw["amount"]) if s("amount") else None, "file": s("file"),
                "vendor_alias": s("vendor_alias")}
    if kind == "parties":
        split = lambda k: [x.strip().upper() for x in s(k).split(";") if x.strip()]
        return {"id": s("party_id"), "name": s("name"), "type": s("type") or "other",
                "aliases": split("aliases"), "ref_prefixes": [norm_ref(x) for x in split("ref_prefixes")]}
    raise ValueError(kind)
