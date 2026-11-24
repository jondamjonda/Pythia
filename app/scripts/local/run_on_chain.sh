#!/usr/bin/env bash
# Wait for the currently-running OFF n=70 arm to finish, THEN fire the ON arm.
# The two full-posture stacks cannot co-exist on this host (memory wall), so the ON arm must
# start only after the OFF sentinel lands. Detached; survives across turns.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ART="$HERE/../../../engine/eval/artifacts"
OFFLOG="$ART/off_n70_run.log"
CHAIN="$ART/on_chain.log"
echo "[chain] waiting for the OFF arm to finish ..." > "$CHAIN"
for i in $(seq 1 360); do            # up to ~3h of waiting
  if grep -q "OFF n=70 batch done" "$OFFLOG" 2>/dev/null; then
    echo "[chain] OFF sentinel seen at iter $i" >> "$CHAIN"; break
  fi
  # belt-and-suspenders: if the OFF eval process is gone, stop waiting too
  if ! pgrep -f "aq_answer_eval.*posture-tag off" >/dev/null 2>&1; then
    echo "[chain] OFF eval process gone at iter $i" >> "$CHAIN"; break
  fi
  sleep 30
done
echo "[chain] starting the ON arm now" >> "$CHAIN"
bash "$HERE/run_on_batch.sh" >> "$CHAIN" 2>&1
echo "[chain] ON arm launcher returned (see on_n70_run.log / deploy_on_n70.json)" >> "$CHAIN"
