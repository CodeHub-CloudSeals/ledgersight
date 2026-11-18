"""FinTwin's optional language model (2026-11-16): Gemini on Vertex AI, used only to UNDERSTAND a question and to
WORD the answer. Every fact still comes from the deterministic intents in core/query.py.

- Switched on with LEDGERSIGHT_ASSISTANT_MODEL=vertex (empty = no model, exactly as before). The route
  "vertex:<model>" must also be in config/policies.json allowed_model_routes (a governed change); otherwise the
  model is not used.
- understand(): the model sees only the question text, the list of supported intents and this run's item ids /
  case ids / exception types (no amounts, no names). Its JSON choice is validated against those lists.
- phrase(): the model sees the question and the exact result already computed for this client. Its reply is kept
  only if every number and every ID in it appears in that result (core/query.grounded); otherwise the
  deterministic text is shown.
- One time budget per question (LEDGERSIGHT_ASSISTANT_BUDGET, default 8 s). A timeout, an error or invalid output
  falls back silently to the deterministic answer. Only the error type is logged, never question or answer text.
Uses the google-genai SDK in Vertex mode with the Cloud Run service account (no API keys), like TrustOps' llm.py.
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

DEFAULT_MODEL = "gemini-2.5-flash"
INTENTS = ("summary", "list_exceptions", "list_unmatched", "explain", "who_approved", "outstanding", "list_matches",
           "help", "smalltalk", "unknown")
STATUSES = ("open", "accepted", "rejected", "returned")
ISO = re.compile(r"^20\d\d-\d\d-\d\d$")

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fintwin-model")
_client: Any = None          # the model client; tests put a fake here


def mode() -> str:
    return os.environ.get("LEDGERSIGHT_ASSISTANT_MODEL", "").strip().lower()


def model_id() -> str:
    return os.environ.get("LEDGERSIGHT_VERTEX_MODEL", "").strip() or DEFAULT_MODEL


def route() -> str:
    return f"vertex:{model_id()}"


def budget() -> float:
    try:
        return max(1.0, float(os.environ.get("LEDGERSIGHT_ASSISTANT_BUDGET", "8")))
    except ValueError:
        return 8.0


def enabled(allowed_routes) -> bool:
    """The model is used only when switched on AND its route is allowlisted by policy."""
    return mode() == "vertex" and route() in allowed_routes


class VertexClient:
    """Gemini via google-genai in Vertex mode (Cloud Run service account, no API keys)."""

    def __init__(self) -> None:
        from google import genai  # lazy: only needed when the model is switched on
        from google.genai import types
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or _adc_project()
        self.model = model_id()
        self.client = genai.Client(vertexai=True, project=project,
                                   location=os.environ.get("LEDGERSIGHT_VERTEX_LOCATION", "europe-west1"),
                                   http_options=types.HttpOptions(timeout=int(budget() * 1000)))

    def complete_json(self, system: str, user: str, max_tokens: int) -> str:
        from google.genai import types
        cfg: dict[str, Any] = {"system_instruction": system, "temperature": 0.1, "max_output_tokens": max_tokens,
                               "response_mime_type": "application/json"}
        if "flash" in self.model:   # 2.5 "thinking" would otherwise use the output budget
            cfg["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        r = self.client.models.generate_content(model=self.model, contents=user, config=types.GenerateContentConfig(**cfg))
        text = getattr(r, "text", None)
        if not text:
            raise ValueError("empty model response")
        return text


def _adc_project() -> str | None:
    try:
        import google.auth
        return google.auth.default()[1]
    except Exception:
        return None


def _get_client():
    global _client
    if _client is None:
        _client = VertexClient()
    return _client


def call(system: str, user: str, max_tokens: int, deadline: float) -> dict | None:
    """One JSON call within the time left. None on timeout, error or anything that is not a JSON object."""
    left = deadline - time.monotonic()
    if left <= 0.2:
        return None
    try:
        fut = _pool.submit(lambda: _get_client().complete_json(system, user, max_tokens))
        out = json.loads(fut.result(timeout=left))
        return out if isinstance(out, dict) else None
    except Exception as e:  # noqa: BLE001 - any failure means "answer without the model"
        print(f"[ledgersight] FinTwin model call skipped: {type(e).__name__}", flush=True)
        return None


UNDERSTAND_SYSTEM = (
    "You route questions for FinTwin, a read-only assistant over ONE client's bank reconciliation. "
    "Return only a JSON object: {\"intent\": one of " + json.dumps(list(INTENTS)) + ", \"ref\": an id copied exactly "
    "from known_refs or null, \"types\": a list chosen from exception_types (may be empty), \"status\": one of "
    + json.dumps(list(STATUSES)) + " or null, \"date_from\": \"YYYY-MM-DD\" or null, \"date_to\": \"YYYY-MM-DD\" or null}. "
    "Intents: summary = overall run summary or totals; list_exceptions = list review items / exceptions, optionally "
    "by type, status (open = waiting, accepted = approved) and bank date range; list_unmatched = unmatched or "
    "unreconciled transactions; explain = why a specific item or line (ref) was flagged or matched; who_approved = "
    "who approved, rejected or reviewed something (ref optional); outstanding = what is still owed, on a ref or "
    "overall; list_matches = automatic matches; help = what you can do; smalltalk = greetings, thanks, chit-chat; "
    "unknown = anything else. Never invent ids. Answer with JSON only."
)

PHRASE_SYSTEM = (
    "You are FinTwin, a friendly, read-only financial assistant. Rewrite the given result as a short, natural answer "
    "to the question (1-4 sentences, plain text, no markdown, no tables). Use ONLY the facts in the result. Copy every "
    "amount, date and id exactly as written there; write counts as digits. Do not add advice, numbers, names or ids "
    "that are not in the result. If the result says nothing was found, say so plainly. The table and sources are "
    "shown under your answer, so you may summarise instead of listing every row. "
    "Return only a JSON object: {\"answer\": \"...\"}."
)


def understand(question: str, refs: list[str], types: list[str], deadline: float) -> dict | None:
    """The model's reading of the question, validated; None means: use the rules as today."""
    out = call(UNDERSTAND_SYSTEM, json.dumps({"question": question, "known_refs": refs[:300], "exception_types": types}),
               200, deadline)
    if not out:
        return None
    intent = out.get("intent")
    if intent not in INTENTS:
        return None
    by_upper = {r.upper(): r for r in refs}
    ref = out.get("ref")
    ref = by_upper.get(str(ref).strip().upper()) if ref else None
    if out.get("ref") and ref is None:
        return None                                   # an id that is not in this run: do not trust the reading
    if intent == "explain" and not ref:
        return None
    ts = out.get("types") or []
    if not isinstance(ts, list) or any(t not in types for t in ts):
        return None
    status = out.get("status") if out.get("status") in STATUSES else None
    dates = {k: (out.get(k) if isinstance(out.get(k), str) and ISO.match(out.get(k)) else None) for k in ("date_from", "date_to")}
    return {"intent": intent, "ref": ref, "types": ts, "status": status, **dates}


def phrase(question: str, result: dict, deadline: float, grounded) -> str | None:
    """A natural wording of an exact result, or None when it could not be produced or added anything."""
    payload = {"question": question, "result": result.get("answer", ""), "columns": result.get("columns") or [],
               "rows": (result.get("rows") or [])[:40]}
    out = call(PHRASE_SYSTEM, json.dumps(payload, ensure_ascii=False), 400, deadline)
    text = (out or {}).get("answer")
    if not isinstance(text, str) or not text.strip() or len(text) > 1500:
        return None
    text = text.strip()
    return text if grounded(text, payload, extra_counts=[len(result.get("rows") or [])]) else None
