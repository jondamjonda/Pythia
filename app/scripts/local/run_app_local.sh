#!/usr/bin/env bash
# run_app_local.sh — start the WHOLE Polarius app locally (engine + backend + frontend) in the background,
# so you can open it in your browser. Run this on your OWN Mac — the servers keep running after the script
# exits, and stay up across Claude Code sessions (unlike anything Claude launches, which its sandbox kills).
#
#   Start:   ./scripts/local/run_app_local.sh          → brings all three up, prints the URL to open
#   Stop:    ./scripts/local/run_app_local.sh stop     → stops all three
#   Logs:    ./logs/engine.log · ./logs/backend.log · ./logs/frontend.log
#
# The UI defaults to port 3001 (port 3000 is often taken by the "Běh na Dvě Promile" project). Override with
# POLARIUS_UI_PORT=3005 ./scripts/local/run_app_local.sh
#
# NOTE: intentionally NOT using `set -e` — the readiness loops below poll with curl, which returns non-zero
# until each server is up; under `set -e` that first failed poll would abort the script (exit 7). We use
# `set -u` only and let the loops tolerate the expected transient failures.
set -u
cd "$(dirname "$0")/../.."
APP="$(pwd)"
ENG="$APP/../engine"
UI_PORT="${POLARIUS_UI_PORT:-3001}"
LOG="$APP/logs"; mkdir -p "$LOG"

if [ "${1:-}" = "stop" ]; then
  echo "Stopping Polarius (engine :8099 · backend :8002 · frontend :$UI_PORT) …"
  pkill -f "uvicorn pythia.service" 2>/dev/null || true
  pkill -f "uvicorn regsuper.api.main" 2>/dev/null || true
  pkill -f "next dev -p $UI_PORT" 2>/dev/null || true
  echo "Stopped."
  exit 0
fi

# 1) ENGINE — the Pythia sidecar (GPT-5.5 synthesis + metagate shadow). The OpenAI key is read from the app
#    config and stays local (never printed). Skip if already up.
if curl -s -m 3 http://127.0.0.1:8099/health | grep -q '"status"'; then
  echo "Engine already up on :8099."
else
  KEY="$("$APP/.venv/bin/python" -c "from regsuper.config import get_settings; print(get_settings().openai_api_key or '')")"
  [ -n "$KEY" ] || echo "  ⚠ No OpenAI key in config — the answer feature will fall back / not synthesize."
  echo "Starting engine on :8099 …"
  ( cd "$ENG" && PYTHIA_DATABASE_URL="postgresql+asyncpg://regsuper:regsuper@localhost:5432/pythia" \
      PYTHIA_OPENAI_API_KEY="$KEY" PYTHIA_SYNTHESIZER_PROVIDER=openai \
      PYTHIA_METAGATE_ENABLED=1 PYTHIA_METAGATE_MODE=shadow \
      PYTHIA_INTAKE_V2_ENABLED=1 PYTHIA_INTAKE_V2_MODE=shadow \
      nohup "$ENG/.venv/bin/uvicorn" pythia.service:app --host 127.0.0.1 --port 8099 \
      > "$LOG/engine.log" 2>&1 < /dev/null & )
  for _ in $(seq 1 60); do curl -s -m 3 http://127.0.0.1:8099/health | grep -q '"status"' && break; sleep 1; done
fi

# 2) BACKEND — full posture (run_everything.sh execs uvicorn on :8002; it checks the engine first).
if curl -s -m 3 -o /dev/null http://127.0.0.1:8002/health; then
  echo "Backend already up on :8002."
else
  echo "Starting backend on :8002 …"
  nohup ./scripts/local/run_everything.sh > "$LOG/backend.log" 2>&1 < /dev/null &
  for _ in $(seq 1 60); do curl -s -m 3 -o /dev/null http://127.0.0.1:8002/health && break; sleep 1; done
fi

# 3) FRONTEND — the Next.js UI on $UI_PORT.
if curl -s -m 3 -o /dev/null "http://127.0.0.1:$UI_PORT/"; then
  echo "Frontend already up on :$UI_PORT."
else
  echo "Starting frontend on :$UI_PORT …"
  ( cd "$APP/frontend" && nohup npm run dev -- -p "$UI_PORT" > "$LOG/frontend.log" 2>&1 < /dev/null & )
  for _ in $(seq 1 90); do curl -s -m 3 -o /dev/null "http://127.0.0.1:$UI_PORT/" && break; sleep 1; done
fi

echo
echo "✅ Polarius is up.  Open:  http://localhost:$UI_PORT   (or http://127.0.0.1:$UI_PORT)"
echo "   engine :8099 · backend :8002 · frontend :$UI_PORT · logs in ./logs"
echo "   Stop everything later with:  ./scripts/local/run_app_local.sh stop"
exit 0
