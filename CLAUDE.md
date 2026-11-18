# LedgerSight + TrustOps — working notes for Claude

## Goal
Internal demo (Diana) then Avocet POC (Samantha). One connected journey:
LedgerSight does the reconciliation work; TrustOps governs, authorises and evidences every action.
Synthetic data only. Read-only: never post to a ledger, pay, or email anyone.

## Run / test (Windows, VS Code)
- Env: `.venv\Scripts\activate` ; deps: `pip install -r requirements-dev.txt` (runtime pins in requirements.txt)
- Browser tests: `bash tests/e2e/run_all.sh` (needs Playwright; see tests/e2e/README.md)
- App: Run and Debug -> "LedgerSight (local demo)" (F5) -> http://127.0.0.1:8090. Launch config sets AVOCET_NO_LOGIN=1 (local demo: no login screen, "Acting as" switch
  between CPA Reviewer / Staff Preparer). Honoured only with MFA off + insecure cookie; hosted keeps login.
  Without it: user `presenter`, password from `.env` (gitignored; template `.env.example`). Upload test pack: `demo_files/` (see its README).
- Tests: `.venv\Scripts\python.exe -m pytest -q` (baseline 2026-11-09: 186 passed, incl. tests/test_cpa_demo.py, tests/test_upload_detection.py). Keep all existing tests green; add tests for every change.
  Use the .venv: with a user-site (Windows Store) Python, the isolated PDF subprocess (`python -I`) cannot import pdfplumber and 13 PDF tests fail.
- State persists in AVOCET_DATA_DIR (SQLite ledgersight.db + uploaded files + signing-key.pem). Restart keeps
  everything; delete the folder for a truly empty start. AVOCET_PERSIST=0 = memory only. See PRODUCTION.md.

## Code map
- `ledgersight/config/*.json`   clients, policies (POL-01..10), agents (AG-01..06 + tools), source kinds/connectors, matching thresholds
- `ledgersight/core/policy.py`  TrustOps gate (`evaluate`) — deny by default; ALLOW/ESCALATE/DENY + policy id/version
- `ledgersight/core/reports.py` Finance + Governance reports; JSON/CSV/XLSX/PDF renderers
- `ledgersight/core/query.py`   Ask LedgerSight (AG-06): intent rules, citations, refusals
- `ledgersight/core/recon.py`   deterministic matching engine (no LLM in the money path)
- `ledgersight/core/evidence.py` Ed25519 hash-chained evidence + `verify`
- `ledgersight/core/ingest.py`  file checks, mapping, canonical rows
- `ledgersight/service.py`      workspaces, uploads, runs, decisions, trace, exports (calls `gate` first)
- `ledgersight/app.py`          FastAPI routes; `ledgersight/static/` UI (index.html, avocet.js/css)
- `tests/test_ledgersight.py`, `ledgersight/fixtures/expectations.json` (golden results)

## Already working (verified)
Golden cases from uploaded fixtures: HB004 POSSIBLE_DUPLICATE, HB005 PARTIAL_RECEIPT ($300 residual),
HB006 MISSING_GL, HB009 MISSING_RECEIPT, HB010 UNMATCHED_DEPOSIT. Client scope concealment, maker-checker,
forbidden actions, signed evidence + tamper check, per-transaction trace, CSV + JSON review pack.

## Built 2026-11-04 (tests in tests/test_governance.py)
Gaps 1–6 below are implemented: ESCALATE + policy ids, agents gated as actors (`Service.agent_gate`), 6-step
workflow timeline per run, RETURN + MAKER_CHECKER reason, two reports sharing run_id in 4 formats, Ask LedgerSight,
Rulebooks tab, source labels, live Agents monitor (GET /agents, built from the evidence log). Also: AP (vendor bills) source kind + JSON file format. UI is functional but
not yet restyled (Praveen owns dashboard/UI/generic-names items). Policy editing in-app: in development.

## Built 2026-11-09 for the Avocet CPA demo (tests in tests/test_cpa_demo.py)
Delivery note C02–C09. Client 1 fixtures gained HB013/HB014: CA-TAX-01 (FL->CA sale, missing taxability/nexus/registration
-> TAX_REVIEW, POL-11, no tax computed) and UK-FX-01 (GBP bill paid in USD, no approved FX source -> FX_REVIEW
APPROVED_FX_RATE_MISSING, POL-12, not converted or matched). HB001–HB012 results unchanged. Every exception has
case_id, client_status, run/trace/agent/rulebook linkage and source_record_refs; run has six stages (Intake..Complete)
and match_linkage. Sources show a source envelope. GET /trustops/proof reads ALLOW/ESCALATE/DENY/DENY from the audit
trail. Ask: other configured client names are refused; "1–31 October 2026" ranges; case ids queryable. Finance pack:
totals by status, source totals, tax/FX items, CPA next steps. UI: Overview case cards -> review queue, run stages,
source envelope, proof panel on Guardrails, demo/synthetic label.
Diana review 06/11 made Ask LedgerSight a floating button bottom-right; on 2026-11-12 (user request) it moved to the
bottom of the left sidebar (always visible, icon-only when collapsed; Ctrl+K). GET /audit-trail.{pdf,csv,xlsx,json} exports the
whole signed audit trail (button on the Audit trail tab). NOT built here (infra/ops): C10 HTTPS domain + DNS, tenant
URLs, sandbox pipeline, production URL, email access.

## Roles: CPA reviewer vs staff preparer (2026-11-12, tests in tests/test_roles.py)
CPA reviewer (owner_reviewer) sees every page and holds `governance.read`. Staff preparer sees Overview ("My work":
to propose / returned to me / waiting for a CPA reviewer), Sources, Reconciliation, Review queue (+ "Returned to me"),
Counterparties, Trace, Reports (draft note while items are open; no whole-audit-trail download) and Rulebooks.
Agents, Guardrails, Audit trail, Data & security and People are hidden AND refused on the server (POL-02,
governance.read): /evidence, /trustops/proof, /audit-trail.*, /evidence/tamper-check, /actions/attempt, /v1/trust.
/agents stays open (the Reconciliation workflow card). Approve/Reject/Return are shown to the CPA reviewer only
(user decision 2026-11-12); the preparer proposes. The server still refuses a preparer decision (POL-06/POL-02).

## Notifications (2026-11-12, tests in tests/test_notifications.py)
Bell in the top bar. A proposal (exception.proposed) notifies CPA reviewers; approve/reject/return notify staff
preparers; your own actions never notify you. Read from the signed evidence log (GET /notifications), polled every
10 s while visible + on focus; a new item refreshes the run so both people's review queues stay in step. "Seen" is
per person per book on the server (POST /notifications/seen, stored in the book state as notif_seen).

## Proof on review items (2026-11-12, tests in tests/test_proof.py)
Both roles (`exception.attach`, policy set trustops-policy-2026.11.4) can attach proof to an OPEN/RETURNED review item:
PDF, PNG, JPG, XLSX, CSV, TXT, EML up to 10 MB; content must match the extension (magic bytes); exact duplicates
refused. Stored unopened as <id>.proof, SHA-256 fingerprinted, signed (exception.attachment_added), listed on the
card + review trail, downloadable as an attachment (nosniff, fingerprint re-checked). Carried to a re-run with the
case. Attaching notifies the other person (bell). Never used by reconciliation.

## Review feedback 2026-11-13 (tests in tests/test_feedback_1113.py)
Review items offer "Suggested comments" (fixed wording by role and case type; a click fills the note, sends nothing).
Every refused Google sign-in shows one plain message (GOOGLE_REFUSED); the page never shows a bare HTTP code.
Wrong files: refusal names the columns found and what is missing; sample templates (GET /v1/templates/{kind}.csv,
synthetic rows from the fixtures) are linked under the upload box and in the refusal. Sources only links to
Reconciliation (the run starts there). Same files are not run twice: Run is off with "Already reconciled as RUN-x";
a direct request returns that run flagged `reused` and the page does not replay it. No run button on Agents.
Success messages fade after 8 s; errors stay.

## Review feedback, second list (2026-11-13, tests in tests/test_feedback_1114.py)
Every CSV download starts with `Report, <name>` + Report ID, client, period, run, generated by/at (review CSV:
"Reconciliation Review Export", then a blank line, then the table). Every JSON download has a `header` object
(report_name, report_id, client, period, run_id, generated_by, generated_at); JSON reports download with a file
name (the on-screen ?preview=1 stays inline). The chat (Ask LedgerSight, AG-06) is now "FinTwin - Your Financial
Digital Twin" and opens with only "Hi, I'm FinTwin! How can I help you?" (question chips stay at the bottom).

## TrustOps integration (Naveen, merged 2026-11-13; tests in tests/test_trustops_integration.py)
With TRUSTOPS_URL + TRUSTOPS_KEY set (`trustops_client.configured()`), governance lives in TrustOps: Rulebooks,
Guardrails and Audit trail are hidden (Agents stays: user request 2026-11-13) (one "TrustOps ↗" link instead), approvals happen only in TrustOps (review items
show the TrustOps status + reviewer, no Approve/Reject), people are managed in TrustOps, Reports shows the finance
report + a "Governance record" card. Our UI follows it: `canOpen` refuses those pages (`TROPS_TABS`), the Overview
guide points to TrustOps, no suggested decision comments. Without the settings nothing changes (local demo, tests).
Feedback 2026-11-13 (tests/test_trustops_notifications.py): nothing is sent to TrustOps when a run completes. A person
(CPA reviewer or staff preparer) presses "Send to TrustOps" on a review item (POST .../exceptions/{id}/send-to-trustops,
optional note + "Send to" approver); each send is signed (`exception.sent_to_trustops`) and the other person's bell shows
it; approvals/rejections made in TrustOps reach both roles' bells (the bell poll syncs the latest run). Review item:
status bar ("Not sent yet" / "Waiting for approval · Sent to X · time", note sent, reviewer, refresh icon) above one
button row; "Approve in TrustOps" only for people listed as approvers in TrustOps. Agent cards are equal height.

## Action items 2026-11-16 (lead's list + LedgerSight<->TrustOps integration findings; tests in tests/test_action_items_1116.py)
FinTwin: only the conversation scrolls (min-height:0 flex body), header and question box stay put; `askScroll()` shows the
newest exchange (a long answer from its question down). Files under review work (decision, proposal, proof, item sent
to TrustOps: `Service.review_work`) can't be cleared by anyone, CPA reviewer included (403 DECISIONS_EXIST); /sources
says `locked` + why, Clear is disabled; a new period (Overview > Start next period / Data & security > Clear this
client's data) still works. One date format with TrustOps: "16 Nov 2026" / "16 Nov 2026, 14:05" (browser local time;
JS `fmtDate`/`fmtDateTime`; Python `reports.fmt_date`/`fmt_datetime`/`fmt_any`, files say UTC; CSV table rows keep ISO
dates, headers and XLSX/PDF use the format). GET /runs (newest first, filters day/month/by in UTC, review totals per run
and across runs: open/sent/approved/rejected/returned); Reports has a run picker (`S.repRunId`) and an All runs list;
Reconciliation has the Runs list with Day/Month/Run-by filters (local dates) and "Show this run" (`switchRun`); Overview
shows "Review items across all runs" with a run switcher. Review exports: one "Export reviews" card on Reports (View
review pack = pack.html page, Download CSV, Download signed pack JSON); the copies on Audit trail were removed.
Bulk send: checkboxes + Select all + "Send selected to TrustOps" (one approver, optional note) → POST
/runs/{id}/send-to-trustops {exception_ids,to,note}; each item is its own TrustOps decision and signed record;
decided/already-sent items are skipped and reported. The approver goes in the decide call (`assign_to`), no separate
assign; a TrustOps refusal (4xx {error,message}) is shown in its words ("could not be made the approver"). Nobody can
send or reassign an item to themself (422; approver lists hide you). Rejected rows: upload health has
`import_status`/`status_text` ("Imported with N of M rows rejected"); Import health lists them, GET
/uploads/{id}/rejected.csv (reason column), POST /uploads/{id}/acknowledge-rejections (signed); a run is refused (422)
until every file in use with rejected rows is fixed (re-upload replaces it) or acknowledged. TrustOps client: circuit
breaker (TRUSTOPS_BREAKER_SECONDS=15) after a connection failure, failed status lookups cached 10 s, sync_trustops
stops on the breaker and after 2 s; decisions stay fail-closed. FinTwin explains a TrustOps refusal (paused /
not authorised for this client / can't be reached / rule X) instead of the read-only text. PDF confirm: field
objects ({"value":...}) are reduced to their value; anything else is 422, never 500.
"Confirm & next file" keeps the card on screen (dimmed, `map-wait`) while the next file is read (`continueQueue`); the
page data refreshes after the next card is drawn, so the upload page no longer flashes between files.

## Feedback 2026-11-18 (tests in tests/test_feedback_1118.py; TrustOps tests/e2e/test_app_batch_1811.py)
Bulk send = ONE request to TrustOps (`trustops_client.decide_many` -> POST /v1/decisions/batch, 60 s timeout, TrustOps
saves its state once); before, 70+ separate calls each waited for a full state.json upload, some passed the 6 s timeout,
the breaker opened and the rest failed as "could not be reached". Every submit carries `reuse_pending`: TrustOps returns
the item still waiting for that resource instead of creating a second copy (lost answer + Try again). A TrustOps
without the batch endpoint (404/405) is asked item by item (`decide_each`; tests patch decide_many = decide_each).
UI: button "Send to TrustOps"; count says why others can't be sent (already sent / decided); after a send with
failures a dialog lists the not-sent/skipped items grouped by reason and the queue opens on a "Not sent" chip; the bell
text is "Not sent: case X did not reach TrustOps" + reason, and drops once the item was sent again. Locked sources text
is plain ("These files can't be cleared. N review items ... Start next period"). Run picker at the TOP of
Reconciliation, Review queue and Trace (`renderRunBars`, ids rc-/exc-/tr-runbar); "All runs" list folded under it on
Reconciliation. Pages follow the latest run unless the person picked an earlier one (`S.runPinned`). Trace takes
?run_id= (an earlier run uses the lines stored with it).

## Hosted storage in Cloud Storage (2026-11-16, tests in tests/test_cloud_sync.py)
Like TrustOps: with LEDGERSIGHT_STATE_GCS_URI=gs://bucket/prefix, `cloud_sync.start` restores an empty AVOCET_DATA_DIR
(db snapshot + uploaded files) before the Store opens and writes `owner.json`; `Store.on_change` marks it dirty and a
background thread uploads a SQLite backup snapshot (generation check) and mirrors files (new and deleted) about 1 s
later; flushed at exit. An older revision that sees another owner stops writing. /readyz: durable_storage +
cloud_storage. Unchanged snapshots are not uploaded again. Hosted (moved 2026-11-16 from us-east4 to europe-west1,
next to TrustOps): service ledgersight in europe-west1, bucket gs://trustops-uat-ledgersight-state-eu/ledgersight
(versioned, 30-day history), image repo europe-west1/ledgersight, domain ledgersight.cloudseals.com mapped there. The
signing key file is synced with the data (Secret Manager not set up). The old us-east4 service and its bucket
were deleted on 2026-11-16 after the move. Without the setting nothing changes.

## FinTwin as a normal chat (2026-11-16, tests in tests/test_fintwin_model.py)
User choice: "AI phrasing on exact data". Small talk (`query.smalltalk`: hi/hello/thanks/help/who are you/bye) is answered
without any model, status CHAT (no data badges in the chat); before a run it explains the next step (`NO_RUN_TEXT`, the
stale "Run Match first" is gone). Write/SQL/other-client questions are still refused by `query.classify` + the gate, and the
model is never called for them. With LEDGERSIGHT_ASSISTANT_MODEL=vertex and route `vertex:<model>` in
allowed_model_routes (`vertex:gemini-2.5-flash` added): `assistant_model.understand` sends only the question, intent list,
this run's ids/case ids/types (no amounts, no names) and the JSON reading is validated (`canonical_question` turns it into
the plain question the rules already answer); `assistant_model.phrase` words the exact result and `query.grounded` drops
it unless every number and ID is in that result (`ai_worded`, `answer_rules`; UI: "Written by AI from the records below").
One 8 s budget (LEDGERSIGHT_ASSISTANT_BUDGET); timeout/error = rules, only the error type is logged. The gate event carries
the model route and a signed `query.answered` event records what produced the answer. google-genai==3.3.0, Vertex mode,
service account, europe-west1. Data & security `ai_note` / `model_routes_in_use` follow the setting. Off = unchanged.

## Google sign-in role
New Google sign-ins get AVOCET_GOOGLE_DEFAULT_ROLE (preparer by default; owner_reviewer = CPA reviewer; anything else
falls back to preparer). Accounts already on file keep their role. Cloud Run is set to owner_reviewer for the demo.

## File intake (2026-11-09): upload only
No "Load All Files" button (removed by request). Files come from the device, a saved email, or Google
Drive/Sheets/Docs/Slides (needs GOOGLE_CLIENT_ID/GOOGLE_API_KEY, see .env.example). After bank + ledger are imported,
Sources offers "Run reconciliation →". Clear buttons are off when there is nothing to clear; a pending upload whose
data was cleared (e.g. by a colleague) is dropped from the page. Uploads use kind=auto: `ingest.detect_kind` decides the type from columns + a dry-run import (file name
is only a tie-break hint); anything else is refused with the columns found and what was missing. Non-table PDFs go
to receipt/invoice intake. Unsupported formats are refused in the UI (no longer "held"). The /samples API answers
only when AVOCET_SHOW_SAMPLES=1, which only tests/conftest.py sets (launch.json and cloudrun.sh set 0).
Presenter upload pack: demo_files/0_client1_october (generated by make_fixtures; includes one file that must be refused).

## Gaps vs engineering spec (original list, now built)
1. TrustOps decisions: policy only returns ALLOW/DENY. Add ESCALATE + policy IDs/versions
   (e.g. POL-03 READ_ONLY -> DENY ledger.post, POL-04 REVIEW_DUPLICATE, POL-05 MISSING_GL -> ESCALATE,
   required role CPA reviewer). Agent steps currently log "ALLOW:OK" without calling `evaluate` —
   gate agents as actors (agent journal-post attempt must be DENY and logged).
2. Workflow Run timeline: `service.run()` logs one event. Record per-agent steps
   (AG-01 Intake, AG-02 Mapper, AG-03 Recon, AG-04 Exception triage, AG-05 Report, AG-06 Query)
   with trace id, rows in/out, policy decision, duration. New UI panel.
3. Approvals: add RETURN alongside ACCEPT/REJECT. Preparer clicking approve currently gets
   ROLE_NOT_AUTHORISED; demo story expects MAKER_CHECKER — make the reason shown match.
4. Two reports sharing run_id: Finance Reconciliation Report (LedgerSight) and
   TrustOps Governance Report (agents, policy versions, ALLOW/ESCALATE/DENY, approvals, verification).
5. Ask LedgerSight: read-only, intent-limited Q&A (unmatched items, why X flagged, outstanding on X,
   who approved X) with source citations; refuse cross-client, SQL, write requests without revealing
   whether other-client data exists.
6. Admin Rulebooks (read-only policy list) + source status labels
   (VERIFIED_UPLOAD / LIVE_CONNECTOR / SIMULATED / PLANNED; QuickBooks/Xero = PLANNED).

## Built 2026-11-09: Google sign-in (tests in tests/test_google_login.py)
`POST /v1/login/google` verifies a Google ID token (google-auth, same pattern as cloudseals-trustops-core's
`security.py`); unverified or wrong-domain emails are rejected (`AVOCET_GOOGLE_LOGIN_DOMAIN`, reuses the
existing `GOOGLE_CLIENT_ID`). Any verified account on that domain is let in: a never-seen email is
auto-provisioned `AVOCET_GOOGLE_DEFAULT_ROLE` (see above); an email already on file (bootstrap owner, or
invited via `/v1/invites`) keeps whatever role it was given there instead. `/v1/auth-config` exposes
`google_login`/`google_client_id`; the login screen shows a "Sign in with Google" button only when both are
configured.

## Known setup issues
- `run.sh` is Linux-only; use the VS Code launch config on Windows.
- `pdf_worker.py` resource limits are skipped on Windows; PDF subprocess gets a minimal env — test PDF upload on Windows.

## Rules
No client names or addresses in code or sample data. Clients come from `config/clients.json`
(default "Client 1" / "Client 2", renamable in Overview; ids client-a / client-b); counterparties are generic (Customer 01, Print Supplier...).
Edit the CSVs in `fixtures/`, then `python -m ledgersight.make_fixtures` to rebuild QBO/TSV/XLSX/PDF/EML.
A test (`test_no_client_names_or_addresses_in_code_or_sample_data`) fails if old names come back.
Never claim a feature works without a passing test or a manual check. Mark unbuilt items "in development".
Never hardcode demo results — compute from uploaded files.
