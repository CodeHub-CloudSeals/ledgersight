"""Workspace-scoped CPA reconciliation service.

Tenant = Avocet. Workspace = one Avocet client. Every method takes the
authenticated user and a workspace id, and every action goes through the policy
gate before it happens; the decision (ALLOW or DENY) is written to that
workspace's signed evidence chain. Nothing here can post to a ledger, move money
or contact a client: no such code path exists.
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from .auth import User
from .core import evidence, ingest, policy, recon

TENANT = os.environ.get("LEDGERSIGHT_TENANT", "firm-01")  # internal id; never shown as a customer name
WORKSPACES = {
    "harborline": {"name": "Harborline Design LLC", "period": "October 2026", "industry": "Design studio"},
    "bayview": {"name": "Bayview Marine Services", "period": "October 2026", "industry": "Marine services"},
}
# Which client books this deployment shows. The local demo runs one client (AVOCET_CLIENTS=harborline).
_only = [c.strip() for c in os.environ.get("AVOCET_CLIENTS", "").split(",") if c.strip() in WORKSPACES]
if _only:
    WORKSPACES = {k: WORKSPACES[k] for k in _only}
DATA_DIR = Path(os.environ.get("AVOCET_DATA_DIR", "/tmp/avocet-data"))
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class Denied(Exception):
    def __init__(self, code: str, conceal: bool = False):
        super().__init__(code)
        self.code, self.conceal = code, conceal


def _ts():
    return datetime.now(timezone.utc).isoformat()


class Workspace:
    def __init__(self, wid: str, key: Ed25519PrivateKey):
        self.id = wid
        self.meta = WORKSPACES[wid]
        self.uploads: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}  # kind -> accepted import (latest)
        self.digests: dict[tuple, str] = {}  # (kind, sha) -> upload id, idempotency (workspace-scoped)
        self.blobs: dict[str, bytes] = {}
        self.party_reviews: dict[str, dict] = {}  # line_id -> human confirmation of who the counterparty is
        self.runs: dict[str, dict] = {}
        self.log = evidence.EvidenceLog(key, "ledgersight-ed25519-1")
        self.started_at = _ts()


class Service:
    def __init__(self):
        key_pem = os.environ.get("AVOCET_SIGNING_KEY_PEM")
        self._key = (serialization.load_pem_private_key(key_pem.encode(), None) if key_pem
                     else Ed25519PrivateKey.generate())
        self.key_source = "secret store" if key_pem else "ephemeral (generated at start-up)"
        self.ws: dict[str, Workspace] = {w: Workspace(w, self._key) for w in WORKSPACES}
        self.security_log = evidence.EvidenceLog(self._key, "ledgersight-ed25519-1")
        self.region = os.environ.get("AVOCET_REGION_DECLARED", "local-development")

    def public_key_pem(self) -> bytes:
        return self._key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)

    # ---- gate ----------------------------------------------------------
    def gate(self, user: User, wid: str, action: str, *, data_class="synthetic", model_route="deterministic",
             maker_id=None, refs=None, run_id=None, rule=None, approval_id=None, batch_sha=None) -> Workspace:
        mem = {user.user_id: user.workspaces}
        req = {"actor_id": user.user_id, "role": user.role, "workspace_id": wid, "tool_action": action,
               "data_class": data_class, "model_route": model_route, "maker_id": maker_id}
        d = policy.evaluate(req, mem)
        target = self.ws.get(wid) if wid in user.workspaces else None
        log = target.log if target else self.security_log
        log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                   workflow_pack="cpa-bank-reconciliation", run_id=run_id, source_batch_sha256=batch_sha,
                   source_line_refs=refs or [], canonical_version="canon-1", matching_rule_id=rule,
                   model_route=model_route, action=action, policy_decision=f"{d.decision}:{d.reason_code}",
                   approval_id=approval_id)
        if d.decision != "ALLOW":
            raise Denied(d.reason_code, conceal=(d.reason_code == "WORKSPACE_SCOPE"))
        if wid not in self.ws:
            raise Denied("WORKSPACE_SCOPE", conceal=True)
        return self.ws[wid]

    def workspaces_for(self, user: User):
        return [{"id": w, **WORKSPACES[w], "status": self.status(w)} for w in WORKSPACES if w in user.workspaces]

    def status(self, wid):
        w = self.ws[wid]
        last = list(w.runs.values())[-1] if w.runs else None
        exc = last["result"]["exceptions"] if last else []
        return {"bank": "bank" in w.batches, "ledger": "ledger" in w.batches, "ar": "ar" in w.batches,
                "documents": "documents" in w.batches, "parties": "parties" in w.batches,
                "runs": len(w.runs), "uploads": len(w.uploads),
                "last_run": last["id"] if last else None,
                "matched": len(last["result"]["matches"]) if last else None,
                "exceptions": len(exc), "open_exceptions": sum(e["status"] == "OPEN" for e in exc),
                "variance_cents": last["result"]["variance_cents"] if last else None,
                "unexplained_cents": last["result"]["unexplained_variance_cents"] if last else None,
                "last_activity": w.log.events[-1]["timestamp_utc"] if w.log.events else w.started_at}

    # ---- uploads -------------------------------------------------------
    def upload(self, user, wid, kind, filename, blob, data_class="synthetic"):
        if kind not in ingest.CANONICAL:
            raise ValueError("unknown source type")
        w = self.gate(user, wid, "upload.create", data_class=data_class)
        if filename.lower().endswith(".pdf"):
            raise ingest.IngestError("PDFs go through document intake: choose 'Invoice / receipt PDF'.")
        header, rows = ingest.read_table(filename, blob)  # quarantine checks happen here
        uid = "UP-" + uuid.uuid4().hex[:8]
        sha = __import__("hashlib").sha256(blob).hexdigest()
        prefix = DATA_DIR / TENANT / wid
        prefix.mkdir(parents=True, exist_ok=True)
        (prefix / f"{uid}.{filename.rsplit('.', 1)[-1].lower()}").write_bytes(blob)
        mapping = ingest.propose_mapping(kind, header)
        rec = {"id": uid, "kind": kind, "filename": filename, "sha256": sha, "size": len(blob),
               "header": header, "preview": [[str(c) if c is not None else "" for c in r] for r in rows[:6]],
               "rows": len(rows), "proposed_mapping": mapping, "state": "MAPPED_PENDING_CONFIRMATION",
               "uploaded_by": user.user_id, "uploaded_at": _ts(),
               "duplicate_of": w.digests.get((kind, sha)),
               "mapping_method": "deterministic header rules (no AI model invoked)"}
        w.uploads[uid] = rec
        w.blobs[uid] = blob
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="agent", actor_id="agent:intake",
                     action="upload.quarantine_passed", policy_decision="ALLOW:OK", source_batch_sha256=sha,
                     workflow_pack="cpa-bank-reconciliation", model_route="deterministic")
        return rec

    def commit_mapping(self, user, wid, upload_id, mapping):
        w = self.gate(user, wid, "mapping.commit")
        up = w.uploads.get(upload_id)
        if not up:
            raise KeyError(upload_id)
        if up["state"] == "VALIDATED":
            return up  # idempotent
        dup = w.digests.get((up["kind"], up["sha256"]))
        if dup and dup in w.uploads and w.uploads[dup]["state"] == "VALIDATED":
            up["state"] = "DUPLICATE_BATCH"
            up["health"] = {**w.uploads[dup]["health"], "note": f"identical file already imported as {dup}; "
                            "no rows added twice"}
            return up
        res = ingest.import_table(up["kind"], up["filename"], w.blobs[upload_id], mapping)
        up.update(state="VALIDATED", committed_mapping=mapping, committed_by=user.user_id)
        up["health"] = {"received": res.rows_received, "accepted": len(res.accepted), "rejected": res.rejected,
                        "debit_cents": res.debit_cents, "credit_cents": res.credit_cents, "net_cents": res.net_cents,
                        "control_total_ok": res.rows_received == len(res.accepted) + len(res.rejected)}
        w.batches[up["kind"]] = {"upload_id": upload_id, "sha256": up["sha256"], "rows": res.accepted}
        w.digests[(up["kind"], up["sha256"])] = upload_id
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                     action="mapping.confirmed", policy_decision="ALLOW:OK", source_batch_sha256=up["sha256"],
                     workflow_pack="cpa-bank-reconciliation", canonical_version="canon-1")
        return up

    # ---- text-PDF document intake ----------------------------------------
    def upload_pdf(self, user, wid, filename, blob, data_class="synthetic"):
        w = self.gate(user, wid, "upload.create", data_class=data_class)
        if not filename.lower().endswith(".pdf"):
            raise ingest.IngestError("PDF files only for document intake")
        if len(blob) > ingest.MAX_BYTES:
            raise ingest.IngestError("file exceeds 10 MB limit")
        import hashlib, json, subprocess, sys
        try:
            proc = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("pdf_worker.py"))],
                                  input=blob, capture_output=True, timeout=15, env={"PATH": os.environ.get("PATH", "")})
            out = json.loads(proc.stdout or b"{}") if proc.returncode == 0 else {"error": "parser stopped (resource limit)"}
        except subprocess.TimeoutExpired:
            out = {"error": "parser timed out (15 s limit)"}
        sha = hashlib.sha256(blob).hexdigest()
        if out.get("error"):
            w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="agent", actor_id="agent:intake",
                         action="document.rejected", policy_decision="DENY:" + out["error"][:60],
                         source_batch_sha256=sha, workflow_pack="cpa-bank-reconciliation", model_route="deterministic")
            raise ingest.IngestError(out["error"])
        did = "PDF-" + uuid.uuid4().hex[:6]
        prefix = DATA_DIR / TENANT / wid
        prefix.mkdir(parents=True, exist_ok=True)
        (prefix / f"{did}.pdf").write_bytes(blob)
        rec = {"id": did, "kind": "pdf", "filename": filename, "sha256": sha, "size": len(blob), **out,
               "state": "EXTRACTED_PENDING_CONFIRMATION", "uploaded_by": user.user_id, "uploaded_at": _ts()}
        w.uploads[did] = rec
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="agent", actor_id="agent:extract",
                     action="document.extracted" + (".unsafe_content_flagged" if out["unsafe_content"] else ""),
                     policy_decision="ALLOW:OK", source_batch_sha256=sha, model_route="deterministic",
                     workflow_pack="cpa-bank-reconciliation")
        return rec

    def confirm_pdf(self, user, wid, doc_id, fields, doc_type):
        w = self.gate(user, wid, "mapping.commit")
        up = w.uploads.get(doc_id)
        if not up or up["kind"] != "pdf":
            raise KeyError(doc_id)
        if up["state"] == "CONFIRMED":
            return up
        ref = (fields.get("reference") or "").strip()
        if not ref:
            raise ValueError("reference is required to link the document")
        amt = ingest.to_cents(fields["amount"]) if (fields.get("amount") or "").strip() else None
        row = {"id": doc_id, "doc_type": doc_type or "vendor_invoice", "reference": ref, "ref_norm": ingest.norm_ref(ref),
               "vendor_alias": (fields.get("vendor") or "")[:80], "amount_cents": amt, "file": up["filename"],
               "source_row": 1, "source_sha256": up["sha256"], "confirmed_by": user.user_id}
        batch = w.batches.setdefault("documents", {"upload_id": None, "sha256": "", "rows": []})
        batch["rows"] = [r for r in batch["rows"] if r["ref_norm"] != row["ref_norm"]] + [row]
        batch["sha256"] = __import__("hashlib").sha256((batch["sha256"] + up["sha256"]).encode()).hexdigest()
        changed = {k: v for k, v in fields.items() if str(v) != str(up["fields"].get(k, {}).get("value", ""))}
        up.update(state="CONFIRMED", confirmed_fields=fields, human_edits=changed, confirmed_by=user.user_id)
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                     action="document.confirmed", policy_decision="ALLOW:OK", source_batch_sha256=up["sha256"],
                     source_line_refs=[ref], workflow_pack="cpa-bank-reconciliation")
        return up

    # ---- email intake: a saved email (.eml) with attachments ------------------
    EMAIL_ATTACH_EXT = (".csv", ".xlsx", ".tsv", ".txt", ".ofx", ".qfx", ".qbo", ".pdf")

    def read_email(self, user, wid, blob):
        """Opens a saved email and hands back its attachments. Each attachment then goes
        through exactly the same checks as a direct upload. The email body is data only:
        instruction-like text is flagged, never acted on."""
        import base64, email, hashlib, re
        from email import policy as _pol
        w = self.gate(user, wid, "upload.create")
        if len(blob) > 25 * 1024 * 1024:
            raise ingest.IngestError("email larger than 25 MB")
        msg = email.message_from_bytes(blob, policy=_pol.default)
        if not (msg.get("From") or msg.get("Subject")):
            raise ingest.IngestError("not a saved email (.eml)")
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        from .pdf_worker import INJECTION
        flags = [ln.strip()[:160] for ln in text.splitlines() if INJECTION.search(ln)]
        atts, skipped = [], []
        for part in msg.iter_attachments():
            name = (part.get_filename() or "attachment").replace("/", "_").replace("\\", "_")
            data = part.get_payload(decode=True) or b""
            if not name.lower().endswith(self.EMAIL_ATTACH_EXT):
                skipped.append({"name": name, "reason": "file type not accepted"}); continue
            if len(data) > ingest.MAX_BYTES:
                skipped.append({"name": name, "reason": "over 10 MB"}); continue
            atts.append({"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                         "data_b64": base64.b64encode(data).decode()})
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                     action="email.opened" + (".unsafe_content_flagged" if flags else ""), policy_decision="ALLOW:OK",
                     source_batch_sha256=hashlib.sha256(blob).hexdigest(),
                     source_line_refs=[a["name"] for a in atts], workflow_pack="cpa-bank-reconciliation")
        return {"from": str(msg.get("From", "")), "subject": str(msg.get("Subject", "")), "date": str(msg.get("Date", "")),
                "attachments": atts, "skipped": skipped, "unsafe_content": flags}

    def load_sample(self, user, wid):
        prefix = wid
        out = []
        for kind in ("bank", "ledger", "ar", "documents", "parties"):
            name = f"{prefix}_{kind}.csv"
            if not (FIXTURES / name).exists():
                continue
            up = self.upload(user, wid, kind, name, (FIXTURES / name).read_bytes())
            out.append(self.commit_mapping(user, wid, up["id"], up["proposed_mapping"]))
        return out

    # ---- runs ----------------------------------------------------------
    def run(self, user, wid):
        w = self.gate(user, wid, "run.create")
        if "bank" not in w.batches or "ledger" not in w.batches:
            raise ValueError("Import a bank statement and a ledger first.")
        key = tuple(w.batches[k]["sha256"] for k in sorted(w.batches))
        for r in w.runs.values():
            if r["batch_key"] == key:
                return r  # idempotent: same inputs, same run
        b = w.batches
        res = recon.reconcile(wid, b["bank"]["rows"], b["ledger"]["rows"],
                              b.get("ar", {}).get("rows"), b.get("documents", {}).get("rows"))
        rid = "RUN-" + uuid.uuid4().hex[:6]
        lines = {r["id"]: r for k in ("bank", "ledger") for r in b[k]["rows"]}
        lines.update({r["id"]: r for r in b.get("ar", {}).get("rows", [])})
        run = {"id": rid, "batch_key": key, "created_by": user.user_id, "created_at": _ts(),
               "engine_version": res.engine_version, "result": res.to_dict(),
               "lines": {k: {kk: (str(vv) if kk == "date" else vv) for kk, vv in v.items()} for k, v in lines.items()},
               "decisions": {}}
        w.runs[rid] = run
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="agent", actor_id="agent:recon",
                     run_id=rid, action="run.compute", policy_decision="ALLOW:OK", model_route="deterministic",
                     matching_rule_id=res.engine_version, workflow_pack="cpa-bank-reconciliation",
                     source_line_refs=[m.bank_id for m in res.matches])
        return run

    def decide(self, user, wid, run_id, exc_id, decision, note):
        if decision not in ("ACCEPT", "REJECT"):
            raise ValueError("decision must be ACCEPT or REJECT")
        w = self.ws.get(wid)
        run = w.runs.get(run_id) if w and wid in user.workspaces else None
        exc = next((e for e in (run or {}).get("result", {}).get("exceptions", []) if e["id"] == exc_id), None)
        maker = (exc or {}).get("proposed_by", "agent:recon")
        w = self.gate(user, wid, "exception.decide", maker_id=maker, run_id=run_id,
                      refs=(exc or {}).get("evidence_refs"), rule=(exc or {}).get("rule_id"))
        if not exc:
            raise KeyError(exc_id)
        if exc["status"] != "OPEN":
            raise ValueError(f"already {exc['status']}")
        aid = "APR-" + uuid.uuid4().hex[:6]
        exc.update(status="ACCEPTED" if decision == "ACCEPT" else "REJECTED", reviewer=user.user_id,
                   reviewed_at=_ts(), review_note=note, approval_id=aid,
                   effect="Local review decision only. Nothing posted to any accounting system or bank.")
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                     run_id=run_id, action=f"exception.{decision.lower()}", policy_decision="ALLOW:OK",
                     approval_id=aid, source_line_refs=exc["evidence_refs"], matching_rule_id=exc["rule_id"],
                     workflow_pack="cpa-bank-reconciliation")
        return exc

    def propose(self, user, wid, run_id, exc_id, note):
        """Preparer proposes a resolution; becomes the maker, so cannot also approve."""
        w = self.gate(user, wid, "exception.propose", run_id=run_id)
        exc = next(e for e in w.runs[run_id]["result"]["exceptions"] if e["id"] == exc_id)
        exc.update(proposed_by=user.user_id, proposal_note=note)
        return exc

    # ---- evidence + export --------------------------------------------
    # ---- counterparties: who is each transaction with? ---------------------
    # Deterministic evidence only. A line is LINKED when two independent signals agree,
    # SUGGESTED on one signal, AMBIGUOUS when signals point at different parties, and
    # UNLINKED with no evidence. Nothing is guessed; a person confirms or corrects.
    def _party_index(self, w):
        parties = {p["id"]: p for p in w.batches.get("parties", {}).get("rows", [])}
        run = list(w.runs.values())[-1] if w.runs else None
        b = w.batches
        lines = []
        for kind in ("bank", "ledger", "ar", "documents"):
            for r in b.get(kind, {}).get("rows", []):
                lines.append((kind, r))
        invoices = {r["ref_norm"]: r for r in b.get("ar", {}).get("rows", [])}
        pair = {}
        if run:
            for m in run["result"]["matches"]:
                pair[m["bank_id"]] = m["ledger_id"]; pair[m["ledger_id"]] = m["bank_id"]

        def direct(kind, r):
            sig = []
            text = " ".join(str(r.get(k) or "") for k in ("description", "customer_alias", "vendor_alias")).upper()
            ref = r.get("ref_norm") or ""
            inv = invoices.get(ref) if kind != "ar" else r
            for p in parties.values():
                if inv:
                    cust = (inv.get("customer_alias") or "").upper()
                    if cust and cust in p["aliases"]:
                        sig.append((p["id"], "invoice", f"invoice {inv['id']} is billed to {inv['customer_alias']} (open invoices file, row {inv['source_row']})"))
                hit = next((a for a in p["aliases"] if len(a) >= 4 and a in text and not (inv and a == (inv.get('customer_alias') or '').upper())), None)
                if hit:
                    sig.append((p["id"], "name", f"text “{hit.title()}” appears in the {kind} line"))
                pre = next((x for x in p["ref_prefixes"] if x and ref.startswith(x)), None)
                if pre:
                    sig.append((p["id"], "reference", f"reference {r.get('reference') or ref} uses {p['name']}'s prefix {pre}"))
            return sig

        sigs = {r["id"]: direct(k, r) for k, r in lines}
        out = []
        for kind, r in lines:
            sg = list(sigs[r["id"]])
            other = pair.get(r["id"])
            if other:
                for pid in {x[0] for x in sigs.get(other, [])}:
                    sg.append((pid, "matched_line", f"its matched partner {other} points to the same party"))
            by = {}
            for pid, typ, why in sg:
                by.setdefault(pid, {"types": set(), "why": []})
                by[pid]["types"].add(typ); by[pid]["why"].append(why)
            ranked = sorted(by.items(), key=lambda kv: -len(kv[1]["types"]))
            if not ranked:
                state, pid = "UNLINKED", None
            elif len(ranked) > 1 and len(ranked[1][1]["types"]) == len(ranked[0][1]["types"]):
                state, pid = "AMBIGUOUS", None
            else:
                pid = ranked[0][0]
                t = ranked[0][1]["types"]
                state = "LINKED" if len(t) >= 2 or "invoice" in t else "SUGGESTED"
            rev = w.party_reviews.get(r["id"])
            if rev:
                if rev["decision"] == "CONFIRM":
                    pid, state = rev["party_id"], "CONFIRMED"
                elif rev["decision"] == "REJECT" and rev["party_id"] == pid:
                    pid, state = None, "REJECTED"
            amt = r.get("amount_cents") if kind != "ar" else r.get("amount_cents")
            out.append({"line_id": r["id"], "kind": kind, "date": str(r.get("date") or ""), "reference": r.get("reference") or r.get("id"),
                        "description": r.get("description") or r.get("customer_alias") or r.get("vendor_alias") or r.get("doc_type") or "",
                        "amount_cents": amt, "open_cents": r.get("open_cents"), "party_id": pid, "state": state,
                        "signals": by.get(pid, {}).get("why", []) if pid else [why for v in by.values() for why in v["why"]],
                        "candidates": [k for k, _ in ranked], "review": rev})
        return parties, out, run

    def parties(self, user, wid):
        w = self.gate(user, wid, "workspace.read")
        parties, links, _ = self._party_index(w)
        res = []
        for p in parties.values():
            mine = [l for l in links if l["party_id"] == p["id"]]
            bank = [l for l in mine if l["kind"] == "bank"]
            res.append({**p, "transactions": len(bank), "records": len(mine),
                        "money_in_cents": sum(l["amount_cents"] for l in bank if l["amount_cents"] > 0),
                        "money_out_cents": -sum(l["amount_cents"] for l in bank if l["amount_cents"] < 0),
                        "needs_check": sum(l["state"] in ("SUGGESTED",) for l in mine),
                        "confirmed": sum(l["state"] == "CONFIRMED" for l in mine)})
        unlinked = [l for l in links if l["state"] in ("UNLINKED", "AMBIGUOUS", "REJECTED") and l["kind"] == "bank"]
        return {"parties": res, "unlinked": unlinked, "register_loaded": bool(parties)}

    def party(self, user, wid, pid):
        w = self.gate(user, wid, "workspace.read", refs=[pid])
        parties, links, run = self._party_index(w)
        if pid not in parties:
            raise KeyError(pid)
        mine = [l for l in links if l["party_id"] == pid]
        ex = []
        if run:
            ids = {l["line_id"] for l in mine}
            ex = [e for e in run["result"]["exceptions"] if ids & set(e["evidence_refs"])]
        inv = [l for l in mine if l["kind"] == "ar"]
        # One row per real-world transaction: the bank line, with its matched ledger entry,
        # the invoice it pays and any supporting document attached underneath.
        pair = {}
        if run:
            for m in run["result"]["matches"]:
                pair[m["bank_id"]] = m["ledger_id"]
        by_id = {l["line_id"]: l for l in mine}
        by_ref = {}
        for l in mine:
            if l["kind"] in ("ar", "documents"):
                by_ref.setdefault((l["kind"], ingest.norm_ref(l["reference"])), l)
        used, txns = set(), []
        for b in [l for l in mine if l["kind"] == "bank"]:
            ref = ingest.norm_ref(b["reference"])
            parts = [x for x in (by_id.get(pair.get(b["line_id"])), by_ref.get(("ar", ref)), by_ref.get(("documents", ref))) if x]
            used.update([b["line_id"], *(x["line_id"] for x in parts)])
            sig = list(dict.fromkeys(b["signals"] + [s for x in parts for s in x["signals"] if "matched partner" not in s]))
            txns.append({**b, "signals": sig, "attached": parts})
        for l in mine:
            if l["line_id"] not in used:
                txns.append({**l, "attached": []})
        return {"party": parties[pid], "links": mine, "transactions": txns, "exceptions": ex,
                "owed_cents": sum((l.get("open_cents") or 0) for l in inv),
                "paid_against_invoices_cents": sum(e.get("amount_cents", 0) for e in ex if e["type"] == "PARTIAL_RECEIPT"),
                "residual_cents": sum((e.get("residual_cents") or 0) for e in ex if e["type"] == "PARTIAL_RECEIPT"),
                "all_parties": [{"id": p["id"], "name": p["name"]} for p in parties.values()]}

    def review_party_link(self, user, wid, line_id, party_id, decision, note):
        if decision not in ("CONFIRM", "REJECT"):
            raise ValueError("decision must be CONFIRM or REJECT")
        w = self.gate(user, wid, "party.confirm", refs=[line_id, party_id])
        parties, links, _ = self._party_index(w)
        if party_id not in parties or not any(l["line_id"] == line_id for l in links):
            raise KeyError(line_id)
        rec = {"party_id": party_id, "decision": decision, "by": user.user_id, "at": _ts(), "note": note}
        w.party_reviews[line_id] = rec
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                     action=f"party.{decision.lower()}", policy_decision="ALLOW:OK", source_line_refs=[line_id, party_id],
                     workflow_pack="cpa-bank-reconciliation", model_route="deterministic")
        return rec

    # ---- end-to-end trace of one transaction --------------------------------
    def trace_index(self, user, wid):
        w = self.gate(user, wid, "workspace.read")
        run = list(w.runs.values())[-1] if w.runs else None
        bank = w.batches.get("bank", {}).get("rows", [])
        out = []
        for b in bank:
            st, label = "IMPORTED", "Imported, not yet matched"
            if run:
                m = next((m for m in run["result"]["matches"] if m["bank_id"] == b["id"]), None)
                ex = [e for e in run["result"]["exceptions"] if e["bank_id"] == b["id"]]
                if ex:
                    st = ex[0]["status"] if ex[0]["status"] != "OPEN" else "OPEN"
                    label = ex[0]["type"].replace("_", " ").lower()
                elif m:
                    st, label = "MATCHED", f"matched to {m['ledger_id']}"
            out.append({"id": b["id"], "date": str(b["date"]), "description": b["description"],
                        "amount_cents": b["amount_cents"], "status": st, "label": label})
        return {"run_id": run["id"] if run else None, "lines": out}

    def trace(self, user, wid, line_id):
        w = self.gate(user, wid, "workspace.read", refs=[line_id])
        line, kind = None, None
        for k in ("bank", "ledger", "ar"):
            for r in w.batches.get(k, {}).get("rows", []):
                if r["id"] == line_id:
                    line, kind = r, k
        if not line:
            raise KeyError(line_id)
        up = next((u for u in w.uploads.values() if u.get("sha256") == line["source_sha256"]), {})
        run = list(w.runs.values())[-1] if w.runs else None
        steps = [{"stage": "Source", "title": f"Row {line['source_row']} of {up.get('filename', 'source file')}",
                  "detail": {k: str(v) for k, v in line.items() if k not in ("source_sha256", "ref_norm")},
                  "who": up.get("uploaded_by"), "when": up.get("uploaded_at"),
                  "proof": f"file SHA-256 {line['source_sha256']}"},
                 {"stage": "Checked & imported", "title": "Passed file checks; columns confirmed by a person",
                  "detail": {"mapping": up.get("committed_mapping"), "mapping_method": up.get("mapping_method"),
                             "rows_received": (up.get("health") or {}).get("received"),
                             "rows_accepted": (up.get("health") or {}).get("accepted")},
                  "who": up.get("committed_by"), "when": up.get("uploaded_at"), "proof": "control totals balance"}]
        related, decision = [], None
        if run:
            res = run["result"]
            m = next((m for m in res["matches"] if line_id in (m["bank_id"], m["ledger_id"])), None)
            exs = [e for e in res["exceptions"] if line_id in e["evidence_refs"] or line_id in (e["bank_id"], e["ledger_id"])]
            if m:
                other = m["ledger_id"] if m["bank_id"] == line_id else m["bank_id"]
                related.append(run["lines"].get(other))
                steps.append({"stage": "Matched", "title": f"Paired with {other} by rule {m['rule_id']}",
                              "detail": {"why": m["why"], "engine": res["engine_version"]}, "who": "agent:recon (deterministic)",
                              "when": run["created_at"], "proof": f"run {run['id']}"})
            for e in exs:
                for ref in e["evidence_refs"]:
                    if ref != line_id and run["lines"].get(ref):
                        related.append(run["lines"][ref])
                steps.append({"stage": "Flagged for review", "title": f"{e['id']} · {e['type'].replace('_', ' ').lower()}",
                              "detail": {"why": e["why"], "suggestion": e.get("suggestion"), "rule": e["rule_id"],
                                         "still_owed": e.get("residual_cents")},
                              "who": "agent:recon (deterministic)", "when": run["created_at"], "proof": f"run {run['id']}"})
                if e["status"] != "OPEN":
                    decision = e
                    steps.append({"stage": "Decided", "title": f"{e['status'].title()} by {e['reviewer']}",
                                  "detail": {"note": e.get("review_note"), "effect": e.get("effect")},
                                  "who": e["reviewer"], "when": e.get("reviewed_at"), "proof": f"approval {e['approval_id']}"})
                elif e.get("proposed_by"):
                    steps.append({"stage": "Proposed", "title": f"Resolution proposed by {e['proposed_by']}",
                                  "detail": {"note": e.get("proposal_note")}, "who": e["proposed_by"], "when": None, "proof": "awaiting reviewer"})
            if not m and not exs:
                steps.append({"stage": "Not in run", "title": "Not part of the latest run", "detail": {}, "who": None, "when": None, "proof": ""})
        docs = [d for d in w.batches.get("documents", {}).get("rows", []) if d["ref_norm"] and d["ref_norm"] == line.get("ref_norm")]
        for d in docs:
            steps.append({"stage": "Supporting document", "title": f"{d['doc_type']} {d['reference']} ({d['file']})",
                          "detail": {"amount": d.get("amount_cents"), "vendor": d.get("vendor_alias")},
                          "who": d.get("confirmed_by") or "documents index", "when": None, "proof": f"doc {d['id']}"})
        ids = {line_id, *(r["id"] for r in related if r)}
        evs = [dict(e, seq=i) for i, e in enumerate(w.log.events)
               if ids & set(e.get("source_line_refs") or [])
               or (e.get("source_batch_sha256") == line["source_sha256"])
               or (run and e.get("run_id") == run["id"] and e["action"] == "run.compute")]
        return {"line": {k: str(v) for k, v in line.items()}, "kind": kind, "related": [r for r in related if r],
                "steps": steps, "events": evs, "decision": decision,
                "chain": evidence.verify(w.log.events, self.public_key_pem())}

    def evidence(self, user, wid):
        w = self.gate(user, wid, "export.read")
        return {"events": w.log.events, "verification": evidence.verify(w.log.events, self.public_key_pem()),
                "key_id": w.log.key_id, "key_source": self.key_source}

    def tamper_check(self, user, wid):
        """Demonstrates detection on a COPY of the chain; the real chain is never altered."""
        w = self.gate(user, wid, "export.read")
        import copy
        events = copy.deepcopy(w.log.events)
        if len(events) < 2:
            raise ValueError("not enough events yet")
        i = len(events) // 2
        events[i]["action"] = "ledger.post"
        return {"tampered_index": i, "verification": evidence.verify(events, self.public_key_pem()),
                "original_chain": evidence.verify(w.log.events, self.public_key_pem())}

    def export_csv(self, user, wid, run_id) -> str:
        w = self.gate(user, wid, "export.read", run_id=run_id)
        run = w.runs[run_id]
        buf = io.StringIO()
        wr = csv.writer(buf)
        wr.writerow(["client", "run_id", "record", "type", "bank_line", "ledger_line", "amount_usd", "rule",
                     "reason", "status", "reviewer", "approval_id", "review_note", "source_sha256_bank"])
        bank_sha = w.batches["bank"]["sha256"]
        for m in run["result"]["matches"]:
            wr.writerow([w.meta["name"], run_id, "MATCH", "MATCHED", m["bank_id"], m["ledger_id"],
                         f"{m['amount_cents']/100:.2f}", m["rule_id"], m["why"], "PROPOSED_MATCH", "", "", "", bank_sha])
        for e in run["result"]["exceptions"]:
            wr.writerow([w.meta["name"], run_id, e["id"], e["type"], e["bank_id"] or "", e["ledger_id"] or "",
                         f"{e['amount_cents']/100:.2f}", e["rule_id"], e["why"], e["status"], e.get("reviewer", ""),
                         e.get("approval_id", ""), e.get("review_note", ""), bank_sha])
        return buf.getvalue()

    def documents(self, w):
        return w.batches.get("documents", {}).get("rows", [])

    def review_pack(self, user, wid, run_id) -> dict:
        w = self.gate(user, wid, "export.read", run_id=run_id)
        run = w.runs[run_id]
        return {"client": w.meta, "workspace_id": wid, "tenant_id": TENANT, "run": run["result"] | {"id": run_id},
                "inputs": {k: {"sha256": v["sha256"], "rows": len(v["rows"])} for k, v in w.batches.items()},
                "evidence": {"events": w.log.events, "public_key_pem": self.public_key_pem().decode(),
                             "verification": evidence.verify(w.log.events, self.public_key_pem()),
                             "verify_offline": "python -m ledgersight.verify_pack <this file>"},
                "boundary": "Read-only review output. No postings, payments or client emails.",
                "generated_by": user.user_id, "generated_at": _ts()}

    # ---- reset ---------------------------------------------------------
    def clear_source(self, user, wid, kind):
        """Remove one imported source (e.g. a wrong bank file). Runs built on it are withdrawn too,
        because their results no longer match the inputs. The removal itself is recorded."""
        w = self.gate(user, wid, "upload.create", refs=[kind])
        if kind not in ingest.CANONICAL and kind != "pdf":
            raise ValueError("unknown source type")
        removed = [uid for uid, u in w.uploads.items() if u["kind"] == kind or (kind == "documents" and u["kind"] == "pdf")]
        old_sha = w.batches.get(kind, {}).get("sha256")
        w.batches.pop(kind, None)
        for uid in removed:
            w.uploads.pop(uid, None); w.blobs.pop(uid, None)
            for ext in ("csv", "xlsx", "pdf"):
                (DATA_DIR / TENANT / wid / f"{uid}.{ext}").unlink(missing_ok=True)
        for k in [k for k in w.digests if k[0] == kind]:
            del w.digests[k]
        if kind == "parties":
            w.party_reviews.clear()
        withdrawn = list(w.runs)
        w.runs.clear()
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                     action=f"source.cleared:{kind}", policy_decision="ALLOW:OK", source_batch_sha256=old_sha,
                     source_line_refs=[f"uploads_removed:{len(removed)}", f"runs_withdrawn:{len(withdrawn)}"],
                     workflow_pack="cpa-bank-reconciliation")
        return {"cleared": kind, "uploads_removed": len(removed), "runs_withdrawn": len(withdrawn)}

    def reset(self, user, wid):
        self.gate(user, wid, "workspace.reset")
        old = self.ws[wid]
        shutil.rmtree(DATA_DIR / TENANT / wid, ignore_errors=True)
        new = Workspace(wid, self._key)
        new.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                       action="workspace.reset", policy_decision="ALLOW:OK",
                       workflow_pack="cpa-bank-reconciliation",
                       source_line_refs=[f"prior_chain_head:{old.log.events[-1]['event_hash'] if old.log.events else 'none'}",
                                         f"prior_event_count:{len(old.log.events)}"])
        self.ws[wid] = new
        return {"reset": wid, "files_deleted": True}

    # ---- trust view ----------------------------------------------------
    def trust(self):
        return {
            "tenant": TENANT, "workspaces": list(WORKSPACES),
            "region_declared": self.region,
            "region_note": "Declared by deployment configuration. Not an independently verified data-residency attestation.",
            "data_classes_allowed": sorted(policy.ALLOWED_DATA_CLASSES),
            "hard_boundary": "No tax returns, SSNs, preparer IDs, banking credentials, unmasked account numbers or real client records.",
            "model_routes_allowed": sorted(r for r in policy.ALLOWED_MODEL_ROUTES if r),
            "model_routes_in_use": ["deterministic"],
            "ai_note": "No language model is called anywhere. Mapping and matching are rule-based.",
            "forbidden_actions": sorted(policy.FORBIDDEN),
            "roles": {k: sorted(v) for k, v in policy.ROLE_ACTIONS.items()},
            "agents": [
                {"id": "agent:intake", "does": "File checks, quarantine, checksums, control totals", "model": "none"},
                {"id": "agent:mapping", "does": "Proposes column mapping; human confirms", "model": "none (rules)"},
                {"id": "agent:recon", "does": "Deterministic matching and typed exceptions", "model": "none"},
                {"id": "agent:explainer", "does": "Reason text from verified source refs", "model": "none (templates)"},
            ],
            "evidence": {"algorithm": "Ed25519 over SHA-256 hash chain", "key_source": self.key_source,
                         "public_key_pem": self.public_key_pem().decode()},
            "retention": "Client files deleted on workspace reset or at account expiry (14 days).",
            "policy_version": policy.POLICY_VERSION,
            "security_events": len(self.security_log.events),
        }
