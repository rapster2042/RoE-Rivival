#!/usr/bin/env python3
"""Сборка наземной сетки ground_iceland_2022.json из накопленных точек пола.

Источники высот — уже проверенные каталоги диска:
- extracted/entity_named_points.json — позиции ~85К QEntity мира
  EU_IceLand_4096 с именами ресурсов (каждая сущность стоит на
  ландшафте или полу здания);
- config/loot-sites.json — точки лута, лежащие на поверхности.

Клетки сетки 32м. Высота клетки — медиана «приземлённых» образцов
(сугробы, камни, ветки, деревья, дорожные объекты, точки лута — pivot
на поверхности). Живая калибровка 20261018T001045: на клетках, где
стоял игрок, медиана даёт +1см (p15 утапливал ботов на ~2м, p75
поднимал на ~0.3м). Клетки без приземлённых образцов — только
интерьерные объекты (мебель сидит на этажах): там берём 15-й
процентиль как нижний этаж.

Отдельно выгружается список `interior` — клетки с интерьерными
сущностями (interiorvolume, мебель). Сервер использует маску для
линии огня: внутри/снаружи здания видимости нет.

Пустые клетки не заполняются: GroundMap в рантайме интерполирует
соседей и доучивается по позициям живых игроков.
"""
import argparse
import json
import math
import os
import re

# Pivot у ландшафта: природные и уличные объекты.
TERRAIN = re.compile(
    r'(snowdrift|snowstone|branch|stone|rock|wood|tree|bush|stump|grass'
    r'|roadside|roadblocks|graveyard|stfoot|cliff|mountain|ice_|snow)')
# Интерьерные сущности: объёмные маркеры зданий и мебель.
INTERIOR = re.compile(
    r'(interiorvolume|sofa|carpet|blinds|piano|_wc_|walltable|_bed|kitchen'
    r'|wardrobe|bookshelf|chair|desk_|toilet|bath_|fridge|microwave'
    r'|fireplace|_table)')
PERCENTILE = 0.5
INTERIOR_PERCENTILE = 0.15
# Объёмные пропы-окклюдеры линии огня: pivot сущности стоит у
# основания объекта, поэтому цилиндр описывается как (x,y,r,top).
# Размеры без точных AABB — консервативная оценка по классам
# (сугроб ~2м насыпь, камень ~4м валун); при живых подтверждениях
# значения уточняются.
OCCLUDER = (
    (re.compile(r'snowstone'), 300, 700),
    (re.compile(r'snowdrift'), 350, 200),
    (re.compile(r'rock|cliff|boulder'), 300, 700),
)
# Проёмы в стенах: двери и окна (blinds — маркер окна). Линия огня
# внутрь/наружу здания легальна только рядом с такой сущностью.
OPENING = re.compile(
    r'(_door|blinds|_window|_glass|glass|entrance|gate_|arch)')


def load_points(args):
    terrain, interior, other = [], [], []
    structure, openings, occluders = [], [], []
    loot_spawns = []
    if args.entities and os.path.exists(args.entities):
        for row in json.load(open(args.entities)):
            name, x, y, z = row if len(row) == 4 else ('', *row)
            (terrain if TERRAIN.search(name)
             else interior if INTERIOR.search(name)
             else other).append((x, y, z))
            if STRUCTURE.search(name):
                structure.append((x, y, z,
                                  bool(INTERIOR_ANCHOR.search(name))))
            if OPENING.search(name):
                openings.append((x, y, z))
            for pat, radius, height in OCCLUDER:
                if pat.search(name):
                    occluders.append((x, y, radius, z + height))
                    break
    by_12m = {}
    for e in json.load(open(args.loot)):
        terrain.append((e['x'], e['y'], e['z']))
        by_12m.setdefault((int(e['x'] // CLUSTER_CELL),
                           int(e['y'] // CLUSTER_CELL)), []).append(e)
    # Опорные точки спавна ботов по таблице лута: нижний уровень
    # каждого 12м-кластера (пол 1-го этажа/улицы, без 2-го этажа/чердака).
    for members in by_12m.values():
        min_z = min(m['z'] for m in members)
        low = [m for m in members if m['z'] - min_z <= 120]
        pick = low[len(low) // 2]
        loot_spawns.append((pick['x'], pick['y'], min_z))
    return terrain, interior, other, structure, openings, occluders, loot_spawns


# Структурные сущности зданий: объёмы, коробки домов, стены, окна,
# двери и мебель — участники пространственной кластеризации.
STRUCTURE = re.compile(
    r'(interiorvolume|_building_|buiding_|_1floor_|_2floor_|_cabin|'
    r'gasstation|chapel|hightower|warehouse|hangar|_house|_wall|'
    r'lowwall|stonewall|_glass|_door|sofa|carpet|_bed|kitchen|'
    r'wardrobe|bookshelf|chair|desk_|toilet|bath_|fridge|microwave|'
    r'fireplace|_table|_wc_|walltable|piano|bench|_cab_)')
# Якорь интерьера: само здание/этаж/объём. Кластер без якоря (голый
# забор, уличная мебель) бокса не даёт.
INTERIOR_ANCHOR = re.compile(
    r'(interiorvolume|_building_|buiding_|_1floor_|_2floor_|_cabin|'
    r'gasstation|chapel|hightower|warehouse|hangar|_house)')

CLUSTER_CELL = 1200    # кластерная ячейка 12м — члены дома связаны
BOX_PAD_XY = 250       # запас на толщину стен
BOX_PAD_Z_UP = 250     # запас над верхним этажом/мебелью
BOX_PAD_Z_DN = 150     # запас под нижним этажом


def interior_boxes(structure):
    """AABB-боксы помещений: union-find кластеров структурных сущностей.

    interiorvolume есть не у каждого дома (live 20261018T021307:
    imj_1floor_002 без объёма — бот простреливал стены), поэтому
    кластер собирается по соседним 12м-ячейкам из всех структурных
    сущностей — стен, дверей, стёкол, мебели; бокс остаётся только
    у кластера с якорем-зданием. Клеточная маска 32м давала дыру:
    бот в одной клетке со зданием «считался внутри».
    """
    cell = CLUSTER_CELL
    grid = {}
    for x, y, z, anchor in structure:
        grid.setdefault((int(x // cell), int(y // cell)), []).append(
            (x, y, z, anchor))
    parent = {k: k for k in grid}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for cx, cy in grid:
        for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
            other = (cx + dx, cy + dy)
            if other in grid:
                ra, rb = find((cx, cy)), find(other)
                if ra != rb:
                    parent[ra] = rb
    clusters = {}
    for key, members in grid.items():
        clusters.setdefault(find(key), []).extend(members)
    boxes = []
    for members in clusters.values():
        if not any(a for _, _, _, a in members):
            continue
        xs = [p[0] for p in members]
        ys = [p[1] for p in members]
        zs = [p[2] for p in members]
        boxes.append([min(xs) - BOX_PAD_XY, min(ys) - BOX_PAD_XY,
                      min(zs) - BOX_PAD_Z_DN,
                      max(xs) + BOX_PAD_XY, max(ys) + BOX_PAD_XY,
                      max(zs) + BOX_PAD_Z_UP])
    return boxes


def percentile(zs, q):
    return int(sorted(zs)[min(len(zs) - 1, int(len(zs) * q))])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--entities', default='../extracted/entity_named_points.json')
    p.add_argument('--loot', default='config/loot-sites.json')
    p.add_argument('--out', default='server/directory/ground_iceland_2022.json')
    p.add_argument('--cell', type=int, default=3200)
    args = p.parse_args()
    terrain, interior, other, structure, openings, occluders, loot_spawns = \
        load_points(args)
    cells_t, cells_i, cells_o = {}, {}, {}
    for x, y, z in terrain:
        cells_t.setdefault((int(x // args.cell), int(y // args.cell)),
                           []).append(z)
    for x, y, z in interior:
        cells_i.setdefault((int(x // args.cell), int(y // args.cell)),
                           []).append(z)
    for x, y, z in other:
        cells_o.setdefault((int(x // args.cell), int(y // args.cell)),
                           []).append(z)
    keys = sorted(set(cells_t) | set(cells_i) | set(cells_o))
    grid = []
    for cx, cy in keys:
        k = (cx, cy)
        if k in cells_t:
            z = percentile(cells_t[k], PERCENTILE)
        elif k in cells_i:
            z = percentile(cells_i[k], INTERIOR_PERCENTILE)
        else:
            z = percentile(cells_o[k], INTERIOR_PERCENTILE)
        grid.append((cx, cy, z))
    interior_cells = sorted(cells_i)
    boxes = interior_boxes(structure)
    payload = {'map': 16010, 'cell': args.cell, 'cells': grid,
               'interior': interior_cells, 'interior_boxes': boxes,
               'openings': openings, 'occluders': occluders,
               'loot_spawns': loot_spawns}
    with open(args.out, 'w') as h:
        json.dump(payload, h, separators=(',', ':'))
    tier1 = sum(1 for k in keys if k in cells_t)
    print(f'{args.out}: {len(grid)} клеток ({tier1} по приземлённым, '
          f'{len(interior_cells)} интерьерных, {len(boxes)} боксов, '
          f'{len(openings)} проёмов, {len(occluders)} '
          f'пропов-окклюдеров), '
          f'{len(terrain) + len(interior) + len(other)} образцов')


if __name__ == '__main__':
    main()
