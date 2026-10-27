#!/usr/bin/env python3
"""Съёмка реальной поверхности через серверный телепорт игрока.

Эндпоинт (флаг --teleport-command) следит за runs/<id>/teleport.json;
эта утилита пишет туда команды {seq, position:[x,y,z]} — сервер шлёт
живому участнику own-0x27 на точку, клиентская физика сажает его на
подлинную поверхность, а observe() доучивает пол (lift сохраняется в
learned_ground_iceland_2022.json между сессиями).

Режимы:
  --pos X Y [Z]                 один прыжок
  --spiral CX CY RINGS          обход кольцами, кольцо = 32м
  --grid STEP                   полная карта: сетка STEP см (по умолч.
                                6400) от края до края heightfield,
                                точки ближе к игроку — первыми

Безопасность: высота сброса считается от СВЕЖЕГО Z игрока (поверхность
региона гладкая), при неудаче — отзыв домой и повтор с удвоенным
запасом (escalate 15→30→60→120→240м). Следующий прыжок запрещён, пока
клиент в воздухе — wait_stable требует два подряд замера Z в 50см.
Три неудачи подряд — стоп (что-то сломалось глобально).
"""
import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CELL = 3200  # клетка learned-карты
# Рамки heightfield Iceland (world см): ORIGIN + 2112 ячеек × шаг.
MAP_X0, MAP_Y0 = 202000.0, 156000.0
MAP_X1, MAP_Y1 = 649744.0, 586848.0
MARGINS = (1500, 3000, 6000, 12000, 24000)


def member_pos(log_path, target=None):
    """Позиции участников из ПОСЛЕДНЕГО bot_positions (свежий срез).

    target — avatar-uid (hex-строка из журнала): смотрим только на
    него — walker-клиента дуэтного запуска, игрока не трогаем.
    """
    last = None
    try:
        with open(log_path, encoding='utf-8', errors='replace') as h:
            h.seek(0, 2)
            h.seek(max(0, h.tell() - 400_000))
            for line in h:
                if 'bot_positions' not in line:
                    continue
                try:
                    e = json.loads(line[line.index('{'):])
                except ValueError:
                    continue
                members = [m for m in e.get('members', ())
                           if not target or m.get('uid') == target]
                if members:
                    last = [tuple(m['pos']) for m in members]
    except OSError:
        pass
    return last or []


def _member_near(positions, x, y):
    for px, py, pz in positions:
        if math.hypot(px - x, py - y) <= 2000:
            return (px, py, pz)
    return None


def command(path, seq, x, y, z, target=None):
    tmp = str(path) + '.tmp'
    cmd = {'seq': seq, 'position': [int(x), int(y), int(z)]}
    if target:
        cmd['target'] = target
    with open(tmp, 'w', encoding='utf-8') as h:
        json.dump(cmd, h)
    # Windows: эндпоинт читает файл каждые ~40мс — моментальный
    # read-lock ронял os.replace (WinError 5) и убивал многочасовой
    # sweep. Короткий retry переживает гонку.
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.15)
    os.replace(tmp, path)


def wait_stable(log_path, x, y, timeout, poll=0.8, target=None):
    """Два подряд замера Z в 50см рядом с (x,y) -> позиция, иначе None.

    Паузы в падении внутри геометрии выглядят как «стабильно» —
    поэтому после пары совпадений ждём ещё 2с и проверяем: утёк
    ниже 2м за это время = ложная посадка (провал сквозь пол).
    """
    deadline = time.time() + timeout
    prev = None
    while time.time() < deadline:
        pos = _member_near(member_pos(log_path, target), x, y)
        if pos and prev and abs(pos[2] - prev[2]) <= 50:
            settled = pos
            time.sleep(2.0)
            again = _member_near(member_pos(log_path, target), x, y)
            if again and abs(again[2] - settled[2]) <= 200:
                return again
            prev = again or pos
            continue
        if pos:
            prev = pos
        time.sleep(poll)
    return None


def hop_monitor(log_path, x, y, drop_z, timeout, target=None,
                poll=0.5):
    """Монитор падения после телепорта.

    Каждые poll секунд смотрим свежий замер:
      landed  — два замера в 50см + речек через 2с в пределах 2м;
      fell    — падение продолжается дальше 300м ниже точки сброса
                (живая проверка: провалы шли в минусовые тысячи;
                честный рельеф не просаживает на 300м за один прыжок),
                либо Z вообще ушёл в минус;
      timeout — ни посадки, ни явного провала за timeout секунд.
    Глубокие, но настоящие поверхности (>40м ниже опоры) больше
    не считаются провалом — клиент сел, и это фиксируем.
    """
    deadline = time.time() + timeout
    prev = None
    drops = 0
    while time.time() < deadline:
        pos = _member_near(member_pos(log_path, target), x, y)
        if pos:
            if prev:
                dz = pos[2] - prev[2]
                drops = drops + 1 if dz < -300 else 0
                # стабилизация: два замера в 50см + речек через 2с
                if abs(dz) <= 50:
                    time.sleep(2.0)
                    again = _member_near(member_pos(log_path, target),
                                         x, y)
                    if again and abs(again[2] - pos[2]) <= 200:
                        return 'landed', again
            if pos[2] < 0 or (drop_z - pos[2] > 30000 and drops >= 2):
                return 'fell', pos
            prev = pos
        time.sleep(poll)
    return 'timeout', prev


def pick_mask():
    """Допустимые клетки pick-карты 32x32 -> маска по мировым координатам.

    winter_spawn_points_2022.json: [cellX,cellY,worldX,worldY,z] —
    pick-карта размечает играбельную область; вне помеченных клеток
    телепорт уводит за край карты. Возвращает (mask, to_cell) где
    to_cell(x,y) -> индексы pick-клетки.
    """
    import json as _json
    f = ROOT / 'server/directory/winter_spawn_points_2022.json'
    pts = _json.loads(f.read_text(encoding='utf-8'))['points']
    mask = {(p[0], p[1]) for p in pts}
    # аффинная подгонка world->cell по обеим осям
    def fit(ci, wi):
        n = len(pts)
        sc = sum(p[ci] for p in pts)
        sw = sum(p[wi] for p in pts)
        scc = sum(p[ci] ** 2 for p in pts)
        scw = sum(p[ci] * p[wi] for p in pts)
        a = (n * scw - sc * sw) / (n * scc - sc * sc)
        b = (sw - a * sc) / n
        return a, b
    ax, bx = fit(0, 2)   # world_x = ax*cellx + bx
    ay, by = fit(1, 3)
    def to_cell(x, y):
        return (int(round((x - bx) / ax)), int(round((y - by) / ay)))
    return mask, to_cell


LEARNED_FILE = ROOT / 'server/directory/learned_ground_iceland_2022.json'

sys.path.insert(0, str(ROOT / 'server/directory'))
from ground_map_2022 import HeightField  # noqa: E402

HF = HeightField.load(ROOT / 'server/directory/heightfield_iceland_2022.json')


def learned_cells():
    """Клетки, уже покрытые эндпоинтом: lift-поправки И learned-z
    (обе записи означают «клетка подтверждена»; файл обновляет эндпоинт)."""
    try:
        d = json.loads(LEARNED_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return set()
    return ({(c[0], c[1]) for c in d.get('lift', [])} |
            {(c[0], c[1]) for c in d.get('learned', [])})


def confirm_cell(x, y, timeout=30.0, poll=1.0):
    """True, когда клетка (x,y) реально попала в learned-файл.

    Эндпоинт отбрасывает первый репорт после телепорта (dxy>STEADY_DXY)
    и сохраняет клетку только когда игрок простоит на ней до второго
    замера; flush идёт раз в ~10с. Держим клиента на месте, пока ключ
    не появится в файле — иначе клетка теряется молча (~27% посадок).
    """
    key = (int(x // CELL), int(y // CELL))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if key in learned_cells():
            return True
        time.sleep(poll)
    return False


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', required=True, help='каталог рана (runs/...)')
    p.add_argument('--pos', nargs='+', type=float, metavar='X/Y/Z')
    p.add_argument('--spiral', nargs=3, type=float,
                   metavar=('CX', 'CY', 'RINGS'))
    p.add_argument('--grid', nargs='*', type=float,
                   metavar='X0_Y0_X1_Y1_STEP',
                   help='полная карта; без аргументов — вся рамка, шаг 64м')
    p.add_argument('--points-file', help='JSON [[x,y],...] — явный список '
                                       'клеток вместо сетки; pick-маска и '
                                       'learned-пропуск применяются')
    p.add_argument('--land-timeout', type=float, default=12.0)
    p.add_argument('--home', nargs=3, type=float, metavar=('X', 'Y', 'Z'))
    p.add_argument('--target', metavar='UID',
                   help='avatar-uid walker-клиента (из bot_positions); '
                        'без него телепорт бьёт по всем участникам')
    p.add_argument('--command-file', default='teleport.json',
                   help='имя командного файла в run-каталоге; эндпоинт '
                        'читает teleport*.json — параллельные sweep '
                        'пишут в teleport.json / teleport2.json')
    p.add_argument('--no-confirm', action='store_true',
                   help='старый режим: шагать дальше сразу после посадки, '
                        'не дожидаясь записи клетки в learned-файл')
    p.add_argument('--confirm-timeout', type=float, default=30.0,
                   help='макс. ожидание появления клетки в learned-файле')
    p.add_argument('--dry', action='store_true')
    args = p.parse_args()
    run = Path(args.run)
    cmd_path = run / args.command_file
    log_path = run / 'endpoint.log'
    seq = int(time.time()) & 0xffff

    cur = member_pos(log_path, args.target)
    if not cur and not args.dry:
        print('!!  no member position — is the player in match?')
        return
    start = cur[0] if cur else (MAP_X0, MAP_Y0, 0)
    home = tuple(args.home) if args.home else start

    targets = []
    if args.pos:
        targets.append((args.pos[0], args.pos[1],
                        args.pos[2] if len(args.pos) > 2 else None))
    if args.spiral:
        cx, cy, rings = args.spiral
        for r in range(1, int(rings) + 1):
            for i in range(max(6, r * 3)):
                a = 2 * math.pi * i / max(6, r * 3)
                targets.append((cx + r * CELL * math.cos(a),
                                cy + r * CELL * math.sin(a), None))
    if args.grid is not None:
        g = list(args.grid) or [MAP_X0, MAP_Y0, MAP_X1, MAP_Y1, 6400]
        x0, y0, x1, y1 = g[:4]
        step = g[4] if len(g) > 4 else 6400
        known = learned_cells()
        mask, to_cell = pick_mask()
        pts, skipped, oob = [], 0, 0
        gy = y0
        while gy <= y1:
            gx = x0
            while gx <= x1:
                if to_cell(gx, gy) not in mask:
                    oob += 1
                elif (int(gx // CELL), int(gy // CELL)) in known:
                    skipped += 1
                else:
                    d2 = (gx - start[0]) ** 2 + (gy - start[1]) ** 2
                    pts.append((d2, gx, gy))
                gx += step
            gy += step
        # Порядок — цепочка ближайшего соседа от позиции игрока:
        # соседние прыжки в пределах одного-двух секторов (~64м),
        # стриминг коллизии клиента успевает подгрузить геометрию.
        # «Ближние первыми» разбрасывало прыжки на 100м+ и клиент
        # падал сквозь незагруженный мир.
        cx, cy = start[0], start[1]
        while pts:
            bi = min(range(len(pts)),
                     key=lambda i: (pts[i][1] - cx) ** 2
                     + (pts[i][2] - cy) ** 2)
            _, x, y = pts.pop(bi)
            targets.append((x, y, None))
            cx, cy = x, y
        print(f'grid: {len(targets)} points, {skipped} known, '
              f'{oob} off pick-map', flush=True)
    if args.points_file:
        known = learned_cells()
        mask, to_cell = pick_mask()
        pts, skipped, oob = [], 0, 0
        for x, y in json.loads(Path(args.points_file).read_text(encoding='utf-8')):
            if to_cell(x, y) not in mask:
                oob += 1
            elif (int(x // CELL), int(y // CELL)) in known:
                skipped += 1
            else:
                pts.append((x, y))
        cx, cy = start[0], start[1]
        while pts:
            bi = min(range(len(pts)),
                     key=lambda i: (pts[i][0] - cx) ** 2
                     + (pts[i][1] - cy) ** 2)
            x, y = pts.pop(bi)
            targets.append((x, y, None))
            cx, cy = x, y
        print(f'points: {len(targets)} targets, {skipped} known, '
              f'{oob} off pick-map', flush=True)

    ok = bad = aborted = 0
    unconfirmed = []
    last_ok_z = None          # z последней подтверждённой посадки
    t0 = time.time()

    def probe_cell(x, y, z):
        """Один прогон клетки: телепорт + адаптивная высота по MARGINS.
        Возвращает ('landed', pos) | ('fail', None) | ('abort', None).

        Высота сброса — от z последней подтверждённой клетки (+margin,
        т.е. первый заход ≈50ft над соседом), а не от текущей позиции:
        airborne/проваленная позиция отравляла base_z и давала сбросы
        с ~1.5км, которые монитор считал провалами."""
        nonlocal seq, last_ok_z
        cur = member_pos(log_path, args.target)
        if not cur:
            return 'abort', None
        base_z = max(p[2] for p in cur)
        anchor = last_ok_z if last_ok_z is not None else base_z
        # Сброс выше большего из двух прогнозов: z последней
        # подтверждённой клетки и грубая heightfield-высота этой
        # клетки. На крутом подъёме соседняя клетка может быть
        # на десятки метров выше прошлой — сброс от anchor+15м
        # ставил игрока ПОД рельеф -> чистый провал сквозь мир.
        hf_z = HF.floor(x, y) if HF is not None else None
        if hf_z is not None:
            anchor = max(anchor, hf_z)
        # Адаптивная высота: провал/таймаут -> отзыв домой -> выше.
        for margin in MARGINS:
            drop_z = z if z is not None else anchor + margin
            seq += 1
            command(cmd_path, seq, x, y, drop_z, args.target)
            outcome, landed = hop_monitor(log_path, x, y, drop_z,
                                          args.land_timeout,
                                          target=args.target)
            if outcome == 'landed':
                last_ok_z = landed[2]
                return 'landed', landed
            seq += 1
            command(cmd_path, seq, home[0], home[1], home[2] + 400,
                    args.target)
            wait_stable(log_path, home[0], home[1], args.land_timeout,
                        target=args.target)
            print(f'  .. {x:.0f},{y:.0f} {outcome} '
                  f'retry +{margin / 100:.0f}m', flush=True)
        return 'fail', None

    for n, (x, y, z) in enumerate(targets, 1):
        if args.dry:
            print(f'[dry] -> {x:.0f},{y:.0f}')
            continue
        status, landed = probe_cell(x, y, z)
        if status == 'abort':
            print('!!  no fresh member position — abort', flush=True)
            aborted += 1
            break
        if status == 'landed':
            if (args.no_confirm
                    or confirm_cell(landed[0], landed[1],
                                    args.confirm_timeout)):
                print(f'OK  [{n}/{len(targets)}] {x:.0f},{y:.0f} '
                      f'z={landed[2]:.0f}', flush=True)
                ok += 1
            else:
                print(f'UNCF[{n}/{len(targets)}] {x:.0f},{y:.0f} '
                      f'z={landed[2]:.0f} — not in DB, queued for recheck',
                      flush=True)
                unconfirmed.append((x, y, z))
        else:
            print(f'FAIL[{n}/{len(targets)}] {x:.0f},{y:.0f}', flush=True)
            bad += 1
            if bad >= 3:
                print('!!  3 failures — stopping sweep', flush=True)
                break
        if n % 20 == 0:
            el = time.time() - t0
            rate = el / n
            print(f'== {n}/{len(targets)}  ok={ok} bad={bad}  '
                  f'elapsed={el / 60:.0f}m eta={rate * (len(targets) - n) / 60:.0f}m',
                  flush=True)

    # Второй проход: клетки, не попавшие в learned-файл. Повторный
    # облёт подтверждает их либо объявляет необучаемыми (observe()
    # эндпоинта навсегда отбрасывает |lift|>LIFT_MAX вне рамки).
    if unconfirmed and not args.dry:
        print(f'verify pass: re-probing {len(unconfirmed)} unconfirmed cells',
              flush=True)
        still = []
        for n, (x, y, z) in enumerate(unconfirmed, 1):
            status, landed = probe_cell(x, y, z)
            if status == 'abort':
                print('!!  no fresh member position — abort verify pass',
                      flush=True)
                aborted += 1
                break
            if (status == 'landed'
                    and confirm_cell(landed[0], landed[1],
                                     args.confirm_timeout)):
                print(f'OK* [{n}/{len(unconfirmed)}] {x:.0f},{y:.0f} '
                      f'z={landed[2]:.0f}', flush=True)
                ok += 1
            else:
                print(f'LOST[{n}/{len(unconfirmed)}] {x:.0f},{y:.0f} '
                      f'— observe() rejected', flush=True)
                still.append((x, y))
        unconfirmed = still
        if unconfirmed:
            (run / f'unlearnable_{cmd_path.name[:-5]}.json').write_text(
                json.dumps(unconfirmed), encoding='utf-8')
    print(f'done: {ok} stored, {bad} failed, {aborted} aborted, '
          f'{len(unconfirmed)} unlearnable')


if __name__ == '__main__':
    main()
