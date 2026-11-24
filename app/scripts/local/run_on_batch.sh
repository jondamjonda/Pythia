#!/usr/bin/env bash
# Detached ON n=70 full-posture batch (mirror of run_off_batch.sh, ON side).
# Tear down whatever stack is up (the OFF arm leaves its engine/backend running), boot EVERYTHING-ON
# via run_aq_all.sh (engine :8099 metagate=shadow claim_verify=enforce gpt-5.5), take the FE down
# (the WS10 memory-wall mitigation), score the 70 gold at posture=on, write the artifact.
set -u; cd "$(dirname "$0")/../.."; mkdir -p logs

# 1. tear down the OFF stack (it was booted via run_aq_off.sh, not run_app_local.sh start, so stop + hard-kill)
./scripts/local/run_app_local.sh stop >/dev/null 2>&1 || true
pkill -f "run_aq_off" 2>/dev/null || true
lsof -ti:8099 2>/dev/null | xargs kill 2>/dev/null || true
pkill -f "uvicorn pythia.service" 2>/dev/null || true
sleep 4

# 2. boot everything-ON, detached
nohup bash scripts/local/run_aq_all.sh > logs/on_batch_boot.log 2>&1 &
sleep 45

# 3. wait for the engine to answer health (run_aq_all.sh's own readiness probe uses /health + "status")
for i in $(seq 1 60); do curl -s -m3 http://127.0.0.1:8099/health 2>/dev/null | grep -q '"status"' && break; sleep 5; done

# 4. take the FE down to free memory for the enforce answers (kill dev server on 3000/3001)
(lsof -ti:3000 2>/dev/null | xargs kill 2>/dev/null; lsof -ti:3001 2>/dev/null | xargs kill 2>/dev/null; pkill -f "next.*300" 2>/dev/null) || true
sleep 3

# 5. score the ON arm (600s per-item ceiling; slow-but-valid answers are not lost)
cd ../engine
.venv/bin/python eval/aq_answer_eval.py --limit 70 --posture-tag on --timeout 600 \
  --out eval/artifacts/deploy_on_n70.json > eval/artifacts/on_n70_run.log 2>&1
echo "ON n=70 batch done -> deploy_on_n70.json" >> eval/artifacts/on_n70_run.log
