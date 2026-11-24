#!/usr/bin/env bash
# Detached OFF n=70 baseline batch — boot clean OFF, FE-down for memory, score, write the artifact.
set -u; cd "$(dirname "$0")/../.."
./scripts/local/run_app_local.sh stop >/dev/null 2>&1; sleep 3
nohup bash scripts/local/run_aq_off.sh > logs/off_batch_boot.log 2>&1 & sleep 45
for i in $(seq 1 40); do curl -s -m3 http://127.0.0.1:8099/health/live 2>/dev/null | grep -q alive && break; sleep 5; done
(lsof -ti:3001 | xargs kill 2>/dev/null; pkill -f "next.*300" 2>/dev/null); sleep 3
cd ../engine
.venv/bin/python eval/aq_answer_eval.py --limit 70 --posture-tag off --timeout 600 \
  --out eval/artifacts/deploy_off_n70.json > eval/artifacts/off_n70_run.log 2>&1
echo "OFF n=70 batch done → deploy_off_n70.json" >> eval/artifacts/off_n70_run.log
