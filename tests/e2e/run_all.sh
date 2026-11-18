#!/usr/bin/env bash
# Browser end-to-end tests: fresh isolated servers on 8091 (no-login demo) and 8092 (login), run, stop.
# usage: tests/e2e/run_all.sh [shots_dir]      (E2E_PY = python with playwright; default: the project .venv)
D="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$D/../.." && pwd)"
TMP="$(mktemp -d)"
PY="${E2E_PY:-$ROOT/.venv/Scripts/python}"; [ -x "$PY" ] || PY="${E2E_PY:-$ROOT/.venv/bin/python}"
stop() { for port in 8091 8092; do
  if command -v powershell >/dev/null 2>&1; then powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force }";
  else fuser -k $port/tcp 2>/dev/null; fi; done; }
stop; sleep 1
bash "$D/serve.sh" 8091 "$TMP/data-8091" > "$TMP/srv-8091.log" 2>&1 &
bash "$D/serve.sh" 8092 "$TMP/data-8092" login > "$TMP/srv-8092.log" 2>&1 &
for port in 8091 8092; do for i in $(seq 1 40); do curl -s -m 1 http://127.0.0.1:$port/healthz >/dev/null && break; sleep 0.5; done; done
"$PY" "$D/e2e.py" "${1:-$TMP/shots}"; rc=$?
stop; rm -rf "$TMP/data-8091" "$TMP/data-8092"
exit $rc
