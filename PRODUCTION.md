# LedgerSight + TrustOps: production readiness

Status as of 2026-11-12 (v1.0.0, after the readiness audit and the architecture review). "Done" means covered by an automated test
(`python -m pytest -q` plus the browser checks in `tests/e2e/`).

## Production-grade now

| Area | What is in place |
|---|---|
| Storage | SQLite database + file store in `AVOCET_DATA_DIR`. **Hosted (since 2026-11-16): synced to a private, versioned Cloud Storage bucket** (`LEDGERSIGHT_STATE_GCS_URI`, `ledgersight/cloud_sync.py`, the same approach as TrustOps): an empty data folder is restored from the bucket at start-up, every change is uploaded within about a second, and the last changes are flushed on shutdown, so redeploys and restarts keep the data. The database object is written with a generation check and an owner record, so an older revision still running during a rollout stops writing. `/readyz` shows `durable_storage` and `cloud_storage` (restored objects, last sync, error). Without the setting, Cloud Run's data folder is temporary and is cleared on every redeploy or restart |
| Audit trail | Ed25519-signed, hash-chained, append-only; safe under concurrent requests; a cleared client's trail is archived, never deleted |
| Signing key | From the secret store (`AVOCET_SIGNING_KEY_PEM`), else a key file kept with the data (owner-only permissions) |
| Governance | Deny-by-default policy engine (POL-01…12) for people and agents; ALLOW / ESCALATE / DENY with policy id and version on every record |
| Access | Invitation-only sign-in, password + single-use authenticator code, optional Google sign-in for one verified domain (`AVOCET_GOOGLE_LOGIN_DOMAIN`, reviewer allow-list `AVOCET_GOOGLE_REVIEWERS`), session and account expiry, revocation, CSRF header, per-client scope that hides other clients. Lockouts and used codes survive restarts; sign-in is rate-limited per client address (behind Cloud Run the address the front end appends, `AVOCET_TRUST_PROXY=1`) |
| File safety | Type and size checks, macro/archive/linked-workbook refusal, formulas never run, PDFs parsed in an isolated process, prompt-injection text flagged |
| Operations | `/healthz` (liveness + version), `/readyz` (storage, signer, config), `X-Request-ID` on every response, storage errors logged, Docker image with a data volume and readiness health check |
| Configuration | Clients, policies, agents, sources and matching thresholds in `ledgersight/config/*.json`; no client names in code (a test enforces it) |

## Local-only settings (never on a shared server)

`AVOCET_NO_LOGIN=1` removes the sign-in screen on a laptop. It is ignored unless MFA is off and secure cookies are off.
Files are uploaded from the device, a saved email, or Google Drive / Sheets / Docs / Slides (set `GOOGLE_CLIENT_ID`
and `GOOGLE_API_KEY`). There is no built-in loader in the app. Every uploaded file's type is worked out from its
content; anything that is not a recognised finance record is refused with the reason. Demo files to upload:
`demo_files/0_client1_october`. `AVOCET_SHOW_SAMPLES=1` only enables the fixture API used by automated tests.

## Not production-ready yet (in development)

| Gap | Why it matters | Next step |
|---|---|---|
| Single process | State is held by one server process; it cannot scale out to several | Move state to PostgreSQL; run several workers |
| Real client data | Only synthetic data is accepted (POL-07) | Data-processing approval, encryption at rest, retention policy |
| Live connectors | QuickBooks/Xero are PLANNED | Build and test OAuth connectors (read-only) |
| Scanned documents | Image-only PDFs are refused | OCR intake with human confirmation |
| Single sign-on | Local accounts, plus Google sign-in for one Workspace domain | SAML/OIDC for firms on other identity providers |
| Durable hosted storage | Done with Cloud Storage sync (above). A change made in the last second before a crash (not a normal shutdown) can be lost | Postgres when several instances are needed |
| Backups | `python -m ledgersight.backup <folder>` makes a consistent online copy (database + files); not yet scheduled | Schedule it (e.g. Cloud Scheduler job) and test a restore |
| Deploys | During a rollout the new revision restores the data and takes ownership; anything the old revision writes after that moment is not kept | Deploy when idle to be safe |
| Monitoring | JSON request logs with request id on stdout (Cloud Logging); startup/liveness probes on /readyz and /healthz | Alerts on /readyz failures and error rates |
| Independent review | Security controls are self-tested | External penetration test before client data |
| Policy editing | Rulebooks are read-only in the app | Versioned policy publishing with approval |
