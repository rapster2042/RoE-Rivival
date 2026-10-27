"""Общие боты-игроки комнаты2022: spawn/be7/553/547, FSM, общий лут смерти.

Это диагностическая политика нашего стенда, не восстановленный AI сервера:
навигация идёт по проверенной решётке 660 клеток рождения (прямые участки
без обхода препятствий), видимость — дистанция + сектор обзора без
окклюзии, шанс попадания шкалирован по дистанции. Боты не садятся в
транспорт и не эвакуируются; уцелевшие при вылете вертолёта погибают.
"""
import math
import random
import struct
from collections import OrderedDict
from copy import copy
from dataclasses import replace

from actor_probe_2022 import ActorState
from peer_avatar_2022 import (AIM_ACTION_OFF, AIM_ACTION_ON,
                              AIM_ACTION_KIND_REMOTE, AIM_STATUS_DEFAULT,
                              encode_peer_ik_sync)
from peer_motion_2022 import (encode_peer_action, encode_peer_trajectory,
                              encode_peer_stop, encode_peer_turn,
                              target_ik_pitch)
from peer_equipment_2022 import encode_peer_equipment
from weapon_probe_2022 import (WeaponState, GUN_PROFILES, RELOAD_TIMES,
                              ARMOR_DURABILITY, ARMOR_ABSORB)
from world_probe_2022 import encode_health

# Диапазон вне avatar(0x204)/actor-мишени(0x205): отдельные unit ids.
BOT_UID_BASE = 0x2080000100000
BOT_NAMES = tuple(f'Bot-{n:02d}' for n in range(1, 65))
# Проверенная живым клиентом раскладка avatar: character id + 4 нуля.
BOT_CHARACTERS = tuple(range(1, 27))
# Оружие/наборы из проверенных каталогов; боезапас лутается после смерти.
BOT_GUNS = (10201, 10202, 10101, 10102, 10402, 20001)
BOT_AMMO = {10201: 30001, 10202: 30002, 10101: 30001, 10102: 30002,
            10402: 30004, 20001: 30005}
BOT_HELMETS = (0, 200101, 200102, 200103)
BOT_VESTS = (201001, 201002, 201003, 201004)
BOT_KITS = (200904, 200906, 200907)
BOT_MEDKIT = 70001
# Учебный баланс бота: hp/durability ниже игрокских — иначе жилет L3
# (3000, absorb 55%) + hp1000 требовал ~250 попаданий (live 042138).
BOT_HP = 400
BOT_ARMOR_SCALE = 10       # durability жилета/шлема бота = табличная/10
# Диагностические пределы стенда: дистанции в сантиметрах сцены.
SIGHT_RANGE = 10000          # обзор бота ~100м
DOORWAY_RANGE = 800          # не используется: проёмы моделируются явно
FOV_COS = math.cos(math.radians(75))
ENGAGE_KEEP_FACTOR = 1.2     # гистерезис удержания цели
TARGET_SWITCH_FACTOR = 0.7   # новая цель должна быть заметно ближе
FIRE_INTERVAL = 0.45
GROUND_SNAP = 50           # привязка к полу в пределах 0.5м без «полёта»
SYNC_INTERVAL = 2.0        # абсолютный 0x27 ре-анкор актора каждые ~2с
MOTION_KEEPALIVE = 1.2     # траектория в покое — стоячий актор клиента не засыпает
WANDER_DELAY = 7.0         # пауза у цели перед шагом осмотра
WANDER_RADIUS = 2000       # осмотр в пределах стоянки (< EVAC_STANDOFF)
RADAR_INTERVAL = 1.0       # позиции ботов в лог для радара каждую секунду
FIRE_FINISH_DELAY = 0.25     # как у actor-мишени: 0x11d8(3)+0x11d4(mode1)
HIT_BASE = 0.9               # p(0м); линейно к HIT_FLOOR на SIGHT_RANGE
HIT_FLOOR = 0.2
MOVE_SPEED = 400             # как у actor-мишени
FALL_SPEED = 4500            # падение до пола ~45м/с (высоты в сантиметрах)
WAYPOINT_REACHED = 300
AVOID_STEP = 450             # боковой шаг обхода пропа (> радиуса 3.5м)
AVOID_MAX = 8                # боковых шагов на один маршрут, потом узел скипается
CLIMB_MAX = 250              # максимальный шаг вверх по полу (стена/кромка)
PROP_PAD = 200               # поле вокруг цилиндра пропа — меш камня шире
TARGET_AIRBORNE = 500        # цель >5м над полом модели — LOS недостоверен
SPAWN_ANCHOR_TOLERANCE = 100  # лут-якорь валиден как точка рельефа, если
                              # его z в пределах 1м от пола (иначе — пол
                              # строения: бот падал бы сквозь него)
EVAC_STANDOFF = 2500         # боты подходят к вертолёту, но не садятся
HUNT_MEMORY = 20.0           # seconds to pursue last seen position after LOS loss
COVER_TRIES = 12             # cover point candidates per plan
COVER_MIN_D = 1500           # cover 15-45m from the bot
COVER_MAX_D = 4500
COVER_TARGET_MIN = 1200      # never pick cover closer than 12m to the target
STRAFE_DIST = 2000           # perpendicular fallback offset when no cover exists
STORM_HOP_MIN = 8000         # >80m from safety: walking can't beat zone ticks
STORM_HOP_SEE = 15000        # member within 150m: keep the hop believable
STORM_HOP_NEAR_STEP = 5000   # 50m step toward safety when possibly observed
STORM_HOP_CD = 3.0           # seconds between rescue hops
CELL_EDGE = 17000            # клетки 128м; связь до ~170м по горизонтали
POINT_STEP = 800             # узел SC25 каждые 8м пройденного пути
# Retail периодически подбрасывал ботов в район впереди игрока, иначе
# живые участники пустой карты встречались бы редко. Не все сразу:
# лимит перебросов за период, и только боты вне поля зрения всех
# участников (иначе — видимый «поп» модели).
MIGRATION_FIRST_DELAY = 20.0   # первая проверка — после посадки спавна
MIGRATION_INTERVAL = 15.0      # период пересмотра раскладки
MIGRATION_NEAR = 20000         # бот в пределах 200м уже «присутствует»
MIGRATION_FAR = 35000          # перебрасываем только >350м от всех членов
MIGRATION_MIN_D = 12000        # высадка в 120-220м от игрока
MIGRATION_MAX_D = 22000
MIGRATION_DESIRED = 4          # целевое число живых ботов рядом
MIGRATION_PER_TICK = 2         # максимум перебросов на участника за период
MIGRATION_TRIES = 16           # попыток подобрать точку высадки
MIGRATION_BOT_GAP = 4000       # минимум 40м между ботами на высадке
MIGRATION_HEADING_MIN = 1500   # смещение >15м между проверками = игрок идёт
MIGRATION_HEADING_ARC = math.radians(75)  # сектор «впереди» по курсу

UNSTICK_WINDOW = 12.0       # окно контроля продвижения по маршруту
UNSTICK_MIN_PROGRESS = 300  # <3м смещения за окно при непустом пути = застрял
UNSTICK_MIN_D = 3000        # точка выхода из кармана — 30-80м от бота
UNSTICK_MAX_D = 8000
UNSTICK_TRIES = 16


class BotGraph:
    """Путевой граф по клеткам рождения map16010: соседи по радиусу."""

    def __init__(self, points):
        self.nodes = [tuple(p[2:]) for p in points]
        self.edges = [[] for _ in self.nodes]
        for i, a in enumerate(self.nodes):
            for j in range(i + 1, len(self.nodes)):
                b = self.nodes[j]
                dx, dy = a[0]-b[0], a[1]-b[1]
                if dx*dx + dy*dy <= CELL_EDGE**2:
                    w = math.sqrt(dx*dx + dy*dy)
                    self.edges[i].append((j, w))
                    self.edges[j].append((i, w))
        if not all(self.edges):
            raise ValueError('Граф клеток рождения не связный')

    def nearest(self, position):
        px, py = position[0], position[1]
        return min(range(len(self.nodes)),
                   key=lambda i: (self.nodes[i][0]-px)**2 + (self.nodes[i][1]-py)**2)

    def path(self, start_position, goal_position):
        """A* ближайшая клетка→ближайшая клетка + прямой хвост к цели."""
        start, goal = self.nearest(start_position), self.nearest(goal_position)
        open_set = {start: 0.0}
        came, best, order = {}, {start: 0.0}, [start]
        while order:
            node = min(order, key=lambda n: open_set.get(n, 1e18))
            order.remove(node)
            if node == goal:
                break
            for nxt, w in self.edges[node]:
                score = best[node] + w
                if score < best.get(nxt, 1e18):
                    best[nxt] = score
                    hx, hy = (self.nodes[nxt][0]-self.nodes[goal][0],
                              self.nodes[nxt][1]-self.nodes[goal][1])
                    open_set[nxt] = score + math.sqrt(hx*hx + hy*hy)
                    came[nxt] = node
                    if nxt not in order:
                        order.append(nxt)
        if goal != start and goal not in came:
            return [tuple(goal_position)]
        nodes = [goal]
        while nodes[-1] != start:
            nodes.append(came[nodes[-1]])
        points = [self.nodes[i] for i in reversed(nodes)]
        if goal_position != points[-1]:
            points.append(tuple(goal_position))
        return points[1:] if len(points) > 1 else points

    def safe_node(self, position, world):
        """Ближайшая клетка вне закрытых регионов; логика BotController.safe."""
        grid = world.fixture.region_grid
        if grid is None:
            return None
        closed = world.closed
        frozen = world.fixture.zero_region_is_frozen

        def safe(node):
            # FastRegionIndex requires a 3-element position; nav nodes are 2D.
            point = (node[0], node[1], 0)
            if frozen and hasattr(grid, 'region_id') \
                    and grid.region_id(point) in (0, None):
                return False
            regions = grid.regions(point)
            return bool(regions) and not regions <= closed

        px, py = position[0], position[1]
        candidates = [((n[0]-px)**2 + (n[1]-py)**2, n) for n in self.nodes
                      if safe(n)]
        return min(candidates)[1] if candidates else None

    @staticmethod
    def bot_unsafe(position, world):
        """Та же оценка зоны, что у BotController.safe(), для бота."""
        # hazard — диагностический opt-in; игрок учитывает hazard_enabled,
        # бот должен видеть тот же мир (live 064239: бот 00008 умер от
        # zone damage внутри выключенного hazard-box у evacuation_refuge).
        if getattr(world.fixture, 'hazard_enabled', True):
            x0, y0, x1, y1 = world.fixture.hazard
            if x0 <= position[0] <= x1 and y0 <= position[1] <= y1:
                return True
        grid = world.fixture.region_grid
        if grid is None:
            return False
        if world.fixture.zero_region_is_frozen and hasattr(grid, 'region_id') \
                and grid.region_id(position) in (0, None):
            return True
        regions = grid.regions(position)
        return not regions or regions <= world.closed


class SharedBot:
    """Одна общая сущность: внешность, инвентарь смерти, ActorState, FSM."""

    def __init__(self, sequence, avatar_fixture, weapon_fixture, rng, position):
        self.uid = BOT_UID_BASE + sequence
        self.name = BOT_NAMES[sequence-1]
        self.avatar = [rng.choice(BOT_CHARACTERS), 0, 0, 0, 0]
        self.gun = rng.choice(BOT_GUNS)
        self.magazine = GUN_PROFILES[self.gun][1]
        helmet = rng.choice(BOT_HELMETS)
        vest = rng.choice(BOT_VESTS)
        equipment = tuple(x for x in (helmet, vest, rng.choice(BOT_KITS)) if x)
        ammo = BOT_AMMO[self.gun]
        medkits = rng.randint(0, 3)
        starter = (((self.gun, 1), (ammo, rng.randint(60, 120)))
                   + (((BOT_MEDKIT, medkits),) if medkits else ()))
        inventory_fixture = replace(weapon_fixture, gun_on_start=True,
                                    starting_equipment=equipment,
                                    starter_items=starter, reserve=0)
        self.inventory = WeaponState(inventory_fixture, self.uid)
        if self.inventory.guns:
            self.inventory.guns[1].uid = (0x1e00000200000000 + self.uid) & 0xffffffffffffffff
            self.inventory.hand_slot = 1
        self.inventory.position = position
        actor_fixture = replace(avatar_fixture, uid=self.uid, position=position,
                                waypoints=(position,), speed=MOVE_SPEED,
                                hp=BOT_HP, damage=30,
                                armor_definition=vest,
                                armor_durability=ARMOR_DURABILITY.get(vest, 0)
                                                  // BOT_ARMOR_SCALE,
                                armor_absorb_percent=ARMOR_ABSORB.get(vest, 0),
                                collision_boxes=())
        self.actor = ActorState(actor_fixture)
        self.actor.started = True
        self.phase = 'travel'
        self.path = []
        self.goal = None
        self.target = None
        self.next_shot_at = 0.0
        self.reload_until = 0.0
        self.finish_at = None
        self.last_shot_target = None
        self.zone_hurt_at = 0.0
        self.moved_at = None
        self.settled_at = None
        self.grounded = False
        self.sync_at = None    # первый 0x27 ре-анкор через SYNC_INTERVAL после спавна
        self.moving = False
        self.idle_at = None    # момент остановки — отсчёт паузы до осмотра
        self.keepalive_at = 0.0  # последняя траектория в покое
        self.strolling = False # идёт короткий шаг осмотра у точки стоянки
        self.detours = 0       # боковые шаги обхода на текущем маршруте
        self.watch_at = None   # unstick watchdog window start
        self.watch_pos = None  # anchor position at window start
        self.cover_goal = None # cover point for the current engagement
        self.hunt_until = 0.0  # maneuver to last seen target position until then
        self.last_target_pos = None
        self.storm_hop_at = 0.0  # rescue-hop cooldown
        self.cell = None
        self.motion_sequence = 0
        self.motion_direction = (0.0, 1.0, 0.0)
        self.yaw = rng.uniform(-180.0, 180.0)
        self.aiming = False
        self.look_angles = None
        self.look_direction = None
        self.look_pitch = 0.0
        self.look_ik = None
        self.ammo_counter = 0

    @property
    def position(self):
        return self.actor.position

    @property
    def dead(self):
        return self.phase == 'dead'


class SharedBotRooms:
    """Реестр ботов по room_uid; состояние одно на комнату, не по соединению."""

    def __init__(self, avatar_fixture, weapon_fixture, spawn_points, count,
                 pellet_damage, seed=None, ground=None):
        if not avatar_fixture or not weapon_fixture or not spawn_points:
            raise ValueError('Общие боты требуют avatar/weapon fixture и клетки')
        if type(count) is not int or not 1 <= count <= 64:
            raise ValueError('Количество ботов 1..64')
        if type(pellet_damage) is not int or not 1 <= pellet_damage <= 100:
            raise ValueError('Учебный урон ботов — целый 1..100')
        self.fixture = avatar_fixture
        self.weapon_fixture = weapon_fixture
        self.cells = tuple(spawn_points)
        self.graph = BotGraph(spawn_points)
        self.count = count
        self.damage = pellet_damage
        self.ground = ground
        self.rng = random.Random(seed)
        self.rooms = {}

    def sync(self, avatars, evac_rooms, death_parcels):
        """Группировка членов по комнатам и однократный spawn ботов.

        eligible — все живые участники комнаты (им уходят pick-маркеры
        ещё до world: в retail боты отмечены на pick map); members —
        вошедшие в мир (им реплицируются боты)."""
        grouped, eligible = {}, {}
        for (room_uid, _), member in avatars.members.items():
            state = member['state']
            if (not member['avatar'] or state.get('transport_retired')
                    or state.get('match_finished')):
                continue
            eligible.setdefault(room_uid, {})[member['avatar']] = member
            if not state.get('world') or not state.get('room_sent'):
                continue
            grouped.setdefault(room_uid, {})[member['avatar']] = member
        for room_uid in tuple(self.rooms):
            if room_uid not in eligible:
                del self.rooms[room_uid]
        for room_uid, members in eligible.items():
            room = self.rooms.get(room_uid)
            if room is None:
                room = self.rooms[room_uid] = {'members': {}, 'eligible': {},
                                               'bots': [], 'spawned': False,
                                               'seen': set(), 'picked': set(),
                                               'shots': {}, 'hits': {},
                                               'events': []}
            room['eligible'] = members
            room['members'] = grouped.get(room_uid, {})
            room['evac'] = evac_rooms.get(room_uid) if evac_rooms else None
            room['parcels'] = death_parcels(room_uid) if death_parcels else None
            if not room['spawned'] and members:
                room['spawned'] = True
                cells = self.rng.sample(self.cells, self.count)
                for n, cell in enumerate(cells, 1):
                    bot = SharedBot(n, self.fixture, self.weapon_fixture,
                                    self.rng, self._spawn_point(cell))
                    bot.cell = (cell[0], cell[1])
                    room['bots'].append(bot)

    def _spawn_point(self, cell):
        """Точка сброса клетки: приоритет — нижний уровень точки лута
        в пределах клетки (где лут гарантированно лежит на полу без
        провала сквозь текстуры); иначе — точный пол клетки из
        PhysX-поля высот и сдвиг по кольцу из-под столбов пропов."""
        position = tuple(cell[2:])
        if self.ground is None:
            return position
        loot_spawns = getattr(self.ground, 'loot_spawns', ())
        if loot_spawns:
            px, py = position[0], position[1]
            # Лут-точка — проверенный авторский якорь только там, где
            # её высота совпадает с рельефом (terrain_only, как стенд
            # ROE-LIVE). Точка на +2м..+22м над hf — пол строения;
            # _settle затем тянет бота сквозь него к голому терраину.
            near = []
            for p in loot_spawns:
                if (abs(p[0] - px) > 6400 or abs(p[1] - py) > 6400
                        or self.ground.blocked(p[0], p[1], p[2])):
                    continue
                floor = self.ground.query(p[0], p[1])
                if (floor is not None
                        and abs(p[2] - floor) <= SPAWN_ANCHOR_TOLERANCE):
                    near.append((p[0], p[1], round(floor)))
            if near:
                return self.rng.choice(near)
        floor = self.ground.query(position[0], position[1])
        if floor is not None and not self.ground.blocked(position[0],
                                                         position[1], floor):
            return (position[0], position[1], round(floor))
        for radius in (800, 1600, 3200, 6400):
            for k in range(8):
                angle = math.tau * k / 8
                x = position[0] + math.cos(angle) * radius
                y = position[1] + math.sin(angle) * radius
                ground = self.ground.query(x, y)
                if (ground is not None
                        and not self.ground.blocked(x, y, ground)):
                    return (round(x), round(y), round(ground))
        return position

    @staticmethod
    def _stand(bot):
        """Позиция на проводе: z — точка пола под стопами, как в
        репортах C2S живых участников (измеренные узлы ROE-LIVE
        native309 = рельеф+0..1см; их стационарные мишени передают
        сырой terrain_z и стоят на земле ровно). Сдвиг +110 к центру
        капсулы был ошибочной компенсацией старого неточного рельефа
        и ставил всех ботов на ~1.1м над землёй."""
        x, y, z = bot.position
        return (round(x), round(y), round(z))

    def spawn_packets(self, bot, tick):
        """Тот же набор, что у PeerAvatarRooms: listing+profile+553+547+1069+27."""
        fixture = self.fixture
        position = self._stand(bot)
        listing = bytearray(fixture.listing)
        struct.pack_into('>3i', listing, 1, *position)
        struct.pack_into('>Q', listing, 14, bot.uid)
        profile = bytearray(fixture.profile)
        struct.pack_into('>Q', profile, 8, bot.uid)
        # Идент @16: u32 len+NUL-строка, как у PeerAvatarRooms.packets().
        name = bot.name.encode('utf-8')[:60] + b'\x00'
        profile = bytearray(profile[:16] + struct.pack('>I', len(name)) + name
                            + profile[16 + 4 + 13:])
        delta = len(name) - 13
        struct.pack_into('>3i', profile, 1051+delta, *position)
        struct.pack_into('>I', profile, 1064+delta, tick)
        movement = bytearray(fixture.movement)
        struct.pack_into('>Q3i', movement, 0, bot.uid, *position)
        struct.pack_into('>I', movement, 31, tick)
        return [(0x565, bytes(listing)), (0xbe7, bytes(profile)),
                (0x553, struct.pack('>Q5IB', bot.uid, *bot.avatar, 1)),
                (0x547, encode_peer_equipment(bot.inventory, bot.uid)),
                # Original peer spawn tail: status 21 (AIMING) before the
                # first scope transition replaces it with 40/41.
                (0x2b, struct.pack('>QI', bot.uid, AIM_STATUS_DEFAULT)),
                (0x1069, encode_health(bot.uid, bot.actor.hp)),
                (0x27, bytes(movement))]

    def _dead_listing(self, bot):
        listing = bytearray(self.fixture.listing)
        struct.pack_into('>3i', listing, 1, *self._stand(bot))
        struct.pack_into('>Q', listing, 14, bot.uid)
        return bytes(listing)

    def emit(self, room, packets):
        """Пакеты каждому живому участнику комнаты."""
        return [(member['state'], list(packets))
                for member in room['members'].values()]

    @staticmethod
    def _pick_packet(bot):
        """Маркер выбора клетки на карте — как в retail: боты отмечены
        на pick map в конце матчмейкинга. Формат зеркалит ответ
        choose_cell 0x11e0 {kind=1,type=2,uid,x,y,flag}."""
        if not bot.cell:
            return None
        return (0x11e0, struct.pack('>IIQIII', 1, 2, bot.uid,
                                    bot.cell[0], bot.cell[1], 0))

    def snapshots(self, room, tick):
        """Поздний вход: весь состав ботов одному участнику.

        По одной группе пакетов на бота и на кадр: единый слепок 20 ботов
        это ~22КБ, а live2022 клиент молча роняет кадры сверх ~3.6КБ
        (run 20261017T224450 — spawn не отрендерился ни разу)."""
        outputs = []
        for avatar, member in room['members'].items():
            if avatar in room['seen']:
                continue
            room['seen'].add(avatar)
            for bot in room['bots']:
                pick = None if avatar in room['picked'] else self._pick_packet(bot)
                if not bot.dead:
                    messages = self.spawn_packets(bot, tick)
                else:
                    messages = [(0x565, self._dead_listing(bot)),
                                (0x533, struct.pack('>IQ', 0, bot.uid))]
                outputs.append((member['state'],
                                ([pick] if pick else []) + messages))
                if not bot.dead and bot.aiming and bot.look_direction is not None:
                    # Keep the existing spawn frame size; replay current look
                    # separately so a late joiner need not wait for a change.
                    # Peer aim rides 0x2b STATUS_ENTER (original capture):
                    # 40 = E_EU_ST_SCOPE_AIMING.
                    replay = [
                        self._ammo(bot),
                        (0x2b, struct.pack('>QI', bot.uid, 40)),
                        encode_peer_action(self.fixture.movement, bot.uid,
                                           self._stand(bot), AIM_ACTION_ON,
                                           tick, bot.motion_sequence,
                                           AIM_ACTION_KIND_REMOTE),
                        encode_peer_turn(bot.uid,bot.look_direction,tick)]
                    ik = encode_peer_ik_sync(bot.uid,
                                             struct.pack('>3f', bot.look_pitch,
                                                         0.0, 0.0),
                                             tick & 0xffffffff, 0)
                    if ik is not None:
                        replay.append(ik)
                    outputs.append((member['state'], replay))
            room['picked'].add(avatar)
        return outputs

    def note_fire(self, state, payload, event, now):
        """Дедуп выстрелов участника для проверки попаданий по ботам."""
        room = self.rooms.get(state.get('room_uid'))
        member = room and next((m for m in room['members'].values()
                                if m['state'] is state), None)
        if (not member or event.get('action') != 'fire' or len(payload) != 49):
            return
        shot = struct.unpack_from('>6f', payload)
        position = state['world'].position
        if (position is None or not all(math.isfinite(x) for x in (*shot, *position))
                or sum((a-b)**2 for a, b in zip(shot[:3], position)) > 300**2):
            return
        counter = struct.unpack_from('>I', payload, 24)[0]
        tick = struct.unpack_from('>I', payload, 29)[0]
        shots = room['shots'].setdefault(member['avatar'], OrderedDict())
        shots.setdefault((counter, tick), (shot, event.get('gun_definition'), now))
        while len(shots) > 64:
            expired, _ = shots.popitem(last=False)
            room['hits'][member['avatar']] = {
                key for key in room['hits'].get(member['avatar'], set())
                if key[:2] != expired}

    def hit(self, state, payload, now):
        """0x11dd участника по общему боту; armor/death как у ActorState."""
        if len(payload) != 106:
            return None
        target_uid = struct.unpack_from('>Q', payload, 4)[0]
        room = self.rooms.get(state.get('room_uid'))
        bot = room and next((b for b in room['bots']
                             if b.uid == target_uid), None)
        if bot is None or bot.dead:
            return None
        member = next((m for m in room['members'].values()
                       if m['state'] is state), None)
        if member is None:
            return None
        shots = room['shots'].get(member['avatar'], {})
        key = (struct.unpack_from('>I', payload, 88)[0],
               struct.unpack_from('>I', payload, 96)[0])
        accepted = shots.get(key)
        if not accepted or not 0 <= now - accepted[2] <= 2:
            return [(state, [])], dict(action='bot_hit_rejected',
                                       reason='unaccepted_or_expired_shot',
                                       target=hex(target_uid))
        actor = bot.actor
        actor.shots = shots
        actor.hits = room['hits'].setdefault(member['avatar'], set())
        # Урон симметричен PvP: pellet_damage участника по боту, не
        # собственный fixture бота (было 30 — дробовик не убивал за 2).
        responses, event = actor.hit(payload, damage=self.damage)
        if event['action'] != 'hit_damage':
            return [(state, [])], dict(event, action='bot_hit_rejected')
        # 0x11ea — только стрелку; здоровье/броня/смерть бота — всей комнате
        # включая стрелка: бот не имеет own-канала как участник.
        outputs = [(state, [(code, body) for code, body in responses
                            if code == 0x11ea])]
        shared = [(code, body) for code, body in responses if code != 0x11ea]
        if shared:
            outputs += self.emit(room, shared)
        bot.inventory.position = self._stand(bot)
        if bot.actor.fixture.armor_definition:
            bot.inventory.equipment_durability[12] = bot.actor.armor_durability
        event.update(action='bot_hit_damage', bot=hex(bot.uid),
                     source=hex(member['avatar']),
                     shared_bots=True, original_damage_formula=False)
        if actor.hp <= 0:
            outputs += self._loot(room, bot)
            bot.phase = 'dead'
            bot.target = None
        return outputs, event

    def _loot(self, room, bot):
        """Однократный выпад инвентаря бота в общие пакеты смерти."""
        parcels = room.get('parcels')
        if parcels is None:
            return []
        drops, _ = parcels.transfer(bot.inventory, bot.position)
        return self.emit(room, drops) if drops else []

    def kill(self, room, bot):
        """Смерть без выстрела (шторм/вылет): 1069+533 всем и лут."""
        bot.phase = 'dead'
        bot.target = None
        bot.actor.hp = 0
        packets = [(0x1069, encode_health(bot.uid, 0)),
                   (0x533, struct.pack('>IQ', 0, bot.uid))]
        return self.emit(room, packets) + self._loot(room, bot)

    @staticmethod
    def _distance(a, b):
        return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)

    def _visible_targets(self, room, bot, now):
        """Живые цели в зоне обзора: участники и другие боты.

        Участник в транспорте/на посадке исключается; бот-цель — любой
        живой бот комнаты кроме стрелка (friendly fire не введён —
        retail bots hostile, live 20261018T023901: боты «ждали» игрока
        и не стреляли друг в друга именно потому, что целей-ботов нет)."""
        out = []
        for member in room['members'].values():
            state = member['state']
            world = state.get('world')
            vehicle = state.get('vehicles')
            if (not world or world.dead or world.position is None
                    or (vehicle and (vehicle.seated
                                     or vehicle.climb_protected(now)))):
                continue
            distance = self._distance(bot.position, world.position)
            engaged = bot.target is member
            if distance > SIGHT_RANGE*(ENGAGE_KEEP_FACTOR if engaged else 1):
                continue
            if not engaged and bot.moving and distance > WAYPOINT_REACHED:
                # Первичное обнаружение — только в секторе обзора по
                # курсу. Стоячий бот — караульный: сканирует весь круг,
                # иначе у площадки все смотрят на борт и никого не видят
                # (live 055853: боты у вертолёта не вступали в бой).
                dx = world.position[0]-bot.position[0]
                dy = world.position[1]-bot.position[1]
                length = math.hypot(dx, dy) or 1.0
                facing = (dx*math.sin(math.radians(bot.yaw))
                          + dy*math.cos(math.radians(bot.yaw)))
                if facing/length < FOV_COS:
                    continue
            if self.ground is not None:
                # Реальный z цели выше пола модели — стрелок палит по
                # устаревшей карте сквозь невидимый для игрока рельеф
                # (live 060439: бот под сугробом, z цели +26м над полом
                # клетки). Прыжок/перелаз <5м не отсекаются; крыша и
                # глиссер — тоже не цель. Снимается, когда observe()
                # поднимет пол клетки до реальной поверхности.
                tfloor = self.ground.query(world.position[0],
                                           world.position[1])
                if (tfloor is not None
                        and world.position[2]-tfloor > TARGET_AIRBORNE):
                    continue
            if self.ground is not None and not self._line_of_fire(
                    bot, world.position):
                continue
            out.append((distance, member))
        for other in room['bots']:
            if other is bot or other.dead:
                continue
            distance = self._distance(bot.position, other.position)
            engaged = bot.target is other
            if distance > SIGHT_RANGE*(ENGAGE_KEEP_FACTOR if engaged else 1):
                continue
            if not engaged and bot.moving and distance > WAYPOINT_REACHED:
                dx = other.position[0]-bot.position[0]
                dy = other.position[1]-bot.position[1]
                length = math.hypot(dx, dy) or 1.0
                facing = (dx*math.sin(math.radians(bot.yaw))
                          + dy*math.cos(math.radians(bot.yaw)))
                if facing/length < FOV_COS:
                    continue
            if self.ground is not None:
                tfloor = self.ground.query(other.position[0],
                                           other.position[1])
                if (tfloor is not None
                        and other.position[2]-tfloor > TARGET_AIRBORNE):
                    continue
            if self.ground is not None and not self._line_of_fire(
                    bot, other.position):
                continue
            out.append((distance, other))
        return out

    @staticmethod
    def _target_position(target):
        """Позиция цели: живой участник или бот."""
        if isinstance(target, SharedBot):
            return target.position
        return target['state']['world'].position

    def _line_of_fire(self, bot, target):
        """Препятствия между ботом и целью — рельеф, пропы и стены.

        Рельеф и камни/сугробы — выборка пола GroundMap.line_clear.
        Стены — боксы помещений: внутри↔снаружи только через проём
        (дверь/окно из выгрузки). Исключения «в упор» нет: снятая у
        двери проверка пропустила выстрел сквозь глухую стену с 7м
        (live 20261018T023901). Полного PhysX стен нет — совместимая
        аппроксимация по извлечённым сущностям."""
        return self.ground.line_clear(*bot.position, *target)

    def _fire(self, room, bot, tick, now):
        """Выстрел: aim+fire-sync + дистанционный шанс попадания."""
        member = bot.target
        target = self._target_position(member)
        dx, dy = target[0]-bot.position[0], target[1]-bot.position[1]
        yaw = math.degrees(math.atan2(dy, dx))
        bot.yaw = yaw
        bot.motion_direction = (dx, dy, 0.0)
        bot.actor.attack_counter = (bot.actor.attack_counter + 1) & 0xffffffff
        packets = self._aim_packets(bot, target, tick)
        packets += [(0x11d8, struct.pack('>BQII', 0, bot.uid,
                                      tick & 0xffffffff, 0)),
                 (0x11d4, bot.actor.fire_sync(tick, 0, target))]
        bot.finish_at = now + FIRE_FINISH_DELAY
        bot.last_shot_target = tuple(target)
        bot.next_shot_at = now + FIRE_INTERVAL
        bot.magazine -= 1
        if bot.magazine <= 0:
            bot.reload_until = now + RELOAD_TIMES.get(bot.gun, (2.0, 2.8))[1]
            bot.magazine = GUN_PROFILES[bot.gun][1]
        # Original relays a peer weapon's magazine (0x4a0); bots have no client
        # to stream it, so the server synthesizes it on fire/aim. Without a
        # magazine the remote SCOPE_AIMING pose degrades into LOAD_BULLET
        # instead of pointing (same fix that made the player peer point).
        packets.append(self._ammo(bot))
        distance = self._distance(bot.position, target)
        chance = max(HIT_FLOOR,
                     HIT_BASE - distance/SIGHT_RANGE*(HIT_BASE-HIT_FLOOR))
        victim_packets, death_outputs = [], []
        if isinstance(member, SharedBot):
            if not member.dead and self.rng.random() < chance:
                victim_packets, death_outputs = self._damage_bot(
                    room, bot, member)
        elif not member['state']['world'].dead and self.rng.random() < chance:
            victim_packets = self._damage_victim(bot, member['state'])
        room['events'].append(dict(
            action='bot_fired', bot=hex(bot.uid),
            position=tuple(map(round, bot.position)),
            target_position=tuple(map(round, target)),
            distance=round(distance), chance=round(chance, 2),
            hit=bool(victim_packets or death_outputs)))
        return packets, victim_packets, death_outputs, distance, chance

    def _ammo(self, bot):
        """0x4a0 AMMUNITION_SYNC for a bot actor (slot 1, current magazine).

        Mirrors the original peer broadcast (602 peer records): a remote weapon
        needs a magazine before SCOPE_AIMING(40), otherwise the status degrades
        into the LOAD_BULLET animation. The counter is monotonic per bot so two
        sends in one frame (aim transition + shot) are both applied."""
        bot.ammo_counter = (bot.ammo_counter + 1) & 0xffffffff
        return (0x4a0, struct.pack('>QIII', bot.uid, 1,
                                   max(0, bot.magazine) & 0xffffffff,
                                   bot.ammo_counter))

    def _aim_packets(self, bot, target, tick):
        """Keep facing and upper-body pitch separate from travel direction."""
        packets = []
        aiming = target is not None
        if bot.aiming != aiming:
            bot.aiming = aiming
            bot.motion_sequence = (bot.motion_sequence + 1) & 0xffffffff
            if aiming:
                # Magazine before SCOPE_AIMING: a remote weapon with no clip
                # would degrade the status into LOAD_BULLET (no point).
                packets.append(self._ammo(bot))
            # Original peer channel: 0x2b STATUS_ENTER {rid, status};
            # 40 = SCOPE_AIMING, 41 = STOP_AIM (AIM group default).
            packets.append((0x2b, struct.pack('>QI', bot.uid,
                                              40 if aiming else 41)))
            # Decomp channel: 0x27 kind 0x10 (remote actor branch) action
            # 20/AIM | 21/DIS_AIM at the current position without flushing
            # motion.
            packets.append(encode_peer_action(
                self.fixture.movement, bot.uid, self._stand(bot),
                AIM_ACTION_ON if aiming else AIM_ACTION_OFF,
                tick, bot.motion_sequence, AIM_ACTION_KIND_REMOTE))
        if not aiming:
            bot.look_angles = None
            bot.look_direction = None
            bot.look_ik = None
            return packets
        origin = (*bot.position[:2], bot.position[2] + 150)
        end = (*target[:2], target[2] + 120)
        direction = tuple(b-a for a,b in zip(origin,end))
        pitch = target_ik_pitch(origin,end)
        bot.look_direction, bot.look_pitch = direction, pitch
        turn = encode_peer_turn(bot.uid,direction,tick)
        # Quantized direction (incl. vertical) is the remote aim/facing signal.
        angles = turn[1][8:12]
        if bot.look_angles != angles:
            packets.append(turn)
            bot.look_angles = angles
        # Upper-body aim point rides the original 0x11d6 SHOOT_IK_SYNC relay
        # (float1 = aim pitch in degrees, up-negative -> actor+0x680). Peers get
        # it relayed in the original stream (602 peer records), so a bot that
        # only sends the 0x30 turn never raises/points its weapon. Gate on
        # change like the turn so a held aim does not flood the poll.
        if bot.look_ik != pitch:
            ik = encode_peer_ik_sync(bot.uid, struct.pack('>3f', pitch, 0.0, 0.0),
                                     tick & 0xffffffff, 0)
            if ik is not None:
                packets.append(ik)
                bot.look_ik = pitch
        return packets

    def _damage_victim(self, bot, state):
        """Урон по участнику: staged world+weapon, как у shared PvP."""
        weapon, world = state['weapon'], state['world']
        staged_weapon, staged_world = copy(weapon), copy(world)
        staged_weapon.equipment_durability = dict(weapon.equipment_durability)
        damage, armor_packets, _ = staged_weapon.absorb_combat_damage(self.damage)
        own_packets, _ = staged_world.apply_damage(damage, bot.uid)
        weapon.equipment_durability.update(staged_weapon.equipment_durability)
        world.__dict__.update(staged_world.__dict__)
        return armor_packets + own_packets

    def _damage_bot(self, room, shooter, victim):
        """Урон бота по боту: та же броня и health-пакет, что у 0x11dd."""
        actor = victim.actor
        absorbed = min(actor.armor_durability,
                       self.damage * actor.fixture.armor_absorb_percent // 100)
        remaining = self.damage - absorbed
        helmet_absorbed = min(actor.helmet_durability,
                              remaining * actor.fixture.helmet_absorb_percent // 100)
        actor.armor_durability -= absorbed
        actor.helmet_durability -= helmet_absorbed
        actor.hp = max(0, actor.hp - (remaining - helmet_absorbed))
        packets = [(0x1069, encode_health(victim.uid, actor.hp))]
        if absorbed or helmet_absorbed:
            packets.append(actor.armor_sync())
        # kill() возвращает room-outputs (state, packets): их нельзя
        # примешивать в список пакетов — в кадре смерти бота-цели
        # send_application_batch падал на int<=dict и ронял весь
        # poll-такт (live 9ec85f: peer_timer_error x6).
        death_outputs = self.kill(room, victim) if actor.hp <= 0 else []
        return packets, death_outputs

    def _move(self, bot, now):
        """Продвижение по пути; темп 25Hz-точек для плавной SC25."""
        if bot.moved_at is None:
            bot.moved_at = now
            return None
        delta = min(0.5, max(0.0, now-bot.moved_at))
        bot.moved_at = now
        if delta < 0.05 or not bot.path:
            return None
        budget = MOVE_SPEED*delta
        position = list(bot.position)
        moved = False
        while bot.path and budget > 0:
            target = bot.path[0]
            dx, dy = target[0]-position[0], target[1]-position[1]
            distance = math.hypot(dx, dy)
            if distance <= WAYPOINT_REACHED:
                position[:2] = target[:2]
                if self.ground is None:
                    position[2] = target[2]
                bot.path.pop(0)
                continue
            step = min(budget, distance)
            nx = position[0] + dx/distance*step
            ny = position[1] + dy/distance*step
            # Пол точки шага: ходимый настил (сугроб) поднимает z —
            # бот заходит на него как игрок, а не обходит как камень.
            step_z = position[2]
            if self.ground is not None:
                step_z = self.ground.query(nx, ny) or position[2]
            blocker = (self.ground.blocker_near(nx, ny, step_z, PROP_PAD)
                       if self.ground is not None else None)
            if blocker is not None:
                # Бот уже внутри этого столба (спавн/старый путь в поле
                # валунов) — шаг наружу разрешён, иначе вечный стоп
                # (live 052420: бот 000b замер внутри окклюдера).
                # Проверка по реальному диску, не по диску с полем.
                ox, oy, orad, otop = blocker
                if ((position[0]-ox)**2 + (position[1]-oy)**2 <= orad*orad
                        and position[2] < otop
                        and math.hypot(nx-ox, ny-oy)
                        > math.hypot(position[0]-ox, position[1]-oy)):
                    blocker = None
            # Стена по высоте пола — как проп: шаг недоступен, иначе
            # _settle «телепортирует» бота на скалу/крышу. Спуск не
            # ограничен: шаг двигает только x/y, падение потом ведёт
            # _settle — как игрок, сошедший с уступа (вертикальный
            # гейт спуска посадил 19/20 ботов на плато, live 051935).
            # Ярус лута строения над рельефом — тоже стена: вход под
            # плиту прячет бота под пол (terrain_only ROE-LIVE).
            loot_wall = getattr(self.ground, 'loot_wall_at', None)
            wall = (self.ground is not None
                    and (step_z - position[2] > CLIMB_MAX
                         or (loot_wall is not None
                             and loot_wall(nx, ny, position[2]) is not None)))
            if blocker is not None or wall:
                # Камень/сугроб/кромка на шаге — обход по касательной от
                # центра пропа, а не удаление узла: сплошная лента камней
                # съедала весь маршрут и боты вечно перестраивали путь
                # на месте (live 20261018T032003: 5 ботов замерли в 680м
                # от борта).
                px, py = -dy/distance, dx/distance
                if blocker is not None:
                    away = ((blocker[0]-position[0])*px
                            + (blocker[1]-position[1])*py)
                    signs = (-1.0, 1.0) if away > 0 else (1.0, -1.0)
                else:
                    signs = (-1.0, 1.0)  # у стены нет центра — обе стороны
                detoured = False
                if bot.detours < AVOID_MAX:
                    for sign in signs:
                        sx = position[0] + px*AVOID_STEP*sign
                        sy = position[1] + py*AVOID_STEP*sign
                        side_z = self.ground.query(sx, sy) or position[2]
                        if (not self.ground.blocked_near(sx, sy, side_z,
                                                         PROP_PAD)
                                and side_z - position[2] <= CLIMB_MAX):
                            bot.detours += 1
                            bot.path.insert(0, (sx, sy, target[2]))
                            detoured = True
                            break
                if not detoured:
                    bot.path.pop(0)
                continue
            position[0], position[1] = nx, ny
            if self.ground is None:
                # Узлы несут высоту сброса, не пола; с GroundMap вертикаль
                # решает _settle, без него — старая интерполяция к узлу.
                position[2] += (target[2]-position[2])*min(1.0, step/distance)
            budget -= step
            moved = True
            bot.motion_direction = (dx/distance, dy/distance, 0.0)
            bot.yaw = math.degrees(math.atan2(dy, dx))
        if moved:
            bot.actor.position = tuple(position)
        return moved

    def _settle(self, room, bot, now):
        """Вертикаль как у игрока: падение, пока нет контакта с полом.

        Клиент сажает локального персонажа коллизией на ландшафт; для
        ботов полом служит GroundMap (позиции QEntity/лута + наблюдения
        живых игроков). Выше пола — спуск с FALL_SPEED, на полу —
        привязка к его высоте, включая подъём по склону."""
        if self.ground is None:
            return False
        if bot.settled_at is None:
            bot.settled_at = now
            return False
        delta = min(0.5, max(0.0, now - bot.settled_at))
        bot.settled_at = now
        floor = self.ground.query(bot.position[0], bot.position[1])
        if floor is None:
            return False
        x, y, z = bot.position
        if z <= floor + GROUND_SNAP:
            # Контакт: привязка к полу; событие только при приземлении,
            # не на каждом шаге спуска по склону.
            if z != floor:
                bot.actor.position = (x, y, floor)
            if not bot.grounded:
                room['events'].append(dict(
                    action='bot_grounded', bot=hex(bot.uid),
                    position=tuple(map(round, bot.position))))
            bot.grounded = True
            return z != floor
        bot.grounded = False
        new_z = max(floor, z - FALL_SPEED*delta)
        bot.actor.position = (x, y, new_z)
        return True

    def _stroll_point(self, bot, anchor):
        """Точка короткого шага осмотра рядом со стоянкой (с полом)."""
        for _ in range(4):
            angle = self.rng.uniform(0.0, math.tau)
            radius = self.rng.uniform(WANDER_RADIUS * 0.3, WANDER_RADIUS)
            x = anchor[0] + math.cos(angle) * radius
            y = anchor[1] + math.sin(angle) * radius
            if self.ground is not None:
                if (self.ground.query(x, y) is not None
                        and not self.ground.blocked_near(
                            x, y, bot.position[2], PROP_PAD)):
                    return (x, y, bot.position[2])
            else:
                return (x, y, bot.position[2])
        return None

    def _cover_point(self, bot, target):
        """Cover point: floor exists, not inside a prop, and ground
        line_clear to the target fails (terrain/walls/props block it).
        If no candidate blocks LOS, first walkable point is a fallback —
        moving open ground still beats standing still under fire."""
        if self.ground is None:
            return self._strafe_point(bot, target)
        fallback = None
        for _ in range(COVER_TRIES):
            angle = self.rng.uniform(0.0, math.tau)
            dist = self.rng.uniform(COVER_MIN_D, COVER_MAX_D)
            x = bot.position[0] + math.cos(angle)*dist
            y = bot.position[1] + math.sin(angle)*dist
            floor = self.ground.query(x, y)
            if (floor is None
                    or self.ground.blocked_near(x, y, floor, PROP_PAD)
                    or self._distance((x, y), target) < COVER_TARGET_MIN):
                continue
            if not self.ground.line_clear(x, y, floor, *target):
                return (x, y, floor)
            if fallback is None:
                fallback = (x, y, floor)
        return fallback

    def _strafe_point(self, bot, target):
        """Perpendicular offset when there is no real cover nearby."""
        dx = target[0]-bot.position[0]
        dy = target[1]-bot.position[1]
        length = math.hypot(dx, dy) or 1.0
        side = 1.0 if self.rng.random() < 0.5 else -1.0
        x = bot.position[0] - dy/length*STRAFE_DIST*side
        y = bot.position[1] + dx/length*STRAFE_DIST*side
        if self.ground is None:
            return (x, y, bot.position[2])
        floor = self.ground.query(x, y)
        if floor is None or self.ground.blocked_near(x, y, floor, PROP_PAD):
            return None
        return (x, y, floor)

    def _combat_move(self, room, bot, now, target_pos):
        """Engaged movement: keep a running route to the picked cover,
        replan once it is reached (or the route emptied). Returns the
        _move flag so the caller can mirror travel's motion/stop emit."""
        if (bot.cover_goal is not None and bot.path
                and self._distance(bot.position, bot.cover_goal)
                > WAYPOINT_REACHED):
            return self._move(bot, now)
        point = (self._cover_point(bot, target_pos)
                 or self._strafe_point(bot, target_pos))
        if point is None:
            bot.cover_goal = None
            bot.path = []
            return None
        bot.cover_goal = point
        bot.goal = point
        bot.path = self.graph.path(bot.position, point)
        bot.detours = 0
        return self._move(bot, now)

    def _storm_rescue(self, room, bot, world, safe, now, tick):
        """Deep-zone emergency: a bot far from the nearest safe node
        cannot walk out before the ticks kill it (400hp at the scaled
        rate is ~13s ≈ 50m at MOVE_SPEED — live 070331: 26 bots died in
        one stage-20 collapse). Full jump to the safe node when no
        member is near enough to watch it; a short step toward safety
        when a member could see it, so it reads as a sprint."""
        if self.ground is None or now < bot.storm_hop_at:
            return []
        dist = self._distance(bot.position, safe)
        if dist <= STORM_HOP_MIN:
            return []
        nearest = min(
            (self._distance(bot.position, m['state']['world'].position)
             for m in room['members'].values()
             if m['state'].get('world') is not None
             and m['state']['world'].position is not None), default=None)
        if nearest is None or nearest > STORM_HOP_SEE:
            point = tuple(safe)
        else:
            dx = safe[0]-bot.position[0]
            dy = safe[1]-bot.position[1]
            length = math.hypot(dx, dy) or 1.0
            step = min(dist, STORM_HOP_NEAR_STEP)
            x = bot.position[0] + dx/length*step
            y = bot.position[1] + dy/length*step
            floor = self.ground.query(x, y)
            if (floor is None
                    or self.ground.blocked_near(x, y, floor, PROP_PAD)):
                return []
            point = (x, y, floor)
        bot.storm_hop_at = now + STORM_HOP_CD
        old = bot.position
        bot.actor.position = point
        bot.inventory.position = point
        bot.path, bot.goal, bot.detours = [], None, 0
        bot.cover_goal = None
        bot.strolling = False
        bot.moving = False
        bot.grounded = True
        bot.settled_at = None
        room['events'].append(dict(
            action='bot_storm_hop', bot=hex(bot.uid),
            from_pos=tuple(map(round, old)),
            to_pos=tuple(map(round, point))))
        return [self._absolute_sync(bot, tick), self._motion(bot, tick)]

    def _unstick_point(self, bot):
        """Точка локального выхода из кармана окклюдеров: пол есть,
        не в пропе. Радиус 30-80м — ближний хоп, не видимый телепорт."""
        for _ in range(UNSTICK_TRIES):
            angle = self.rng.uniform(0.0, math.tau)
            dist = self.rng.uniform(UNSTICK_MIN_D, UNSTICK_MAX_D)
            x = bot.position[0] + math.cos(angle)*dist
            y = bot.position[1] + math.sin(angle)*dist
            floor = self.ground.query(x, y)
            if floor is None:
                continue
            if self.ground.blocked_near(x, y, floor, PROP_PAD):
                continue
            return (x, y, floor)
        return None

    def _watchdog(self, room, bot, now, tick):
        """Контроль застревания: у бота есть маршрут, но за UNSTICK_WINDOW
        он сместился < UNSTICK_MIN_PROGRESS — карман окклюдеров с
        пинг-понгом обходов (live 053201: 0012 дрожал ±1м у спавна до
        смерти от шторма). Локальный хоп на свободную точку, полный сброс
        маршрута — дальше FSM сам перестроит цель/эвакуацию."""
        if self.ground is None or bot.strolling or not bot.path:
            bot.watch_at, bot.watch_pos = None, None
            return []
        if bot.watch_at is None:
            bot.watch_at, bot.watch_pos = now, bot.position
            return []
        if now - bot.watch_at < UNSTICK_WINDOW:
            return []
        progressed = self._distance(bot.position, bot.watch_pos)
        bot.watch_at, bot.watch_pos = now, bot.position
        if progressed >= UNSTICK_MIN_PROGRESS:
            return []
        point = self._unstick_point(bot)
        if point is None:
            return []
        old = bot.position
        bot.actor.position = point
        bot.inventory.position = point
        bot.path, bot.goal, bot.detours = [], None, 0
        bot.moving = False
        bot.grounded = True
        bot.settled_at = None
        room['events'].append(dict(
            action='bot_unstuck', bot=hex(bot.uid),
            from_pos=tuple(map(round, old)),
            to_pos=tuple(map(round, point)),
            progressed=round(progressed)))
        return [self._absolute_sync(bot, tick), self._motion(bot, tick)]

    def _member_heading(self, room, member, now):
        """Курс участника по смещению между проверками: идущий игрок
        получает высадку в секторе впереди, стоячий — по кругу."""
        trail = room.setdefault('member_trail', {})
        avatar = member['avatar']
        pos = member['state']['world'].position
        previous = trail.get(avatar)
        trail[avatar] = pos
        if previous is not None and self._distance(previous, pos) > MIGRATION_HEADING_MIN:
            return math.atan2(pos[1]-previous[1], pos[0]-previous[0])
        return None

    def _migration_point(self, room, bot, pos, heading, world):
        """Точка высадки впереди участника: пол есть, не в пропе, не в
        закрытой зоне и не вплотную к другому живому боту."""
        for _ in range(MIGRATION_TRIES):
            angle = (self.rng.uniform(0.0, math.tau) if heading is None
                     else heading + self.rng.uniform(-MIGRATION_HEADING_ARC,
                                                     MIGRATION_HEADING_ARC))
            dist = self.rng.uniform(MIGRATION_MIN_D, MIGRATION_MAX_D)
            x = pos[0] + math.cos(angle)*dist
            y = pos[1] + math.sin(angle)*dist
            floor = self.ground.query(x, y)
            if floor is None:
                continue
            if self.ground.blocked_near(x, y, floor, PROP_PAD):
                continue
            point = (x, y, floor)
            if self.graph.bot_unsafe(point, world):
                continue
            if any(self._distance(point, b.position) < MIGRATION_BOT_GAP
                   for b in room['bots'] if not b.dead and b is not bot):
                continue
            return point
        return None

    def _migrate(self, room, now, tick):
        """Retail-переброска: живые боты, далёкие от всех участников,
        подтягиваются в район впереди игрока малыми партиями. Цель —
        держать MIGRATION_DESIRED ботов в радиусе MIGRATION_NEAR у
        каждого участника, не подсвечивая телепорт в кадре."""
        if self.ground is None:
            return []
        members = [m for m in room['members'].values()
                   if m['state'].get('world') is not None
                   and m['state']['world'].position is not None
                   and not m['state']['world'].dead]
        if not members:
            return []
        world = members[0]['state']['world']
        packets = []
        for member in members:
            pos = member['state']['world'].position
            near = sum(1 for b in room['bots'] if not b.dead
                       and self._distance(b.position, pos) <= MIGRATION_NEAR)
            deficit = min(MIGRATION_DESIRED - near, MIGRATION_PER_TICK)
            if deficit <= 0:
                continue
            heading = self._member_heading(room, member, now)
            for bot in room['bots']:
                if deficit <= 0:
                    break
                if (bot.dead or bot.target is not None
                        or any(self._distance(bot.position,
                                              m['state']['world'].position)
                               <= MIGRATION_FAR for m in members)):
                    continue
                point = self._migration_point(room, bot, pos, heading, world)
                if point is None:
                    continue
                old = bot.position
                bot.actor.position = point
                bot.inventory.position = point
                bot.path, bot.goal, bot.detours = [], None, 0
                bot.moving = False
                bot.grounded = True
                bot.settled_at = None
                bot.idle_at = now
                deficit -= 1
                # Абсолютный ре-анкор + траектория сразу: актора нет
                # в зоне видимости участника, позиция станет опорной.
                packets += [self._absolute_sync(bot, tick),
                            self._motion(bot, tick)]
                room['events'].append(dict(
                    action='bot_migrated', bot=hex(bot.uid),
                    member=hex(member['avatar']),
                    from_pos=tuple(map(round, old)),
                    to_pos=tuple(map(round, point))))
        return packets

    def _motion(self, bot, tick, direction=None):
        """Один узел SC25 с текущей позицией — движение и падение."""
        bot.motion_sequence = (bot.motion_sequence + 1) & 0xffffffff
        points = [dict(position=self._stand(bot),
                       bytes=(0, 0, 0), value=tick)]
        return (0x25, encode_peer_trajectory(
            bot.uid,
            dict(points=points, word_1=MOVE_SPEED, bytes=(1, 0, 0),
                 floats=(bot.motion_direction if direction is None
                         else direction)),
            bot.motion_sequence, tick))

    def _absolute_sync(self, bot, tick):
        """Абсолютный 0x27 — ре-анкор модели, как у живых участников.

        SC25-точки теряются вместе с кадрами; без периодической
        абсолютной позиции клиент держит актора на устаревшем месте
        (live 20261018T003942: бот стрелял рядом, но был невидим)."""
        movement = bytearray(self.fixture.movement)
        struct.pack_into('>Q3i', movement, 0, bot.uid, *self._stand(bot))
        struct.pack_into('>I', movement, 31, tick)
        return (0x27, bytes(movement))

    def _poll_bot(self, room, bot, now, tick):
        packets, victim_outputs = [], []
        # Вертикаль применяется в любом состоянии: бот в бою тоже
        # продолжает падать до пола.
        settled = self._settle(room, bot, now)
        if bot.sync_at is None:
            bot.sync_at = now + SYNC_INTERVAL
        elif now >= bot.sync_at:
            bot.sync_at = now + SYNC_INTERVAL
            packets.append(self._absolute_sync(bot, tick))
        if bot.finish_at is not None and now >= bot.finish_at:
            packets.extend([(0x11d8, struct.pack('>BQII', 3, bot.uid,
                                                 tick & 0xffffffff,
                                                 0)),
                            (0x11d4, bot.actor.fire_sync(tick, 1,
                                                         bot.last_shot_target))])
            bot.finish_at = None
        world = next((m['state']['world'] for m in room['members'].values()
                      if m['state'].get('world')), None)
        unsafe = world is not None and self.graph.bot_unsafe(bot.position, world)
        if unsafe and world.fixture.zone_damage and world.fixture.zone_interval:
            if now - bot.zone_hurt_at >= world.fixture.zone_interval:
                bot.zone_hurt_at = now
                # Доля урона по HP бота — шанс успеть выйти из зоны тот
                # же, что у игрока на полных HP (live 053201: 400HP боты
                # умирали за 3 тика, эвакуация не успевала).
                zone_hit = max(1, round(world.fixture.zone_damage
                                        * BOT_HP
                                        / (world.fixture.hp or BOT_HP)))
                bot.actor.hp = max(0, bot.actor.hp-zone_hit)
                packets.append((0x1069, encode_health(bot.uid,
                                                      bot.actor.hp)))
                if bot.actor.hp <= 0:
                    return packets, self.kill(room, bot)
        if bot.target is not None:
            # Удержанная цель проверяется отдельно: мёртвые исключены
            # из кандидатов, но старый target без проверки продолжал
            # получать огонь вечно (live d1478b: 0000d/00011 стреляли
            # в труп 00004 сотни тиков подряд).
            gone = (bot.target.dead if isinstance(bot.target, SharedBot)
                    else bot.target['state']['world'].dead)
            if gone:
                bot.target = None
        candidates = self._visible_targets(room, bot, now)
        if candidates:
            candidates.sort(key=lambda x: x[0])
            nearest_distance, nearest = candidates[0]
            if bot.target is not nearest:
                current = (self._distance(bot.position,
                                          self._target_position(bot.target))
                           if bot.target is not None else None)
                if current is None or nearest_distance < current*TARGET_SWITCH_FACTOR:
                    bot.target = nearest
        else:
            bot.target = None
            bot.cover_goal = None
        packets += self._aim_packets(bot, self._target_position(bot.target)
                                     if bot.target is not None else None, tick)
        if bot.target is not None:
            target_pos = tuple(self._target_position(bot.target))
            bot.last_target_pos = target_pos
            bot.hunt_until = now + HUNT_MEMORY
            if bot.reload_until and now >= bot.reload_until:
                bot.reload_until = 0.0
            if not bot.reload_until and now >= bot.next_shot_at:
                fire, victim, death_outputs, distance, chance = self._fire(
                    room, bot, tick, now)
                packets += fire
                if victim:
                    if isinstance(bot.target, SharedBot):
                        # Бот-цель: урон широковещательно в комнату.
                        packets += victim
                    else:
                        victim_outputs.append((bot.target['state'], victim))
                victim_outputs += death_outputs
            # Fight on the move: work toward cover instead of standing
            # on the spot aiming (live 060344 — visible target, bot
            # froze and traded damage until dead).
            # A bot fighting inside a closing region hops out rather
            # than standing in the ticks.
            if unsafe and world is not None:
                safe = self.graph.safe_node(bot.position, world)
                if safe:
                    packets += self._storm_rescue(room, bot, world, safe,
                                                  now, tick)
            moved = self._combat_move(room, bot, now, target_pos)
            packets += self._watchdog(room, bot, now, tick)
            if moved or settled:
                bot.inventory.position = self._stand(bot)
                bot.moving = True
                bot.idle_at = None
                packets.append(self._motion(bot, tick))
            elif bot.moving:
                bot.moving = False
                bot.idle_at = now
                bot.motion_sequence = ((bot.motion_sequence + 1)
                                       & 0xffffffff)
                packets.append((0x25, encode_peer_stop(
                    bot.uid, self._stand(bot),
                    bot.motion_sequence, tick, 0, bot.motion_direction)))
            return packets, victim_outputs
        # Путешествие: шторм → выход, вертолёт → подход без посадки,
        # иначе случайный обход. Позиция вертолёта берётся только после
        # появления в общем пуле — до спавна бот не знает точку эвакуации.
        # evac['helicopter'] — это uid посадочного борта (int), сам
        # vehicle живёт в общем pool любого участника (configuration==6).
        evac = room.get('evac')
        heli_uid = evac and evac.get('helicopter')
        vehicles = next((m['state'].get('vehicles')
                         for m in room['members'].values()
                         if m['state'].get('vehicles')), None)
        heli = None
        if vehicles is not None:
            if heli_uid:
                heli = vehicles.vehicles.get(heli_uid)
            if heli is None:
                heli = next((v for v in vehicles.vehicles.values()
                             if v.get('configuration') == 6), None)
        rope = heli is not None
        goal = None
        if rope:
            # Реальная площадка — evacuation_refuge мира; позиция entity
            # вертолёта — точка спавна борта, не посадки (live 052420:
            # боты столпились на 8км мимо игрока у площадки).
            refuge = (getattr(world.fixture, 'evacuation_refuge', None)
                      if world is not None else None)
            if refuge is not None:
                goal = tuple(refuge)
            elif heli.get('position'):
                goal = tuple(heli['position'])
        if unsafe:
            safe = self.graph.safe_node(bot.position, world)
            if safe:
                goal = safe
                packets += self._storm_rescue(room, bot, world, safe,
                                              now, tick)
        if (goal is None and bot.hunt_until > now
                and bot.last_target_pos is not None):
            # LOS lost mid-fight: maneuver to the last seen position
            # instead of wandering; reaching it ends the pursuit.
            if (self._distance(bot.position, bot.last_target_pos)
                    <= WAYPOINT_REACHED*2):
                bot.hunt_until = 0.0
            else:
                goal = tuple(bot.last_target_pos)
        if goal is not None and self._distance(bot.position, goal) <= (
                EVAC_STANDOFF if rope else WAYPOINT_REACHED):
            if not bot.strolling:
                bot.path = []
            bot.goal = goal
        elif not bot.path or bot.goal != goal:
            if goal is None:
                for _ in range(6):
                    node = self.graph.nodes[
                        self.rng.randrange(len(self.graph.nodes))]
                    if (self.ground is None
                            or not self.ground.blocked(*node)):
                        break
                goal = node
            bot.goal = goal
            bot.path = self.graph.path(bot.position, goal)
            bot.detours = 0
        moved = self._move(bot, now)
        packets += self._watchdog(room, bot, now, tick)
        if moved or settled:
            bot.inventory.position = self._stand(bot)
            bot.moving = True
            bot.idle_at = None
            packets.append(self._motion(bot, tick))
        elif bot.moving:
            bot.moving = False
            bot.idle_at = now
            bot.motion_sequence = (bot.motion_sequence + 1) & 0xffffffff
            packets.append((0x25, encode_peer_stop(
                bot.uid, self._stand(bot),
                bot.motion_sequence, tick, 0, bot.motion_direction)))
        else:
            # Стоячий бот: актор клиента засыпает без потока состояния —
            # модель висит и не принимает попаданий до близкого подхода
            # (live 20261018T015303). Как у стоящего игрока: периодическая
            # траектория в покое, плюс редкий шаг осмотра у точки стоянки,
            # чтобы дозор у вертолёта не выглядел мёртвым.
            bot.strolling = False
            if bot.idle_at is None:
                bot.idle_at = now
            if (self.ground is not None
                    and self.ground.blocked(*bot.position)):
                # Зашёл внутрь камня/сугроба по старому пути — выход
                # немедленно, без паузы осмотра.
                bot.idle_at = now - WANDER_DELAY
            if now - bot.keepalive_at >= MOTION_KEEPALIVE:
                bot.keepalive_at = now
                # Нулевое направление — иначе клиент проигрывает бег на
                # месте по последнему вектору (live 20261018T034237).
                packets.append(self._motion(bot, tick, (0.0, 0.0, 0.0)))
            if now - bot.idle_at >= WANDER_DELAY:
                bot.idle_at = None
                anchor = bot.goal if bot.goal is not None else bot.position
                point = self._stroll_point(bot, anchor)
                if point is not None:
                    bot.path = [point]
                    bot.detours = 0
                    bot.strolling = True
        return packets, victim_outputs

    def evacuation_departed(self, room_uid):
        """Вылет вертолёта: все живые боты гибнут до разбора комнаты."""
        room = self.rooms.get(room_uid)
        if room is None:
            return [], dict(action='bots_mass_death', reason='no_room',
                            room=hex(room_uid))
        outputs = []
        dead = 0
        for bot in room['bots']:
            if not bot.dead:
                outputs += self.kill(room, bot)
                dead += 1
        return outputs, dict(action='bots_mass_death',
                             reason='evacuation_departed',
                             room=hex(room_uid), count=dead,
                             experimental=True)

    def poll(self, now, tick):
        outputs, events = [], []
        for room in self.rooms.values():
            if room['spawned']:
                # Pick-маркеры уходят даже до world участника — фаза выбора
                # клетки, как у retail в конце матчмейкинга. Один список
                # ~600Б на участника, в пределах проверенного размера кадра.
                for avatar, member in room['eligible'].items():
                    if avatar in room['picked'] or not room['bots']:
                        continue
                    room['picked'].add(avatar)
                    picks = [pick for pick in
                             (self._pick_packet(b) for b in room['bots'])
                             if pick]
                    if picks:
                        outputs.append((member['state'], picks))
                if room['members']:
                    outputs += self.snapshots(room, tick)
            evac = room.get('evac')
            if evac and evac.get('finished') or not room['members']:
                continue
            if 'migrate_at' not in room:
                room['migrate_at'] = now + MIGRATION_FIRST_DELAY
            elif now >= room['migrate_at']:
                room['migrate_at'] = now + MIGRATION_INTERVAL
                migration = self._migrate(room, now, tick)
                if migration:
                    outputs += self.emit(room, migration)
            for bot in room['bots']:
                if bot.dead:
                    continue
                packets, victim_outputs = self._poll_bot(room, bot, now, tick)
                if packets:
                    outputs += self.emit(room, packets)
                outputs += victim_outputs
            # Раз в секунду позиции всех ботов и участников в журнал
            # endpoint — tools/teleport_sweep.py читает их как зонд.
            if room['members'] and now >= room.get('radar_at', 0.0):
                room['radar_at'] = now + RADAR_INTERVAL
                events.append(dict(
                    action='bot_positions',
                    bots=[dict(uid=hex(b.uid),
                               pos=tuple(map(round, b.position)),
                               phase=b.phase, grounded=b.grounded,
                               dz=(None if self.ground is None else
                                   (None if self.ground.query(
                                       b.position[0], b.position[1])
                                    is None else round(
                                        b.position[2] - self.ground.query(
                                            b.position[0], b.position[1])))),
                               hp=b.actor.hp,
                               target=(hex(b.target.uid)
                                       if isinstance(b.target, SharedBot)
                                       else (hex(b.target['avatar'])
                                             if b.target else None)))
                          for b in room['bots']],
                    members=[dict(uid=hex(m['avatar']),
                                  pos=tuple(map(round, pos)))
                             for m in room['members'].values()
                             for pos in (m['state']['world'].position
                                         if m['state'].get('world')
                                         else None,)
                             if pos is not None]))
            events += room['events']
            room['events'].clear()
        # Контракт кадра: только (int code, bytes payload). Один
        # бракованный элемент иначе роняет send_application_batch и весь
        # poll-такт (live 9ec85f).
        clean = []
        for state, packets in outputs:
            good = [(code, body) for code, body in packets
                    if type(code) is int and 0 <= code <= 65535
                    and isinstance(body, (bytes, bytearray))]
            if len(good) != len(packets):
                events.append(dict(action='bot_packet_dropped',
                                   dropped=len(packets)-len(good)))
            if good:
                clean.append((state, good))
        return clean, events
