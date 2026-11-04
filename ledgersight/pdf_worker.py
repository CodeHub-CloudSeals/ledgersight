"""Isolated text-PDF extractor. Runs as a separate, resource-limited process:

    python -I ledgersight/pdf_worker.py < file.pdf  -> JSON on stdout

Text extraction only: no OCR, no JavaScript, no embedded files, no network.
Document text is DATA. Instruction-shaped content is flagged, never followed.
Every proposed field carries the exact line and page it was read from.
"""
from __future__ import annotations

import io
import json
import re
import sys

MAX_PAGES = 20

INJECTION = re.compile(r"(ignore (all |any )?(previous|prior) instructions|disregard .*instructions|"
                       r"transfer \$?[\d,]+|wire (the )?funds|send (the )?(password|credentials)|"
                       r"you are now|system prompt|approve (this|all) (payment|invoice)s? automatically)", re.I)
REF = re.compile(r"\b(?:invoice|receipt|ref(?:erence)?|order)\s*(?:no\.?|number|#)?\s*[:#]?\s*([A-Z]{1,6}-?\d{2,7})\b", re.I)
DATE = re.compile(r"\b(20\d\d-\d\d-\d\d|\d{1,2}/\d{1,2}/20\d\d)\b")
AMT = re.compile(r"\$?\s*(-?[\d,]+\.\d{2})\b")
TOTAL_LABELS = ("amount due", "total due", "balance due", "grand total", "total")


def _limits():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    except Exception:
        pass


def extract(blob: bytes) -> dict:
    import pdfplumber

    if not blob.startswith(b"%PDF-"):
        return {"error": "not a PDF file"}
    if b"/Encrypt" in blob:
        return {"error": "encrypted PDF rejected"}
    if b"/JavaScript" in blob or b"/EmbeddedFile" in blob or b"/Launch" in blob:
        return {"error": "PDF with scripts, launch actions or embedded files rejected"}
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        if len(pdf.pages) > MAX_PAGES:
            return {"error": f"more than {MAX_PAGES} pages"}
        pages = [(i + 1, (p.extract_text() or "")) for i, p in enumerate(pdf.pages)]
    lines = [(pg, ln.strip()) for pg, t in pages for ln in t.splitlines() if ln.strip()]
    if not lines:
        return {"error": "no selectable text (scanned image PDF). OCR intake is not enabled in this sandbox."}

    fields: dict[str, dict] = {}

    def cite(name, value, pg, line, conf):
        if name not in fields:
            fields[name] = {"value": value, "page": pg, "source_text": line[:160], "confidence": conf}

    cite("vendor", lines[0][1][:80], lines[0][0], lines[0][1], "medium")
    for pg, ln in lines:
        m = REF.search(ln)
        if m:
            cite("reference", m.group(1).upper(), pg, ln, "high")
        m = DATE.search(ln)
        if m:
            cite("date", m.group(1), pg, ln, "high" if re.search(r"date", ln, re.I) else "medium")
    for label in TOTAL_LABELS:
        for pg, ln in lines:
            if label in ln.lower():
                m = AMT.findall(ln)
                if m:
                    cite("amount", m[-1].replace(",", ""), pg, ln, "high")
                    break
        if "amount" in fields:
            break
    flags = [{"page": pg, "source_text": ln[:160]} for pg, ln in lines if INJECTION.search(ln)]
    for f in ("reference", "date", "amount"):
        fields.setdefault(f, {"value": "", "page": None, "source_text": "not found — enter manually", "confidence": "none"})
    return {"pages": len(pages), "lines": len(lines), "fields": fields, "unsafe_content": flags,
            "method": "text extraction + deterministic patterns (no AI model, no OCR)"}


MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_WORDS = {"date", "posted", "posting"}
AMOUNT_WORDS = {"amount", "amt", "value"}
DEBIT_WORDS = {"debit", "debits", "withdrawal", "withdrawals", "paid", "out"}
CREDIT_WORDS = {"credit", "credits", "deposit", "deposits", "received", "in"}
MONEY = re.compile(r"[-(]?\$?[\d,]+\.\d{2}\)?(CR|DR)?")


def _iso(raw: str) -> str | None:
    s = raw.strip()
    m = re.fullmatch(r"(20\d\d)-(\d\d)-(\d\d)", s)
    if m:
        return s
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(20\d\d)", s)  # US statements: MM/DD/YYYY
    if m:
        return f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}"
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(20\d\d)", s)
    if m and m[2].lower() in MONTHS:
        return f"{m[3]}-{MONTHS[m[2].lower()]:02d}-{int(m[1]):02d}"
    return None


def _money(raw: str) -> str:
    s = raw.replace("$", "").replace(",", "").strip()
    neg = s.startswith("(") and s.endswith(")") or s.endswith("DR") or s.startswith("-")
    s = s.strip("()").replace("DR", "").replace("CR", "").strip("- ")
    return ("-" if neg and s else "") + s


def extract_statement(blob: bytes) -> dict:
    """A bank statement (or ledger listing) printed as a text PDF -> a table of rows.

    Finds the header row (Date ... Description ... Amount, or Debit/Credit columns) and reads every
    later line by column position. Lines without a valid date (opening/closing balance, page totals)
    are skipped. Values are returned as text for the normal column-mapping step; nothing is guessed."""
    import pdfplumber

    if not blob.startswith(b"%PDF-"):
        return {"error": "not a PDF file"}
    if b"/Encrypt" in blob:
        return {"error": "encrypted PDF rejected"}
    if b"/JavaScript" in blob or b"/EmbeddedFile" in blob or b"/Launch" in blob:
        return {"error": "PDF with scripts, launch actions or embedded files rejected"}
    lines, texts = [], []
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        if len(pdf.pages) > MAX_PAGES:
            return {"error": f"more than {MAX_PAGES} pages"}
        npages = len(pdf.pages)
        for pg, page in enumerate(pdf.pages, 1):
            texts.append(page.extract_text() or "")
            rows: dict[int, list] = {}
            for w in page.extract_words(keep_blank_chars=False, x_tolerance=1.5):
                rows.setdefault(round(w["top"] / 3), []).append(w)
            for k in sorted(rows):
                lines.append((pg, sorted(rows[k], key=lambda w: w["x0"])))
    if not any(t.strip() for t in texts):
        return {"error": "no selectable text (scanned image PDF). OCR intake is not enabled in this sandbox."}

    def header_cols(words):
        cols = []
        for w in words:  # merge header words that sit close together ("Txn ID", "Paid out")
            if cols and w["x0"] - cols[-1]["x1"] < 7:
                cols[-1].update(text=cols[-1]["text"] + " " + w["text"], x1=w["x1"])
            else:
                cols.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"]})
        names = [set(c["text"].lower().split()) for c in cols]
        has_date = any(n & DATE_WORDS for n in names)
        has_amt = any(n & AMOUNT_WORDS for n in names) or (any(n & DEBIT_WORDS for n in names) and any(n & CREDIT_WORDS for n in names))
        return cols if has_date and has_amt and len(cols) >= 3 else None

    cols, out_rows, skipped = None, [], 0
    for pg, words in lines:
        h = header_cols(words)
        if h:
            cols = h
            continue
        if not cols:
            continue
        cells = [""] * len(cols)
        money_cols = [i for i, c in enumerate(cols) if set(c["text"].lower().split()) & (AMOUNT_WORDS | DEBIT_WORDS | CREDIT_WORDS | {"balance"})]
        for w in words:
            if money_cols and MONEY.fullmatch(w["text"]):  # right-aligned figures: nearest money column by right edge
                j = min(money_cols, key=lambda i: abs(cols[i]["x1"] - w["x1"]))
            else:  # left-aligned text: the last column whose header starts at or before the word
                j = max([i for i, c in enumerate(cols) if c["x0"] <= w["x0"] + 2] or [0])
            cells[j] = (cells[j] + " " + w["text"]).strip()
        d_idx = next(i for i, c in enumerate(cols) if set(c["text"].lower().split()) & DATE_WORDS)
        iso = _iso(cells[d_idx])
        if not iso:
            skipped += 1 if any(cells) else 0
            continue
        cells[d_idx] = iso
        out_rows.append(cells)
    if not cols:
        return {"error": "no transaction table found (expected a header row with Date and Amount, or Debit and Credit)"}
    if not out_rows:
        return {"error": "a table header was found but no dated transaction lines"}

    header = [c["text"] for c in cols]
    low = [set(h.lower().split()) for h in header]
    deb = next((i for i, n in enumerate(low) if n & DEBIT_WORDS and not n & (AMOUNT_WORDS | CREDIT_WORDS)), None)
    cre = next((i for i, n in enumerate(low) if n & CREDIT_WORDS and not n & (AMOUNT_WORDS | DEBIT_WORDS - {"paid"})), None)
    amt = next((i for i, n in enumerate(low) if n & AMOUNT_WORDS), None)
    if amt is None and deb is not None and cre is not None:  # separate Paid out / Paid in columns -> one signed amount
        header.append("Amount")
        for r in out_rows:
            d, c = _money(r[deb]) or "0", _money(r[cre]) or "0"
            r.append(f"{float(c) - float(d):.2f}")
    elif amt is not None:
        for r in out_rows:
            r[amt] = _money(r[amt])
    full = "\n".join(texts)
    cur = re.search(r"\bcurrency\s*[:\-]?\s*([A-Z]{3})\b", full, re.I)
    if not any("currency" in h.lower() or h.lower() in ("ccy", "cur") for h in header):
        header.append("Currency")
        for r in out_rows:
            r.append((cur[1] if cur else "USD").upper())
    if not any(set(h.lower().split()) & {"id", "txn", "transaction", "line"} for h in header):
        header.insert(0, "Line ID")
        for i, r in enumerate(out_rows, 1):
            r.insert(0, f"PDF-{i:03d}")
    flags = [{"page": None, "source_text": ln.strip()[:160]} for ln in full.splitlines() if INJECTION.search(ln)]
    return {"pages": npages, "header": header, "rows": out_rows, "skipped_lines": skipped, "unsafe_content": flags,
            "method": "PDF table read by column position + deterministic header rules (no AI model, no OCR)"}


if __name__ == "__main__":
    _limits()
    try:
        data = sys.stdin.buffer.read()
        out = extract_statement(data) if sys.argv[1:2] == ["statement"] else extract(data)
    except Exception as e:  # malformed PDFs end here, inside the sandboxed process
        out = {"error": f"could not parse PDF safely ({type(e).__name__})"}
    sys.stdout.write(json.dumps(out))
