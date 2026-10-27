"""Regenerate config/zone-sets.json — scattered-closure zone ladders.

Live 070331: the authored ladders dropped 33 contiguous regions at
stage 20 — an unescapable wall that killed 26 bots in one transition
and can trap a player. Per request each closure now halves the worst
hit and the dropped regions are scattered around the map (greedy
max-min distance against everything already closed), so there is
always a route between the hot cells instead of a solid front.

Ladder shape (same timings/durations as the original sets):
    safe: 64 -> 64 -> 48 -> 36 -> 24 -> 12 -> {refuge} -> {refuge}
    closes per transition: 0, 16, 12, 12, 12, 11, 0   (max 33 -> 16)

previous_regions = safe set AT the stage; future_regions = announced
next safe set (wire contract: future ⊆ previous, monotone shrink).
"""
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'server' / 'directory'))
from region_index_2022 import FastRegionIndex  # noqa: E402

INDEX = ROOT / 'runtime' / 'regions-iceland-fast.dat'
OUT = ROOT / 'config' / 'zone-sets.json'

SETS = [
    ('center-24',    24, [365300, 544000, 259100]),
    ('southwest-54', 54, [236800, 377600, 280786]),
    ('northeast-46', 46, [569548, 565483, 255664]),
    ('southeast-35', 35, [531100, 275200, 250067]),
    ('west-19',      19, [249600, 326400, 287056]),
]

# safe-set sizes after each closing transition
SAFE_LADDER = [64, 48, 36, 24, 12, 1]
TIMINGS = [(0.0, 1, 18), (18.3, 10, 42), (60.3, 20, 30),
           (90.3, 30, 24), (114.3, 40, 21), (135.3, 50, 54),
           (189.3, 50, 54)]


def centroids(grid):
    """Sample the area index; centroid per region id."""
    cells = {}
    for r in range(0, 8192, 40):
        for c in range(0, 8192, 40):
            rid = grid.region_id((204800 + c*50, 204800 + r*50, 0))
            if rid:
                cells.setdefault(rid, []).append((c, r))
    return {rid: (sum(p[0] for p in pts)/len(pts),
                  sum(p[1] for p in pts)/len(pts))
            for rid, pts in cells.items()}


def scattered_drops(center, refuge, sizes, seed):
    """Pick which regions close at each rung: greedy max-min distance
    against already-closed + already-picked cells, refuge excluded.
    Deterministic per set via seeded shuffle of the pool order."""
    import random
    rng = random.Random(seed)
    closed, rungs = set(), []
    for target in sizes:
        need = (64 - len(closed)) - target   # open minus desired safe
        pool = sorted(set(range(1, 65)) - closed - {refuge})
        rng.shuffle(pool)
        picked = []
        while len(picked) < need and pool:
            if not closed and not picked:
                best = pool[0]
            else:
                anchor = closed | set(picked)
                best = max(pool, key=lambda rid: min(
                    (center[rid][0]-center[o][0])**2
                    + (center[rid][1]-center[o][1])**2
                    for o in anchor))
            pool.remove(best)
            picked.append(best)
        closed.update(picked)
        rungs.append(picked)
    return rungs


def build_set(name, refuge, position, center):
    sizes = SAFE_LADDER[1:]           # 48,36,24,12,1 safe targets
    seed = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16)
    rungs = scattered_drops(center, refuge, sizes, seed)
    safe_sets = []
    safe = set(range(1, 65))
    for drops in rungs:
        safe -= set(drops)
        safe_sets.append(sorted(safe))
    # previous_regions[i] = safe set in effect AT stage i; the closure
    # lands a stage after it was announced in future_regions[i-1].
    prev = [sorted(set(range(1, 65))), sorted(set(range(1, 65)))] + safe_sets
    # rows: prev[i] is safe at stage i, future[i] = prev[i+1]
    stages = []
    for i, (at, stage, dur) in enumerate(TIMINGS):
        cur = prev[i] if i < len(prev) else prev[-1]
        nxt = prev[i+1] if i + 1 < len(prev) else prev[-1]
        stages.append(dict(at_seconds=at, stage=stage, duration=dur,
                           future_regions=nxt, previous_regions=cur))
    return dict(name=name,
                evacuation_refuge=dict(
                    region_id=refuge, position=position,
                    policy='fixed_diagnostic_safe_until_match_end',
                    provenance='EU_ICELAND_4096/AREAINDEX.BIN SHA9dfca77d; '
                               'native_fast region lookup; heli654B/104 XYZ+20 '
                               'and657C/38 XYZ+0'),
                stages=stages)


def main():
    grid = FastRegionIndex.load(INDEX)
    center = centroids(grid)
    out = dict(
        description=('Rotating zone ladders — scattered closures '
                     '(gen_zone_sets.py). Each stage closes at most 16 '
                     'regions, picked max-min spread so hot cells are '
                     'isolated islands with routes between; final region '
                     'anchors the heli refuge.'),
        sets=[build_set(name, refuge, pos, center)
              for name, refuge, pos in SETS])
    # wire-contract sanity: future ⊆ previous, monotone, refuge last
    for s in out['sets']:
        prev = set(range(1, 65))
        for row in s['stages']:
            cur, fut = set(row['previous_regions']), set(row['future_regions'])
            assert cur <= prev and fut <= cur, (s['name'], row['stage'])
            prev = cur
        assert prev == {s['evacuation_refuge']['region_id']}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding='utf8')
    for s in out['sets']:
        ladder = [len(r['previous_regions']) for r in s['stages']]
        print(s['name'], 'safe ladder:', ladder)


if __name__ == '__main__':
    main()
