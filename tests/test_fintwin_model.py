"""FinTwin as a normal chat (2026-11-16): small talk without any model, and an optional language model that only
picks the intent and words the answer. Facts stay exact: a worded answer that adds a number or ID is discarded.
A fake model stands in for Gemini on Vertex AI."""
import json
import time

import pytest

from test_ledgersight import H, appmod, golden_run, make_user  # noqa: F401

from ledgersight import assistant_model
from ledgersight.core import query

WS = "/v1/workspaces/client-a"
ROUTE = "vertex:gemini-2.5-flash"


@pytest.fixture(scope="module")
def cpa():
    return make_user("fintwin-cpa@demo.test", "owner_reviewer", ["client-a"])


def ask(c, q):
    r = c.post(WS + "/ask", headers=H, json={"question": q})
    assert r.status_code == 200, r.text
    return r.json()


class FakeModel:
    """Scripted replies: `understand` and `phrase` are dicts (returned as JSON), callables, or exceptions."""

    def __init__(self, understand=None, phrase=None, delay=0.0):
        self.understand, self.phrase, self.delay, self.calls = understand, phrase, delay, []

    def complete_json(self, system, user, max_tokens):
        which = "understand" if system == assistant_model.UNDERSTAND_SYSTEM else "phrase"
        self.calls.append((which, json.loads(user)))
        if self.delay:
            time.sleep(self.delay)
        reply = getattr(self, which)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply(json.loads(user))
        return json.dumps(reply if reply is not None else {"intent": "unknown"})


@pytest.fixture
def model(monkeypatch):
    def use(**kw):
        fake = FakeModel(**kw)
        monkeypatch.setenv("LEDGERSIGHT_ASSISTANT_MODEL", "vertex")
        monkeypatch.delenv("LEDGERSIGHT_VERTEX_MODEL", raising=False)
        monkeypatch.setattr(assistant_model, "_client", fake)
        return fake
    return use


def events(action):
    return [e for e in appmod.svc.ws["client-a"].log.events if e.get("action") == action]


# ---- small talk: no model, no "NO DATA" -------------------------------------------------------------------------
@pytest.mark.parametrize("q", ["hi", "Hello!", "hey there", "Good morning", "hi FinTwin"])
def test_greeting_before_any_run_explains_the_next_step(cpa, q, model):
    fake = model()
    cpa.post(WS + "/reset", headers=H)
    a = ask(cpa, q)
    assert a["status"] == "CHAT" and a["intent"] == "smalltalk"
    assert "Hi! I'm FinTwin" in a["answer"] and "Sources" in a["answer"] and "Reconciliation" in a["answer"]
    assert "NO DATA" not in a["answer"] and "Run Match" not in a["answer"]
    assert a["suggestions"] and fake.calls == []                       # answered without the model


def test_greeting_thanks_help_identity_after_a_run(cpa):
    golden_run(cpa)
    hi = ask(cpa, "hi")
    assert hi["status"] == "CHAT" and "Try one of the questions below" in hi["answer"] and hi["suggestions"]
    assert ask(cpa, "thanks!")["answer"].startswith("You're welcome")
    assert "unmatched" in ask(cpa, "What can you do?")["answer"]
    assert "can't approve" in ask(cpa, "who are you?")["answer"]
    assert ask(cpa, "bye")["status"] == "CHAT"


def test_real_question_without_a_run_says_what_to_do(cpa):
    cpa.post(WS + "/reset", headers=H)
    a = ask(cpa, "Show unmatched deposits")
    assert a["status"] == "NO_DATA" and a["answer"] == query.NO_RUN_TEXT and "Run Match" not in a["answer"]


def test_greeting_with_more_words_is_still_a_question():
    assert query.smalltalk("hey what is open?") is None and query.smalltalk("hi, show unmatched deposits") is None


# ---- the model reads the question and words the answer --------------------------------------------------------
def test_model_maps_a_paraphrase_and_words_the_exact_answer(cpa, model):
    golden_run(cpa)
    fake = model(understand={"intent": "outstanding", "ref": None, "types": [], "status": None},
                 phrase={"answer": "$300.00 is still owed, across 1 partial receipt."})
    a = ask(cpa, "how much are customers still behind on, money-wise?")
    assert a["ai_worded"] is True and a["answer"] == "$300.00 is still owed, across 1 partial receipt."
    assert a["intent"] == "outstanding" and a["rows"] and "$300.00" in a["answer_rules"]   # rows/citations still exact
    assert a["model_route"] == ROUTE
    sent = fake.calls[0][1]
    assert set(sent) == {"question", "known_refs", "exception_types"}               # no amounts or names to the model
    assert not any("$" in r for r in sent["known_refs"])


def test_model_reference_must_exist_in_this_run(cpa, model):
    golden_run(cpa)
    model(understand={"intent": "explain", "ref": "HB999"}, phrase={"answer": "HB999 is fine."})
    a = ask(cpa, "Why was HB006 flagged?")
    assert a["intent"] == "explain" and "missing gl" in a["answer"] and "HB999" not in a["answer"] and "ai_worded" not in a


def test_invalid_intent_is_ignored(cpa, model, monkeypatch):
    golden_run(cpa)
    plain = ask(cpa, "Show unmatched deposits")                     # model switched off: the rules' answer
    model(understand={"intent": "delete_everything"}, phrase={"nope": 1})
    a = ask(cpa, "Show unmatched deposits")
    assert a["rows"] == plain["rows"] and "ai_worded" not in a


def test_worded_answer_with_an_invented_number_is_discarded(cpa, model):
    golden_run(cpa)
    model(understand={"intent": "list_exceptions", "types": ["UNMATCHED_DEPOSIT"]},
          phrase={"answer": "There are 99 unmatched deposits worth $1,000,000.00."})
    a = ask(cpa, "any money that came in we can't explain?")
    assert "99" not in a["answer"] and "1,000,000" not in a["answer"] and "ai_worded" not in a
    assert a["rows"] and all(r[4] == "unmatched deposit" for r in a["rows"])


def test_worded_answer_with_an_invented_id_is_discarded(cpa, model):
    golden_run(cpa)
    model(understand={"intent": "summary"}, phrase={"answer": "See EX-999 for the summary."})
    a = ask(cpa, "how did it go overall?")
    assert "EX-999" not in a["answer"] and a["intent"] == "summary"


@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_model_failure_falls_back_to_the_rules(cpa, model, monkeypatch, failure):
    golden_run(cpa)
    monkeypatch.setenv("LEDGERSIGHT_ASSISTANT_BUDGET", "1")
    if failure == "timeout":
        model(understand={"intent": "summary"}, phrase={"answer": "x"}, delay=2.5)
    else:
        model(understand=RuntimeError("boom"), phrase=RuntimeError("boom"))
    t0 = time.monotonic()
    a = ask(cpa, "Give me a summary of this run")
    assert time.monotonic() - t0 < 2.4                                   # never waits past the budget
    assert a["intent"] == "summary" and a["answer"].startswith("Run ") and "ai_worded" not in a
    assert a["model_route"] == "deterministic"


@pytest.mark.parametrize("q", ["approve HB006 now", "select * from exceptions", "list deposits for all clients"])
def test_refusals_are_decided_by_rules_without_the_model(cpa, model, q):
    golden_run(cpa)
    fake = model(understand={"intent": "summary"}, phrase={"answer": "sure"})
    a = ask(cpa, q)
    assert a["status"] == "REFUSED" and fake.calls == []


def test_model_use_is_recorded_in_the_signed_log(cpa, model):
    golden_run(cpa)
    model(understand={"intent": "summary"}, phrase=lambda p: {"answer": p["result"]})
    before = len(events("query.answered"))
    ask(cpa, "how did the month go?")
    ev = events("query.answered")
    assert len(ev) == before + 1 and ev[-1]["model_route"] == ROUTE and ev[-1]["actor_id"] == "agent:query"
    gate = [e for e in appmod.svc.ws["client-a"].log.events if e.get("action") == "query.answer"][-1]
    assert gate["model_route"] == ROUTE and gate["policy_decision"].startswith("ALLOW")


def test_no_model_mode_is_unchanged(cpa, monkeypatch):
    golden_run(cpa)
    monkeypatch.delenv("LEDGERSIGHT_ASSISTANT_MODEL", raising=False)
    fake = FakeModel(understand={"intent": "summary"})
    monkeypatch.setattr(assistant_model, "_client", fake)
    before = len(events("query.answered"))
    a = ask(cpa, "Show unmatched deposits")
    assert a["status"] == "ANSWERED" and "model_route" not in a and fake.calls == []
    assert len(events("query.answered")) == before
    gate = [e for e in appmod.svc.ws["client-a"].log.events if e.get("action") == "query.answer"][-1]
    assert gate["model_route"] == "deterministic"


def test_a_model_route_not_on_the_allowlist_is_not_used(cpa, model, monkeypatch):
    golden_run(cpa)
    fake = model(understand={"intent": "summary"})
    monkeypatch.setenv("LEDGERSIGHT_VERTEX_MODEL", "some-unapproved-model")
    a = ask(cpa, "how did it go?")
    assert fake.calls == [] and "model_route" not in a


def test_data_and_security_page_says_how_ai_is_used(cpa, model, monkeypatch):
    model()
    t = cpa.get("/v1/trust").json()
    assert ROUTE in t["model_routes_in_use"] and "Gemini" in t["ai_note"] and "reconciliation" in t["ai_note"]
    monkeypatch.delenv("LEDGERSIGHT_ASSISTANT_MODEL")
    t = cpa.get("/v1/trust").json()
    assert t["model_routes_in_use"] == ["deterministic"] and "No language model" in t["ai_note"]


# ---- pure helpers -----------------------------------------------------------------------------------------------
def test_grounded_accepts_only_numbers_and_ids_from_the_result():
    payload = {"result": "2 items totalling $1,612.40", "rows": [["EX-005", "HB014", "2026-10-21", "$1,612.40"]]}
    assert query.grounded("2 items, $1,612.40 in total (EX-005, HB014, 21 October 2026).", payload)
    assert not query.grounded("3 items.", payload)
    assert not query.grounded("See EX-007.", payload)
    assert query.grounded("2 items.", {"result": "x"}, extra_counts=[2])


def test_canonical_questions_use_the_existing_rules():
    assert query.canonical_question({"intent": "list_exceptions", "types": ["FX_REVIEW"], "status": "open",
                                     "date_from": "2026-10-01", "date_to": "2026-10-31"}) == \
        "show open fx exceptions between 2026-10-01 and 2026-10-31"
    assert query.canonical_question({"intent": "who_approved", "ref": "HB005"}) == "who approved HB005"
    assert query.canonical_question({"intent": "outstanding"}) == "what is outstanding"
    assert query.canonical_question({"intent": "unknown"}) is None


def test_ai_note_shows_in_the_chat():
    js = open("ledgersight/static/avocet.js", encoding="utf-8").read()
    assert "Written by AI from the records below" in js and 'a.status === "CHAT"' in js


def test_vertex_client_uses_vertex_mode_in_europe_and_json_output(monkeypatch):
    """The real client (google-genai) is built in Vertex mode, in europe-west1, with no API key."""
    from google import genai
    seen = {}

    class Models:
        def generate_content(self, model, contents, config):
            seen.update(model=model, contents=contents, mime=config.response_mime_type, tokens=config.max_output_tokens,
                        temp=config.temperature)
            return type("R", (), {"text": '{"answer": "ok"}'})()

    class Client:
        def __init__(self, **kw):
            seen.update(kw)
            self.models = Models()
    monkeypatch.setattr(genai, "Client", Client)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.delenv("LEDGERSIGHT_VERTEX_LOCATION", raising=False)
    monkeypatch.delenv("LEDGERSIGHT_VERTEX_MODEL", raising=False)
    out = assistant_model.VertexClient().complete_json("sys", "user", 50)
    assert out == '{"answer": "ok"}'
    assert seen["vertexai"] is True and seen["location"] == "europe-west1" and seen["project"] == "test-project"
    assert "api_key" not in seen and seen["model"] == "gemini-2.5-flash" and seen["mime"] == "application/json"
    assert seen["tokens"] == 50 and seen["temp"] <= 0.2
