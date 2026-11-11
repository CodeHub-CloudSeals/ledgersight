"""Workspace-scoped CPA reconciliation service.

Tenant = Avocet. Workspace = one Avocet client. Every method takes the
authenticated user and a workspace id, and every action goes through the policy
gate before it happens; the decision (ALLOW or DENY) is written to that
workspace's signed evidence chain. Nothing here can post to a ledger, move money
or contact a client: no such code path exists.
"""
from __future__ import annotations

import copy
import csv
import io
import os
import hashlib
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from .auth import User
from .config import load as load_config
from .core import evidence, ingest, policy, query, recon, reports
from .core.store import Store

SOURCES = load_config("sources.json")
SOURCE_KINDS = [k["kind"] for k in SOURCES["kinds"]]

TENANT = os.environ.get("LEDGERSIGHT_TENANT", "firm-01")  # internal id; never shown as a customer name
# Client books come from configuration (generic demo names), never from code.
WORKSPACES = {c["id"]: {k: v for k, v in c.items() if k != "id"} for c in load_config("clients.json")["clients"]}
# Which client books this deployment shows. The local demo runs one client (AVOCET_CLIENTS=client-a).
_only = [c.strip() for c in os.environ.get("AVOCET_CLIENTS", "").split(",") if c.strip() in WORKSPACES]
if _only:
    WORKSPACES = {k: WORKSPACES[k] for k in _only}
DATA_DIR = Path(os.environ.get("AVOCET_DATA_DIR", "/tmp/avocet-data"))
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class Denied(Exception):
    def __init__(self, code: str, conceal: bool = False, policy_id: str = ""):
        super().__init__(code)
        self.code, self.conceal, self.policy_id = code, conceal, policy_id


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
        self.tables: dict[str, tuple] = {}  # "sha:<digest>" -> (header, rows) read from a PDF statement
        self.party_reviews: dict[str, dict] = {}  # line_id -> human confirmation of who the counterparty is
        self.runs: dict[str, dict] = {}
        self.held: dict[str, dict] = {}  # files of any type, stored unopened until JSON conversion exists
        self.log = evidence.EvidenceLog(key, "ledgersight-ed25519-1")
        self.started_at = _ts()
        self.saved_events = 0  # how many evidence events are already in durable storage

    # ---- durable state (everything except file bytes, which live on disk) ----
    def to_state(self) -> dict:
        return {"meta": self.meta, "uploads": self.uploads, "batches": self.batches,
                "digests": [[k, s, uid] for (k, s), uid in self.digests.items()],
                "tables": {k: [list(h), [list(r) for r in rows]] for k, (h, rows) in self.tables.items()},
                "party_reviews": self.party_reviews, "runs": self.runs, "held": self.held, "started_at": self.started_at}

    @classmethod
    def from_state(cls, wid: str, key, state: dict, events: list[dict], files_dir: Path) -> "Workspace":
        from datetime import date
        w = cls(wid, key)
        w.meta.update(state.get("meta") or {})
        w.uploads = state.get("uploads", {})
        w.batches = state.get("batches", {})
        for k in ("bank", "ledger"):  # dates come back as text; the matching engine needs real dates
            for r in w.batches.get(k, {}).get("rows", []):
                if isinstance(r.get("date"), str):
                    r["date"] = date.fromisoformat(r["date"][:10])
        w.digests = {(k, s): uid for k, s, uid in state.get("digests", [])}
        w.tables = {k: (h, rows) for k, (h, rows) in state.get("tables", {}).items()}
        w.party_reviews = state.get("party_reviews", {})
        w.runs = state.get("runs", {})
        w.held = state.get("held", {})
        for r in w.runs.values():
            r["batch_key"] = tuple(r.get("batch_key") or ())
        w.started_at = state.get("started_at", w.started_at)
        for uid in w.uploads:
            f = next(iter(files_dir.glob(f"{uid}.*")), None) if files_dir.exists() else None
            if f:
                w.blobs[uid] = f.read_bytes()
        w.log.events = events
        w.saved_events = len(events)
        return w


PERSIST = os.environ.get("AVOCET_PERSIST", "1") != "0"


class Service:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.store = Store(DATA_DIR / "ledgersight.db") if PERSIST else None
        self._key, self.key_source = self._load_key()
        self.ws: dict[str, Workspace] = {}
        for w in WORKSPACES:
            state = self.store.load_workspace(w) if self.store else None
            self.ws[w] = (Workspace.from_state(w, self._key, state, self.store.load_events(w), DATA_DIR / TENANT / w)
                          if state else Workspace(w, self._key))
        self.security_log = evidence.EvidenceLog(self._key, "ledgersight-ed25519-1")
        if self.store:
            self.security_log.events = self.store.load_events("_security")
        self._security_saved = len(self.security_log.events)
        self.region = os.environ.get("AVOCET_REGION_DECLARED", "local-development")

    def _load_key(self):
        """Signing key: secret store (env) > key file kept with the data > ephemeral (no storage)."""
        pem = os.environ.get("AVOCET_SIGNING_KEY_PEM")
        if pem:
            return serialization.load_pem_private_key(pem.encode(), None), "secret store"
        if not self.store:
            return Ed25519PrivateKey.generate(), "ephemeral (generated at start-up)"
        path = DATA_DIR / "signing-key.pem"
        if path.exists():
            return serialization.load_pem_private_key(path.read_bytes(), None), "key file in the data directory"
        key = Ed25519PrivateKey.generate()
        if self.store and any(self.store.load_events(w) for w in WORKSPACES):
            # History exists but its key is gone: a new key cannot verify it. Keep running so the data can be
            # read and exported, but /readyz reports not ready (audit_chain_matches_key = false).
            print("[ledgersight] WARNING: signing key missing but audit history exists; the existing chain will "
                  "not verify. Restore signing-key.pem or AVOCET_SIGNING_KEY_PEM.", flush=True)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # created private, never world-readable
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        return key, "key file in the data directory"

    def save(self, wid: str | None = None) -> None:
        """Write the book's state and any new evidence events to durable storage."""
        if not self.store:
            return
        for w in ([self.ws[wid]] if wid in self.ws else []):
            for _ in range(3):  # another request may change the state mid-copy; retry
                try:
                    state = copy.deepcopy(w.to_state())
                    break
                except RuntimeError:
                    continue
            else:
                state = None
            if state is not None:
                self.store.save_workspace(w.id, state)
            new = w.log.events[w.saved_events:]
            if new:
                self.store.append_events(w.id, w.saved_events, list(new))
                w.saved_events += len(new)
        new = self.security_log.events[self._security_saved:]
        if new:
            self.store.append_events("_security", self._security_saved, list(new))
            self._security_saved += len(new)

    def _chains_verify(self) -> bool:
        """Every stored chain must verify with the key in use. A lost or swapped key makes the history
        unverifiable; that must stop the service being 'ready' instead of passing silently."""
        if getattr(self, "_chain_ok", None) is None:
            pub = self.public_key_pem()
            self._chain_ok = all(evidence.verify(w.log.events, pub)["state"] != "FAIL" for w in self.ws.values() if w.log.events)
        return self._chain_ok

    def ready(self) -> dict:
        checks = {"signer": self._key is not None, "storage": (self.store.healthy() if self.store else None),
                  "config": bool(policy.POLICIES) and bool(WORKSPACES), "audit_chain_matches_key": self._chains_verify()}
        return {"ready": all(v is not False for v in checks.values()), "checks": checks,
                "persistence": "sqlite" if self.store else "memory only"}

    def public_key_pem(self) -> bytes:
        return self._key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)

    # ---- gate ----------------------------------------------------------
    def gate(self, user: User, wid: str, action: str, **kw) -> Workspace:
        return self._gate(user, wid, action, **kw)[0]

    def _gate(self, user: User, wid: str, action: str, *, data_class="synthetic", model_route="deterministic",
              maker_id=None, refs=None, run_id=None, rule=None, approval_id=None, batch_sha=None, trace_id=None,
              record=True, **extra) -> tuple[Workspace, policy.Decision]:
        """A person asks TrustOps for an action. Every answer is signed into the evidence chain.
        record=False is only for high-frequency read-only views (the live agent monitor): the policy is
        still evaluated and any DENY is still recorded; only routine ALLOWed reads are not logged."""
        mem = {user.user_id: user.workspaces}
        req = {"actor_id": user.user_id, "role": user.role, "workspace_id": wid, "tool_action": action,
               "data_class": data_class, "model_route": model_route, "maker_id": maker_id, **extra}
        d = policy.evaluate(req, mem)
        target = self.ws.get(wid) if wid in user.workspaces else None
        log = target.log if target else self.security_log
        if record or d.decision != "ALLOW":
            log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                       workflow_pack="cpa-bank-reconciliation", run_id=run_id, source_batch_sha256=batch_sha,
                       source_line_refs=refs or [], canonical_version="canon-1", matching_rule_id=rule,
                       model_route=model_route, action=action, policy_decision=d.label(), policy_id=d.policy_id,
                       policy_version=d.policy_version, required_role=d.required_role, approval_id=approval_id,
                       trace_id=trace_id)
        if d.decision != "ALLOW":
            raise Denied(d.reason_code, conceal=(d.reason_code == "WORKSPACE_SCOPE"), policy_id=d.policy_id)
        if wid not in self.ws:
            raise Denied("WORKSPACE_SCOPE", conceal=True, policy_id="POL-01")
        return self.ws[wid], d

    def agent_gate(self, user: User, w: Workspace, agent_actor: str, action: str, *, run_id=None, trace_id=None,
                   refs=None, batch_sha=None, rule=None, data_class="synthetic", model_route="deterministic",
                   **extra) -> policy.Decision:
        """An agent asks TrustOps for an action. Agents act only inside the delegating person's
        client scope and only with their registered tools. The decision is signed; nothing is raised,
        so the caller decides how to stop (fail closed) on DENY."""
        agent = policy.AGENT_BY_ACTOR.get(agent_actor, {})
        mem = {agent_actor: {w.id} & user.workspaces}
        req = {"actor_id": agent_actor, "role": agent_actor, "workspace_id": w.id, "tool_action": action,
               "data_class": data_class, "model_route": model_route, **extra}
        d = policy.evaluate(req, mem)
        w.log.append(tenant_id=TENANT, workspace_id=w.id, actor_type="agent", actor_id=agent_actor,
                     agent_code=agent.get("code"), delegated_by=user.user_id, workflow_pack="cpa-bank-reconciliation",
                     run_id=run_id, trace_id=trace_id, source_batch_sha256=batch_sha, source_line_refs=refs or [],
                     canonical_version="canon-1", matching_rule_id=rule, model_route=model_route, action=action,
                     policy_decision=d.label(), policy_id=d.policy_id, policy_version=d.policy_version,
                     required_role=d.required_role)
        return d

    def _agent_or_stop(self, user, w, agent_actor, action, **kw) -> policy.Decision:
        d = self.agent_gate(user, w, agent_actor, action, **kw)
        if d.decision == "DENY":
            raise Denied(d.reason_code, policy_id=d.policy_id)
        return d

    PROFILE_LIMITS = {"name": 80, "period": 40, "industry": 60}

    def update_profile(self, user, wid, fields: dict):
        """Rename a client book / set its period. Names are entered by the firm, never shipped in code."""
        w = self.gate(user, wid, "workspace.configure", refs=sorted(k for k in fields if k in self.PROFILE_LIMITS))
        clean = {}
        for k, limit in self.PROFILE_LIMITS.items():
            if k in fields:
                v = " ".join(str(fields[k] or "").split())
                if not v or len(v) > limit:
                    raise ValueError(f"{k} must be 1–{limit} characters")
                clean[k] = v
        if not clean:
            raise ValueError("nothing to change")
        w.meta.update(clean)
        WORKSPACES[wid] = w.meta
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                     action="workspace.profile_updated", policy_decision="ALLOW:OK", policy_id="POL-02",
                     source_line_refs=sorted(clean), workflow_pack="cpa-bank-reconciliation")
        return {"id": wid, **w.meta}

    def workspaces_for(self, user: User):
        return [{"id": w, **WORKSPACES[w], "status": self.status(w)} for w in WORKSPACES if w in user.workspaces]

    def status(self, wid):
        w = self.ws[wid]
        last = list(w.runs.values())[-1] if w.runs else None
        exc = last["result"]["exceptions"] if last else []
        return {**{k: k in w.batches for k in SOURCE_KINDS},
                "runs": len(w.runs), "uploads": len(w.uploads),
                "last_run": last["id"] if last else None,
                "matched": len(last["result"]["matches"]) if last else None,
                "exceptions": len(exc), "open_exceptions": sum(e["status"] in ("OPEN", "RETURNED") for e in exc),
                "variance_cents": last["result"]["variance_cents"] if last else None,
                "unexplained_cents": last["result"]["unexplained_variance_cents"] if last else None,
                "last_activity": w.log.events[-1]["timestamp_utc"] if w.log.events else w.started_at}

    # ---- uploads -------------------------------------------------------
    def upload(self, user, wid, kind, filename, blob, data_class="synthetic", origin="upload", hint=None):
        """kind="auto": the file's type is worked out from its content (ingest.detect_kind); a file that is
        not a recognised finance record is refused with the reason. A PDF that is not a statement table
        goes to receipt/invoice intake instead (the result then carries routed="document")."""
        auto = kind == "auto"
        if not auto and kind not in ingest.CANONICAL:
            raise ValueError("unknown source type")
        w = self.gate(user, wid, "upload.create", data_class=data_class)
        is_pdf = filename.lower().endswith(".pdf")
        if is_pdf and not auto and kind not in self.PDF_TABLE_KINDS:
            raise ingest.IngestError("PDFs go through document intake: choose 'Invoice / receipt PDF'.")
        if len(blob) > ingest.MAX_BYTES:
            raise ingest.IngestError("file exceeds 10 MB limit")
        sha = hashlib.sha256(blob).hexdigest()
        self._agent_or_stop(user, w, "agent:intake", "upload.parse", batch_sha=sha, data_class=data_class, refs=[kind])
        unsafe = []
        if is_pdf:  # a statement printed as PDF: read its table in the isolated parser
            out = self._pdf_worker(blob, "statement")
            if out.get("error"):
                if auto:  # not a statement table: treat it as a receipt / invoice (that path refuses bad PDFs)
                    return {"routed": "document", **self.upload_pdf(user, wid, filename, blob, data_class)}
                raise ingest.IngestError(out["error"])
            header, rows, unsafe = out["header"], out["rows"], out.get("unsafe_content", [])
            if len(rows) > ingest.MAX_ROWS:
                raise ingest.IngestError(f"more than {ingest.MAX_ROWS} rows")
        else:
            header, rows = ingest.read_table(filename, blob)  # quarantine checks happen here
        detection = None
        if auto:
            kind, detection = ingest.detect_kind(header, rows, hint)  # raises IngestError if nothing fits
            if is_pdf and kind not in self.PDF_TABLE_KINDS:
                raise ingest.IngestError(f"A PDF table was found but it reads as a {ingest.KIND_LABEL[kind]}; "
                                         "upload that list as CSV or Excel instead.")
        if is_pdf:
            w.tables[f"sha:{sha}"] = (header, rows)
        uid = "UP-" + uuid.uuid4().hex[:8]
        prefix = DATA_DIR / TENANT / wid
        prefix.mkdir(parents=True, exist_ok=True)
        (prefix / f"{uid}.{filename.rsplit('.', 1)[-1].lower()}").write_bytes(blob)
        mapping = ingest.propose_mapping(kind, header)
        rec = {"id": uid, "kind": kind, "filename": filename, "sha256": sha, "size": len(blob),
               "header": header, "preview": [[str(c) if c is not None else "" for c in r] for r in rows[:6]],
               "rows": len(rows), "proposed_mapping": mapping, "state": "MAPPED_PENDING_CONFIRMATION",
               "uploaded_by": user.user_id, "uploaded_at": _ts(),
               "duplicate_of": w.digests.get((kind, sha)), "origin": origin, "unsafe_content": unsafe,
               "mapping_method": ("PDF table read by column position, then " if is_pdf else "") +
                                 "deterministic header rules (no AI model invoked)",
               "detected": bool(detection), "detection": detection, "kind_label": ingest.KIND_LABEL[kind]}
        w.uploads[uid] = rec
        w.blobs[uid] = blob
        self._agent_or_stop(user, w, "agent:mapping", "mapping.propose", batch_sha=sha, data_class=data_class, refs=[kind])
        return rec

    HELD_NOTE = "Stored safely. Not opened, parsed or run. Conversion to JSON is in development."

    def hold_file(self, user, wid, filename, blob, data_class="synthetic"):
        """Accept a document of any type and keep it, unopened, until JSON conversion is built.

        The bytes are written to disk under a neutral name and are never parsed, rendered or executed,
        so a macro workbook, archive or Office file cannot do anything. A held file is not used by
        reconciliation. The request is checked by TrustOps and signed into the evidence chain."""
        if not blob:
            raise ingest.IngestError("the file is empty")
        if len(blob) > ingest.MAX_BYTES:
            raise ingest.IngestError("file exceeds 10 MB limit")
        name = Path(str(filename or "upload").replace("\\", "/")).name[:200] or "upload"
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        sha = hashlib.sha256(blob).hexdigest()
        w = self.gate(user, wid, "upload.create", data_class=data_class, batch_sha=sha, refs=[f"held:{ext or 'no-extension'}"])
        hid = "HF-" + uuid.uuid4().hex[:8]
        prefix = DATA_DIR / TENANT / wid
        prefix.mkdir(parents=True, exist_ok=True)
        (prefix / f"{hid}.held").write_bytes(blob)
        rec = {"id": hid, "filename": name, "extension": ext, "size": len(blob), "sha256": sha,
               "state": "HELD_FOR_CONVERSION", "note": self.HELD_NOTE, "uploaded_by": user.user_id, "uploaded_at": _ts(),
               "duplicate_of": next((h["id"] for h in w.held.values() if h["sha256"] == sha), None)}
        w.held[hid] = rec
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
        res = ingest.import_table(up["kind"], up["filename"], w.blobs[upload_id], mapping,
                                  table=w.tables.get(f"sha:{up['sha256']}"))
        up.update(state="VALIDATED", committed_mapping=mapping, committed_by=user.user_id)
        up["health"] = {"received": res.rows_received, "accepted": len(res.accepted), "rejected": res.rejected,
                        "debit_cents": res.debit_cents, "credit_cents": res.credit_cents, "net_cents": res.net_cents,
                        "control_total_ok": res.rows_received == len(res.accepted) + len(res.rejected)}
        w.batches[up["kind"]] = {"upload_id": upload_id, "sha256": up["sha256"], "rows": res.accepted,
                                 "origin": up.get("origin", "upload"), "filename": up["filename"]}
        w.digests[(up["kind"], up["sha256"])] = upload_id
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                     action="mapping.confirmed", policy_decision="ALLOW:OK", source_batch_sha256=up["sha256"],
                     workflow_pack="cpa-bank-reconciliation", canonical_version="canon-1")
        return up

    # ---- text-PDF document intake ----------------------------------------
    PDF_TABLE_KINDS = ("bank", "ledger")  # statements / ledger listings may arrive as PDF

    @staticmethod
    def _pdf_worker(blob: bytes, mode: str | None = None) -> dict:
        """Parse a PDF in a separate, resource-limited process with a minimal environment."""
        import json, subprocess, sys
        try:
            proc = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("pdf_worker.py")), *([mode] if mode else [])],
                                  input=blob, capture_output=True, timeout=15, env={"PATH": os.environ.get("PATH", "")})
            return json.loads(proc.stdout or b"{}") if proc.returncode == 0 else {"error": "parser stopped (resource limit)"}
        except subprocess.TimeoutExpired:
            return {"error": "parser timed out (15 s limit)"}
        except ValueError:
            return {"error": "parser returned no result"}
    def upload_pdf(self, user, wid, filename, blob, data_class="synthetic"):
        w = self.gate(user, wid, "upload.create", data_class=data_class)
        if not filename.lower().endswith(".pdf"):
            raise ingest.IngestError("PDF files only for document intake")
        if len(blob) > ingest.MAX_BYTES:
            raise ingest.IngestError("file exceeds 10 MB limit")
        self._agent_or_stop(user, w, "agent:intake", "document.extract", batch_sha=hashlib.sha256(blob).hexdigest(),
                            data_class=data_class, refs=["pdf"])
        out = self._pdf_worker(blob)
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
        dup = next((u["id"] for u in w.uploads.values() if u.get("kind") == "pdf" and u.get("sha256") == sha), None)
        rec = {"id": did, "kind": "pdf", "filename": filename, "sha256": sha, "size": len(blob), **out,
               "state": "EXTRACTED_PENDING_CONFIRMATION", "uploaded_by": user.user_id, "uploaded_at": _ts(),
               "duplicate_of": dup}
        w.uploads[did] = rec
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="agent", actor_id="agent:intake", agent_code="AG-01",
                     delegated_by=user.user_id,
                     action="document.extracted" + (".unsafe_content_flagged" if out["unsafe_content"] else ""),
                     policy_decision="ALLOW:OK", policy_id="POL-02", source_batch_sha256=sha, model_route="deterministic",
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
        batch = w.batches.setdefault("documents", {"upload_id": None, "sha256": "", "rows": [], "origin": "upload"})
        batch["rows"] = [r for r in batch["rows"] if r["ref_norm"] != row["ref_norm"]] + [row]
        batch["sha256"] = hashlib.sha256((batch["sha256"] + up["sha256"]).encode()).hexdigest()
        batch["pdf_added"] = True
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
        import base64, email
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
        """Synthetic sample files, imported through the normal upload path but labelled SIMULATED."""
        out = []
        for kind in SOURCE_KINDS:
            name = f"{wid}_{kind}.csv"
            if not (FIXTURES / name).exists():
                continue
            up = self.upload(user, wid, kind, name, (FIXTURES / name).read_bytes(), origin="sample")
            out.append(self.commit_mapping(user, wid, up["id"], up["proposed_mapping"]))
        return out

    def sample_files(self, user, wid):
        self.gate(user, wid, "workspace.read")
        return sorted(p.name for p in FIXTURES.glob(f"{wid}_*") if p.suffix != ".json")

    # ---- sources: what is connected, and how honestly can we label it ----------
    def sources(self, user, wid):
        w = self.gate(user, wid, "workspace.read")
        kinds = []
        now = datetime.now(timezone.utc)
        for k in SOURCES["kinds"]:
            b = w.batches.get(k["kind"])
            status = "NOT_CONNECTED" if not b else ("SIMULATED" if b.get("origin") == "sample" and not b.get("pdf_added") else "VERIFIED_UPLOAD")
            rec = {**k, "status": status, "rows": len(b["rows"]) if b else 0, "sha256": b["sha256"] if b else None,
                   "filename": b.get("filename") if b else None}
            if b:  # source envelope: where these rows came from, in this client's context
                up = w.uploads.get(b.get("upload_id") or "") or {}
                dates = sorted(str(r.get("date") or r.get("issue_date") or "")[:10] for r in b["rows"]
                               if r.get("date") or r.get("issue_date"))
                at = up.get("uploaded_at")
                rec["envelope"] = {
                    "tenant_id": TENANT, "client_id": wid, "client_name": w.meta.get("name"),
                    "source_file": b.get("filename"), "document_type": k["label"], "source_digest": b["sha256"],
                    "upload_time": at, "uploaded_by": up.get("uploaded_by"), "confirmed_by": up.get("committed_by"),
                    "currency": sorted({r.get("currency") for r in b["rows"] if r.get("currency")}),
                    "period": w.meta.get("period"), "date_range": [dates[0], dates[-1]] if dates else None,
                    "row_count": len(b["rows"]), "access": "READ_ONLY", "origin": b.get("origin", "upload"),
                    "freshness_seconds": round((now - datetime.fromisoformat(at)).total_seconds()) if at else None}
            kinds.append(rec)
        conns = []
        for c in SOURCES["connectors"]:
            st = c["status"]
            if c.get("env") and all(os.environ.get(e) for e in c["env"]):
                st = c.get("status_when_configured", st)
            conns.append({"id": c["id"], "name": c["name"], "status": st})
        return {"kinds": kinds, "connectors": conns, "status_labels": SOURCES["status_labels"],
                "not_accepted": SOURCES["not_accepted"], "accepted_extensions": sorted(ingest.ALLOWED_EXT | {".pdf", ".eml"})}

    # ---- runs ----------------------------------------------------------
    def run(self, user, wid):
        w = self.gate(user, wid, "run.create")
        if "bank" not in w.batches or "ledger" not in w.batches:
            labels = {k["kind"]: k["label"] for k in SOURCES["kinds"]}
            missing = [labels[k] for k in ("bank", "ledger") if k not in w.batches]
            raise ValueError(f"Missing: {' and '.join(missing)}. Upload and confirm "
                             f"{'it' if len(missing) == 1 else 'them'} on the Sources page, then run again.")
        key = tuple(w.batches[k]["sha256"] for k in sorted(w.batches))
        for r in w.runs.values():
            if r["batch_key"] == key:
                return r  # idempotent: same inputs, same run
        b = w.batches
        rid = "RUN-" + uuid.uuid4().hex[:6]
        trace = "TR-" + uuid.uuid4().hex[:12]
        steps: list[dict] = []
        ctx = {}
        # rows that failed import (bad date, unreadable amount...) are not in the totals: carry them into the run
        rejected = [(k, w.uploads[b[k]["upload_id"]]) for k in ("bank", "ledger")
                    if b[k].get("upload_id") in w.uploads and (w.uploads[b[k]["upload_id"]].get("health") or {}).get("rejected")]
        n_rejected = sum(len(u["health"]["rejected"]) for _, u in rejected)
        # where every row came from: "<file> row <n>", so each match/exception cites its source records
        src_of = {r["id"]: f"{v.get('filename') or k} row {r.get('source_row')}"
                  for k, v in b.items() for r in v.get("rows", []) if r.get("id")}

        def step(agent_actor, action, title, rows_in, work, **gate_kw):
            """One agent step: ask TrustOps first, then do the work, then record what happened."""
            agent = policy.AGENT_BY_ACTOR[agent_actor]
            t0 = time.perf_counter()
            d = self.agent_gate(user, w, agent_actor, action, run_id=rid, trace_id=trace, **gate_kw)
            rec = {"agent": agent["code"], "stage": self.STAGE_OF_AGENT.get(agent["code"], ""),
                   "name": agent["name"], "actor_id": agent_actor, "action": action, "title": title,
                   "decision": d.decision, "reason_code": d.reason_code, "policy_id": d.policy_id,
                   "policy_name": d.policy_name, "policy_version": d.policy_version, "rows_in": rows_in, "rows_out": 0,
                   "trace_id": trace, "step_id": f"{trace}.{len(steps) + 1}", "started_at": _ts(), "status": "DONE"}
            steps.append(rec)
            if d.decision == "DENY":  # fail closed: the workflow stops and the refusal is already signed
                rec.update(status="BLOCKED", duration_ms=round((time.perf_counter() - t0) * 1000, 2),
                           summary=f"Stopped: TrustOps denied {action} ({d.reason_code}, {d.policy_id}).")
                raise Denied(d.reason_code, policy_id=d.policy_id)
            rows_out, summary, extra = work()
            rec.update(rows_out=rows_out, summary=summary, duration_ms=round((time.perf_counter() - t0) * 1000, 2), **(extra or {}))
            return d

        # AG-01: re-hash every stored file and prove it is the file that was checked at upload
        def intake():
            checked, ok = 0, 0
            for k, batch in b.items():
                uid = batch.get("upload_id")
                if uid in w.blobs and uid in w.uploads:
                    checked += 1
                    ok += hashlib.sha256(w.blobs[uid]).hexdigest() == w.uploads[uid]["sha256"]
            if checked != ok:
                raise ValueError("a stored source file no longer matches its fingerprint; re-import it")
            n = sum(len(x["rows"]) for x in b.values())
            return n, (f"{len(b)} sources present ({', '.join(f'{k} {len(v['rows'])}' for k, v in b.items())}); "
                       f"{ok}/{checked} stored files re-hashed and unchanged" +
                       (f"; {n_rejected} bank/ledger row(s) could not be imported and are raised as cases" if n_rejected else "")), \
                {"sources": sorted(b), "rejected_rows": n_rejected}
        step("agent:intake", "upload.verify", "Verify sources and file fingerprints",
             sum(len(x["rows"]) for x in b.values()), intake, refs=sorted(b))

        # AG-02: every row must already be in the canonical shape (integer cents, ISO currency, a date)
        def mapper():
            bad = [r["id"] for k in ("bank", "ledger") for r in b[k]["rows"]
                   if not isinstance(r.get("amount_cents"), int) or len(r.get("currency") or "") != 3 or not r.get("date")]
            n = len(b["bank"]["rows"]) + len(b["ledger"]["rows"])
            if bad:
                raise ValueError(f"rows not in canonical form: {bad[:5]}")
            # Map: the column mapping each source was imported with, and the person who confirmed it
            maps = {}
            for k, v in b.items():
                up = w.uploads.get(v.get("upload_id") or "") or {}
                maps[k] = {"file": v.get("filename"), "confirmed_by": up.get("committed_by"),
                           "columns": {f: c for f, c in (up.get("committed_mapping") or {}).items() if c}}
            confirmed = sum(1 for m in maps.values() if m["confirmed_by"])
            return n, (f"{n} bank and ledger rows in canonical form (amounts in integer cents, ISO currency, dates); "
                       f"column mappings confirmed by a person for {confirmed} of {len(maps)} sources"), {"mappings": maps}
        step("agent:mapping", "canonical.verify", "Check every row is in the standard format",
             len(b["bank"]["rows"]) + len(b["ledger"]["rows"]), mapper)

        # AG-03: deterministic matching
        def reconcile():
            res = recon.reconcile(wid, b["bank"]["rows"], b["ledger"]["rows"], b.get("ar", {}).get("rows"),
                                  b.get("documents", {}).get("rows"), b.get("ap", {}).get("rows"))
            ctx["res"] = res
            return (len(res.matches) + len(res.exceptions),
                    f"{len(res.matches)} matched automatically, {len(res.exceptions)} exception(s) raised; "
                    f"variance {res.variance_cents / 100:.2f}, unexplained {res.unexplained_variance_cents / 100:.2f}",
                    {"engine_version": res.engine_version})
        step("agent:recon", "run.compute", "Match bank to ledger", len(b["bank"]["rows"]) + len(b["ledger"]["rows"]),
             reconcile, rule=recon.ENGINE_VERSION)
        result = ctx["res"].to_dict()
        for k, up in rejected:
            for rj in up["health"]["rejected"]:
                raw = rj.get("raw") or {}
                amt = None
                for f in ("amount", "debit", "credit"):
                    try:
                        if raw.get(f) not in (None, "", "None"):
                            amt = ingest.to_cents(raw[f]) * (-1 if f == "credit" else 1)
                            break
                    except ValueError:
                        continue
                eid = f"EX-{len(result['exceptions']) + 1:03d}"
                result["exceptions"].append({
                    "id": eid, "type": "REJECTED_SOURCE_ROW", "bank_id": None, "ledger_id": None, "amount_cents": amt or 0,
                    "rule_id": "R0_IMPORT_REJECTED",
                    "why": f"Row {rj['row']} of {up['filename']} ({k}) could not be imported: {rj['reason']}. "
                           f"It is NOT in the {k} total, so the variance above does not include it.",
                    "evidence_refs": [], "suggestion": "Correct the row in the source file and upload it again, then re-run.",
                    "alternatives": [], "residual_cents": None, "status": "OPEN", "case_id": f"{k.upper()}-ROW-{rj['row']}",
                    "client_status": "INCOMPLETE_SOURCE", "reason_code": "ROW_NOT_IMPORTED", "missing_inputs": [],
                    "facts": {"file": up["filename"], "row": rj["row"], "source": k, "reason": rj["reason"],
                              "amount_as_written": next((str(raw[f]) for f in ("amount", "debit", "credit") if raw.get(f) not in (None, "", "None")), "")}})
        result["rejected_source_rows"] = n_rejected
        # run linkage for every proposed match, kept beside the matches so the match records themselves stay
        # identical whatever file format the rows came from
        match_linkage = [{"bank_id": m["bank_id"], "ledger_id": m["ledger_id"], "client_status": "MATCHED",
                          "run_id": rid, "trace_id": trace, "agent_id": "AG-03", "rule_id": m["rule_id"],
                          "rulebook_version": policy.POLICY_VERSION,
                          "source_record_refs": [src_of[x] for x in (m["bank_id"], m["ledger_id"]) if x in src_of]}
                         for m in result["matches"]]

        # AG-04: TrustOps decides how each exception must be handled
        def triage():
            routed = {}
            for e in result["exceptions"]:
                d = self.agent_gate(user, w, "agent:triage", "exception.route", run_id=rid, trace_id=trace,
                                    refs=e["evidence_refs"], rule=e["rule_id"], exception_type=e["type"])
                e["trustops"] = {**d.to_dict(), "required_role_label": policy.ROLE_LABELS.get(d.required_role or "", d.required_role)}
                e.update(run_id=rid, trace_id=trace, agent_id="AG-03", routed_by="AG-04",
                         rulebook_version=d.policy_version,
                         source_record_refs=[src_of[x] for x in e["evidence_refs"] if x in src_of])
                routed[d.policy_id] = routed.get(d.policy_id, 0) + 1
            return (len(result["exceptions"]),
                    f"{len(result['exceptions'])} exception(s) sent to TrustOps: " +
                    (", ".join(f"{p} x{n}" for p, n in sorted(routed.items())) or "none") + "; all need a CPA reviewer",
                    {"escalations": routed})
        step("agent:triage", "exception.triage", "Route exceptions for human review", len(result["exceptions"]), triage)

        # AG-05: reports are composed from this run; both carry the same run id
        step("agent:report", "report.compose", "Prepare finance and governance reports",
             len(result["matches"]) + len(result["exceptions"]),
             lambda: (2, f"Finance report FIN-{rid} and governance report GOV-{rid} ready (same run id {rid})",
                      {"reports": [f"FIN-{rid}", f"GOV-{rid}"]}))

        # AG-06: the query assistant may cite only this book's rows and this run
        lines = {r["id"]: dict(r, kind=k) for k in ("bank", "ledger", "ar", "ap") for r in b.get(k, {}).get("rows", [])}
        step("agent:query", "query.index", "Index this run for Ask LedgerSight", len(lines),
             lambda: (len(lines) + len(result["exceptions"]),
                      f"{len(lines)} source rows and {len(result['exceptions'])} exceptions available to read-only questions", None))

        inputs = {k: {"sha256": v["sha256"], "rows": len(v["rows"]), "filename": v.get("filename"), "upload_id": v.get("upload_id"),
                      "currency": sorted({r.get("currency") for r in v["rows"] if r.get("currency")}),
                      "net_cents": (sum(r.get("amount_cents") or 0 for r in v["rows"] if r.get("currency") in (None, result.get("base_currency", "USD")))
                                    if k in ("bank", "ledger") else
                                    sum(r.get("open_cents") or 0 for r in v["rows"] if r.get("currency") == "USD")
                                    if k in ("ar", "ap") else None)}
                  for k, v in b.items()}
        run = {"id": rid, "trace_id": trace, "batch_key": key, "created_by": user.user_id, "created_at": _ts(), "inputs": inputs,
               "engine_version": result["engine_version"], "result": result, "workflow": steps,
               "stages": self._stages(steps, result), "rulebook_version": policy.POLICY_VERSION,
               "match_linkage": match_linkage,
               "lines": {k: {kk: (str(vv) if kk == "date" else vv) for kk, vv in v.items()} for k, v in lines.items()},
               "decisions": {}}
        w.runs[rid] = run
        return run

    # The customer-facing six stages of a run. The six registered agents carry them; AG-02 both validates the
    # canonical form and proves the column mapping, and AG-05/AG-06 together complete the run.
    STAGES = [("Intake", ("AG-01",)), ("Validate", ("AG-02",)), ("Map", ("AG-02",)), ("Reconcile", ("AG-03",)),
              ("Exception", ("AG-04",)), ("Complete", ("AG-05", "AG-06"))]
    STAGE_OF_AGENT = {"AG-01": "Intake", "AG-02": "Validate · Map", "AG-03": "Reconcile", "AG-04": "Exception",
                      "AG-05": "Complete", "AG-06": "Complete"}

    @staticmethod
    def _stages(steps: list[dict], result: dict) -> list[dict]:
        by = {s["agent"]: s for s in steps}
        exc = result["exceptions"]
        open_ = [e for e in exc if e["status"] in ("OPEN", "RETURNED")]
        out = []
        for name, agents in Service.STAGES:
            ss = [by[a] for a in agents if a in by]
            status = ("BLOCKED" if any(s["status"] == "BLOCKED" for s in ss)
                      else "DONE" if len(ss) == len(agents) and all(s["status"] == "DONE" for s in ss) else "NOT_RUN")
            s0 = ss[0] if ss else {}
            if name == "Validate":
                summary = f"{s0.get('rows_in', 0)} bank and ledger rows checked: integer cents, ISO currency, dates"
            elif name == "Map":
                maps = s0.get("mappings") or {}
                summary = (f"column mappings confirmed by a person for {sum(1 for m in maps.values() if m.get('confirmed_by'))} "
                           f"of {len(maps)} sources: " + ", ".join(sorted(str(m.get('file')) for m in maps.values())))
            elif name == "Exception":
                counts: dict[str, int] = {}
                for e in exc:
                    counts[e.get("client_status") or e["type"]] = counts.get(e.get("client_status") or e["type"], 0) + 1
                summary = (f"{len(exc)} case(s) routed for CPA review: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
                           if exc else "no exceptions")
            elif name == "Complete":
                summary = (f"Reports ready; {len(open_)} case(s) still awaiting a CPA decision, so they are not complete"
                           if open_ else "Reports ready; every case has a CPA decision")
            else:
                summary = s0.get("summary", "")
            out.append({"stage": name, "agents": list(agents), "status": status, "summary": summary,
                        "step_ids": [s["step_id"] for s in ss], "trace_id": s0.get("trace_id"),
                        "case_ids": [e.get("case_id") for e in exc] if name == "Exception" else []})
        return out

    DECISIONS = {"ACCEPT": "ACCEPTED", "REJECT": "REJECTED", "RETURN": "RETURNED"}

    def decide(self, user, wid, run_id, exc_id, decision, note):
        if decision not in self.DECISIONS:
            raise ValueError("decision must be ACCEPT, REJECT or RETURN")
        w = self.ws.get(wid)
        run = w.runs.get(run_id) if w and wid in user.workspaces else None
        exc = next((e for e in (run or {}).get("result", {}).get("exceptions", []) if e["id"] == exc_id), None)
        maker = (exc or {}).get("proposed_by", "agent:recon")
        tro = (exc or {}).get("trustops") or {}
        w, d = self._gate(user, wid, "exception.decide", maker_id=maker, run_id=run_id,
                          refs=(exc or {}).get("evidence_refs"), rule=(exc or {}).get("rule_id"),
                          required_role=tro.get("required_role"), escalation_policy_id=tro.get("policy_id"))
        if not exc:
            raise KeyError(exc_id)
        if exc["status"] not in ("OPEN", "RETURNED"):
            raise ValueError(f"already {exc['status']}")
        aid = "APR-" + uuid.uuid4().hex[:6]
        effect = ("Returned to the preparer for correction. " if decision == "RETURN" else "Local review decision only. ") + \
                 "Nothing posted to any accounting system or bank."
        exc.setdefault("history", []).append({"decision": decision, "by": user.user_id, "at": _ts(), "note": note, "approval_id": aid})
        exc.update(status=self.DECISIONS[decision], reviewer=user.user_id, reviewed_at=_ts(), review_note=note,
                   approval_id=aid, effect=effect, approval_policy=d.policy_id)
        w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                     run_id=run_id, action=f"exception.{decision.lower()}", policy_decision=d.label(),
                     policy_id=d.policy_id, policy_version=d.policy_version,
                     approval_id=aid, source_line_refs=exc["evidence_refs"], matching_rule_id=exc["rule_id"],
                     workflow_pack="cpa-bank-reconciliation", trace_id=run.get("trace_id"))
        return exc

    def propose(self, user, wid, run_id, exc_id, note):
        """Preparer proposes a resolution; becomes the maker, so cannot also approve."""
        w = self.gate(user, wid, "exception.propose", run_id=run_id)
        run = w.runs.get(run_id)
        exc = next((e for e in (run or {}).get("result", {}).get("exceptions", []) if e["id"] == exc_id), None)
        if not exc:
            raise KeyError(exc_id)
        if exc["status"] not in ("OPEN", "RETURNED"):
            raise ValueError(f"already {exc['status']}")
        exc.update(proposed_by=user.user_id, proposal_note=note, status="OPEN")
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
        for k in ("bank", "ledger", "ar", "ap"):
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
                t = e.get("trustops")
                if t:
                    steps.append({"stage": "TrustOps decision", "title": f"{t['decision']} · {t['policy_id']} {t['policy_name']}",
                                  "detail": {"required_role": t.get("required_role_label"), "policy_set": t["policy_version"],
                                             "routed_by": "AG-04 Exception Triage"},
                                  "who": "TrustOps", "when": run["created_at"], "proof": f"trace {run.get('trace_id')}"})
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
        out = csv.writer(buf)

        class wr:  # every cell goes through the spreadsheet-injection guard, like the other exports
            @staticmethod
            def writerow(cells):
                out.writerow([reports._safe(c) for c in cells])
        wr.writerow(["client", "run_id", "record", "type", "bank_line", "ledger_line", "amount_usd", "rule",
                     "reason", "status", "reviewer", "approval_id", "review_note", "source_sha256_bank"])
        bank_sha = (run.get("inputs") or {}).get("bank", {}).get("sha256") or w.batches.get("bank", {}).get("sha256", "")
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
                "inputs": ({k: {"sha256": v["sha256"], "rows": v["rows"]} for k, v in run["inputs"].items()} if run.get("inputs") else
                           {k: {"sha256": v["sha256"], "rows": len(v["rows"])} for k, v in w.batches.items()}),
                "evidence": {"events": w.log.events, "public_key_pem": self.public_key_pem().decode(),
                             "verification": evidence.verify(w.log.events, self.public_key_pem()),
                             "verify_offline": "python -m ledgersight.verify_pack <this file>"},
                "boundary": "Read-only review output. No postings, payments or client emails.",
                "generated_by": user.user_id, "generated_at": _ts()}

    # ---- reports (AG-05): finance + governance, same run id, several formats --------
    def report(self, user, wid, run_id, kind, fmt="json") -> tuple[dict, bytes]:
        if kind not in reports.KINDS or fmt not in reports.FORMATS:
            raise ValueError("report must be finance or governance; format json, csv, xlsx or pdf")
        w = self.gate(user, wid, "export.read", run_id=run_id)
        run = w.runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        self._agent_or_stop(user, w, "agent:report", "report.compose", run_id=run_id, trace_id=run.get("trace_id"),
                            refs=[f"{kind}.{fmt}"])
        common = {"client": w.meta, "run": run, "generated_by": user.user_id, "generated_at": _ts()}
        if kind == "finance" and run.get("inputs"):
            rep = reports.finance_report(inputs={k: {kk: vv for kk, vv in v.items() if kk != "upload_id"} for k, v in run["inputs"].items()}, **common)
        elif kind == "finance":
            inputs = {k: {"sha256": v["sha256"], "rows": len(v["rows"]), "filename": v.get("filename"),
                          "currency": sorted({r.get("currency") for r in v["rows"] if r.get("currency")}),
                          # bank/ledger: net movement; AR/AP: total still open (only same-currency USD rows are summed)
                          "net_cents": (sum(r.get("amount_cents") or 0 for r in v["rows"] if r.get("currency") in (None, "USD"))
                                        if k in ("bank", "ledger") else
                                        sum(r.get("open_cents") or 0 for r in v["rows"] if r.get("currency") == "USD")
                                        if k in ("ar", "ap") else None)}
                      for k, v in w.batches.items()}
            rep = reports.finance_report(inputs=inputs, **common)
        else:
            rep = reports.governance_report(events=w.log.events, verification=evidence.verify(w.log.events, self.public_key_pem()),
                                            policies=policy.POLICIES, policy_version=policy.POLICY_VERSION,
                                            key_id=w.log.key_id, **common)
        return rep, reports.RENDER[fmt](rep)

    def audit_trail(self, user, wid, fmt="pdf") -> tuple[dict, bytes]:
        """The client's full signed audit trail as a downloadable report. Exporting is itself recorded."""
        if fmt not in reports.FORMATS:
            raise ValueError("format must be json, csv, xlsx or pdf")
        w = self.gate(user, wid, "export.read", refs=[f"audit.{fmt}"])
        events = list(w.log.events)
        run = list(w.runs.values())[-1] if w.runs else None
        rep = reports.audit_report(client={"id": wid, **w.meta}, events=events,
                                   verification=evidence.verify(events, self.public_key_pem()),
                                   policy_version=policy.POLICY_VERSION, key_id=w.log.key_id, key_source=self.key_source,
                                   last_run_id=run["id"] if run else None, generated_by=user.user_id, generated_at=_ts())
        return rep, reports.RENDER[fmt](rep)

    # ---- Ask LedgerSight (AG-06): read-only, intent-limited, cited ------------------
    def ask(self, user, wid, question):
        question = (question or "")[:500]
        w = self.gate(user, wid, "query.ask")
        run = list(w.runs.values())[-1] if w.runs else None
        parties = w.batches.get("parties", {}).get("rows", [])
        allowed = [w.meta["name"], wid] + [p["name"] for p in parties] + [a for p in parties for a in p["aliases"]] + \
                  [r.get(f) for k in ("ar", "ap", "documents") for r in w.batches.get(k, {}).get("rows", [])
                   for f in ("customer_alias", "vendor_alias") if r.get(f)]
        cls = query.classify(question, allowed, self._other_client_terms(wid))
        d = self.agent_gate(user, w, "agent:query", "query.answer", run_id=run["id"] if run else None,
                            trace_id=run.get("trace_id") if run else None, query_class=cls)
        if d.decision != "ALLOW":
            return {**query.refusal(d.reason_code), "trustops": d.to_dict()}
        ctx = {"client": w.meta, "run": run, "lines": (run or {}).get("lines", {}),
               "files": {u["sha256"]: u["filename"] for u in w.uploads.values()}}
        res = query.answer(question, ctx)
        res["trustops"] = d.to_dict()
        res["run_id"] = run["id"] if run else None
        return res

    @staticmethod
    def _other_client_terms(wid: str) -> list[str]:
        """Names and ids of every other client book the firm has configured, including books not loaded in
        this deployment. A question naming one is refused exactly like any unknown name, so the refusal
        never reveals which other clients exist."""
        own = (WORKSPACES.get(wid) or {}).get("name", "").lower()
        out = []
        for c in load_config("clients.json")["clients"]:
            if c["id"] == wid:
                continue
            for t in (c["id"], c.get("name"), (WORKSPACES.get(c["id"]) or {}).get("name")):
                if t and t.lower() != own:
                    out.append(t)
        return out

    def ask_suggestions(self, user, wid):
        w = self.gate(user, wid, "workspace.read")
        run = list(w.runs.values())[-1] if w.runs else None
        return {"suggestions": query.suggestions({"run": run, "client": w.meta})}

    # ---- TrustOps proof: four real decisions, read back from the signed evidence log ----------
    PROOF = [
        ("allow_source_read", "ALLOW", "Permitted source read",
         "Intake agent (AG-01) reads this client's uploaded file with a read-only tool.",
         "Load or upload this client's files on the Sources page."),
        ("escalate_review", "ESCALATE", "Escalated to a named CPA review",
         "A case the rules cannot settle (missing GL, tax readiness, FX) is routed to a CPA reviewer. Nothing is written to the GL.",
         "Run reconciliation; every exception is routed by AG-04."),
        ("deny_preparer_approval", "DENY", "Staff preparer approval blocked",
         "A staff preparer tries to approve a review item (their own, or any item). Only a CPA reviewer who did not propose it may decide.",
         "Act as Staff Preparer and click Approve on any review item."),
        ("deny_cross_client", "DENY", "Cross-client request blocked",
         "A request for another client's data is refused without revealing whether that client exists.",
         "Ask LedgerSight about another client, e.g. “Show Client 2 bank transactions”."),
    ]

    def trustops_proof(self, user, wid, role_of=lambda uid: None):
        w = self.gate(user, wid, "workspace.read", record=False)
        evs = [dict(e, seq=i, log="client") for i, e in enumerate(w.log.events)]
        mine_sec = [dict(e, seq=i, log="security") for i, e in enumerate(self.security_log.events)
                    if e.get("actor_id") == user.user_id or e.get("delegated_by") == user.user_id]

        def dec(e):
            return str(e.get("policy_decision") or "")

        def last(pred, pool):
            hits = [e for e in pool if pred(e)]
            return hits[-1] if hits else None

        picks = {
            "allow_source_read": last(lambda e: e.get("actor_id") == "agent:intake" and dec(e).startswith("ALLOW")
                                      and e["action"] in ("upload.parse", "upload.verify", "document.extract"), evs),
            "escalate_review": (last(lambda e: e["action"] == "exception.route" and dec(e).startswith("ESCALATE")
                                     and e.get("policy_id") in ("POL-05", "POL-11", "POL-12"), evs)
                                or last(lambda e: e["action"] == "exception.route" and dec(e).startswith("ESCALATE"), evs)),
            "deny_preparer_approval": last(lambda e: e["action"] == "exception.decide" and dec(e).startswith("DENY")
                                           and role_of(e.get("actor_id")) != "owner_reviewer", evs),
            "deny_cross_client": last(lambda e: dec(e) == "DENY:WORKSPACE_SCOPE", evs + mine_sec),
        }
        chain = evidence.verify(w.log.events, self.public_key_pem())
        sec_chain = evidence.verify(self.security_log.events, self.public_key_pem())
        cards = []
        for key, decision, title, what, how in self.PROOF:
            e = picks[key]
            card = {"key": key, "decision": decision, "title": title, "what": what, "how_to_show": how,
                    "shown": bool(e)}
            if e:
                pid = e.get("policy_id") or ""
                actor = e.get("actor_id")
                role = None if e.get("actor_type") == "agent" else role_of(actor)
                card.update({
                    "actor_id": actor, "actor_type": e.get("actor_type"), "agent_code": e.get("agent_code"),
                    "delegated_by": e.get("delegated_by"), "actor_role": role,
                    "actor_role_label": policy.ROLE_LABELS.get(role or "", role),
                    "action": e["action"], "policy_decision": dec(e), "reason_code": dec(e).split(":", 1)[-1],
                    "policy_id": pid, "policy_name": policy.POLICIES.get(pid, {}).get("name", ""),
                    "policy_rule_version": policy.POLICIES.get(pid, {}).get("version"),
                    "policy_version": e.get("policy_version"), "required_role": e.get("required_role"),
                    "required_role_label": policy.ROLE_LABELS.get(e.get("required_role") or "", e.get("required_role")),
                    "time_utc": e["timestamp_utc"], "run_id": e.get("run_id"), "trace_id": e.get("trace_id"),
                    "refs": (e.get("source_line_refs") or [])[:6], "event_hash": e["event_hash"],
                    "evidence_seq": e["seq"], "evidence_log": e["log"],
                    "result": "Executed read-only" if decision == "ALLOW" else
                              "Routed to a person; nothing executed" if decision == "ESCALATE" else "Refused; nothing executed",
                    # signature state is claimed only for this client's chain, and only if it verifies
                    "signature": (chain if e["log"] == "client" else sec_chain)["state"]})
            cards.append(card)
        return {"client_id": wid, "policy_version": policy.POLICY_VERSION, "chain": chain, "cards": cards,
                "all_shown": all(c["shown"] for c in cards)}

    # ---- agent attempts (security demonstration; nothing is ever executed) ----------
    def agent_attempt(self, user, wid, agent_code, action, **extra):
        agent = policy.AGENTS.get(agent_code) or policy.AGENT_BY_ACTOR.get(agent_code)
        if not agent:
            raise ValueError("unknown agent")
        if wid not in user.workspaces or wid not in self.ws:
            self.security_log.append(tenant_id=TENANT, workspace_id=wid, actor_type="agent", actor_id=agent["actor_id"],
                                     agent_code=agent["code"], delegated_by=user.user_id, action=action,
                                     policy_decision="DENY:WORKSPACE_SCOPE", policy_id="POL-01",
                                     policy_version=policy.POLICY_VERSION, workflow_pack="cpa-bank-reconciliation")
            raise Denied("WORKSPACE_SCOPE", conceal=True, policy_id="POL-01")
        d = self.agent_gate(user, self.ws[wid], agent["actor_id"], action, **extra)
        return {"decision": d.decision, "reason_code": d.reason_code, "policy_id": d.policy_id, "policy_name": d.policy_name,
                "actor": f"{agent['code']} {agent['name']}", "executed": False}

    # ---- live agent monitor: which agent is working, and how is it doing ----------
    ACTIVE_SECONDS = 8

    def agents_status(self, user, wid):
        """Built only from the signed evidence log and the latest run, so it shows what agents really did."""
        w = self.gate(user, wid, "workspace.read", record=False)
        now = datetime.now(timezone.utc)
        run = list(w.runs.values())[-1] if w.runs else None
        steps = {s["agent"]: s for s in (run or {}).get("workflow", [])}
        evs = [dict(e, seq=i) for i, e in enumerate(w.log.events) if e.get("actor_type") == "agent" and e.get("agent_code")]
        out = []
        for code, a in policy.AGENTS.items():
            mine = [e for e in evs if e["agent_code"] == code]
            counts = {k: sum(str(e["policy_decision"]).startswith(k) for e in mine) for k in ("ALLOW", "ESCALATE", "DENY")}
            last = mine[-1] if mine else None
            age = (now - datetime.fromisoformat(last["timestamp_utc"])).total_seconds() if last else None
            if not last:
                status, note = "NOT_USED_YET", "Has not been asked to do anything in this book yet."
            elif str(last["policy_decision"]).startswith("DENY"):
                status, note = "BLOCKED", f"Last request was refused by TrustOps: {last['action']} ({last['policy_decision'].split(':', 1)[1]}, {last.get('policy_id')})."
            elif age is not None and age <= self.ACTIVE_SECONDS:
                status, note = "WORKING", f"Active in the last {self.ACTIVE_SECONDS} seconds: {last['action']}."
            else:
                status, note = "IDLE", f"Finished. Last action: {last['action']}."
            out.append({"code": code, "actor_id": a["actor_id"], "name": a["name"], "does": a["does"], "tools": a["tools"],
                        "model": a["model"], "status": status, "status_note": note, "counts": counts, "actions": len(mine),
                        "last": ({"at": last["timestamp_utc"], "seconds_ago": round(age, 1), "action": last["action"],
                                  "decision": last["policy_decision"], "policy_id": last.get("policy_id"), "run_id": last.get("run_id")}
                                 if last else None),
                        "last_run_step": steps.get(code),
                        "recent": [{"seq": e["seq"], "at": e["timestamp_utc"], "action": e["action"], "decision": e["policy_decision"],
                                    "policy_id": e.get("policy_id"), "refs": (e.get("source_line_refs") or [])[:4]} for e in mine[-5:][::-1]]})
        return {"run_id": run["id"] if run else None, "trace_id": (run or {}).get("trace_id"), "server_time": now.isoformat(),
                "active_window_seconds": self.ACTIVE_SECONDS, "agents": out,
                "feed": [{"seq": e["seq"], "at": e["timestamp_utc"], "agent": e["agent_code"], "actor_id": e["actor_id"],
                          "action": e["action"], "decision": e["policy_decision"], "policy_id": e.get("policy_id"),
                          "run_id": e.get("run_id"), "delegated_by": e.get("delegated_by")} for e in evs[-40:][::-1]]}

    # ---- rulebooks: the policy set, read-only ------------------------------------
    def rulebooks(self):
        return {"version": policy.POLICY_VERSION, "policies": list(policy.POLICIES.values()),
                "forbidden_actions": sorted(policy.FORBIDDEN),
                "roles": [{"role": r, "label": policy.ROLE_LABELS.get(r, r), "actions": sorted(a)}
                          for r, a in policy.ROLE_ACTIONS.items() if not r.startswith("agent:")],
                "agents": list(policy.AGENTS.values()),
                "editing": "Read-only here. Policy changes are made in ledgersight/config/policies.json and take effect on restart; "
                           "in-app editing is in development."}

    # ---- reset ---------------------------------------------------------
    def clear_source(self, user, wid, kind):
        """Remove one imported source (e.g. a wrong bank file). Runs built on it are withdrawn too,
        because their results no longer match the inputs. The removal itself is recorded."""
        w = self.gate(user, wid, "upload.create", refs=[kind])
        if kind not in ingest.CANONICAL and kind != "pdf":
            raise ValueError("unknown source type")
        decided = sum(1 for r in w.runs.values() for e in r["result"]["exceptions"]
                      if e["status"] != "OPEN" or e.get("proposal_note"))
        if decided and user.role != "owner_reviewer":
            w.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                         action=f"source.clear:{kind}", policy_decision="DENY:DECISIONS_EXIST", policy_id="POL-06",
                         source_line_refs=[f"decided_or_proposed:{decided}"], workflow_pack="cpa-bank-reconciliation")
            raise Denied("DECISIONS_EXIST", policy_id="POL-06")
        removed = [uid for uid, u in w.uploads.items() if u["kind"] == kind or (kind == "documents" and u["kind"] == "pdf")]
        old_sha = w.batches.get(kind, {}).get("sha256")
        w.batches.pop(kind, None)
        for uid in removed:
            u = w.uploads.pop(uid, None); w.blobs.pop(uid, None)
            if u:
                w.tables.pop(f"sha:{u.get('sha256')}", None)
            for f in (DATA_DIR / TENANT / wid).glob(f"{uid}.*"):
                f.unlink(missing_ok=True)
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
        self.save(wid)  # make sure the old chain is fully stored before it is archived
        archived = self.store.archive_events(wid) if self.store else None
        shutil.rmtree(DATA_DIR / TENANT / wid, ignore_errors=True)
        new = Workspace(wid, self._key)
        new.log.append(tenant_id=TENANT, workspace_id=wid, actor_type="human", actor_id=user.user_id,
                       action="workspace.reset", policy_decision="ALLOW:OK",
                       workflow_pack="cpa-bank-reconciliation",
                       source_line_refs=[f"prior_chain_head:{old.log.events[-1]['event_hash'] if old.log.events else 'none'}",
                                         f"prior_event_count:{len(old.log.events)}"] + ([f"archived_as:{archived}"] if archived else []))
        self.ws[wid] = new
        self.save(wid)
        return {"reset": wid, "files_deleted": True}

    # ---- trust view ----------------------------------------------------
    def trust(self, user=None):
        return {
            "tenant": TENANT, "workspaces": [w for w in WORKSPACES if user is None or w in user.workspaces],
            "region_declared": self.region,
            "region_note": "Declared by deployment configuration. Not an independently verified data-residency attestation.",
            "data_classes_allowed": sorted(policy.ALLOWED_DATA_CLASSES),
            "hard_boundary": "No tax returns, SSNs, preparer IDs, banking credentials, unmasked account numbers or real client records.",
            "model_routes_allowed": sorted(r for r in policy.ALLOWED_MODEL_ROUTES if r),
            "model_routes_in_use": ["deterministic"],
            "ai_note": "No language model is called anywhere. Mapping and matching are rule-based.",
            "forbidden_actions": sorted(policy.FORBIDDEN),
            "roles": {k: sorted(v) for k, v in policy.ROLE_ACTIONS.items()},
            "agents": [{"id": a["actor_id"], "code": a["code"], "name": a["name"], "does": a["does"], "model": a["model"]}
                       for a in policy.AGENTS.values()],
            "evidence": {"algorithm": "Ed25519 over SHA-256 hash chain", "key_source": self.key_source,
                         "public_key_pem": self.public_key_pem().decode()},
            "retention": "Client files are deleted when a client's data is cleared; the audit trail of a cleared client is archived, not deleted.",
            "storage": ("SQLite database and file store in the data directory" if self.store else "memory only (lost on restart)"),
            "policy_version": policy.POLICY_VERSION,
            "security_events": len(self.security_log.events),
        }
