#!/bin/bash
# Announced aim-pose demo: lead-in, then repeated shot/shot/stop cycles.
R=runs/20261024T005055-290b3a
PY=.tools/python/python.exe
LEAD=${1:-30}
CYCLES=${2:-6}
send(){ $PY -X utf8 -B tools/scene_action_2022.py --run "$R" --action peer_point --part fire --mode $1 --pitch -8 >/dev/null 2>&1; }
printf '[]\n' > "$R/live-commands.json"
echo "$(date -u +%H:%M:%S) lead-in ${LEAD}s"
sleep "$LEAD"
for i in $(seq 1 "$CYCLES"); do
  echo "$(date -u +%H:%M:%S) cycle $i shots"
  send 0; sleep 0.4; send 0
  sleep 1; send 1
  sleep 3; send 1
  sleep 4; send 1
  sleep 12
done
echo "$(date -u +%H:%M:%S) done"
