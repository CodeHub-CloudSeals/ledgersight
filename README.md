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

1. **Import**: computer, saved email (.eml) or Google Drive (optional). CSV, Excel, TSV, OFX/QFX/QBO, text PDF.
2. **Agent 1 Intake**: type/size/macro/formula checks, control totals, SHA-256 fingerprint.
3. **Agent 2 Mapping / Agent 3 Document**: propose columns / read PDF fields with cited source lines. ✋ Person confirms.
4. **Agent 4 Reconciliation** (core): exact rules, abstains when unsure, typed exceptions.
5. **Agent 5 Exception**: reason + suggested next step. ✋ Owner/reviewer accepts or rejects (maker ≠ checker).
6. **Agent 6 Counterparty**: links transactions to customers/vendors with evidence. ✋ Person confirms weak links.
7. **Trace / Audit trail / Review pack**: per-transaction lineage, Ed25519 hash-chained evidence, offline verifier.

TrustOps gate (`ledgersight/core/policy.py`): deny-by-default; forbidden actions, client scope, role,
data class, model route and maker-checker are checked before every action; ALLOW and DENY are both signed.

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
python -m pytest -q
```

## Optional settings

| Variable | Purpose |
|---|---|
| `AVOCET_REQUIRE_MFA` | `1` = password + authenticator code (default), `0` = password only |
| `AVOCET_CLIENTS` | comma list of client books to show, e.g. `client-a` |
| `AVOCET_SIGNING_KEY_PEM` | Ed25519 private key from the secret store (otherwise generated at start-up) |
| `AVOCET_REGION_DECLARED` | region shown on the Data & security page |
| `GOOGLE_CLIENT_ID`, `GOOGLE_API_KEY` | enable the Google Drive picker (read-only, drive.file scope) |
