#!/usr/bin/env bash
# usage: serve.sh PORT DATADIR [login]   - an isolated LedgerSight server for the browser tests
cd "$(dirname "$0")/../.."
rm -rf "$2"; mkdir -p "$2"
PY=".venv/Scripts/python"; [ -x "$PY" ] || PY=".venv/bin/python"
export AVOCET_SECURE_COOKIE=0 AVOCET_REQUIRE_MFA=0 AVOCET_SHOW_SAMPLES=0 AVOCET_CLIENTS=client-a AVOCET_BOOTSTRAP_USER=presenter \
  AVOCET_BOOTSTRAP_NAME=Administrator AVOCET_REGION_DECLARED="this computer (local)" AVOCET_DATA_DIR="$2"
if [ "$3" = "login" ]; then export AVOCET_BOOTSTRAP_PASSWORD="E2e-Test-Password-42" AVOCET_BOOTSTRAP_TOTP="JBSWY3DPEHPK3PXP"; else export AVOCET_NO_LOGIN=1; fi
exec "$PY" -m uvicorn ledgersight.app:app --host 127.0.0.1 --port "$1" --workers 1
