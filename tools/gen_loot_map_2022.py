"""Каталог лута по исходным точкам EU_LootBox из ResRespawnPointCfg.

Источник позиций — config/loot-sites.json, извлечённый из
extracted/common_sfc/01499_8f817397.txt (SFC Common.sfc ordinal1499,
TypeId 1/3/5, instance-name EU_LootBox/ENTITY_eLoot_level03/05).
PosZ исходный — включает высоты внутри зданий, без terrain-оценок.

index = InstanceID точки (135158..204863, вне fixture 15700-91003 и вне
drop-remap 0x01000000+); uid = 0x31d0000000000 + InstanceID*8 — формула
совпадает с проверенным fixture uid 0x31d0000168800 (instance 184576*8).

Распределение предметов случайное per-site с сидом: каждый запуск
генерирует другой набор, как оригинальный лут-генератор (алгоритм
оригинала не восстановлен — пулы диагностические по tier).
"""
import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Пулы проверенных definition; привязка калибра к патронам — диагностическая.
GUNS_T1 = [20001, 20003, 10110, 10303, 10101, 10102]
GUNS_T3 = [10201, 10202, 10203, 10204, 10205, 10206, 10208, 10301, 10305, 10601]
GUNS_T5 = [10401, 10402, 10403, 10306, 10605]
AMMO_FOR = {20001: 30005, 20003: 30005,
            10101: 30004, 10102: 30004, 10110: 30004, 10303: 30004,
            10201: 30001, 10202: 30002, 10203: 30002, 10204: 30002,
            10205: 30001, 10206: 30001, 10208: 30001,
            10301: 30002, 10305: 30002, 10306: 30002, 10601: 30002,
            10401: 30003, 10402: 30003, 10403: 30003, 10605: 30003}
# Все 7 рюкзаков (traversal+storage) во всех тирах: иначе T3-сайты
# (6023 из 7915) делают BMX/snowboard/motor в ~9 раз чаще island-packs.
PACKS = [200901, 200902, 200903, 200904, 200906, 200907, 200908]
SUPPORT_T1 = [70001, 70002, 200101, 201001] + PACKS
SUPPORT_T3 = [70002, 70003, 200102, 200103, 201002, 201003,
              50001, 50002, 50003, 40101, 40103, 40302, 40401, 40502] + PACKS
SUPPORT_T5 = [70003, 200104, 201004, 40104, 40105, 40303, 40305, 40403, 40405] + PACKS
STACK = {30001: 60, 30002: 60, 30003: 30, 30004: 30, 30005: 60, 30006: 60,
         30009: 60, 70001: 1, 70002: 2, 70003: 1, 50001: 3, 50002: 3, 50003: 3}


def build(sites, rng):
    entities = []
    for site in sites:
        tier, instance = site['type'], site['instance']
        pos = [site['x'], site['y'], site['z']]
        guns = {1: GUNS_T1, 3: GUNS_T3, 5: GUNS_T5}[tier]
        support = {1: SUPPORT_T1, 3: SUPPORT_T3, 5: SUPPORT_T5}[tier]
        entries = []
        if rng.random() < 0.62:
            gun = rng.choice(guns)
            entries.append((gun, 1))
            if gun in AMMO_FOR and rng.random() < 0.9:
                entries.append((AMMO_FOR[gun], STACK[AMMO_FOR[gun]]))
        else:
            item = rng.choice(support)
            entries.append((item, STACK.get(item, 1)))
        # Второй слот соседнего предмета с той же точки — редко.
        if rng.random() < 0.18:
            item = rng.choice(support)
            entries.append((item, STACK.get(item, 1)))
        for offset, (definition, quantity) in enumerate(entries):
            entities.append(dict(
                index=instance + offset * 400000,
                entity_uid=0x31d0000000000 + instance * 8 + offset,
                item_id=definition,
                quantity=quantity,
                position=[pos[0] + offset * 40, pos[1] + offset * 40, pos[2]]))
    return {'radius_cm': 10000, 'visible_limit': 128, 'batch_limit': 32,
            'entities': entities,
            'source': 'ResRespawnPointCfg EU_LootBox TypeId 1/3/5',
            'randomized': True, 'original_loot_generator': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sites', type=Path, default=ROOT/'config/loot-sites.json')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=None,
                        help='Фиксированный сид; без него — случайный')
    args = parser.parse_args()
    sites = json.loads(args.sites.read_text(encoding='utf-8'))
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**31)
    catalog = build(sites, random.Random(seed))
    args.out.write_text(json.dumps(catalog, ensure_ascii=False), encoding='ascii')
    print(f'loot-sites: {len(sites)} точек, {len(catalog["entities"])} предметов, сид {seed} → {args.out}')


if __name__ == '__main__':
    main()
