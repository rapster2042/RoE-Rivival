#!/bin/bash
# Announced aim-HOLD experiment: isolate what makes the remote raise survive a
# held ADS instead of dropping after one animation.
#   P1 raise alone            -> does the pose hold with nothing else sent?
#   P2 raise alone (repeat)   -> is P1 reproducible?
#   P3 ik_motion              -> does the 0x11d6 pitch relay break the hold?
#   P4 mag + status 40, raise -> does the pipeline's own order break the hold?
#   P5 mode 1 (state 3)       -> does the release still lower the weapon?
#   P6 raise + 2s re-assert   -> does a slow re-assert restart or keep it?
R=runs/20261024T005055-290b3a
PY=.tools/python/python.exe
LEAD=${1:-45}
stamp(){ date -u +%H:%M:%S; }
send(){ $PY -X utf8 -B tools/scene_action_2022.py --run "$R" "$@" >/dev/null 2>&1; }
printf '[]\n' > "$R/live-commands.json"
echo "$(stamp) lead-in ${LEAD}s"
sleep "$LEAD"
echo "$(stamp) P1 raise alone"
send --action peer_point --part motion --mode 0
sleep 30
echo "$(stamp) P2 raise alone (repeat)"
send --action peer_point --part motion --mode 0
sleep 25
echo "$(stamp) P3 ik_motion (IK + raise)"
send --action peer_point --part ik_motion --mode 0 --pitch -10
sleep 22
echo "$(stamp) P4 magazine + status 40, then raise 0.3s later"
send --action peer_ammo --slot 1 --clip 30
send --action peer_status --status 40
sleep 0.3
send --action peer_point --part motion --mode 0
sleep 22
echo "$(stamp) P5 release (mode 1 -> state 3)"
send --action peer_point --part motion --mode 1
sleep 6
echo "$(stamp) P6 raise + re-assert every 2s x3"
send --action peer_point --part motion --mode 0
sleep 2
send --action peer_point --part motion --mode 0
sleep 2
send --action peer_point --part motion --mode 0
sleep 20
echo "$(stamp) done"
