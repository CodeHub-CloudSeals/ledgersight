#!/usr/bin/env bash
# Deploy LedgerSight to Google Cloud Run (US region), with login + MFA on and secrets in Secret Manager.
#
#   ./deploy/cloudrun.sh <GCP_PROJECT_ID> [REGION]        default REGION = us-east4
#
# Re-running is safe: it redeploys and keeps the existing secrets.
#
# IMPORTANT (state): LedgerSight keeps its SQLite database and audit trail in /data. On Cloud Run that
# folder lives inside the single running instance, so the service is pinned to exactly 1 instance that
# stays warm. Data survives normal use but is LOST when you redeploy or Google restarts the instance.
# Fine for a demo / sandbox with sample data; for a pilot, move storage to Cloud SQL or a Filestore volume.
set -euo pipefail

PROJECT="${1:?usage: ./deploy/cloudrun.sh <GCP_PROJECT_ID> [REGION]}"
REGION="${2:-us-east4}"
SERVICE="ledgersight"
SA="ledgersight-run@${PROJECT}.iam.gserviceaccount.com"
cd "$(dirname "$0")/.."

command -v gcloud >/dev/null || { echo "gcloud not found. Install: https://cloud.google.com/sdk/docs/install  (or: brew install --cask google-cloud-sdk)"; exit 1; }
gcloud config set project "$PROJECT" >/dev/null
gcloud auth print-access-token >/dev/null 2>&1 || gcloud auth login

echo "==> Enabling APIs (one-time)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com >/dev/null

# Python with 'cryptography' to generate the signing key and authenticator secret locally.
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
.venv/bin/python -m pip install -q cryptography >/dev/null

new_secret() {  # name, value
  if gcloud secrets describe "$1" >/dev/null 2>&1; then echo "    $1: exists (kept)"; return 1; fi
  printf '%s' "$2" | gcloud secrets create "$1" --replication-policy=automatic --data-file=- >/dev/null
  echo "    $1: created"
}

echo "==> Secrets (Secret Manager)"
PW="$(.venv/bin/python -c 'import secrets;print(secrets.token_urlsafe(18))')"
TOTP="$(.venv/bin/python -c 'import base64,secrets;print(base64.b32encode(secrets.token_bytes(20)).decode().rstrip("="))')"
PEM="$(.venv/bin/python -c 'from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K;from cryptography.hazmat.primitives import serialization as s;print(K.generate().private_bytes(s.Encoding.PEM,s.PrivateFormat.PKCS8,s.NoEncryption()).decode())')"
CREATED_PW=0
new_secret ledgersight-admin-password "$PW" && CREATED_PW=1 || true
new_secret ledgersight-admin-totp "$TOTP" || true
new_secret ledgersight-signing-key "$PEM" || true

echo "==> Service account with read access to those secrets only"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gcloud iam service-accounts create ledgersight-run --display-name="LedgerSight Cloud Run" >/dev/null
for s in ledgersight-admin-password ledgersight-admin-totp ledgersight-signing-key; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
done

echo "==> Building and deploying to Cloud Run ($REGION)"
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA" \
  --allow-unauthenticated \
  --min-instances 1 --max-instances 1 --concurrency 40 \
  --cpu 1 --memory 1Gi --no-cpu-throttling \
  --timeout 120 \
  --set-env-vars "^@^AVOCET_REQUIRE_MFA=1@AVOCET_SECURE_COOKIE=1@AVOCET_NO_LOGIN=0@AVOCET_SHOW_SAMPLES=0@AVOCET_DATA_DIR=/data@AVOCET_BOOTSTRAP_USER=presenter@AVOCET_BOOTSTRAP_NAME=CloudSeals Presenter@AVOCET_REGION_DECLARED=Google Cloud ${REGION} (Cloud Run; app, data and logs in this region)" \
  --set-secrets "^@^AVOCET_BOOTSTRAP_PASSWORD=ledgersight-admin-password:latest@AVOCET_BOOTSTRAP_TOTP=ledgersight-admin-totp:latest@AVOCET_SIGNING_KEY_PEM=ledgersight-signing-key:latest"

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo
echo "================================================================"
echo " LedgerSight is live:  $URL"
echo " Health:               $URL/readyz"
echo " User:                 presenter"
if [ "$CREATED_PW" = 1 ]; then
  echo " Password:             $PW"
  echo " Authenticator key:    $TOTP   (Google Authenticator -> Enter a setup key)"
else
  echo " Password / key:       gcloud secrets versions access latest --secret=ledgersight-admin-password"
  echo "                       gcloud secrets versions access latest --secret=ledgersight-admin-totp"
fi
echo "================================================================"
