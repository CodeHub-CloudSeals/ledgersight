"""Signed, hash-chained evidence log with an independent verifier.

Each event carries prior_hash -> event_hash, signed with Ed25519. verify() needs
only the public key and the events, so it can run outside the app (G-D06).
If the signer is unavailable, append() raises and the caller must fail closed.
Raw source values are never put in events; only line refs and digests.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization

GENESIS = "0" * 64
SIGNED_FIELDS = ["event_id", "timestamp_utc", "tenant_id", "workspace_id", "actor_type", "actor_id",
                 "workflow_pack", "run_id", "source_batch_sha256", "source_line_refs", "canonical_version",
                 "matching_rule_id", "model_route", "action", "policy_decision", "approval_id",
                 "prior_hash", "key_id"]


class SignerUnavailable(Exception):
    pass


def _canonical(ev: dict) -> bytes:
    return json.dumps({k: ev.get(k) for k in SIGNED_FIELDS}, sort_keys=True, separators=(",", ":")).encode()


class EvidenceLog:
    def __init__(self, private_key: Ed25519PrivateKey | None, key_id: str):
        self._key = private_key
        self.key_id = key_id
        self.events: list[dict] = []

    @classmethod
    def generate(cls, key_id="demo-ed25519-1"):
        return cls(Ed25519PrivateKey.generate(), key_id)

    def public_key_pem(self) -> bytes:
        return self._key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)

    def append(self, **fields) -> dict:
        if self._key is None:
            raise SignerUnavailable("evidence signer unavailable; material action blocked")
        ev = {k: fields.get(k) for k in SIGNED_FIELDS}
        ev.update(event_id=str(uuid.uuid4()), timestamp_utc=datetime.now(timezone.utc).isoformat(),
                  prior_hash=self.events[-1]["event_hash"] if self.events else GENESIS, key_id=self.key_id)
        body = _canonical(ev)
        ev["event_hash"] = hashlib.sha256(body).hexdigest()
        ev["signature"] = self._key.sign(body).hex()
        self.events.append(ev)
        return ev


def verify(events: list[dict], public_key_pem: bytes | None) -> dict:
    """Returns {"state": PASS|FAIL|UNKNOWN, "checked": n, "reason": ...}. Never PASS without real proof."""
    if not public_key_pem:
        return {"state": "UNKNOWN", "checked": 0, "reason": "no verification key: NOT VERIFIED"}
    if not events:
        return {"state": "UNKNOWN", "checked": 0, "reason": "no events"}
    pub = serialization.load_pem_public_key(public_key_pem)
    assert isinstance(pub, Ed25519PublicKey)
    prior = GENESIS
    for i, ev in enumerate(events):
        if ev.get("prior_hash") != prior:
            return {"state": "FAIL", "checked": i, "reason": f"chain break at event {i}"}
        body = _canonical(ev)
        if hashlib.sha256(body).hexdigest() != ev.get("event_hash"):
            return {"state": "FAIL", "checked": i, "reason": f"hash mismatch at event {i} (tampered)"}
        try:
            pub.verify(bytes.fromhex(ev["signature"]), body)
        except (InvalidSignature, ValueError):
            return {"state": "FAIL", "checked": i, "reason": f"bad signature at event {i}"}
        prior = ev["event_hash"]
    return {"state": "PASS", "checked": len(events), "reason": "chain and signatures valid"}
