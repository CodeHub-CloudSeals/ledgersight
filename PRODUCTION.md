# LedgerSight + TrustOps: production readiness

Status as of 2026-11-04 (v1.0.0). "Done" means covered by an automated test (`python -m pytest -q`, 107 tests).

## Production-grade now

| Area | What is in place |
|---|---|
| Storage | SQLite database + file store in `AVOCET_DATA_DIR`. Client books, uploads, runs, decisions and users survive restarts |
| Audit trail | Ed25519-signed, hash-chained, append-only; safe under concurrent requests; a cleared client's trail is archived, never deleted |
| Signing key | From the secret store (`AVOCET_SIGNING_KEY_PEM`), else a key file kept with the data (owner-only permissions) |
| Governance | Deny-by-default policy engine (POL-01…10) for people and agents; ALLOW / ESCALATE / DENY with policy id and version on every record |
| Access | Invitation-only sign-in, password + authenticator code, session expiry, account expiry, revocation, CSRF header, per-client scope that hides other clients |
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
| Single sign-on | Local accounts only | SSO (SAML/OIDC) for firms |
| Backups | Data directory must be backed up by the host | Scheduled backup + tested restore |
| Monitoring | Logs go to the console | Central logging, metrics and alerts |
| Independent review | Security controls are self-tested | External penetration test before client data |
| Policy editing | Rulebooks are read-only in the app | Versioned policy publishing with approval |
