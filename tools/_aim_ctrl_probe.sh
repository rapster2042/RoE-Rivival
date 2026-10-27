#!/bin/bash
# Control test: a real shot FIRST (known-visible), then the raise alone, then a
# shot again. If the shots render and the raise does not, the pose packet is
# dead on this build; if nothing renders, the observer is not looking at the
# peer avatar.
R=runs/20261024T005055-290b3a
PY=.tools/python/python.exe
LEAD=${1:-20}
stamp(){ date -u +%H:%M:%S; }
send(){ $PY -X utf8 -B tools/scene_action_2022.py --run "$R" "$@" >/dev/null 2>&1; }
printf '[]\n' > "$R/live-commands.json"
echo "$(stamp) lead-in ${LEAD}s"
sleep "$LEAD"
echo "$(stamp) CONTROL shots"
send --action peer_point --part fire --mode 0
sleep 0.4
send --action peer_point --part fire --mode 0
sleep 1
send --action peer_point --part fire --mode 1
sleep 12
echo "$(stamp) RAISE only"
send --action peer_point --part motion --mode 0
sleep 12
echo "$(stamp) CONTROL shots again"
send --action peer_point --part fire --mode 0
sleep 0.4
send --action peer_point --part fire --mode 0
sleep 1
send --action peer_point --part fire --mode 1
sleep 5
echo "$(stamp) done"
