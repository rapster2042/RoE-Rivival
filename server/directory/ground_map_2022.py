"""Наземная сетка2022: высота пола по XY для серверных сущностей.

Заменяет коллизию клиента «падение до пола». Первичный источник —
cooked PhysX рельеф RIGIDTERRAINLINUX.PXBIN (NxHeightField 2112x2112,
2-см сетка), выгруженный tools/build_heightfield.py из data_bas_4.vfs
в heightfield_iceland_2022.{bin,json}. Это настоящий ландшафт карты:
реальные холмы/насыпи, которых нет в выгрузке QEntity.

Запасной источник — server/directory/ground_iceland_2022.json от
tools/build_ground_map.py (позиции QEntity/лута, клетки 32м); нужен
там, где поле не покрывает.

observe(x,y,z) доучивает сетку по реальным позициям живых игроков:
их отсчёты коллизии точнее и имеют приоритет над всеми источниками.

interior_at(x,y) — маска интерьерных клеток (interiorvolume/мебель
в выгрузке): грубое разделение «внутри здания / снаружи» для линии
огня ботов. line_clear(a,b) — выборка пола вдоль отрезка: рельеф
или объёмный проп выше линии выстрела блокируют её (стены зданий
не моделируются — для них служит interior-маска).
"""
import array
import json
import math
import os

NEIGHBOR_RADIUS = 1        # окно 3x3 клеток для IDW
SPIRAL_LIMIT = 24          # максимум колец поиска ближайшей клетки (~768м)
LOS_STEP_CM = 200          # выборка рельефа вдоль линии огня (~2м)
LOS_STEPS_MAX = 48         # потолок выборок на отрезок
LOS_CLEARANCE = 150        # просвет над полом на линии (~1.5м)
EYE_HEIGHT = 150           # глаза бота над точкой пола
TARGET_HEIGHT = 120        # корпус цели над её точкой пола
OBSERVE_TOLERANCE = 250
OBSERVE_PIVOT = 0         # z в репорте игрока — точка под стопами:
                          # измеренные узлы ROE-LIVE native309 дают
                          # рельеф+0..1см, собственные репорты на открытой
                          # местности рельеф+0..25см. Старая оценка +110
                          # (центр капсулы) занижала learned-полы на 1.1м.
PROP_STEP = 350           # подъём на проп-настил (сугроб ~2м), см
FLOOR_LIFT = 300          # cell/learned-кандидат в боксе поднимает пол максимум на столько
FLOOR_DIP = 50            # и опустить максимум на столько (защита от подпола)
LOOT_STEP = 250           # ярус лута не выше одного шага над рельефом —
                          # поверхность (сугроб/наст/порог); выше — плита
                          # строения (стена). Равен CLIMB_MAX ботов: что бот
                          # физически перешагивает, то и должно быть полом.
STEADY_DZ = 150           # репорт «на ногах»: |dz| между соседними репортами
STEADY_DXY = 800          #   и не быстрее бега (парашют/планер отсекаются)
LIFT_MAX = 5000           # sanity-предел аддитивной поправки lift, см
LIFT_HARD = 60000         # grounded-образец выше LIFT_MAX — реальный
                          # крутой рельеф; выше этого — мусор, в learned
CLIFF_DELTA = 500         # сосед ниже на столько — у края стелем пандус
CLIFF_RAMP = 0.5          # доля клетки от общей границы, занятая пандусом
OPENING_RADIUS = 250       # проём (дверь/окно) по XY от линии выстрела
OPENING_Z = 300            # проём по высоте от точки линии


class HeightField:
    """NxHeightField из RIGIDTERRAINLINUX.PXBIN: i16-сетка + калибровка."""

    def __init__(self, meta, samples):
        self.rows = meta['rows']
        self.cols = meta['cols']
        self.ox = meta['origin_x']
        self.oy = meta['origin_y']
        self.sx = meta['step_x']
        self.sy = meta['step_y']
        self.scale = meta['height_scale']
        self.offset = meta['height_offset']
        self.samples = samples  # array('h'), row-major, row=y col=x

    @classmethod
    def load(cls, path):
        path = str(path)
        if not os.path.exists(path):
            return None
        with open(path) as h:
            meta = json.load(h)
        if path.endswith('.json'):
            blob = path[:-5] + '.bin'
        else:
            blob = meta['file']
            if not os.path.isabs(blob):
                blob = os.path.join(os.path.dirname(path), blob)
        if not os.path.exists(blob):
            return None
        samples = array.array('h')
        with open(blob, 'rb') as h:
            samples.frombytes(h.read())
        if len(samples) < meta['rows'] * meta['cols']:
            return None
        return cls(meta, samples)

    def floor(self, x, y):
        """Высота мира в точке — треугольная выборка PhysX heightfield.

        Каждая клетка разбита диагональю на две грани; в массиве карты16010
        все tessellation-флаги=0 (ROE-LIVE probe_iceland_heightfield_2022:
        выбор совпал с native до 0.007см). Билинейная выборка даёт иной
        наклон и промахивалась на десятки см на склонах."""
        c = (x - self.ox) / self.sx
        r = (y - self.oy) / self.sy
        ri, ci = int(r), int(c)
        if not (0 <= ri < self.rows - 1 and 0 <= ci < self.cols - 1):
            return None
        fr, fc = r - ri, c - ci
        base = ri * self.cols + ci
        h00 = self.samples[base]
        h10 = self.samples[base + self.cols]
        h01 = self.samples[base + 1]
        h11 = self.samples[base + self.cols + 1]
        if fr + fc < 1:
            h = h00 + fr * (h10 - h00) + fc * (h01 - h00)
        else:
            h = h11 + (1 - fr) * (h01 - h11) + (1 - fc) * (h10 - h11)
        return h * self.scale + self.offset


class GroundMap:
    def __init__(self, cell, cells, interior=(), occluders=(),
                 boxes=(), openings=(), heightfield=None, loot_spawns=()):
        self.cell = cell
        self.cells = dict(cells)      # (cx,cy) -> z из каталога
        self.interior = set(interior) # (cx,cy) интерьерных клеток
        self.occluders = list(occluders)  # (x,y,r,top) объёмные пропы
        self.loot_spawns = [tuple(p) for p in loot_spawns]  # (x,y,z) нижних точек лута
        self._loot_floors = {}        # (cx,cy) -> минимальный z яруса лута
        for lx, ly, lz in self.loot_spawns:
            key = (int(lx // self.cell), int(ly // self.cell))
            low = self._loot_floors.get(key)
            if low is None or lz < low:
                self._loot_floors[key] = lz
        self._occluder_cells = {}         # (cx,cy) -> индексы пропов
        for i, (ox, oy, orad, _top) in enumerate(self.occluders):
            span = int(orad // self.cell) + 1
            cx, cy = int(ox // self.cell), int(oy // self.cell)
            for dx in range(-span, span + 1):
                for dy in range(-span, span + 1):
                    self._occluder_cells.setdefault(
                        (cx + dx, cy + dy), []).append(i)
        # Боксы помещений (x0,y0,z0,x1,y1,z1) и проёмы (x,y,z) дверей/окон —
        # точная граница «внутри/снаружи» вместо клеточной маски.
        self.boxes = [tuple(b) for b in boxes]
        self.openings = [tuple(o) for o in openings]
        self._box_cells = {}
        for i, (x0, y0, z0, x1, y1, z1) in enumerate(self.boxes):
            for cx in range(int(x0 // self.cell), int(x1 // self.cell) + 1):
                for cy in range(int(y0 // self.cell), int(y1 // self.cell) + 1):
                    self._box_cells.setdefault((cx, cy), []).append(i)
        self._opening_cells = {}
        for i, (ox, oy, oz) in enumerate(self.openings):
            key = (int(ox // self.cell), int(oy // self.cell))
            self._opening_cells.setdefault(key, []).append(i)
        self.heightfield = heightfield
        self.learned = {}             # (cx,cy) -> z из observe() (боксы/без hf)
        self.lift = {}                # (cx,cy) -> поправка к hf, см (observe)
        self._last = {}               # uid -> последний репорт (x,y,z)
        self.dirty = False            # lift/learned менялись с последнего save
        self.learned_path = None
        self._spiral = self._rings(SPIRAL_LIMIT)

    @staticmethod
    def _rings(limit):
        offsets = []
        for r in range(2, limit + 1):
            for dx in range(-r, r + 1):
                for dy in (-r, r):
                    offsets.append((dx, dy))
            for dy in range(-r + 1, r):
                for dx in (-r, r):
                    offsets.append((dx, dy))
        offsets.sort(key=lambda o: o[0] * o[0] + o[1] * o[1])
        return offsets

    @classmethod
    def load(cls, path, heightfield_path=None, learned_path=None):
        if not os.path.exists(path):
            return None
        with open(path) as h:
            payload = json.load(h)
        hf = HeightField.load(heightfield_path) if heightfield_path else None
        gm = cls(payload['cell'],
                 {(cx, cy): z for cx, cy, z in payload['cells']},
                 (tuple(c) for c in payload.get('interior', ())),
                 (tuple(o) for o in payload.get('occluders', ())),
                 (tuple(b) for b in payload.get('interior_boxes', ())),
                 (tuple(o) for o in payload.get('openings', ())),
                 hf,
                 (tuple(p) for p in payload.get('loot_spawns', ())))
        if learned_path:
            gm.learned_path = learned_path
            gm.load_learned(learned_path)
        return gm

    def observe(self, x, y, z, uid=None):
        """Позиция живого игрока — верифицированная высота пола.

        Гейтинг по динамике репорта, а не по близости к полю высот:
        hf ошибается на метры регионально (город-плато +40м к cooked
        терраину, live 20261018T040326), поэтому толеранс к hf
        отбрасывал почти все валидные репорты (6 learned за матч).
        Игрок, стоящий/идущий на месте (|dz|<STEADY_DZ и не быстрее
        бега между репортами), находится на поверхности — её z
        и есть пол независимо от того, что говорит hf. Парашют,
        планер и транспорт отсекаются по скорости; репорт рядом с hf
        принимается и без истории (первый репорт после спавна).

        Репорт внутри XY-бокса здания — кандидат интерьерного пола
        (learned, пол с пивотом); снаружи — аддитивная поправка lift
        к hf: константа поверх рельефа сохраняет наклон склона, а не
        размазывает z точки репорта на всю 32м клетку."""
        if not all(math.isfinite(v) for v in (x, y, z)):
            return
        hf = (self.heightfield.floor(x, y)
              if self.heightfield is not None else None)
        grounded = uid is None      # без uid нет истории — старое поведение
        if uid is not None:
            last = self._last.get(uid)
            self._last[uid] = (x, y, z)
            if last is not None:
                dz = abs(z - last[2])
                dxy = math.hypot(x - last[0], y - last[1])
                grounded = dz <= STEADY_DZ and dxy <= STEADY_DXY
        if not grounded:
            if hf is None or abs(z - OBSERVE_PIVOT - hf) > OBSERVE_TOLERANCE:
                return
        key = (int(x // self.cell), int(y // self.cell))
        if hf is None:
            self.learned[key] = z
        elif self._in_box_xy(x, y):
            self.learned[key] = z
        else:
            lift = z - OBSERVE_PIVOT - hf
            if abs(lift) <= LIFT_MAX:
                self.lift[key] = lift
            elif abs(lift) <= LIFT_HARD:
                # Grounded sample on steep terrain where the coarse
                # heightfield is off by >50m (real ridges deviate to
                # ~460m).  query() only reads learned on no-hf/box
                # cells, so the correction must live in lift to be
                # usable as floor for bots.
                self.lift[key] = lift
            else:
                # Extreme outlier (frozen-air report that passed the
                # steady gate): park it in learned where it stays
                # inert for hf-covered cells instead of poisoning
                # the floor.
                self.learned[key] = z
        self.dirty = True

    def save_learned(self, path):
        """Накопленные поправки на диск — переживают рестарт стека."""
        payload = {'lift': [[k[0], k[1], v] for k, v in self.lift.items()],
                   'learned': [[k[0], k[1], v] for k, v in self.learned.items()]}
        tmp = str(path) + '.tmp'
        with open(tmp, 'w') as h:
            json.dump(payload, h, separators=(',', ':'))
        os.replace(tmp, path)
        self.dirty = False

    def load_learned(self, path):
        """Поправки прошлых сессий — игрок уже прошёл эти клетки."""
        try:
            with open(path) as h:
                payload = json.load(h)
        except (OSError, ValueError):
            return
        for cx, cy, v in payload.get('lift', ()):
            self.lift[(cx, cy)] = v
        for cx, cy, v in payload.get('learned', ()):
            self.learned[(cx, cy)] = v

    def _cell_z(self, key):
        z = self.learned.get(key)
        return z if z is not None else self.cells.get(key)

    def _in_box_xy(self, x, y):
        """Точка в плане внутри бокса помещения (без проверки z)."""
        key = (int(x // self.cell), int(y // self.cell))
        for i in self._box_cells.get(key, ()):
            x0, y0, _z0, x1, y1, _z1 = self.boxes[i]
            if x0 <= x <= x1 and y0 <= y <= y1:
                return True
        return False

    def blocker(self, x, y, z):
        """Первый проп-окклюдер, содержащий точку ниже верха; иначе None."""
        key = (int(x // self.cell), int(y // self.cell))
        for i in self._occluder_cells.get(key, ()):
            ox, oy, orad, otop = self.occluders[i]
            if (x - ox) ** 2 + (y - oy) ** 2 <= orad * orad and z < otop:
                return self.occluders[i]
        return None

    def loot_wall_at(self, x, y, z):
        """Ярус лута строения >2м над голым рельефом — стена для бота.

        Лут-пол в клетке, поднятый над heightfield, — плита/пол здания:
        полная его геометрия не моделируется, поэтому точку под ярусом
        считаем непроходимой (terrain_only-подход ROE-LIVE). Бот на
        самом ярусе (z>=floor-20) не блокируется."""
        key = (int(x // self.cell), int(y // self.cell))
        low = self._loot_floors.get(key)
        if low is None or z >= low - 20:
            return None
        hf = (self.heightfield.floor(x, y)
              if self.heightfield is not None else None)
        if hf is None or low - hf <= LOOT_STEP:
            return None
        return low

    def _loot_step_floor(self, x, y, base):
        """Ярус лута в пределах шага над полом — это поверхность, а не плита.

        Сугроб/снежное поле несёт авторские точки лута, но его геометрии нет
        в cooked PhysX-поле: без этой поправки бот идёт по hf и оказывается на
        1-2м ПОД видимым снегом (live 20261023T203514: бот 0x...000e, 100%
        отсчётов на 192см ниже яруса своей клетки). Порог тот же, что у
        loot_wall_at: выше LOOT_STEP — стена, а не ступенька.
        """
        if self.heightfield is None:
            return None
        key = (int(x // self.cell), int(y // self.cell))
        low = self._loot_floors.get(key)
        if low is None or low <= base:
            return None
        hf = self.heightfield.floor(x, y)
        if hf is None or low - hf > LOOT_STEP:
            return None
        return low

    def blocked(self, x, y, z):
        """Точка внутри пропа-окклюдера (камень/сугроб) ниже его верха
        или под ярусом строения из лут-таблицы."""
        return (self.blocker(x, y, z) is not None
                or self.loot_wall_at(x, y, z) is not None)

    def blocker_near(self, x, y, z, pad):
        """Окклюдер с запасом pad: диск+pad содержит точку ниже верха.

        Каталог — цилиндры 3..3.5м, видимая геометрия камня шире; без
        поля бот чиркает по грани и «проходит сквозь» меш (live 054136).
        Соседние клетки проверяются — pad шире половины клетки быть не
        должен."""
        cx, cy = int(x // self.cell), int(y // self.cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self._occluder_cells.get((cx + dx, cy + dy), ()):
                    ox, oy, orad, otop = self.occluders[i]
                    r = orad + pad
                    if (x - ox) ** 2 + (y - oy) ** 2 <= r * r and z < otop:
                        return self.occluders[i]
        return None

    def blocked_near(self, x, y, z, pad):
        return (self.blocker_near(x, y, z, pad) is not None
                or self.loot_wall_at(x, y, z) is not None)

    def _prop_floor(self, x, y, base):
        """Верх пропа как ступенька, если точка внутри площадки.

        Подъём ограничен PROP_STEP — на низкий сугроб/камень бот встаёт
        сверху (как игрок на насте); выше — это стена, а не пол."""
        top = None
        key = (int(x // self.cell), int(y // self.cell))
        for i in self._occluder_cells.get(key, ()):
            ox, oy, orad, otop = self.occluders[i]
            if ((x - ox) ** 2 + (y - oy) ** 2 <= orad * orad
                    and base < otop <= base + PROP_STEP):
                top = otop if top is None else max(top, otop)
        return top

    def query(self, x, y):
        """Высота пола под точкой.

        Основа — поле высот (настоящий рельеф PhysX) плюс аддитивная
        поправка lift из репортов стоящего игрока; в боксах помещений —
        интерьерные кандидаты в окне над уличным полом; без покрытия
        поля — learned/сетка сущностей."""
        key = (int(x // self.cell), int(y // self.cell))
        hf = self.heightfield.floor(x, y) if self.heightfield else None
        if hf is None:
            z = self.learned.get(key)
            if z is not None:
                return z - OBSERVE_PIVOT
            return self._estimate(x, y)
        # Пол = рельеф + проверенная поправка игрока (lift): аддитивная
        # константа над hf сохраняет наклон клетки, а величина не
        # ограничена FLOOR_LIFT — cooked-терраин ошибается на десятки
        # метров в многоуровневых районах (город-плато +40м).
        floor = hf + self.lift.get(key, 0)
        # Соседняя клетка с резко другим lift — скальная стена где-то
        # внутри этой 32м клетки (hf её не выражает, вся стена в
        # поправке).  Без смешивания бот у края «висит» на плато над
        # обрывом: плавный пандус к уровню соседа по мере приближения
        # к границе убирает полёт.  Стена остаётся непроходимой —
        # CLIMB_MAX в shared_bots всё равно отсекает такой подъём.
        lift_here = self.lift.get(key, 0.0)
        fx = x / self.cell - key[0]
        fy = y / self.cell - key[1]
        for dx, dy, t in ((-1, 0, fx), (1, 0, 1.0 - fx),
                          (0, -1, fy), (0, 1, 1.0 - fy)):
            if t >= CLIFF_RAMP:
                continue
            delta = lift_here - self.lift.get(
                (key[0] + dx, key[1] + dy), 0.0)
            if delta <= CLIFF_DELTA:
                continue  # только спуск: высокая сторона стелется к низкой
            ramp = t / CLIFF_RAMP
            floor = (hf + lift_here) * ramp + (hf + lift_here - delta) * (1 - ramp)
        # Внутри бокса — кандидаты интерьерного пола (learned-репорт
        # минус pivot, оценка ячейки) в окне FLOOR_DIP..FLOOR_LIFT от
        # дна бокса: уровни пола отсчитываются от объёма, а не от
        # уличного рельефа — поле сугробов у площадки эвакуации лежит
        # на +6м над hf и окном от улицы отсекалось как «крыша»
        # (live 055019: боты тонули под снегом у точки эвакуации).
        # Крыша (+27м над дном) по-прежнему не проходит.
        boxes_here = [self.boxes[i] for i in self._box_cells.get(key, ())
                      if (self.boxes[i][0] <= x <= self.boxes[i][3]
                          and self.boxes[i][1] <= y <= self.boxes[i][4])]
        if boxes_here:
            cands = []
            z = self.learned.get(key)
            if z is not None:
                cands.append(z - OBSERVE_PIVOT)
            z = self.cells.get(key)
            if z is not None:
                cands.append(z)
            near = [c for c in cands
                    if any(b[2] - FLOOR_DIP <= c <= b[2] + FLOOR_LIFT
                           for b in boxes_here)]
            if near:
                floor = max(floor, *near)
        else:
            # Снаружи learned-репорт выше базы — поверхность, где реально
            # стоял аватар: сугроб, камень, гребень. Без этого пол клетки
            # остаётся hf+lift и бот идёт под настом, а LOS (_los_floor)
            # уже берёт верхнюю оценку — дезертир без стрельбы
            # (live 055019: бот утонул в сугробе и не видел игрока).
            z = self.learned.get(key)
            if z is not None:
                floor = max(floor, z - OBSERVE_PIVOT)
        step = self._loot_step_floor(x, y, floor)
        if step is not None:
            floor = step
        top = self._prop_floor(x, y, floor)
        return top if top is not None else floor

    def _estimate(self, x, y):
        """Базовая оценка без наблюдений: рельеф > сетка > ближайшая."""
        key = (int(x // self.cell), int(y // self.cell))
        if self.heightfield is not None:
            z = self.heightfield.floor(x, y)
            if z is not None:
                return z
        cx, cy = key
        total = weight = 0.0
        for dx in range(-NEIGHBOR_RADIUS, NEIGHBOR_RADIUS + 1):
            for dy in range(-NEIGHBOR_RADIUS, NEIGHBOR_RADIUS + 1):
                z = self._cell_z((cx + dx, cy + dy))
                if z is None:
                    continue
                mx = (cx + dx + 0.5) * self.cell - x
                my = (cy + dy + 0.5) * self.cell - y
                w = 1.0 / (mx * mx + my * my + 1.0)
                total += w * z
                weight += w
        if weight:
            return total / weight
        for dx, dy in self._spiral:
            z = self._cell_z((cx + dx, cy + dy))
            if z is not None:
                return z
        if self.cells:
            key = min(self.cells,
                      key=lambda k: (k[0] - cx) ** 2 + (k[1] - cy) ** 2)
            return self.cells[key]
        return None

    def interior_at(self, x, y):
        """Клетка с интерьерными сущностями — «внутри здания»."""
        return (int(x // self.cell), int(y // self.cell)) in self.interior

    def interior_box_at(self, x, y, z):
        """Индекс бокса помещения, в котором стоит точка, или None."""
        key = (int(x // self.cell), int(y // self.cell))
        for i in self._box_cells.get(key, ()):
            x0, y0, z0, x1, y1, z1 = self.boxes[i]
            if x0 <= x <= x1 and y0 <= y <= y1 and z0 <= z <= z1:
                return i
        return None

    def _opening_on_line(self, ax, ay, az, bx, by, bz):
        """Линия огня проходит рядом с проёмом (дверь/окно)?

        Выстрел изнутри наружу и обратно легален через дверной или
        оконный проём — сущности mj_door/pl_door/ob_door/rt_blinds
        стоят на проёмах. Проверка точками вдоль отрезка."""
        dist = math.hypot(bx - ax, by - ay)
        steps = min(LOS_STEPS_MAX, max(4, int(dist / LOS_STEP_CM)))
        for step in range(1, steps):
            t = step / steps
            x = ax + (bx - ax) * t
            y = ay + (by - ay) * t
            z = az + (bz - az) * t
            key = (int(x // self.cell), int(y // self.cell))
            seen = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    seen.update(self._opening_cells.get(
                        (key[0] + dx, key[1] + dy), ()))
            for i in seen:
                ox, oy, oz = self.openings[i]
                if (x - ox) ** 2 + (y - oy) ** 2 <= OPENING_RADIUS ** 2 \
                        and abs(z - oz) <= OPENING_Z:
                    return True
        return False

    def _los_floor(self, x, y):
        """Поверхность для линии огня — максимум известных высот.

        Огонь не проходит ни сквозь рельеф, ни сквозь плиту/настил:
        берём верхнюю оценку из (наблюдение, поле высот, сетка).
        Низкий репорт игрока в той же 32м клетке не должен стирать
        холм: learned-клетка на склоне сгладила откос, и бот пробил
        укрытие (live 20261018T023901). Для приземления — query()."""
        key = (int(x // self.cell), int(y // self.cell))
        # База — hf+lift как в query(), а не сырой растр: отрицательная
        # поправка означает ходимый уровень НИЖЕ значения растра
        # (навес/кровля в cooked-поле), и линия на этом уровне чиста.
        # Сырой hf давал «стену» +22м на ровном поле — боты 00001/00003
        # в 73м не видели друг друга (live 20261019T070650).
        hf = self.heightfield.floor(x, y) if self.heightfield else None
        base = (hf + self.lift.get(key, 0)) if hf is not None \
            else self._estimate(x, y)
        z = self.learned.get(key)
        if base is None:
            return z
        return max(base, z) if z is not None else base

    def _crosses_box(self, ax, ay, az, bx, by, bz):
        """Отрезок между точками снаружи пересекает бокс помещения."""
        dist = math.hypot(bx - ax, by - ay)
        steps = min(LOS_STEPS_MAX, max(4, int(dist / LOS_STEP_CM)))
        for step in range(1, steps):
            t = step / steps
            if self.interior_box_at(ax + (bx - ax) * t,
                                    ay + (by - ay) * t,
                                    az + (bz - az) * t) is not None:
                return True
        return False

    def line_clear(self, ax, ay, az, bx, by, bz, interior=True):
        """Открыта ли линия огня a->b по рельефу, пропам и стенам.

        Рост точки над полом: глаза бота EYE_HEIGHT, корпус цели
        TARGET_HEIGHT. Пол выше линии на просвет LOS_CLEARANCE —
        выстрел блокируется (холм, склон, набережная). Пропы-окклюдеры
        (камни, сугробы) — цилиндры (x,y,r,top): отрезок внутри
        радиуса и ниже верха блокируется.

        Стены — боксы помещений из interiorvolume+мебели: внутри↔
        снаружи (или сквозь здание) стрельба блокируется, КРОМЕ
        прохода рядом с проёмом — дверью или окном (сущности
        mj_door/pl_door/ob_door/rt_blinds), как в живой игре.
        Без боксов остаётся грубая клеточная маска.
        interior=False отключает проверку стен (ближний бой у двери).
        """
        if interior:
            if self.boxes:
                ba = self.interior_box_at(ax, ay, az)
                bb = self.interior_box_at(bx, by, bz)
                if ba != bb:
                    if ba is not None and bb is not None:
                        return False      # соседние помещения — стена
                    # внутри↔снаружи: только через проём
                    if not self._opening_on_line(ax, ay, az,
                                                 bx, by, bz):
                        return False
                elif ba is None and self._crosses_box(ax, ay, az,
                                                      bx, by, bz):
                    return False          # оба снаружи, сквозь здание
            elif self.interior_at(ax, ay) != self.interior_at(bx, by):
                return False              # старая клеточная маска
        dist = math.hypot(bx - ax, by - ay)
        steps = min(LOS_STEPS_MAX, max(8, int(dist / LOS_STEP_CM)))
        for step in range(1, steps):
            t = step / steps
            x = ax + (bx - ax) * t
            y = ay + (by - ay) * t
            z = (az + EYE_HEIGHT) + ((bz + TARGET_HEIGHT)
                                     - (az + EYE_HEIGHT)) * t
            floor = self._los_floor(x, y)
            if floor is not None and floor > z + LOS_CLEARANCE:
                return False
            key = (int(x // self.cell), int(y // self.cell))
            for i in self._occluder_cells.get(key, ()):
                ox, oy, orad, otop = self.occluders[i]
                if (x - ox) ** 2 + (y - oy) ** 2 <= orad * orad and z < otop:
                    return False
        return True
