#!/usr/bin/env bash
# run_aq_eval.sh — CP3: the OFF-vs-ON answer-quality eval on gpt-5.5. STAGED, ready to run
# THE MOMENT OpenAI billing is restored (CP2). Until then it will 500 (429 insufficient_quota).
#
# It runs BOTH arms over the full 70-item gold on an isolated engine (:8091 — your :8099/:8002 stack
# untouched), on openai/gpt-5.5, then diffs them. This is the first TRUSTWORTHY answer-quality number.
#
#   ./scripts/local/run_aq_eval.sh            → OFF-arm → ON-arm → --compare  (full 70; ~needs billing)
#   ./scripts/local/run_aq_eval.sh 8          → quick 8-item smoke first (recommended: confirm gpt-5.5 answers, not 429)
#
# NOTE: the metagate coverage bottleneck (CP1) is the ollama LLM support-judge; on gpt-5.5 the judge
# should return "supported" → covered → full answers. THIS eval is what proves (or disproves) that.
set -u
cd "$(dirname "$0")/../.."; APP="$(pwd)"; ENG="$APP/../engine"; LIM="${1:-0}"
LIMFLAG=""; [ "$LIM" != "0" ] && LIMFLAG="--limit $LIM"
KEY="$(cd "$APP" && "$APP/.venv/bin/python" -c "from regsuper.config import get_settings; print(get_settings().openai_api_key or '')")"
[ -n "$KEY" ] || { echo "No OpenAI key in secrets.env — aborting."; exit 1; }

start_engine() {   # $1 = arm label; sets the ON/OFF flags
  pkill -f "uvicorn pythia.service:app --host 127.0.0.1 --port 8091" 2>/dev/null; sleep 2
  local on=""; [ "$1" = "on" ] && on="PYTHIA_INTAKE_V2_MODE=active PYTHIA_STRUCTURED_ANSWER_ENABLED=1 \
    PYTHIA_CTX_WINDOW_ENABLED=1 PYTHIA_EVIDENCE_BUNDLE_ENABLED=1 PYTHIA_TERM_NORM_ENABLED=1"
  local shadow=""; [ "$1" = "off" ] && shadow="PYTHIA_INTAKE_V2_MODE=shadow"
  ( cd "$ENG" && env PYTHIA_DATABASE_URL="postgresql+asyncpg://regsuper:regsuper@localhost:5432/pythia" \
      PYTHIA_OPENAI_API_KEY="$KEY" PYTHIA_SYNTHESIZER_PROVIDER=openai \
      PYTHIA_METAGATE_ENABLED=1 PYTHIA_METAGATE_MODE=shadow PYTHIA_INTAKE_V2_ENABLED=1 $shadow $on \
      nohup "$ENG/.venv/bin/uvicorn" pythia.service:app --host 127.0.0.1 --port 8091 \
      > "$ENG/logs/eval_engine.log" 2>&1 < /dev/null & )
  for i in $(seq 1 60); do curl -s -m 3 http://127.0.0.1:8091/health | grep -q '"status"' && break; sleep 1; done
}

cd "$ENG"; mkdir -p eval/artifacts
echo "### CP3 gpt-5.5 eval — OFF arm ###"; start_engine off
.venv/bin/python eval/aq_answer_eval.py --arm off $LIMFLAG --endpoint http://127.0.0.1:8091/v1/answer --out eval/artifacts/gpt5_off.json
echo "### ON arm ###"; start_engine on
.venv/bin/python eval/aq_answer_eval.py --arm on  $LIMFLAG --endpoint http://127.0.0.1:8091/v1/answer --out eval/artifacts/gpt5_on.json
echo "### DELTA ###"; .venv/bin/python eval/aq_answer_eval.py --compare eval/artifacts/gpt5_off.json eval/artifacts/gpt5_on.json
pkill -f "uvicorn pythia.service:app --host 127.0.0.1 --port 8091" 2>/dev/null
echo "Artifacts: eval/artifacts/gpt5_off.json, gpt5_on.json. Numbers are DIRECTIONAL until a lawyer certifies the gold (CP4)."
