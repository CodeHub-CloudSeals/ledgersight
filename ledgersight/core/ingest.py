"""Safe source ingestion: CSV/XLSX -> canonical lines in integer cents.

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
from datetime import date
from decimal import Decimal, InvalidOperation

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 5000
ALLOWED_EXT = {".csv", ".xlsx", ".tsv", ".txt", ".ofx", ".qfx", ".qbo"}
BANK_FILE_EXT = {".ofx", ".qfx", ".qbo"}  # Open Financial Exchange: US bank / QuickBooks downloads
ZIP_MAGIC = b"PK\x03\x04"
OLE_MAGIC = b"\xd0\xcf\x11\xe0"  # legacy .xls / macro containers

CANONICAL = {
    "bank": ["line_id", "date", "description", "reference", "amount", "currency"],
    "ledger": ["entry_id", "posted_date", "account", "description", "reference", "debit", "credit", "currency"],
    "ar": ["invoice_id", "customer_alias", "issue_date", "amount", "open_balance", "currency"],
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


def to_cents(raw) -> int:
    if raw is None or str(raw).strip() == "":
        raise ValueError("empty amount")
    s = str(raw).strip().replace(",", "").replace("$", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        d = Decimal(s)
    except InvalidOperation as e:
        raise ValueError(f"unparseable amount {raw!r}") from e
    if d != d.quantize(Decimal("0.01")):
        raise ValueError(f"more than 2 decimal places: {raw!r}")
    cents = int(d * 100)
    return -cents if neg else cents


def norm_ref(ref: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (ref or "").upper())


def parse_date(raw) -> date:
    if isinstance(raw, date):
        return raw
    return date.fromisoformat(str(raw).strip()[:10])


def sniff_and_check(filename: str, blob: bytes) -> str:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXT:
        raise IngestError(f"unsupported file type {ext or '(none)'}; CSV or XLSX only")
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

        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = z.namelist()
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
    elif ext in (".csv", ".tsv", ".txt"):
        try:
            text = blob.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = blob.decode("latin-1")
        first = text.splitlines()[0] if text else ""
        delim = "\t" if ext == ".tsv" or first.count("\t") > first.count(",") else ("|" if first.count("|") > first.count(",") else ",")
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    else:
        from openpyxl import load_workbook

        # data_only=False so formulas come back as strings and are rejected, never evaluated
        wb = load_workbook(io.BytesIO(blob), read_only=True, data_only=False)
        ws = wb.worksheets[0]
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
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


REQUIRED = {
    "bank": ["line_id", "date", "amount", "currency"],
    "ledger": ["entry_id", "posted_date", "currency"],
    "ar": ["invoice_id", "amount", "open_balance", "currency"],
    "documents": ["doc_id", "reference"],
    "parties": ["party_id", "name"],
}


def import_table(kind: str, filename: str, blob: bytes, mapping: dict[str, str | None]) -> ImportResult:
    missing = [f for f in REQUIRED[kind] if not mapping.get(f)]
    if missing:
        raise IngestError(f"mapping incomplete: {missing}")
    header, rows = read_table(filename, blob)
    idx = {f: header.index(col) for f, col in mapping.items() if col and col in header}
    res = ImportResult(kind=kind, source_sha256=hashlib.sha256(blob).hexdigest(), rows_received=len(rows))
    seen_ids = set()
    for n, r in enumerate(rows, start=2):
        raw = {f: (r[i] if i < len(r) else None) for f, i in idx.items()}
        try:
            for v in raw.values():
                if isinstance(v, str) and v.startswith("="):
                    raise ValueError("formula cell rejected (not evaluated)")
            rec = _canon(kind, raw)
            rid = rec["id"]
            if rid in seen_ids:
                raise ValueError(f"duplicate source id {rid} in batch")
            seen_ids.add(rid)
            rec["source_row"] = n
            rec["source_sha256"] = res.source_sha256
        except (ValueError, TypeError) as e:
            res.rejected.append({"row": n, "reason": str(e), "raw": {k: str(v) for k, v in raw.items()}})
            continue
        res.accepted.append(rec)
        if kind in ("bank", "ledger"):
            if rec["amount_cents"] < 0:
                res.debit_cents += -rec["amount_cents"]
            else:
                res.credit_cents += rec["amount_cents"]
    return res


def _canon(kind: str, raw: dict) -> dict:
    s = lambda k: ("" if raw.get(k) is None else str(raw.get(k)).strip())
    if kind == "bank":
        cur = s("currency").upper()
        if not re.fullmatch(r"[A-Z]{3}", cur):
            raise ValueError(f"bad currency {cur!r}")
        return {"id": s("line_id"), "date": parse_date(raw["date"]), "description": s("description"),
                "reference": s("reference"), "ref_norm": norm_ref(s("reference")),
                "amount_cents": to_cents(raw["amount"]), "currency": cur}
    if kind == "ledger":
        dr, cr = s("debit"), s("credit")
        if bool(dr) == bool(cr):
            raise ValueError("exactly one of debit/credit required")
        # Ledger lines here are the cash-side postings: debit to income/AR receipt = cash in.
        amt = to_cents(dr) if dr else -to_cents(cr)
        return {"id": s("entry_id"), "date": parse_date(raw["posted_date"]), "account": s("account"),
                "description": s("description"), "reference": s("reference"),
                "ref_norm": norm_ref(s("reference")), "amount_cents": amt, "currency": s("currency").upper()}
    if kind == "ar":
        return {"id": s("invoice_id"), "ref_norm": norm_ref(s("invoice_id")),
                "customer_alias": s("customer_alias"), "amount_cents": to_cents(raw["amount"]),
                "open_cents": to_cents(raw["open_balance"]), "currency": s("currency").upper()}
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
