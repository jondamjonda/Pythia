#!/usr/bin/env bash
# run_graph_ui.sh — start (or restart) the local Nexus graph UI.
#
# Brings up BOTH pieces the diagram needs and leaves them running detached:
#   • FastAPI backend on http://127.0.0.1:8002  (reads PYTHIA_DATABASE_URL from secrets.env →
#     the read-only `pythia` corpus the graph is built from)
#   • Next.js dev server on http://127.0.0.1:3000  (serves /control/graph)
#
# Idempotent: a service already listening is left alone, so re-running this is safe.
# Logs: ./logs/backend.local.log and ./logs/frontend.local.log
#
# FLAGS (LEDGER-runtime-ops-08): this backend is started with NO product flags and the
# DEFAULT (owner) DATABASE_URL — it exists ONLY to serve the read-only /control/graph
# view from PYTHIA_DATABASE_URL. It is NOT a substitute for run_local.sh / run_everything.sh
# (those export the green-gate + RLS/tenancy flags and connect as regsuper_app on :8000/:8002).
# Because it binds :8002 — the SAME port run_everything.sh uses — do NOT run both at once:
# whichever is already listening wins (idempotent skip), so a stale graph-only backend would
# shadow the flagged stack. Stop this one before launching the flagged stack on :8002.
#
# SIDECAR PORT (LEDGER-runtime-ops-11): the Pythia/Nexus engine SIDECAR runs on :8099
# (see run_everything.sh / run_cutover.sh). It is a DIFFERENT process from this :8002 app
# backend; references to :9000 elsewhere are stale doc drift — the canonical sidecar port is 8099.
#
# Usage:  ./scripts/local/run_graph_ui.sh        then open  http://localhost:3000/control/graph
set -uo pipefail
cd "$(dirname "$0")/../.."
mkdir -p logs

ok() { curl -s -o /dev/null --max-time 2 "$1" 2>/dev/null; }

# ── backend :8002 ──────────────────────────────────────────────────────────────────────────────────
if ok http://127.0.0.1:8002/health; then
  echo "backend  :8002  already up"
else
  echo "backend  :8002  starting…"
  nohup .venv/bin/uvicorn regsuper.api.main:app --host 127.0.0.1 --port 8002 \
    > logs/backend.local.log 2>&1 &
  disown
fi

# ── frontend :3000 ─────────────────────────────────────────────────────────────────────────────────
if ok http://127.0.0.1:3000; then
  echo "frontend :3000  already up"
else
  echo "frontend :3000  starting…"
  ( cd frontend && nohup npm run dev > ../logs/frontend.local.log 2>&1 & disown )
fi

# ── wait for both to answer ──────────────────────────────────────────────────────────────────────────
printf "waiting for readiness "
for _ in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 http://127.0.0.1:3000/control/graph 2>/dev/null || true)
  if ok http://127.0.0.1:8002/health && printf '%s' "$code" | grep -qE '200|307|308'; then
    echo
    echo "READY  →  http://localhost:3000/control/graph"
    exit 0
  fi
  printf "."
  sleep 1
done
echo
echo "still starting — tail logs/backend.local.log and logs/frontend.local.log to see why"
exit 1
