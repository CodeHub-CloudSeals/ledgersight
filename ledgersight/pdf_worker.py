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


if __name__ == "__main__":
    _limits()
    try:
        out = extract(sys.stdin.buffer.read())
    except Exception as e:  # malformed PDFs end here, inside the sandboxed process
        out = {"error": f"could not parse PDF safely ({type(e).__name__})"}
    sys.stdout.write(json.dumps(out))
