# LedgerSight

Bank-to-ledger reconciliation workspace for accounting firms, governed by TrustOps.
Rule-based agents check, map, match and explain; a person approves every decision;
every step is signed into a tamper-evident audit trail. Read-only: nothing is posted to
any ledger or bank.

## Run locally

```bash
./run.sh            # creates .venv on first run, then serves http://127.0.0.1:8090
```
First copy `.env.example` to `.env` and set your own administrator password and authenticator secret
(`.env` is never committed). On Windows/VS Code, press F5 ("LedgerSight (local)"): it reads `.env` and opens
without a login screen (`AVOCET_NO_LOGIN=1`, local only). Test data and a walkthrough are in `demo_files/`.
Local mode turns the authenticator-code step off (`AVOCET_REQUIRE_MFA=0`); hosted use must set it to `1`.

## Flow

1. **Import**: computer, saved email (.eml) or Google Drive (optional). CSV, Excel, TSV, JSON, OFX/QFX/QBO, text PDF.
   The file's type is worked out from its content; anything that is not a finance record is refused with the reason.
2. **AG-01 Intake & Validation**: type/size/macro/formula checks, control totals, SHA-256 fingerprint; reads text-PDF
   receipts and invoices with cited source lines. ✋ Person confirms PDF fields.
3. **AG-02 Canonical Mapper**: proposes column mappings and checks every row is in the standard format. ✋ Person confirms.
4. **AG-03 Reconciliation** (core): fixed, explainable rules; abstains when unsure; typed exceptions. Cannot post anything.
5. **AG-04 Exception Triage**: asks TrustOps how each exception must be handled (ALLOW / ESCALATE / DENY) and routes it.
   ✋ A preparer proposes; a CPA reviewer who did not propose it approves, rejects or returns it (maker-checker, POL-06).
6. **AG-05 Report Composer**: Finance Reconciliation Report + TrustOps Governance Report for the same run (PDF/XLSX/CSV/JSON).
7. **AG-06 Query Assistant**: Ask LedgerSight, read-only questions with citations; other clients, SQL and write requests refused.
8. **Counterparties / Trace / Audit trail / Review pack**: links to customers/vendors with evidence, per-transaction lineage,
   Ed25519 hash-chained evidence, offline verifier (`python -m ledgersight.verify_pack`).

The six agents and their tools are configured in `ledgersight/config/agents.json`; policies POL-01…12 in
`config/policies.json`. TrustOps gate (`ledgersight/core/policy.py`): deny-by-default; forbidden actions, client scope,
role, data class, model route and maker-checker are checked before every action; ALLOW, ESCALATE and DENY are all signed.

## Layout

```
ledgersight/
  app.py           FastAPI routes, auth cookies, security headers
  auth.py          invitations, passwords, TOTP MFA, sessions, expiry, revocation
  service.py       client workspaces, import, runs, review, counterparties, trace, evidence
  pdf_worker.py    isolated, resource-limited text-PDF extractor
  verify_pack.py   offline verifier for exported review packs
  core/            ingest, recon (engine), policy (gate), evidence (signer)
  static/          UI
  fixtures/        synthetic sample files (generic names; regenerate derived files with `python -m ledgersight.make_fixtures`)
tests/             acceptance tests (G-D01..G-D12 + negative paths)
```

## Test

```bash
pip install -r requirements-dev.txt     # runtime pins (requirements.txt) + test tools
python -m pytest -q                     # API, engine, governance and security tests
bash tests/e2e/run_all.sh               # browser checks incl. DESIGN_RULES.md (needs Playwright, see tests/e2e/README.md)
```

## Optional settings

| Variable | Purpose |
|---|---|
| `AVOCET_REQUIRE_MFA` | `1` = password + authenticator code (default), `0` = password only |
| `AVOCET_CLIENTS` | comma list of client books to show, e.g. `client-a` |
| `AVOCET_SIGNING_KEY_PEM` | Ed25519 private key from the secret store (otherwise generated at start-up) |
| `AVOCET_REGION_DECLARED` | region shown on the Data & security page |
| `AVOCET_DATA_DIR`, `AVOCET_PERSIST` | where the SQLite database, uploads and signing key live; `AVOCET_PERSIST=0` = memory only |
| `GOOGLE_CLIENT_ID`, `GOOGLE_API_KEY` | enable the Google Drive picker (read-only, drive.file scope) |
| `AVOCET_GOOGLE_LOGIN_DOMAIN` | with `GOOGLE_CLIENT_ID`: "Sign in with Google" for verified accounts on this one domain |
| `AVOCET_GOOGLE_DEFAULT_ROLE` | role for a first-time Google sign-in: `preparer` (default) or `owner_reviewer`; accounts already on file keep theirs |
| `AVOCET_GOOGLE_REVIEWERS` | comma list of emails; when set, only these Google accounts become CPA reviewers, everyone else a preparer |
| `AVOCET_TRUST_PROXY` | `1` behind Cloud Run / a trusted load balancer: rate-limit sign-in by the client address the proxy appends to `X-Forwarded-For` (set by `deploy/cloudrun.sh`; leave unset locally) |
| `AVOCET_PUBLIC_URL` | base URL used in invitation links |
| `AVOCET_STORAGE_DURABLE` | `1` only when the data folder is on durable storage (shown on Data & security and `/readyz`) |

Design rules for the UI are in `DESIGN_RULES.md`; production status in `PRODUCTION.md`.
