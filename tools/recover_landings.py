# -*- coding: utf-8 -*-
"""Recover grounded landings from scene events and merge them into the
learned-ground DB.

Worker logs (sweep_teleport*.log) get truncated on every relaunch, so the
'OK x,y z=...' record is not a durable store.  The scene event stream is
append-only and contains every member position report; a member that
stands still (dz<=STEADY_DZ, dxy<=STEADY_DXY vs the previous report)
yields the same grounded observation the endpoint would have used.

Usage:
    python tools/recover_landings.py --run <run-dir> [--dry-run]

Reads all runs/*/scene/events.jsonl plus every sweep log, builds
cell->z for grounded samples, then appends missing cells to
learned_ground_iceland_2022.json['learned'] entries [cx, cy, z].
A timestamped backup of the learned file is written first.
"""
import argparse
import glob
import json
import os
import re
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'server', 'directory'))

from teleport_sweep import pick_mask, learned_cells, CELL  # noqa: E402
from ground_map_2022 import HeightField, OBSERVE_PIVOT, STEADY_DZ, STEADY_DXY  # noqa: E402

LEARNED = os.path.join(ROOT, 'server', 'directory', 'learned_ground_iceland_2022.json')
GROUND = os.path.join(ROOT, 'server', 'directory', 'ground_iceland_2022.json')
HF_PATH = os.path.join(ROOT, 'server', 'directory', 'heightfield_iceland_2022.json')
OK_RX = re.compile(r'OK  \[\d+/\d+\] (\d+),(\d+) z=(-?\d+)')


def _in_box(boxes, cx, cy):
    x0, y0 = cx * CELL, cy * CELL
    x1, y1 = x0 + CELL, y0 + CELL
    for bx0, by0, _bz0, bx1, by1, _bz1 in boxes:
        if not (x1 < bx0 or x0 > bx1 or y1 < by0 or y0 > by1):
            return True
    return False


def classify(cx, cy, z, hf, boxes):
    """'learned' for no-hf/interior-box cells, 'lift' for hf-covered
    outdoor cells — mirroring ground_map.GroundMap.query, which only
    reads learned where there is no heightfield or inside a box."""
    h = hf.floor(cx * CELL + 1600, cy * CELL + 1600) if hf else None
    if h is None or _in_box(boxes, cx, cy):
        return 'learned', z
    return 'lift', z - OBSERVE_PIVOT - h


def mine_scene(run_dirs):
    """cell -> z from stationary member positions in scene events."""
    out = {}
    last = {}  # uid -> [x,y,z]
    for rd in run_dirs:
        for f in glob.glob(os.path.join(rd, 'scene', 'events.jsonl*')):
            try:
                fh = open(f, encoding='utf-8', errors='replace')
            except OSError:
                continue
            with fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get('action') != 'bot_positions':
                        continue
                    for m in e.get('members', []):
                        uid = m.get('uid')
                        pos = m.get('pos')
                        if not uid or not pos:
                            continue
                        prev = last.get(uid)
                        last[uid] = pos
                        if prev is None:
                            continue
                        dz = abs(pos[2] - prev[2])
                        dxy = max(abs(pos[0] - prev[0]), abs(pos[1] - prev[1]))
                        if dz <= STEADY_DZ and dxy <= STEADY_DXY and pos[2] > 0:
                            cell = (pos[0] // CELL, pos[1] // CELL)
                            out[cell] = pos[2]
    return out


def mine_logs():
    out = {}
    for f in glob.glob(os.path.join(ROOT, 'runs', '*', 'sweep_*.log*')):
        try:
            fh = open(f, encoding='utf-8', errors='replace')
        except OSError:
            continue
        with fh:
            for line in fh:
                m = OK_RX.match(line)
                if m:
                    x, y, z = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    out[(x // CELL, y // CELL)] = z
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', default=None,
                    help='limit scene mining to one run dir (default: all runs)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    run_dirs = ([args.run] if args.run
                else glob.glob(os.path.join(ROOT, 'runs', '*')))
    mask, to_cell = pick_mask()
    known = learned_cells()
    log_z = mine_logs()
    scene_z = mine_scene(run_dirs)

    union = dict(scene_z)
    union.update(log_z)  # worker-confirmed landing z wins over raw scene sample

    addable = {c: z for c, z in union.items() if c not in known}
    in_mask = {c: z for c, z in addable.items()
               if to_cell(c[0] * CELL + 1600, c[1] * CELL + 1600) in mask}

    # Plausibility filter: a frozen mid-air client passes the steady gate
    # and mines as 'grounded' at airborne z.  Real terrain rarely jumps
    # >400 m within one 32 m ring, so flag cells that disagree with the
    # median of their known/recovered neighbours by more than that.
    allz = dict(union)
    try:
        for cx, cy, z in json.loads(
                open(LEARNED, encoding='utf-8').read()).get('learned', []):
            allz.setdefault((cx, cy), z)
    except (OSError, ValueError):
        pass
    plausible, suspect = {}, []
    for c, z in in_mask.items():
        neigh = sorted(v for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                       if (dx or dy)
                       for v in (allz.get((c[0] + dx, c[1] + dy)),)
                       if v is not None)
        if not neigh or abs(z - neigh[len(neigh) // 2]) <= 40000:
            plausible[c] = z  # consistent, or isolated with nothing to compare
        else:
            suspect.append((c, z, neigh[len(neigh) // 2]))
    print('scene-grounded cells:', len(scene_z))
    print('log-landed cells    :', len(log_z))
    print('union not in DB     :', len(addable))
    print('  of which in mask  :', len(in_mask))
    print('  outside pick mask :', len(addable) - len(in_mask))
    print('  suspect outliers  :', len(suspect))
    for c, z, med in suspect[:10]:
        print('    ', c, 'z=', z, 'neighbour median=', med)
    in_mask = plausible

    if args.dry_run or not in_mask:
        return

    hf = HeightField.load(HF_PATH)
    boxes = json.loads(open(GROUND, encoding='utf-8').read()).get(
        'interior_boxes', ())
    d = json.loads(open(LEARNED, encoding='utf-8').read())
    learned = d.setdefault('learned', [])
    lift = {(c[0], c[1]): c[2] for c in d.setdefault('lift', [])}
    have = {(c[0], c[1]) for c in learned}
    n_learned = n_lift = 0
    for c, z in sorted(in_mask.items()):
        kind, v = classify(c[0], c[1], z, hf, boxes)
        if kind == 'learned':
            if (c[0], c[1]) not in have:
                learned.append([int(c[0]), int(c[1]), int(v)])
                n_learned += 1
        elif (c[0], c[1]) not in lift:
            lift[(c[0], c[1])] = v
            n_lift += 1
    d['lift'] = [[k[0], k[1], v] for k, v in lift.items()]
    bak = LEARNED + time.strftime('.bak-%Y%m%dT%H%M%S')
    shutil.copy2(LEARNED, bak)
    tmp = LEARNED + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(d, fh)
    os.replace(tmp, LEARNED)
    print(f'merged: {n_learned} learned + {n_lift} lift '
          f'(backup: {os.path.basename(bak)})')


if __name__ == '__main__':
    main()
