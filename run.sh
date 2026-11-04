#!/usr/bin/env bash
# LedgerSight for Avocet CPA - local demo. Opens on http://127.0.0.1:8090
# Secrets come from .env (copy .env.example to .env and set your own values).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: creating a private Python environment in .venv (one-time, ~1 min)..."
  python3 -m venv .venv
fi
PY=.venv/bin/python
$PY -m pip install -q --upgrade pip >/dev/null
$PY -m pip install -q -r requirements.txt
if [ -f .env ]; then set -a; . ./.env; set +a; fi
export AVOCET_SECURE_COOKIE=0
export AVOCET_CLIENTS=${AVOCET_CLIENTS:-client-a}   # one client book for the demo
export AVOCET_REQUIRE_MFA=${AVOCET_REQUIRE_MFA:-0}   # local demo: password only. Set 1 for hosted/shared use.
export AVOCET_BOOTSTRAP_USER=${AVOCET_BOOTSTRAP_USER:-presenter}
export AVOCET_BOOTSTRAP_NAME=${AVOCET_BOOTSTRAP_NAME:-"Administrator"}
export AVOCET_REGION_DECLARED=${AVOCET_REGION_DECLARED:-"this computer (local)"}
export AVOCET_DATA_DIR=${AVOCET_DATA_DIR:-"$PWD/.ledgersight-data"}
echo ""
echo "  Open:      http://127.0.0.1:8090"
echo "  User:      $AVOCET_BOOTSTRAP_USER"
[ -n "$AVOCET_BOOTSTRAP_PASSWORD" ] || echo "  (No AVOCET_BOOTSTRAP_PASSWORD set: a one-time invite link is printed below instead.)"
echo ""
exec $PY -m uvicorn ledgersight.app:app --host 127.0.0.1 --port 8090 --workers 1
