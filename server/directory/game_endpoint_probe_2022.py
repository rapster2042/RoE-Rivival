"""Экспериментальный loopback UDP endpoint: SYN/ACK и захват TGCP, без игры."""
import argparse
import asyncio
import glob
import json
from itertools import groupby
import math
import os
from pathlib import Path
import signal
import struct
import traceback
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tools'))
from roe_tgcp_2023 import parse_datagram, packet_hash, parse_frame, parse_applications
from roe_scene_2023 import decode_conn_auth_request, decode_enter_room_request
from roe_room_2023 import decode_enter_room_response
from room_cosmetics_2022 import apply_room_cosmetics
from roe_protocol_2022 import encrypt, decrypt
from listener import Capture

# Commands whose C2S bodies are captured for offline analysis (jet/turbo
# trigger hunt, attr writes, traversal transitions). Bounded set; the
# high-rate movement stream (0x2a/0x2f/0x11d5) stays out.
BODY_LOG_COMMANDS = {0x10cc, 0x536, 0x37, 0x31, 0x33, 0x29, 0x114f, 0x11ec, 0x49e, 0x49c, 0x821, 0x2001}
from weapon_probe_2022 import WeaponFixture, WeaponState
from medical_probe_2022 import MedicalState
from live_scene_probe_2022 import LiveSceneCommands
from door_state_2022 import DoorFixture, DoorState
from breakable_state_2022 import BreakableFixture, BreakableState
from world_probe_2022 import WorldFixture, WorldState, encode_health
from vehicle_probe_2022 import VehicleState, EvacuationUIFixture, survivor_event
from match_statistics_2022 import MatchStatistics
from match_result_2022 import MatchEndPolicy
from peer_avatar_2022 import PeerAvatarRooms, encode_peer_fire_sync
from shared_loot_2022 import SharedLootRooms
from shared_doors_2022 import SharedDoorRooms
from peer_lifecycle_2022 import expired_members
from peer_spectator_2022 import PeerSpectators
from shared_evacuation_2022 import SharedEvacuationRooms
from shared_vehicle_2022 import SharedVehicleRooms
from shared_combat_2022 import SharedCombatRooms
from shared_bots_2022 import SharedBotRooms
from ground_map_2022 import GroundMap
from actor_probe_2022 import ActorFixture, ActorState, segment_box_entry
from bot_controller_2022 import BotController
from state_scene_actions_2022 import apply_state_action
from death_parcel_2022 import DeathParcelState, target_inventory
from throwable_actions_2022 import ThrowableActions, PROFILES as THROWABLE_EFFECT_PROFILES
from parcel_catalog_2022 import load_parcel_fixture, make_parcel_state
from environment_probe_2022 import EnvironmentFixture
from loot_catalog_2022 import LootCatalog, LootCatalogState
from roe_scene_2023 import decode_move_request
from reliable_udp_2022 import ReliableSender, ReliableReceiver, FrameStream
from movement_validation_2022 import MovementState
from winter_selection_2022 import default_choice, selected_fixture, choose as choose_winter_kit, load_spawn_points, choose_cell


def encode_datagram(source, destination, sequence, acknowledgement, flags, clock, echoed, data=b''):
    header = struct.pack('>HHIIBBH', source, destination, sequence & 0xffffffff,
                         acknowledgement & 0xffffffff, 0x50, flags, 65535)
    raw = header + struct.pack('<HH', echoed & 0xffff, clock & 0xffff) + data
    return raw + struct.pack('<I', packet_hash(raw))


def load_scene_rules(path, pads=None):
    """Явные одноразовые ответы после входа в комнату, без скрытых полей.

    pads (опциональный dict) получает смещения площадки эвакуации для
    посылочного патчинга при ротации наборов зон: rule_index ->
    {response_index: ((offset, dz), ...)}.
    """
    if path is None:
        return []
    path = Path(path)
    rules = []
    for rule_index, rule in enumerate(json.loads(path.read_text(encoding='utf-8'))):
        trigger = int(str(rule['trigger']), 0)
        responses = []
        for response_index, response in enumerate(rule['responses']):
            code = int(str(response['command']), 0)
            if ('payload_file' in response) == ('payload_hex' in response):
                raise ValueError('Нужен ровно один источник payload')
            payload = ((path.parent / response['payload_file']).read_bytes()
                       if 'payload_file' in response else bytes.fromhex(response['payload_hex']))
            if not 0 <= code <= 65535 or len(payload) > 16384:
                raise ValueError('Команда или размер ответа вне допустимых границ')
            # BC_STATUS и сходные ответы адресованы actor из текущей комнаты.
            offsets = response.get('actor_uid_offsets', [])
            if (not isinstance(offsets, list) or any(type(at) is not int or at < 0 or at + 8 > len(payload) for at in offsets)
                    or len(set(offsets)) != len(offsets)):
                raise ValueError('Неверные смещения actor_uid_offsets')
            tick_offsets = response.get('clock_tick_offsets', [])
            if (not isinstance(tick_offsets, list) or any(type(at) is not int or at < 0 or at + 4 > len(payload) for at in tick_offsets)
                    or len(set(tick_offsets)) != len(tick_offsets)):
                raise ValueError('Неверные смещения clock_tick_offsets')
            fixed_position = response.get('fixed_position', False)
            if type(fixed_position) is not bool or (fixed_position and (code != 0x27 or len(payload) != 40)):
                raise ValueError('fixed_position: boolean только для позиции0x27/40')
            pad_xyz = response.get('evac_pad_xyz')
            if pad_xyz is not None:
                if (not isinstance(pad_xyz, list)
                        or any(type(at) is not int or at < 0 or at + 12 > len(payload)
                               for at in pad_xyz)):
                    raise ValueError('evac_pad_xyz: список смещений XYZ в пределах payload')
                if pads is not None:
                    # Каждая тройка XYZ хранит свою высоту относительно
                    # эталонной площадки захвата (Z=259100 в исходных бинах):
                    # при ротации площадка уезжает, относительные высоты
                    # (борт +1200, трос +10) сохраняются.
                    pads.setdefault(rule_index, {})[response_index] = tuple(
                        (at, struct.unpack_from('>i', payload, at + 8)[0] - 259100)
                        for at in pad_xyz)
            if fixed_position:
                responses.append((code, payload, tuple(offsets), tuple(tick_offsets), True))
            elif tick_offsets:
                responses.append((code,payload,tuple(offsets),tuple(tick_offsets)))
            else:
                responses.append((code, payload, tuple(offsets)) if offsets else (code, payload))
        if not 0 <= trigger <= 65535:
            raise ValueError('Команда trigger вне допустимых границ')
        after_selection = rule.get('after_selection', False)
        if type(after_selection) is not bool:
            raise ValueError('after_selection должен быть boolean')
        delay = rule.get('after_selection_delay_seconds', 0)
        if (type(delay) not in (int, float) or not 0 <= delay <= 36000
                or (delay and not after_selection)):
            raise ValueError('Задержка 0..36000 секунд требует after_selection')
        batch = rule.get('batch_responses', False)
        if type(batch) is not bool:
            raise ValueError('batch_responses должен быть boolean')
        if batch and (len(responses) > 64 or sum(len(r[1])+10 for r in responses) > 32768):
            raise ValueError('Пакетное правило превышает 64 сообщения или 32768 байт')
        rules.append((trigger, responses, after_selection, delay, True) if batch else
                     ((trigger, responses, True, delay) if delay else
                      ((trigger, responses, True) if after_selection else (trigger, responses))))
    return rules


class Endpoint(asyncio.DatagramProtocol):
    def __init__(self, capture, handshake=False, ident=False, grant=False, auth=False, room_template=None, scene_rules=None, scene_clock=False, selection_seconds=None, weapon_fixture=None, live_commands=None, world_fixture=None, actor_fixture=None, evacuation_delay=None, parcel_fixture=None, evacuation_result_fixture=None, loot_catalog=None, environment_fixture=None, reliable_udp=False, idle_handoff=False, diagnostic_vehicle_pellet_damage=None, peer_avatar_fixture=None, shared_ground_loot=False, shared_world_clock=False, shared_peer_health=False, peer_avatar_trajectories=False, peer_spectator=False, shared_evacuation=False, shared_vehicles=False, peer_equipment=False, peer_pellet_damage=None, shared_death_loot=False, door_fixture=None, breakable_fixture=None, shared_loot_catalog=None, shared_bots=0, bot_pellet_damage=None, teleport_command=None, peer_actor_enter=False, zone_sets=None, zone_state=None, scene_pads=None):
        self.capture = capture
        self.breakable_fixture = breakable_fixture
        if breakable_fixture and (peer_avatar_fixture or shared_ground_loot):
            raise ValueError('breakable-config пока требует одиночный профиль')
        self.door_fixture = door_fixture
        self.shared_doors = (SharedDoorRooms(door_fixture)
                             if door_fixture and (peer_avatar_fixture or shared_ground_loot) else None)
        self.sessions = {}
        if shared_peer_health and (not peer_avatar_fixture or not world_fixture or not shared_ground_loot):
            raise ValueError('ОбщиеHP требуют peer-avatar-config/world-config/shared-ground-loot')
        if peer_avatar_trajectories and (not peer_avatar_fixture or not scene_clock):
            raise ValueError('Траектории требуют peer-avatar-config и scene-clock')
        if peer_equipment and not shared_peer_health:
            raise ValueError('peer-equipment требует shared-peer-health')
        self.peer_avatars = PeerAvatarRooms(peer_avatar_fixture, shared_peer_health, peer_avatar_trajectories, peer_equipment, actor_enter_probe=peer_actor_enter) if peer_avatar_fixture else None
        self.appearance_dir = None
        if peer_pellet_damage is not None and (not shared_peer_health or not world_fixture
                or not world_fixture.death_on_zero
                or not 0 <= world_fixture.hp_floor <= 1000):
            raise ValueError('peer-pellet-damage требует shared-peer-health/death_on_zero/hp_floor0..1000')
        self.peer_combat = SharedCombatRooms(self.peer_avatars,peer_pellet_damage) if peer_pellet_damage is not None else None
        if peer_spectator and not shared_peer_health:
            raise ValueError('Наблюдение требует shared-peer-health')
        self.peer_spectators = PeerSpectators(diagnostics=True) if peer_spectator else None
        if shared_evacuation and (not shared_peer_health or not evacuation_result_fixture
                or not evacuation_result_fixture.session_statistics
                or evacuation_result_fixture.result_on_death
                or evacuation_result_fixture.timeout_seconds is not None
                or evacuation_result_fixture.diagnostic_departure_seconds):
            raise ValueError('Общая эвакуация требует shared-peer-health и session result без singleton policy/отлёта')
        self.shared_evacuation = SharedEvacuationRooms(evacuation_delay) if shared_evacuation else None
        if shared_vehicles and not shared_peer_health:
            raise ValueError('Общий транспорт требует shared-peer-health')
        self.shared_vehicles = SharedVehicleRooms() if shared_vehicles else None
        self.peer_timer = None
        if shared_ground_loot and (not weapon_fixture or loot_catalog or parcel_fixture):
            raise ValueError('Общий наземный лут требует weapon-config без loot-catalog/parcel-config')
        if shared_loot_catalog and not shared_ground_loot:
            raise ValueError('shared-loot-catalog требует shared-ground-loot')
        if shared_death_loot and not shared_peer_health:
            raise ValueError('shared-death-loot требует shared-peer-health')
        if shared_death_loot and actor_fixture:
            raise ValueError('Общий лут погибших пока не поддерживает отдельную мишень каждого клиента')
        self.shared_loot = SharedLootRooms(weapon_fixture,shared_death_loot,shared_loot_catalog) if shared_ground_loot else None
        if shared_world_clock and (not self.shared_loot or not world_fixture):
            raise ValueError('Общий календарь требует shared-ground-loot и world-config')
        self.shared_world_clock = shared_world_clock
        self.reliable_udp = reliable_udp
        self.idle_handoff = idle_handoff
        self.diagnostic_vehicle_pellet_damage = diagnostic_vehicle_pellet_damage
        self.retry_timer = None
        self.match_timer = None
        self.match_timers_enabled = False
        self.started = time.monotonic()
        self.handshake = handshake
        self.ident = ident
        self.grant = grant
        self.auth = auth
        self.scene_rules = scene_rules or []
        # Ротация наборов зон: выбор привязан к матчу (room_uid+эпоха в
        # общем режиме, иначе счётчик завершённых матчей), а не к запуску —
        # два матча подряд в одном сеансе получают разные расписания и
        # разные площадки вертолёта.
        self.zone_sets = tuple(zone_sets or ())
        self.scene_pads = scene_pads or {}
        self.zone_rooms = {}
        self.zone_match_seq = 0
        self.zone_state_path = Path(zone_state) if zone_state else None
        self.zone_next = 0
        if self.zone_sets and self.zone_state_path:
            try:
                self.zone_next = json.loads(self.zone_state_path.read_text(
                    encoding='utf-8'))['index']
            except Exception:
                self.zone_next = 0
        # Проверенный шаблон собственного 0x27 (40 байт): uid@0, позиция@8,
        # статус@27 (2 = проверенный prone/lying), tick@31. Повторная выдача
        # на точке смерти прибивает труп, пока клиент шлёт 0x2a.
        self.death_pin_template = None
        for rule in self.scene_rules:
            for response in rule[1]:
                if response[0] == 0x27 and len(response[1]) == 40:
                    self.death_pin_template = bytes(response[1])
                    break
            if self.death_pin_template is not None:
                break
        self.scene_clock = scene_clock
        self.selection_seconds = selection_seconds
        self.weapon_fixture = weapon_fixture
        self.live_commands = live_commands
        self.world_fixture = world_fixture
        if actor_fixture and actor_fixture.behavior and (shared_ground_loot or world_fixture is None):
            raise ValueError("BOT05 controller требует одиночный world, без shared-ground-loot")
        self.actor_fixture = actor_fixture
        self.evacuation_delay = evacuation_delay
        self.parcel_fixture = parcel_fixture
        self.environment_fixture = environment_fixture
        self.evacuation_result_fixture = evacuation_result_fixture
        self.loot_catalog = loot_catalog
        self.room_template = Path(room_template).read_bytes() if room_template else None
        self.room_fields = {f['name']:f for f in decode_enter_room_response(self.room_template)['fields']} if self.room_template else None
        # Только публичное имя собственного локального профиля; не журналировать профиль.
        nickname = self.room_fields.get('profile.base.m014') if self.room_fields else None
        self.room_display_name = (nickname['value'][:-1].decode('utf-8') if nickname else '') or 'LOCAL'
        map_field = self.room_fields.get('profile.base.m644') if self.room_fields else None
        self.spawn_points = (load_spawn_points() if map_field and
            struct.unpack_from('>I', self.room_template, map_field['offset'])[0] == 16010 else None)
        # Пол ботов — та же модель, что у клиента: падение до поверхности.
        # Источник истины — cooked PhysX-рельеф с точным заголовком
        # (origin 192000, step 200, scale 5.3406577, offset 175002.671875)
        # плюс якоря наземного лута. Игровой learned-кэш отключён: он был
        # набран против ошибочной калибровки (поправки ~-55м) и с точным
        # рельефом только вредит.
        module_dir = Path(__file__).resolve().parent
        self.bot_ground = (GroundMap.load(
            module_dir / 'ground_iceland_2022.json',
            module_dir / 'heightfield_iceland_2022.json')
            if shared_bots else None)
        self._ground_save_at = 0.0
        # Диагностический телепорт: JSON {x,y,z,seq} в файле → own-0x27
        # живым участникам. Проверка: принимает ли клиент серверную
        # коррекцию позиции у живого актора (native-пин смерти работал).
        self.teleport_command = teleport_command
        self._teleport_seq = {}
        self._teleport_mtime = {}
        if shared_bots and not (peer_avatar_fixture and weapon_fixture
                and shared_peer_health and shared_world_clock and shared_death_loot
                and self.spawn_points and self.bot_ground):
            raise ValueError('Общие боты требуют peer-avatar/weapon/world-config, '
                             'shared-peer-health/world-clock/death-loot, карту16010 '
                             'и сетку пола ground_iceland_2022.json')
        self.shared_bots = (SharedBotRooms(peer_avatar_fixture, weapon_fixture,
                                           self.spawn_points, shared_bots,
                                           bot_pellet_damage or peer_pellet_damage or 40,
                                           ground=self.bot_ground)
                            if shared_bots else None)

    def connection_made(self, transport):
        self.transport = transport
        if self.peer_avatars or self.shared_loot:
            self.peer_timer = asyncio.get_running_loop().call_later(1, self.poll_peer_rooms)
        if self.reliable_udp:
            self.retry_timer = asyncio.get_running_loop().call_later(.05, self.retry_pending)
        fixture = self.evacuation_result_fixture
        if fixture and (fixture.result_on_death or fixture.timeout_seconds is not None):
            self.match_timers_enabled = True
            self.match_timer = asyncio.get_running_loop().call_later(.1, self.poll_match_ends)
        self.capture.record('ready', transport='udp', local=transport.get_extra_info('sockname'),
                            handshake=self.handshake, ident=self.ident, grant=self.grant, auth=self.auth,
                            room_template=bool(self.room_template), scene_rules=len(self.scene_rules), scene_clock=self.scene_clock,
                            selection_seconds=self.selection_seconds, weapon_probe=bool(self.weapon_fixture),
                            implementation_revision='20261015-starter-v1',
                            weapon_slots=getattr(self.weapon_fixture, 'weapon_slots', None),
                            winter_starter_inventory=getattr(self.weapon_fixture, 'winter_starter_inventory', False))
        if self.loot_catalog:
            self.capture.record('loot_catalog_ready', count=len(self.loot_catalog.items),
                                radius_cm=self.loot_catalog.radius, synthetic=True,
                                original_aoi_verified=False)

    def datagram_received(self, raw, remote):
        self.capture.record('recv', raw, transport='udp', remote=remote)
        try:
            d = parse_datagram(raw)
            identity = (remote, d.source_port, d.destination_port)
            if d.flags & 2:
                previous = self.sessions.get(identity)
                if (self.reliable_udp and previous is not None
                        and previous.get('client_isn') == d.sequence):
                    # Повтор SYN не заменяет ключ и игровое состояние.
                    sender = previous['sender']
                    packet = sender.pending.get(0x10000000)
                    if packet is not None:
                        self.transport.sendto(packet.raw, remote)
                        self.capture.record('udp_syn_retry', remote=remote)
                    return
                self.sessions[identity] = {'seq':0x10000000, 'next_client':(d.sequence+1)&0xffffffff, 'key':os.urandom(16), 'key_sent':False, 'ident_sent':False, 'identity':None, 'auth_sent':False, 'frame_seq':3, 'room_sent':False, 'scene_rules_sent':set(), 'clock_started':False}
                if self.reliable_udp:
                    self.sessions[identity].update(client_isn=d.sequence,
                                                   sender=ReliableSender(0x10000000),
                                                   receiver=ReliableReceiver((d.sequence+1)&0xffffffff),
                                                   incoming_frames=FrameStream())
                self.send(d, remote, self.sessions[identity]['seq'], self.sessions[identity]['next_client'], 0x12)
                return
            state = self.sessions.get(identity)
            if state is None:
                self.capture.record('ignored', reason='Нет SYN', remote=remote)
                return
            state['peer_avatar_anchor'] = (d,remote,time.monotonic())
            # Публичные поля маршрута последнего UDP; без ключа/identity/body.
            state['transport_header'] = SimpleNamespace(source_port=d.source_port,
                destination_port=d.destination_port, clock=d.clock)
            if self.reliable_udp and d.flags & 0x10:
                state['sender'].acknowledge(d.acknowledgement)
            if state.get('transport_retired'):
                if d.data:
                    self.send(d, remote, state['seq']+1, state['next_client'], 0x10)
                return
            if d.data:
                if self.reliable_udp:
                    try:
                        contiguous = state['receiver'].accept(d.sequence, d.data)
                    except ValueError as error:
                        self.capture.record('udp_receive_rejected', remote=remote, error=str(error))
                        contiguous = b''
                    state['next_client'] = state['receiver'].next
                    self.send(d, remote, state['seq']+1, state['next_client'], 0x10)
                    try:
                        frames = state['incoming_frames'].feed(contiguous)
                    except ValueError as error:
                        self.capture.record('udp_stream_rejected', remote=remote, error=str(error))
                        return
                    for frame_bytes in frames:
                        self.process_frame(d, remote, state, frame_bytes, True)
                    return
                fresh = d.sequence == state['next_client']
                if fresh:
                    state['next_client'] = (d.sequence + len(d.data)) & 0xffffffff
                self.send(d, remote, state['seq']+1, state['next_client'], 0x10)
                self.process_frame(d, remote, state, d.data, fresh)
        except ValueError as error:
            self.capture.record('parse_error', error=str(error), remote=remote)

    def process_frame(self, d, remote, state, frame_bytes, fresh):
        identity = (remote, d.source_port, d.destination_port)
        try:
            frame = parse_frame(frame_bytes)
            self.capture.record('tgcp_frame', remote=remote, command=hex(frame.command),
                                version=frame.version, command_version=frame.command_version,
                                flag=frame.flag, frame_sequence=frame.sequence,
                                extra_length=len(frame.extra_header), body_length=len(frame.body))
            if fresh and frame.command == 0x5001 and state['auth_sent'] and state['room_sent']:
                # Длинный зимний опыт: клиент закрывает TGCP и повторяет
                # AUTH/RELAY_ENTER_SCENE без повторного ENTER_ROOM.
                # Разрешение переноса ограничено30с и текущим identity.
                state['handoff_at'] = time.monotonic()
                self.capture.record('gameplay_handoff_pending', synthetic=True, remote=remote)
            if self.handshake and fresh and frame.command == 0x1001 and not state['key_sent']:
                # Наблюдаемая ветка клиента 2022: identity selector=3.
                extra_in = frame.extra_header
                if len(extra_in)>=17 and extra_in[12]==3:
                    length=struct.unpack_from('>I',extra_in,13)[0]
                    if 1<=length<=256 and 17+length<=len(extra_in):
                        identity_in=extra_in[17:17+length]
                        if identity_in[-1:]==b'\0' and b'\0' not in identity_in[:-1]:
                            state['identity']=identity_in
                # Наблюдаемая официальная ветка selector=2, прямой ключ.
                extra = b'\x02\x10' + state['key'] + bytes.fromhex('010000004000000000')
                body = encrypt(b'\0',state['key'])
                response = struct.pack('>HHHHBIII',0x3366,frame.version,frame.command_version,
                                       0x1002,1,frame.sequence,21+len(extra),len(body))+extra+body
                self.send(d,remote,state['seq']+1,state['next_client'],0x18,response)
                state['seq'] += len(response)
                state['key_sent'] = True
                keyfile = self.capture.directory/f"session-{d.source_port}.key"
                keyfile.write_bytes(state['key']);keyfile.chmod(0o600)
                self.capture.record('key_response', command='0x1002', synthetic=True, remote=remote)
            elif self.handshake and fresh and state['key_sent'] and frame.body:
                try:
                    plain = decrypt(frame.body,state['key'])
                    self.capture.record('plaintext',plain,command=hex(frame.command),remote=remote)
                    if self.ident and frame.command==0x2001 and not state['ident_sent']:
                        name=state['identity']
                        if name is None:
                            raise ValueError('Не разобран identity из 1001; IDENT запрещён')
                        # Decoder 141a48570: результат, identity, U64 и три bounded byte-array.
                        # Неизвестные слова выставлены явно в ноль, массивы пусты.
                        reply=(struct.pack('>HB I',0,3,len(name))+name+
                               struct.pack('>QHHIHIHH',0,0,0,0,0,0,0,0))
                        self.send_frame(d,remote,state,0x2002,frame.sequence,reply)
                        state['ident_sent']=True
                        self.capture.record('ident_response',synthetic=True,remote=remote)
                        if self.grant:
                            # Decoder 141a49710: ident20, U64, U8, U32 maximum.
                            grant=struct.pack('>I',1)+bytes(16)+struct.pack('>QBI',0,0,1024)
                            self.send_frame(d,remote,state,0x6002,frame.sequence+1,grant)
                            self.capture.record('grant_response',synthetic=True,remote=remote)
                    if frame.command==0x4013:
                        messages = parse_applications(frame,plain,'client')
                        state['last_application_at'] = time.monotonic()
                        for message in messages:
                            self.capture.record('application',command=hex(message.command),body_length=len(message.payload),
                                                application_sequence=message.sequence, stamp=message.stamp, remote=remote)
                            if message.command in BODY_LOG_COMMANDS:
                                self.capture.record('application_body', command=hex(message.command),
                                                    body_hex=message.payload.hex(), remote=remote)
                            if self.auth and message.command==0x406 and not state['auth_sent']:
                                auth=decode_conn_auth_request(message.payload)
                                if auth['word_1']!=1 or auth['token']!=bytes(16):
                                    raise ValueError('AUTH отличается от явно выданных synthetic ident/token')
                                self.send_application(d,remote,state,0x407,struct.pack('>I',0))
                                state['auth_sent']=True
                                self.capture.record('auth_response',synthetic=True,remote=remote)
                                self.resume_gameplay(identity, state, time.monotonic())
                            elif message.command == 0x7082:
                                if ((self.idle_handoff or self.peer_avatars or self.shared_loot)
                                        and not state.get('gameplay_resumed')
                                        and message.payload == b'\0'):
                                    # Cold78: новый authenticated RELAY без доставленного5001.
                                    # Actor UID берётся из проверенного C2S header, не из имени.
                                    relay_actor_uid = struct.unpack_from('>Q', message.decoded, 22)[0]
                                    resumed = self.resume_gameplay(identity, state, time.monotonic(), relay_actor_uid)
                                    if not resumed:
                                        self.resume_finished_result(identity, d, state,
                                                                    time.monotonic(), relay_actor_uid)
                                # Проверенный SCENE_SYNC и однократный27 текущей позиции;
                                # не создаём заново мир/оружие/loot и таймеры.
                                if state.get('gameplay_resumed'):
                                    for rule in self.scene_rules:
                                        if rule[0] == 0xdcd:
                                            for response in rule[1]:
                                                if response[0] == 0x7083:
                                                    self.send_application(d, remote, state, 0x7083, response[1])
                                    self.restore_resume_position(d, remote, state)
                                    self.restore_resume_inventory(d, remote, state)
                                    self.restore_resume_world(d, remote, state)
                            elif message.command==0xdce:
                                room=decode_enter_room_request(message.payload)
                                self.capture.record('enter_room_request',first_identifier=room['first_identifier'],word=room['word'],second_identifier=room['second_identifier'],remote=remote)
                                if self.room_template and state['auth_sent'] and not state['room_sent']:
                                    data=bytearray(self.room_template)
                                    uid=struct.unpack_from('>Q',message.decoded,22)[0]
                                    state['actor_uid']=uid
                                    state['room_uid']=room['second_identifier']
                                    state['actor_display_name']=self.room_display_name
                                    updates={'result':0,'m004':1,'m00c':uid,'m014':1,'m01c':room['second_identifier'],
                                             'profile.base.m000':room['first_identifier'],'profile.base.m00c':uid}
                                    for name,value in updates.items():
                                        f=self.room_fields[name]
                                        struct.pack_into('>'+f['type'],data,f['offset'],value)
                                    if self.weapon_fixture and self.weapon_fixture.cosmetics_config_file:
                                        data, appearance = apply_room_cosmetics(data,self.weapon_fixture.cosmetics_config_file)
                                        self.capture.record('room_cosmetics_applied',synthetic=True,**appearance)
                                    self.send_application(d,remote,state,0xdcf,bytes(data))
                                    state['room_sent']=True
                                    self.capture.record('enter_room_response',synthetic=True,body_length=len(data),remote=remote)
                                    self.send_own_avatar(d, remote, state)
                            elif self.auth and state['auth_sent'] and message.command==0x1f49 and len(message.payload)==4:
                                self.send_application(d,remote,state,0x1f4a,message.payload)
                                if state['clock_started']:
                                    self.send_clock(d,remote,state)
                            if state['auth_sent'] and state['room_sent']:
                                if message.command == 0x2dd:
                                    # Клиент выходит из сцены в лобби: член
                                    # комнаты должен уйти на ближайшем poll,
                                    # а не ждать 45с idle reaper — иначе
                                    # запоздалое удаление сбрасывает общую
                                    # комнату под уже вошедшей новой сессией
                                    # (сломанный заход training→customs).
                                    if not state.get('match_finished'):
                                        # Выход из сцены без итога — тоже
                                        # граница матча для ротации наборов.
                                        self.zone_match_seq = getattr(
                                            self, 'zone_match_seq', 0) + 1
                                    state['match_finished'] = True
                                    state['match_finished_at'] = time.monotonic()
                                    continue
                                self.cancel_dead_actions(state, remote)
                                if self.check_match_end(d, remote, state, time.monotonic()):
                                    continue
                                if self.peer_spectators and message.command == 0x6fbc:
                                    self.emit_peer_avatars(*self.peer_spectators.relay_camera(
                                        self.peer_avatars,state,message.payload,time.monotonic()))
                                if not self.accept_living_action(state, message, remote):
                                    continue
                                if message.command == 0x2a and not self.accept_movement(d, remote, state, message.payload):
                                    continue
                                if ('weapon' in state and message.command in (0x49e, 0x49c, 0x11da, 0x3b63, 0x3e80, 0x1004, 0x1007, 0x6f57)
                                        and not self.accept_inventory_action(state, message, remote)):
                                    continue
                                # Оригинальный сервер отвечает на оба запроса эхом
                                # тела + BE32-результатом 0 (captures 2023-10-25):
                                # 0x49e SET_HAND_REQ -> 0x49f, 0x536 EU_ACTION_REQ -> 0x537.
                                if message.command == 0x49e:
                                    self.send_application(d, remote, state, 0x49f,
                                                          message.payload + b'\x00' * 4)
                                if message.command == 0x536:
                                    self.send_application(d, remote, state, 0x537,
                                                          message.payload + b'\x00' * 4)
                                    # E_EU_AT_JET_UP (182): turbo jump тратит
                                    # заряд (attr17 + 0x1162 type2) —
                                    # пополняем, чтобы прыжок не иссякал.
                                    if message.payload == struct.pack('>I', 182):
                                        self.send_application(d, remote, state, 0x10d0,
                                                              struct.pack('>II', 17, 99))
                                        weapon = state.get('weapon')
                                        if weapon is not None:
                                            self.send_application_batch(d, remote, state,
                                                                        weapon.ability_cd_sync())
                                # 0x36 SYNC_MOVE_PATH_ID: оригинал шлёт строго
                                # инкрементный BE32 на каждый принятый 0x2a MOVE_1
                                # (avalanche: 4..1366, 1:1 с потоком перемещений).
                                if message.command == 0x2a:
                                    path_id = (state.get('move_path_id', 3) + 1) & 0xffffffff
                                    state['move_path_id'] = path_id
                                    self.send_application(d, remote, state, 0x36,
                                                          struct.pack('>I', path_id))
                                # 0x11d9 CAMERA_ROTATE_REQ: 6×f32 camera+aim + u8
                                # has_target (format doc + capture). Храним цель —
                                # база для ретрансляции прицеливания пирам.
                                if message.command == 0x11d9 and len(message.payload) == 25:
                                    fields = struct.unpack('>6fB', message.payload)
                                    state['aim_camera'] = fields[:3]
                                    state['aim_point'] = fields[3:6]
                                    state['aim_has_target'] = bool(fields[6])
                                if message.command == 0x11d5 and len(message.payload) == 12:
                                    sample_at = time.monotonic()
                                    if sample_at - state.get('peer_ik_sample_at', -1.0) >= .2:
                                        state['peer_ik_sample_at'] = sample_at
                                        self.capture.record('peer_avatar_ik_probe', synthetic=True, remote=remote,
                                                            values=struct.unpack('>3I', message.payload))
                                    if self.peer_avatars:
                                        tick, tick_offset = divmod(int((sample_at-self.started)*1000),40)
                                        tick &= 0xffffffff
                                        # First float = shoot-IK angle in degrees
                                        # (upward negative, like target_ik_pitch);
                                        # reused as the pitch of the aim ray.
                                        pitch = struct.unpack_from('>f',message.payload)[0]
                                        if not math.isfinite(pitch) or abs(pitch) > 89:
                                            pitch = None
                                        self.emit_peer_avatars(*self.peer_avatars.aim_touch(state,sample_at,tick,pitch))
                                        self.emit_peer_avatars(*self.peer_avatars.ik_sync(
                                            state, message.payload, tick, tick_offset))
                                        # Stage 2 of the aim transition: the
                                        # pose goes out one delay AFTER the
                                        # SCOPE_AIMING status, never with it.
                                        self.emit_peer_avatars(*self.peer_avatars.aim_pose_sync(
                                            time.monotonic(), tick))
                                if message.command == 0x11d7 and self.peer_avatars:
                                    tick, tick_offset = divmod(int((time.monotonic()-self.started)*1000),40)
                                    self.emit_peer_avatars(*self.peer_avatars.motion_sync(
                                        state, message.payload, tick & 0xffffffff, tick_offset))
                                # 0x2f TURN_AROUND_SIMPLE -> 0x30 BC_TURN_AROUND_SIMPLE:
                                # подтверждённый канал направления взгляда пира
                                # (handler FUN_140f15740 -> FUN_141184b20 -> +0x11d8).
                                if message.command == 0x2f and self.peer_avatars:
                                    turn_tick, turn_offset = divmod(int((time.monotonic()-self.started)*1000),40)
                                    self.emit_peer_avatars(*self.peer_avatars.turn(
                                        state, message.payload, turn_tick & 0xffffffff, turn_offset))
                                # 0x37 MOVE_SPEC -> 0x38 BC_MOVE_SPEC: авторитетное
                                # эхо спец-движения (rope и т.п.) — без него клиент
                                # стримит канат ~10с и не входит в фазу тяги.
                                # Только rope-вариант: 125 байт на проводе в XVersion
                                # (native_size=641), 121 в retail-билде. Эхо на 75-байтные
                                # транспортные MOVE_SPEC глушит весь spec-move канал.
                                # Эхо отправителю несёт actor_uid (как 0x553 own-avatar),
                                # вещание пирам — server-assigned avatar uid.
                                if (message.command == 0x37 and len(message.payload) in (121, 125)
                                        and state.get('actor_uid')):
                                    spec_body = struct.pack('>Q', state['actor_uid']) + message.payload
                                    self.send_application(d, remote, state, 0x38, spec_body)
                                    if self.peer_avatars:
                                        peer_uid = self.peer_avatars.avatar_uid(state)
                                        if peer_uid is not None:
                                            self.emit_peer_avatars(*self.peer_avatars.broadcast(
                                                state, [(0x38, struct.pack('>Q', peer_uid)
                                                         + message.payload)], 'move_spec'))
                                if self.live_commands and message.command == 0x1f49:
                                    control = state.setdefault('live_control', LiveSceneCommands(self.live_commands))
                                    elapsed = time.monotonic() - self.started
                                    try:
                                        pending = control.poll(state['actor_uid'], int(elapsed*25)&0xffffffff, elapsed)
                                    except ValueError as error:
                                        # Неверный управляющий файл не останавливает мир/урон.
                                        pending = []
                                        self.capture.record('live_scene_error', error=str(error), synthetic=True)
                                    for state_actions, items in groupby(pending,key=lambda row:row[1] is None):
                                        group = list(items)
                                        if not state_actions:
                                            self.send_application_batch(d,remote,state,[(code,payload) for _,code,payload in group])
                                        for name, code, payload in group:
                                            if code is None:
                                                if payload['action'] in ('peer_status','peer_ammo','peer_aim','peer_ik','peer_point','peer_recipe'):
                                                    outputs,events = [],[]
                                                    if self.peer_avatars:
                                                        probe_tick = int((time.monotonic()-self.started)*25) & 0xffffffff
                                                        outputs,events = self.peer_avatars.live_probe(state,payload,probe_tick)
                                                    self.emit_peer_avatars(outputs,events)
                                                    control.acknowledge(name)
                                                    if events:
                                                        self.capture.record('live_state_action',id=name,
                                                            recipient_actor_uid=hex(state['actor_uid']),
                                                            synthetic=True,**events[0])
                                                    continue
                                                if self.shared_loot:
                                                    packets,event = [],dict(action=payload['action'],accepted=False,reason='solo_action_only')
                                                else:
                                                    try:
                                                        packets,event = apply_state_action(state,payload,self.death_reserved_indexes(state))
                                                    except ValueError as error:
                                                        packets,event = [],dict(action=payload['action'],accepted=False,reason=str(error))
                                                self.send_application_batch(d,remote,state,packets)
                                                control.acknowledge(name)
                                                self.capture.record('live_state_action',id=name,recipient_actor_uid=hex(state['actor_uid']),synthetic=True,**event)
                                                continue
                                            self.register_fixture_entity(state, code, payload)
                                            control.acknowledge(name)
                                            self.capture.record('live_scene_command', id=name, command=hex(code), body_length=len(payload), recipient_actor_uid=hex(state['actor_uid']), synthetic=True)
                                if self.scene_clock and message.command==0xdcd and not state['clock_started']:
                                    # Наблюдаемые режим 1/50/4; старое абсолютное время не воспроизводим.
                                    counter=int((time.monotonic()-self.started)*50)&0xffffffff
                                    config=struct.pack('>6I',1,0,counter,50,int(time.time())&0xffffffff,4)
                                    self.send_application(d,remote,state,0x1f5f,config)
                                    state['clock_started']=True
                                    self.capture.record('clock_configuration',synthetic=True,remote=remote)
                                    self.send_own_avatar(d, remote, state)
                                    self.send_clock(d,remote,state)
                                    if self.selection_seconds is not None:
                                        state['selection_deadline']=time.monotonic()-self.started+self.selection_seconds
                                        if (self.peer_avatars and state.get('room_uid')
                                                and self.peer_avatars.room_live(state['room_uid'])):
                                            # Поздний вход в живую комнату: этап отбора уже
                                            # прошёл — иначе 47с гейт блокирует weapon/admit
                                            # и peer-протокол у позднего клиента мёртв.
                                            state['selection_deadline']=time.monotonic()-self.started
                                        self.send_application(d,remote,state,0x486,struct.pack('>II',1,1))
                                        self.send_application(d,remote,state,0x47a,struct.pack('>I',1))
                                        self.send_application(d,remote,state,0x482,struct.pack('>I',self.selection_seconds))
                                        self.capture.record('selection_started',synthetic=True,seconds=self.selection_seconds,remote=remote)
                                        choice = default_choice(self.weapon_fixture)
                                        if choice is not None:
                                            state.setdefault('winter_choice', choice)
                                            self.send_application_batch(d, remote, state, [
                                                (0x11e0, struct.pack('>II', 0, 2)),
                                                (0x11e2, struct.pack('>III', 2, 0, 0)),
                                                (0x10d0, struct.pack('>II', 1, state['winter_choice'])),
                                            ])
                                if default_choice(self.weapon_fixture) is not None and message.command in (0x10cc, 0xbfc):
                                    result = choose_winter_kit(message.command, message.payload,
                                        time.monotonic()-self.started, state.get('selection_deadline'), 'weapon' in state)
                                    if result is not None:
                                        choice, replies, event = result
                                        if choice is not None:
                                            state['winter_choice'] = choice
                                        self.send_application_batch(d, remote, state, replies)
                                        # Climb kit: 0x1162 ABILITY_CD_SYNC — заряды
                                        # способности. type1=grapple, клиент читает
                                        # remaining в policy+1972; без sync заряд=0.
                                        # Wire: rid + count + {type,remaining,cd_ms}.
                                        if choice == 3 and state.get('actor_uid'):
                                            self.send_application(d, remote, state, 0x1162,
                                                struct.pack('>QIIII', state['actor_uid'],
                                                            1, 1, 99, 0))
                                        self.capture.record('winter_selection', synthetic=True, **event)
                                if self.spawn_points and message.command == 0x11df:
                                    point, replies, event = choose_cell(message.payload, state['actor_uid'],
                                        time.monotonic()-self.started, state.get('selection_deadline'),
                                        'weapon' in state, self.spawn_points)
                                    if point is not None:
                                        state['born_point'] = point
                                    self.send_application_batch(d, remote, state, replies)
                                    self.capture.record('winter_selection', synthetic=True, **event)
                                if message.command==0x483 and len(message.payload)==1 and 'selection_deadline' in state:
                                    milliseconds=round(state['selection_deadline']*1000)
                                    tick,fraction=divmod(milliseconds,40)
                                    self.send_application(d,remote,state,0x484,struct.pack('>II',tick&0xffffffff,fraction))
                                    self.capture.record('selection_timer_response',synthetic=True,request_byte=message.payload[0],remote=remote)
                                for index, rule in enumerate(self.scene_rules):
                                    trigger, responses = rule[:2]
                                    if trigger != message.command or index in state['scene_rules_sent']:
                                        continue
                                    if len(rule) >= 3 and rule[2]:
                                        deadline=state.get('selection_deadline')
                                        delay=rule[3] if len(rule) >= 4 else 0
                                        if deadline is None or time.monotonic()-self.started < deadline + delay:
                                            continue
                                    state['scene_rules_sent'].add(index)
                                    batch = len(rule) == 5 and rule[4]
                                    batched_responses = []
                                    for response_index, response in enumerate(responses):
                                        command, payload = response[:2]
                                        if (command == 0x27 and len(payload) == 40 and len(rule) >= 3 and rule[2]
                                                and len(response) >= 3 and 0 in response[2] and state.get('born_point')
                                                and not (len(response) == 5 and response[4])):
                                            patched = bytearray(payload)
                                            struct.pack_into('>iii', patched, 8, *state['born_point'][2:])
                                            payload = bytes(patched)
                                        if len(response) >= 3:
                                            patched = bytearray(payload)
                                            for at in response[2]:
                                                struct.pack_into('>Q',patched,at,state['actor_uid'])
                                            payload = bytes(patched)
                                        if len(response) >= 4:
                                            patched=bytearray(payload)
                                            tick=int((time.monotonic()-self.started)*25)&0xffffffff
                                            for at in response[3]:
                                                struct.pack_into('>I',patched,at,tick)
                                            payload=bytes(patched)
                                        pad_offsets = getattr(self, 'scene_pads', {}).get(index, {}).get(response_index)
                                        if pad_offsets:
                                            picked = self.zone_pick(state)
                                            if picked is not None and picked['refuge'] is not None:
                                                payload = self.patch_evac_pad(payload, pad_offsets,
                                                                              picked['refuge'])
                                        if command == 0x5fb and self.weapon_fixture:
                                            weapon = state.setdefault('weapon', WeaponState(selected_fixture(self.weapon_fixture, state.get('winter_choice')), state['actor_uid']))
                                            weapon.available_loot.update(state.get('loot_spawned_uids', set()))
                                            weapon.loot_available = state.get('loot_spawned', False)
                                            if state.get('scene_position'):
                                                weapon.update_position(state['scene_position'])
                                            if self.shared_loot and not self.shared_loot.attach(state):
                                                continue
                                            payload = weapon.inventory()
                                        if command == 0x4a0 and len(payload) == 20:
                                            weapon = state.get('weapon')
                                            actor, slot, _, _ = struct.unpack('>QIII', payload)
                                            if (isinstance(weapon, WeaponState) and weapon.fixture.starter_items
                                                    and actor == state['actor_uid'] and slot in weapon.guns):
                                                # Сценарий содержит старый Citori clip2: после
                                                # выбранного пистолета он не должен сбрасывать магазин.
                                                payload = weapon.ammunition_sync(slot=slot)
                                        if self.shared_vehicles and command==0x654b:
                                            self.register_fixture_entity(state,command,payload)
                                            payload=self.shared_vehicles.register(state,command,payload)
                                        if batch:
                                            batched_responses.append((command, payload))
                                        else:
                                            self.send_application(d,remote,state,command,payload)
                                        self.register_fixture_entity(state, command, payload)
                                        if command == 0x5fb and self.weapon_fixture and not state.get('winter_kit_initialized'):
                                            kit_responses = state['weapon'].kit_sync()
                                            kit_responses.append(state['weapon'].capacity_sync())
                                            if kit_responses:
                                                if batch:
                                                    batched_responses.extend(kit_responses)
                                                else:
                                                    self.send_application_batch(d, remote, state, kit_responses)
                                                state['winter_kit_initialized'] = True
                                                # Повторная выдача 0x1162 после инициализации
                                                # набора — система способностей уже жива.
                                                # kit_sync уже включает merged-запись
                                                # (jet для всех + grapple при climb),
                                                # но ранние записи могли уйти до готовности.
                                                self.send_application_batch(d, remote, state,
                                                    state['weapon'].ability_cd_sync())
                                                self.capture.record('winter_kit_initialized', synthetic=True,
                                                                    definition=state['weapon'].equipment.get(11),
                                                                    starter_items=list(state['weapon'].fixture.starter_items),
                                                                    starter_item_source=('INITITEM.XML/map16010/stage1'
                                                                        if state['weapon'].fixture.starter_items else 'diagnostic_fixture'),
                                                                    starter_magazine_policy='full_capacity_local',
                                                                    weapon_slots=state['weapon'].fixture.weapon_slots)
                                        if command == 0x5fb and self.weapon_fixture:
                                            magazine_packets = state['weapon'].magazine_syncs()
                                            if batch:
                                                batched_responses.extend(magazine_packets)
                                            else:
                                                self.send_application_batch(d, remote, state, magazine_packets)
                                        if command == 0x5fb and self.weapon_fixture and 'weapon' not in state:
                                            state['weapon'] = WeaponState(selected_fixture(self.weapon_fixture, state.get('winter_choice')), state['actor_uid'])
                                            state['weapon'].loot_available = state.get('loot_spawned', False)
                                            state['weapon'].available_loot.update(state.get('loot_spawned_uids', set()))
                                        self.capture.record('scene_rule_response', synthetic=True,
                                                            trigger=hex(trigger), command=hex(command),
                                                            body_length=len(payload), remote=remote)
                                    if batch:
                                        self.send_application_batch(d, remote, state, batched_responses)
                                if (self.environment_fixture and 'weapon' in state
                                        and not state.get('environment_initialized')
                                        and message.command == 0x1f49):
                                    server_seconds=int(time.time())&0xffffffff
                                    self.send_application_batch(d,remote,state,
                                        self.environment_fixture.messages(server_seconds))
                                    state['environment_initialized']=True
                                    self.capture.record('environment_initialized',synthetic=True,
                                        hour=self.environment_fixture.hour,
                                        temperature_raw=self.environment_fixture.temperature,
                                        server_seconds=server_seconds,
                                        original_temperature_algorithm=False)
                                if self.breakable_fixture and 'weapon' in state and state.get('room_sent'):
                                    if 'breakables' not in state:
                                        state['breakables'] = BreakableState(self.breakable_fixture, state['actor_uid'])
                                    breakables = state['breakables']
                                    position = state.get('last_movement_position', state.get('scene_position'))
                                    if message.command == 0x6f59:
                                        responses, event = breakables.handle(message.payload, position)
                                        self.send_application_batch(d, remote, state, responses)
                                        self.capture.record('breakable_probe', synthetic=True, remote=remote, **event)
                                    elif message.command == 0x1f49:
                                        responses = breakables.snapshot(position)
                                        self.send_application_batch(d, remote, state, responses)
                                        if responses:
                                            self.capture.record('breakable_snapshot', synthetic=True, remote=remote, messages=len(responses))
                                if self.door_fixture and 'weapon' in state and state.get('room_sent'):
                                    position = state.get('last_movement_position', state.get('scene_position'))
                                    if self.shared_doors is not None:
                                        if message.command == 0x6f57:
                                            responses, event, door_outputs = self.shared_doors.handle(state, message.payload, position)
                                            self.send_application_batch(d, remote, state, responses)
                                            self.emit_shared_loot(door_outputs)
                                            self.capture.record('door_probe', synthetic=True, remote=remote, **event)
                                        elif message.command == 0x1f49:
                                            responses = self.shared_doors.snapshot(state, position)
                                            self.send_application_batch(d, remote, state, responses)
                                            if responses:
                                                self.capture.record('door_snapshot', synthetic=True, remote=remote, messages=len(responses))
                                    else:
                                        if 'doors' not in state:
                                            state['doors'] = DoorState(self.door_fixture)
                                        doors = state['doors']
                                        if message.command == 0x6f57:
                                            responses, event = doors.handle(message.payload, position)
                                            self.send_application_batch(d, remote, state, responses)
                                            self.capture.record('door_probe', synthetic=True, remote=remote, **event)
                                        elif message.command == 0x1f49:
                                            responses = doors.snapshot(position)
                                            self.send_application_batch(d, remote, state, responses)
                                            if responses:
                                                self.capture.record('door_snapshot', synthetic=True, remote=remote, messages=len(responses))
                                if self.shared_loot and 'weapon' in state and message.command == 0x1f49:
                                    if self.shared_loot.attach(state) and not state.get('shared_loot_snapshot_sent'):
                                        snapshot = self.shared_loot.snapshot(state)
                                        self.send_application_batch(d, remote, state, snapshot)
                                        state['shared_loot_snapshot_sent'] = True
                                        self.capture.record('shared_loot_snapshot', synthetic=True,
                                                            account_uid=hex(state['actor_uid']),
                                                            messages=len(snapshot))
                                if (self.peer_avatars and 'weapon' in state and 'scene_position' in state
                                        and message.command == 0x1f49 and not state.get('peer_avatar_admitted')):
                                    tick = int((time.monotonic()-self.started)*25)&0xffffffff
                                    outputs, events = self.peer_avatars.admit(state,tick,time.monotonic())
                                    self.emit_peer_avatars(outputs,events)
                                    state['peer_avatar_admitted'] = True
                                bot_hit = None
                                if self.shared_bots and message.command == 0x11dd:
                                    bot_hit = self.shared_bots.hit(state, message.payload, time.monotonic())
                                    if bot_hit is not None:
                                        bot_outputs, bot_event = bot_hit
                                        self.emit_peer_avatars(bot_outputs, [])
                                        self.capture.record('shared_bots_probe', synthetic=True, **bot_event)
                                if self.peer_combat and message.command == 0x11dd and bot_hit is None:
                                    outputs,event,victim = self.peer_combat.hit(state,message.payload,time.monotonic())
                                    self.emit_peer_avatars(outputs,[])
                                    self.capture.record('shared_combat_probe',synthetic=True,**event)
                                    if victim is not None:
                                        state.setdefault('match_statistics',MatchStatistics()).observe(event)
                                        self.emit_peer_avatars(*self.peer_avatars.health(victim,time.monotonic()))
                                        anchor = victim['peer_avatar_anchor']
                                        self.emit_own_death_inventory(anchor[0],anchor[1],victim)
                                        self.cancel_dead_actions(victim,anchor[1])
                                if self.actor_fixture and 'weapon' in state:
                                    actor = state.setdefault('target_actor', ActorState(self.actor_fixture))
                                    if actor.fixture.death_loot_reserve is not None and 'target_inventory' not in state:
                                        state['target_inventory'] = target_inventory(actor, state['weapon'].fixture)
                                    if message.command == 0x1f49:
                                        tick = int((time.monotonic()-self.started)*25) & 0xffffffff
                                        if not actor.started:
                                            self.send_application_batch(d, remote, state, actor.begin(tick))
                                            self.capture.record('actor_probe', action='actor_created', uid=hex(actor.fixture.uid), synthetic=True)
                                        world = state.get('world')
                                        vehicles = state.get('vehicles')
                                        protected = bool(vehicles and (vehicles.seated
                                            or vehicles.climb_protected(time.monotonic()))) or not state['weapon'].gun_owned or bool(world and getattr(world, 'dead', False))
                                        if actor.fixture.behavior and world:
                                            controller = state.setdefault('bot_controller',BotController(actor,state['weapon'])) if 'bot_controller' not in state else state['bot_controller']
                                            bot_packets,bot_events = controller.poll(time.monotonic(),state['weapon'],world,vehicles)
                                            state['target_inventory'] = controller.inventory
                                            self.send_application_batch(d,remote,state,bot_packets)
                                            for bot_event in bot_events:
                                                self.capture.record('actor_probe',synthetic=True,**bot_event)
                                        responses, events = actor.poll(time.monotonic(), tick,
                                            world.position if world else None, protected=protected)
                                        self.send_application_batch(d, remote, state, responses)
                                        for event in events:
                                            self.capture.record('actor_probe', synthetic=True, **event)
                                            if event['action'] == 'bot_attack' and world:
                                                damage, armor_packets, armor_event = state['weapon'].absorb_combat_damage(event['damage'])
                                                damage_packets, damage_event = world.apply_damage(damage,actor.fixture.uid)
                                                damage_event['validation'] = event['validation']
                                                self.send_application_batch(d,remote,state,armor_packets + damage_packets)
                                                self.capture.record('weapon_probe',synthetic=True,remote=remote,**armor_event)
                                                self.capture.record('world_probe',synthetic=True,remote=remote,**damage_event)
                                    if message.command == 0x11dd:
                                        responses, event = actor.hit(message.payload)
                                        state.setdefault('match_statistics', MatchStatistics()).observe(event)
                                        self.send_application_batch(d, remote, state, responses)
                                        self.capture.record('actor_probe', synthetic=True, **event)
                                        if event.get('action') == 'hit_damage' and actor.hp == 0 and 'target_inventory' in state:
                                            inventory = state['target_inventory']
                                            if actor.fixture.armor_definition:
                                                inventory.equipment_durability[12] = actor.armor_durability
                                            corpses = state.setdefault('death_parcels', DeathParcelState())
                                            responses, event = corpses.transfer(inventory, actor.position,
                                                                                self.death_reserved_indexes(state))
                                            self.send_application_batch(d, remote, state, responses)
                                            self.capture.record('death_parcel_probe', synthetic=True, remote=remote, **event)
                                if self.world_fixture and 'weapon' in state:
                                    if 'world' not in state:
                                        if self.shared_world_clock and not self.shared_loot.attach(state):
                                            continue
                                        joined = time.monotonic()
                                        picked = self.zone_pick(state)
                                        state['world'] = WorldState(
                                            picked['fixture'] if picked else self.world_fixture,
                                            state['actor_uid'], joined)
                                        if self.shared_world_clock:
                                            started = self.shared_loot.world_start(state, joined)
                                            if started < joined:
                                                state['world'].align_clock(started, joined)
                                            self.capture.record('shared_world_clock', account_uid=hex(state['actor_uid']),
                                                room_uid=state['room_uid'], epoch=state['shared_loot_epoch'],
                                                elapsed_at_join=joined-started)
                                        state.setdefault('match_statistics', MatchStatistics()).begin(joined)
                                        if state.get('scene_position'):
                                            event = state['world'].update_position(state['scene_position'], time.monotonic())
                                            if event:
                                                self.capture.record('world_probe', synthetic=True, remote=remote, **event)
                                    world = state['world']
                                    if message.command == 0x2a:
                                        movement = decode_move_request(message.payload)
                                        if movement['points']:
                                            event = world.update_position(movement['points'][-1]['position'], time.monotonic())
                                            if event:
                                                self.capture.record('world_probe', synthetic=True, remote=remote, **event)
                                    if message.command == 0x1f49:
                                        vehicles = state.get('vehicles')
                                        protected = bool(vehicles and vehicles.seated
                                            and vehicles.vehicles[vehicles.seated]['configuration'] == 6)
                                        responses, events = world.poll(time.monotonic(), protected=protected)
                                        for command, payload in responses:
                                            self.send_application(d, remote, state, command, payload)
                                        for event in events:
                                            self.capture.record('world_probe', synthetic=True, remote=remote, **event)
                                    if self.peer_avatars:
                                        self.emit_peer_avatars(*self.peer_avatars.health(state, time.monotonic()))
                                    self.emit_own_death_inventory(d, remote, state)
                                self.cancel_dead_actions(state, remote)
                                if self.check_match_end(d, remote, state, time.monotonic()):
                                    continue
                                death_handled = False
                                if 'death_parcels' in state and 'weapon' in state:
                                    corpses = state['death_parcels']
                                    if corpses.matches(message.command, message.payload):
                                        responses, event = corpses.handle(message.command, message.payload, state['weapon'])
                                        state.setdefault('match_statistics', MatchStatistics()).observe(event)
                                        self.send_application_batch(d, remote, state, responses)
                                        if self.shared_loot and self.shared_loot.share_death_parcels:
                                            self.emit_shared_loot(self.shared_loot.death_outputs(state,responses,contents=True))
                                            self.emit_shared_loot(self.shared_loot.publish_external_drops(state,responses))
                                        self.capture.record('death_parcel_probe', synthetic=True, remote=remote, **event)
                                        death_handled = True
                                parcel_pick = (message.command == 0x3b63 and len(message.payload) == 33
                                               and struct.unpack_from('>I', message.payload, 4)[0] != 0)
                                if 'weapon' in state and message.command == 0x2a:
                                    movement = decode_move_request(message.payload)
                                    # Диагностика aim-флага: фиксируем новые комбинации байтов точек.
                                    sig = (tuple(p['bytes'] for p in movement['points']),
                                           movement['bytes'],
                                           tuple(e[0] for e in movement['extras']))
                                    member = self.peer_avatars.members.get(
                                        (state.get('room_uid'), state['actor_uid'])) if self.peer_avatars else None
                                    if sig != state.get('move_sig'):
                                        state['move_sig'] = sig
                                        self.capture.record('move_bytes_probe', synthetic=True, remote=remote,
                                                            aiming=bool(member and member.get('aiming')),
                                                            point_bytes=[list(p['bytes']) for p in movement['points']],
                                                            tail_bytes=list(movement['bytes']),
                                                            extras=[e[0] for e in movement['extras']],
                                                            word_0=movement['word_0'], word_1=movement['word_1'])
                                    if movement['points']:
                                        state.setdefault('match_statistics', MatchStatistics()).move(
                                            [point['position'] for point in movement['points']])
                                        state['last_movement_position'] = movement['points'][-1]['position']
                                        state['weapon'].update_position(state['last_movement_position'])
                                        if self.peer_avatars:
                                            now = time.monotonic()
                                            outputs, events = self.peer_avatars.move(state,state['last_movement_position'],int((now-self.started)*25)&0xffffffff,now,movement)
                                            self.emit_peer_avatars(outputs,events)
                                shared_catalog = self.shared_loot.catalog if self.shared_loot else None
                                active_catalog = self.loot_catalog or shared_catalog
                                if active_catalog and 'weapon' in state and message.command == 0x1f49:
                                    if 'loot_catalog' not in state:
                                        if shared_catalog is not None:
                                            taken, remaining = self.shared_loot.catalog_state(state)
                                            state['loot_catalog'] = LootCatalogState(
                                                shared_catalog, state['weapon'], taken, remaining)
                                        else:
                                            state['loot_catalog'] = LootCatalogState(self.loot_catalog, state['weapon'])
                                    try:
                                        responses, events = state['loot_catalog'].poll()
                                    except Exception as error:
                                        # Исключение в poll не должно глушить последующие
                                        # опросы AOI и остальные команды этого кадра.
                                        self.capture.record('loot_catalog_error', synthetic=True,
                                                            remote=remote, error=repr(error))
                                    else:
                                        self.send_application_batch(d, remote, state, responses)
                                        for event in events:
                                            self.capture.record('loot_catalog_probe', synthetic=True,
                                                                remote=remote, **event)
                                if 'parcels' in state and 'weapon' in state and not death_handled and (message.command == 0x656f or parcel_pick):
                                    responses, event = state['parcels'].handle(message.command, message.payload, state['weapon'])
                                    state.setdefault('match_statistics', MatchStatistics()).observe(event)
                                    self.send_application_batch(d, remote, state, responses)
                                    self.capture.record('parcel_probe', synthetic=True, remote=remote, **event)
                                if 'weapon' in state:
                                    responses, reload_events = state['weapon'].poll(time.monotonic())
                                    responses = self.reconcile_resume_reload(remote, state, responses, reload_events)
                                    self.send_application_batch(d, remote, state, responses)
                                    if self.peer_avatars:
                                        for command, payload in responses:
                                            if command == 0x4a0 and len(payload) == 20:
                                                self.emit_peer_avatars(
                                                    *self.peer_avatars.ammunition(state, payload))
                                    for reload_event in reload_events:
                                        self.capture.record('weapon_probe', synthetic=True, remote=remote, **reload_event)
                                medical_handled = False
                                if 'weapon' in state and 'world' in state:
                                    if 'medical' not in state:
                                        state['medical'] = MedicalState(state['weapon'], state['world'])
                                    medical = state['medical']
                                    result = medical.handle(message.command, message.payload, time.monotonic())
                                    if result is not None:
                                        medical_handled = True
                                        responses, events = result
                                    else:
                                        responses, events = medical.poll(time.monotonic())
                                    self.send_application_batch(d, remote, state, responses)
                                    for event in events:
                                        self.capture.record('medical_probe', synthetic=True, remote=remote, **event)
                                if medical_handled and self.peer_avatars:
                                    self.emit_peer_avatars(*self.peer_avatars.health(state, time.monotonic()))
                                if 'weapon' in state and state['weapon'].fixture.throwable_actions:
                                    grenades = state.setdefault('throwable_actions', ThrowableActions())
                                    now = time.monotonic()
                                    if message.command == 0x1004:
                                        responses, event = grenades.launch(state['weapon'], message.payload, now)
                                        self.send_application_batch(d, remote, state, responses)
                                        self.capture.record('throwable_probe', synthetic=True, remote=remote, **event)
                                    elif message.command == 0x1007:
                                        tick = int((now-self.started)*25) & 0xffffffff
                                        responses, event, finished = grenades.finish(message.payload, state['actor_uid'], now, tick)
                                        self.send_application_batch(d, remote, state, responses)
                                        self.capture.record('throwable_probe', synthetic=True, remote=remote, **event)
                                        if finished is not None:
                                            self.apply_diagnostic_grenade(d, remote, state, finished)
                                    elif message.command == 0x1f49:
                                        for identifier in grenades.expire(now):
                                            self.capture.record('throwable_probe', synthetic=True, remote=remote,
                                                                action='throw_expired', identifier=identifier,
                                                                original_server_physics=False)
                                if 'weapon' in state and message.command in (0x49e, 0x49c, 0x11da, 0x3b63, 0x3e80) and not parcel_pick and not medical_handled:
                                    if self.shared_loot:
                                        responses, event, shared_outputs = self.shared_loot.handle(state, message.command, message.payload)
                                        self.emit_shared_loot(shared_outputs)
                                    else:
                                        responses, event = state['weapon'].handle(message.command, message.payload)
                                    state.setdefault('match_statistics', MatchStatistics()).observe(event)
                                    for command, payload in responses:
                                        self.send_application(d, remote, state, command, payload)
                                        if command == 0x4a0 and len(payload) == 20 and self.peer_avatars:
                                            self.emit_peer_avatars(
                                                *self.peer_avatars.ammunition(state, payload))
                                    self.capture.record('weapon_probe', synthetic=True, remote=remote, **event)
                                    if message.command == 0x11da and self.peer_combat:
                                        self.peer_combat.note_fire(state,message.payload,event,time.monotonic())
                                    if message.command == 0x11da and self.shared_bots:
                                        self.shared_bots.note_fire(state,message.payload,event,time.monotonic())
                                    if (self.peer_avatars is not None and message.command == 0x11da
                                            and isinstance(event,dict) and event.get('action') in ('fire','fire_finish')):
                                        avatar_uid = self.peer_avatars.avatar_uid(state)
                                        messages = (encode_peer_fire_sync(avatar_uid,message.payload)
                                                    if avatar_uid is not None else [])
                                        if messages:
                                            state['peer_fire_counter'] = struct.unpack_from('>I',message.payload,24)[0]
                                            outputs,events = self.peer_avatars.broadcast(state,messages,'fire_sync')
                                            self.emit_peer_avatars(outputs,events)
                                    if message.command == 0x11da and 'vehicles' in state:
                                        state['vehicles'].note_fire(message.payload, event)
                                    if message.command == 0x11da and 'target_actor' in state:
                                        state['target_actor'].note_fire(message.payload, event)
                                if 'vehicles' in state and message.command == 0x1f49:
                                    now=time.monotonic()
                                    responses, events = state['vehicles'].poll(now,int((now-self.started)*25)&0xffffffff)
                                    self.send_application_batch(d, remote, state, responses)
                                    for event in events:
                                        self.capture.record('vehicle_probe', synthetic=True, remote=remote, **event)
                                    if state['vehicles'].evacuation_finished:
                                        self.finish_gameplay(state, remote, 'rescued')
                                parcel_motion = None
                                if (message.command == 0x6549 and 'parcels' in state
                                        and 'weapon' in state and hasattr(state['parcels'], 'observe_motion')):
                                    parcel_motion = state['parcels'].observe_motion(message.payload, state['weapon'])
                                    if parcel_motion:
                                        self.capture.record('parcel_probe', synthetic=True,
                                                            remote=remote, **parcel_motion)
                                if 'vehicles' in state and parcel_motion is None and message.command in (0x6541, 0x6543, 0x6547, 0x6549, 0x657a, 0x11de):
                                    if (self.shared_evacuation and message.command == 0x6541
                                            and (not self.shared_vehicles or len(message.payload)==12
                                                 and state['vehicles'].vehicles.get(
                                                     struct.unpack_from('>Q',message.payload)[0],{}).get('configuration')==6)):
                                        self.shared_evacuation.sync(self.peer_avatars)
                                        responses, event = self.shared_evacuation.geton(state, message.payload, time.monotonic())
                                        if event['action'] == 'shared_evacuation_geton':
                                            self.broadcast_shared_geton(state, responses)
                                    elif self.shared_vehicles:
                                        now=time.monotonic()
                                        # Транспортные команды тоже доказывают, что клиент в сцене:
                                        # deploy глайдера/доски может прийти до первого 0x1f49,
                                        # иначе GETON отсекается invalid_member и место не фиксируется.
                                        if (self.peer_avatars and 'scene_position' in state
                                                and 'room_uid' in state
                                                and not state.get('peer_avatar_admitted')):
                                            outputs, events = self.peer_avatars.admit(state,
                                                int((now-self.started)*25)&0xffffffff, now)
                                            self.emit_peer_avatars(outputs, events)
                                            state['peer_avatar_admitted'] = True
                                        self.shared_vehicles.sync(self.peer_avatars,now)
                                        responses,event,outputs=self.shared_vehicles.handle(
                                            state,message.command,message.payload,now)
                                        self.emit_peer_avatars(outputs,[])
                                    else:
                                        responses, event = state['vehicles'].handle(message.command, message.payload)
                                    if 'world' in state and event['action'] in ('vehicle_move', 'getoff'):
                                        world_event = state['world'].update_position(event['position'], time.monotonic())
                                        if world_event:
                                            self.capture.record('world_probe',synthetic=True,remote=remote,**world_event)
                                    for command, payload in responses:
                                        self.send_application(d, remote, state, command, payload)
                                    self.capture.record('vehicle_probe', synthetic=True, remote=remote, **event)
                except ValueError as error:
                    self.capture.record('decrypt_error',command=hex(frame.command),error=str(error),remote=remote)
        except ValueError as error:
            self.capture.record('frame_error', error=str(error), remote=remote)

    def send(self, d, remote, sequence, acknowledgement, flags, data=b''):
        clock = int((time.monotonic()-self.started)*1000)&0xffff
        raw = encode_datagram(d.destination_port, d.source_port, sequence, acknowledgement, flags, clock, d.clock,data)
        if self.reliable_udp and (data or flags & 2):
            state = self.sessions[(remote, d.source_port, d.destination_port)]
            if not state['sender'].remember(raw, sequence, len(data) + bool(flags & 2), time.monotonic()):
                self.capture.record('udp_delivery_failed', remote=remote, reason='sender_limit')
                return
        self.transport.sendto(raw, remote)
        self.capture.record('send', raw, transport='udp', remote=remote)

    def retry_pending(self):
        """Повторяет байты без вызова игровых обработчиков и AES-сериализации."""
        self.stop_retries()
        for identity, state in list(self.sessions.items()):
            if state.get('transport_retired'):
                continue
            sender = state.get('sender')
            if sender is None:
                continue
            for sequence, raw, attempt in sender.due(time.monotonic()):
                self.transport.sendto(raw, identity[0])
                self.capture.record('udp_retry', remote=identity[0], udp_sequence=sequence,
                                    length=len(raw), attempt=attempt)
            if sender.failed and not state.get('delivery_failed_logged'):
                state['delivery_failed_logged'] = True
                self.capture.record('udp_delivery_failed', remote=identity[0], reason='retry_limit')
        self.retry_timer = asyncio.get_running_loop().call_later(.05, self.retry_pending)

    def stop_retries(self):
        if self.retry_timer is not None:
            self.retry_timer.cancel()
            self.retry_timer = None

    def connection_lost(self, exc):
        self.stop_retries()
        self.stop_peer_timer()
        self.match_timers_enabled = False
        if self.match_timer is not None:
            self.match_timer.cancel()
            self.match_timer = None

    def stop_peer_timer(self):
        if self.peer_timer is not None:
            self.peer_timer.cancel()
            self.peer_timer = None

    def poll_peer_rooms(self):
        self.peer_timer = None
        delay = 1
        try:
            delay = self._poll_peer_rooms()
        except Exception:
            # Одно исключение модуля не должно навсегда убивать общий
            # таймер: run20261017T224450 — TypeError в shared_bots остановил
            # весь poll и эвакуация после all_boarded никогда не доехала.
            self.capture.record('peer_timer_error', synthetic=True,
                                error=traceback.format_exc())
        self.peer_timer = asyncio.get_running_loop().call_later(delay, self.poll_peer_rooms)

    def _poll_teleport(self, now):
        """Команда телепорта из файла: {x,y,z,seq} — own-0x27 живым.

        Цель — реальный ground truth: клиент приземляется своей
        физикой и репортит подлинную поверхность, observe() учится.
        После принудительного переноса серверные якоря (scene_position,
        movement_guard) переставляются на новую точку — иначе guard
        вернёт клиента коррекцией обратно.
        """
        if not self.teleport_command:
            return
        # teleport*.json: каждому писателю свой файл — параллельные
        # sweep-процессы (несколько walker-клиентов) не затирают
        # команды друг друга. seq/mtime трекаются по пути.
        base = (self.teleport_command
                if os.path.isdir(self.teleport_command)
                else os.path.dirname(self.teleport_command) or '.')
        for path in sorted(glob.glob(os.path.join(base,
                                                  'teleport*.json'))):
            self._poll_teleport_file(path, now)

    def _poll_teleport_file(self, path, now):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return
        if mtime == self._teleport_mtime.get(path):
            return
        try:
            with open(path, encoding='utf-8') as h:
                cmd = json.load(h)
        except (OSError, ValueError):
            return
        self._teleport_mtime[path] = mtime
        seq = cmd.get('seq', 0)
        if seq == self._teleport_seq.get(path):
            return
        pos = cmd.get('position')
        if not pos or len(pos) != 3:
            return
        x, y, z = (int(v) for v in pos)
        target = cmd.get('target')
        self._teleport_seq[path] = seq
        tick = int((now - self.started) * 25) & 0xffffffff
        moved = []
        for member in self.peer_avatars.members.values():
            # Необязательный селектор: avatar-uid из bot_positions —
            # walker-клиент ловит телепорты, игрока не трогаем.
            if target and hex(member['avatar']) != str(target):
                continue
            state = member['state']
            world = state.get('world')
            anchor = state.get('peer_avatar_anchor')
            template = state.get('scene_position_sync')
            if (world is None or getattr(world, 'dead', False)
                    or anchor is None or not isinstance(template, bytes)
                    or len(template) != 40):
                continue
            body = bytearray(template)
            struct.pack_into('>Q3i', body, 0, state['actor_uid'], x, y, z)
            struct.pack_into('>I', body, 31, tick)
            self.send_application_batch(anchor[0], anchor[1], state,
                                        [(0x27, bytes(body))])
            state['scene_position'] = (x, y, z)
            guard = state.get('movement_guard')
            if guard is not None:
                guard.reset((x, y, z), now)
            event = world.update_position((x, y, z), now)
            if event:
                self.capture.record('world_probe', synthetic=True, **event)
            if 'weapon' in state:
                state['weapon'].update_position((x, y, z))
            moved.append(hex(state['actor_uid']))
        self.capture.record('teleport_command', synthetic=True,
                            position=[x, y, z], seq=seq, moved=moved)

    def _poll_peer_rooms(self):
        self._poll_teleport(time.monotonic())
        if self.peer_avatars:
            now = time.monotonic()
            self.emit_peer_avatars(*self.peer_avatars.equipment(now))
            self.emit_peer_avatars(*self.peer_avatars.pose_snapshots(now,int((now-self.started)*25)&0xffffffff))
            self.emit_peer_avatars(*self.peer_avatars.settle_motion(now,int((now-self.started)*25)&0xffffffff))
            # Fallback for a tap shorter than one heartbeat: the deferred pose
            # must still leave the server after SCOPE_AIMING.
            self.emit_peer_avatars(*self.peer_avatars.aim_pose_sync(now,int((now-self.started)*25)&0xffffffff))
            self.emit_peer_avatars(*self.peer_avatars.aim_sync(now,int((now-self.started)*25)&0xffffffff))
        for state, reason in expired_members(self.peer_avatars, self.shared_loot, time.monotonic()):
            if self.peer_avatars:
                outputs, events = self.peer_avatars.detach(state, reason)
                self.emit_peer_avatars(outputs, events)
            reset = self.shared_loot.detach(state) if self.shared_loot else False
            if self.shared_doors:
                self.shared_doors.detach(state)
            # Итог завершённого матча ещё доставляется и допускает frozen
            # RELAY30с. Удаление из мира не должно уничтожать этот контракт.
            if reason != 'match_finished':
                state.update(transport_retired=True, room_sent=False)
            state['clock_started'] = False
            state.pop('handoff_at', None)
            self.capture.record('peer_room_expired', synthetic=True, reason=reason,
                                account_uid=hex(state['actor_uid']), room_uid=hex(state['room_uid']),
                                room_reset=reset)
        if self.shared_vehicles:
            now=time.monotonic()
            self.shared_vehicles.sync(self.peer_avatars,now)
            self.emit_peer_avatars(*self.shared_vehicles.snapshots(now))
        if self.shared_evacuation:
            self.shared_evacuation.sync(self.peer_avatars)
            self.poll_shared_evacuation(time.monotonic())
        if self.shared_bots:
            now = time.monotonic()
            tick = int((now-self.started)*25) & 0xffffffff
            self.shared_bots.sync(
                self.peer_avatars,
                self.shared_evacuation.rooms if self.shared_evacuation else {},
                lambda uid: (self.shared_loot.rooms.get(uid) or {}).get('death_parcels')
                            if self.shared_loot else None)
            self.emit_peer_avatars(*self.shared_bots.poll(now, tick))
        if self.peer_spectators:
            self.emit_peer_avatars(*self.peer_spectators.poll(self.peer_avatars, time.monotonic()))
        return .1 if self.peer_avatars and self.peer_avatars.replicate_trajectories else 1

    def broadcast_shared_geton(self, state, packets):
        """Собственный GETON остаётся own; другим адресуется remote Unit UID."""
        key = (state['room_uid'], state['actor_uid'])
        member = self.peer_avatars.members.get(key)
        if member is None:
            return
        outputs = []
        for other_key, other in self.peer_avatars.members.items():
            if other_key[0] != key[0] or other_key == key:
                continue
            translated = []
            for command, payload in packets:
                body = bytearray(payload)
                if command == 0x6542:
                    struct.pack_into('>Q', body, 16, member['avatar'])
                translated.append((command,bytes(body)))
            outputs.append((other['state'], translated))
        self.emit_peer_avatars(outputs, [])

    def poll_shared_evacuation(self, now):
        decisions, events = self.shared_evacuation.poll(now)
        for event in events:
            self.capture.record('shared_evacuation_probe', synthetic=True, **event)
        for decision in decisions:
            room_uid, seats = decision['room_uid'], decision['seats']
            prepared = []
            try:
                for account, state in decision['members'].items():
                    statistics = state['match_statistics']
                    report = statistics.snapshot(decision['ended_at'])
                    rescued = account in seats
                    report['rescued'] = int(rescued)
                    packets = []
                    for passenger, seat in seats.items():
                        source = decision['members'][passenger]
                        unit = (passenger if passenger == account else
                                self.peer_avatars.members[(room_uid,passenger)]['avatar'])
                        packets.append((0x6580,survivor_event(decision['helicopter_uid'],
                            unit, source.get('actor_display_name',self.room_display_name),seat)))
                    packets.extend(self.evacuation_result_fixture.messages(account,room_uid,
                        report,state.get('actor_display_name',self.room_display_name)))
                    packets.append((0x6589,struct.pack('>BQ',int(rescued),
                                   decision['helicopter_uid'] if rescued else 0)))
                    prepared.append((state,packets,rescued))
            except (ValueError,KeyError) as error:
                self.capture.record('shared_evacuation_invalid_result',synthetic=True,
                                    room_uid=hex(room_uid),error=str(error))
                continue
            self.shared_evacuation.commit(decision)
            if getattr(self, 'shared_bots', None):
                dead_outputs, dead_event = self.shared_bots.evacuation_departed(room_uid)
                self.emit_peer_avatars(dead_outputs, [])
                self.capture.record('shared_bots_probe', synthetic=True, **dead_event)
            for state, packets, rescued in prepared:
                state['match_statistics'].finish(decision['ended_at'],rescued=rescued)
                state['match_terminal_packets'] = tuple(packets)
                vehicles = state.get('vehicles')
                if vehicles is not None:
                    vehicles.evacuation_finished = True
                anchor = state.get('peer_avatar_anchor')
                if anchor:
                    self.send_application_batch(anchor[0],anchor[1],state,packets)
                self.finish_gameplay(state,anchor[1] if anchor else None,
                                     'rescued' if rescued else decision['reason'])
            self.capture.record('shared_evacuation_probe',synthetic=True,
                action='shared_evacuation_finish',room_uid=hex(room_uid),
                reason=decision['reason'],rescued=len(seats),
                recipients=len(prepared),original_rule_verified=False)

    def poll_match_ends(self):
        """Лимит матча не зависит от heartbeat или потерянного C2S."""
        self.match_timer = None
        now = time.monotonic()
        for identity, state in list(self.sessions.items()):
            if (state.get('transport_retired') or state.get('match_finished')
                    or not state.get('room_sent')):
                continue
            header = state.get('transport_header')
            if header is not None:
                self.check_match_end(header, identity[0], state, now)
        if self.match_timers_enabled:
            self.match_timer = asyncio.get_running_loop().call_later(.1, self.poll_match_ends)

    def check_match_end(self, d, remote, state, now):
        if state.get('match_finished'):
            self.capture.record('finished_action_rejected', synthetic=True, remote=remote)
            return True
        fixture = self.evacuation_result_fixture
        world, statistics = state.get('world'), state.get('match_statistics')
        if fixture is None or world is None or statistics is None:
            return False
        policy = state.setdefault('match_end_policy',
            MatchEndPolicy(fixture.result_on_death, fixture.timeout_seconds))
        if not policy.enabled:
            return False
        vehicles = state.get('vehicles')
        packets, event = policy.poll(now, world, statistics, fixture,
            state['actor_uid'], state.get('room_uid', 1),
            state.get('actor_display_name', self.room_display_name),
            already_rescued=bool(vehicles and vehicles.evacuation_finished))
        if event is None:
            return False
        state['match_terminal_packets'] = tuple(packets)
        self.send_application_batch(d, remote, state, packets)
        self.finish_gameplay(state, remote, event['cause'])
        self.capture.record('match_result', synthetic=True, remote=remote, **event)
        return True

    @staticmethod
    def patch_evac_pad(payload, offsets, refuge):
        """Переносит площадку вертолёта: XYZ по смещениям, высоты
        сохраняют смещение относительно эталонной площадки (dz)."""
        patched = bytearray(payload)
        rx, ry, rz = refuge
        for at, dz in offsets:
            struct.pack_into('>3i', patched, at, rx, ry, rz + dz)
        return bytes(patched)

    def _zone_cursor_write(self):
        if self.zone_state_path is None:
            return
        try:
            self.zone_state_path.write_text(json.dumps({'index': self.zone_next}),
                                            encoding='utf-8')
        except OSError:
            pass

    def zone_pick(self, state):
        """Набор зон текущего матча: один и тот же ключ комнаты/эпохи
        получает один набор, новая эпоха — следующий по кругу. Без
        shared_loot ключ — счётчик завершённых матчей endpoint'а."""
        if not getattr(self, 'zone_sets', ()):
            return None
        room_uid = state.get('room_uid')
        if getattr(self, 'shared_loot', None) is not None and room_uid:
            try:
                self.shared_loot.room(state)  # штампует shared_loot_epoch
            except (KeyError, ValueError):
                return None
            key = ('room', room_uid, state.get('shared_loot_epoch'))
        else:
            key = ('solo', room_uid or state.get('actor_uid'),
                   getattr(self, 'zone_match_seq', 0))
        rooms = getattr(self, 'zone_rooms', None)
        if rooms is None:
            rooms = self.zone_rooms = {}
        index = rooms.get(key)
        if index is None:
            if len(rooms) >= 64:
                rooms.clear()
            cursor = getattr(self, 'zone_next', 0)
            index = cursor % len(self.zone_sets)
            self.zone_next = cursor + 1
            rooms[key] = index
            self._zone_cursor_write()
            self.capture.record('zone_set_selected', synthetic=True,
                                name=self.zone_sets[index]['name'], key=str(key))
        return self.zone_sets[index]

    def finish_gameplay(self, state, remote, cause):
        """Сохраняет итог, отменяет pending действия; transport доставляет ACK/retry."""
        if state.get('match_finished'):
            return
        # Endpoint.__new__ в тестах без __init__: счётчик матчей ленивый.
        self.zone_match_seq = getattr(self, 'zone_match_seq', 0) + 1
        state['match_finished'] = True
        state['match_finished_at'] = time.monotonic()
        state['clock_started'] = False
        weapon, medical = state.get('weapon'), state.get('medical')
        if weapon is not None:
            weapon.pending_reload = None
        if medical is not None:
            medical.pending = medical.completed_at = None
        grenades = state.get('throwable_actions')
        if grenades is not None:
            grenades.pending.clear()
        vehicles = state.get('vehicles')
        if vehicles is not None:
            vehicles.seated = vehicles.seat = vehicles.seated_at = None
            vehicles.climb_until = 0
        # Итог и transport остаются для доставки/аудита. Mutable игровые
        # объекты прежней комнаты больше не нужны новому поиску/сеансу.
        released = []
        for field in ('world', 'weapon', 'medical', 'throwable_actions', 'vehicles',
                      'parcels', 'target_actor', 'target_inventory', 'death_parcels',
                      'loot_catalog', 'live_control', 'loot_spawned_uids'):
            if field in state:
                state.pop(field)
                released.append(field)
        self.capture.record('match_gameplay_finished', synthetic=True, remote=remote,
                            cause=cause, pending_actions_cancelled=True,
                            released_gameplay_fields=released)

    def send_frame(self,d,remote,state,command,sequence,plain,extra=b'', applications=()):
        body=encrypt(plain,state['key'])
        frame=struct.pack('>HHHHBIII',0x3366,10,10,command,1,sequence,21+len(extra),len(body))+extra+body
        if applications:
            # Только маршрутизация собственных ответов; payload/ключи не журналируются.
            self.capture.record('gameplay_frame_send', remote=remote,
                                commands=[hex(code) for code in applications],
                                frame_sequence=sequence, udp_sequence_start=(state['seq']+1)&0xffffffff,
                                source_port=d.destination_port, destination_port=d.source_port,
                                frame_length=len(frame), encrypted_body_length=len(body))
        # В захватах длинные TGCP рамки разделены на потоковые сегменты по 500 байт.
        for at in range(0,len(frame),500):
            chunk=frame[at:at+500]
            self.send(d,remote,state['seq']+1,state['next_client'],0x18,chunk)
            state['seq']+=len(chunk)

    @staticmethod
    def death_reserved_indexes(state):
        indexes = set(state['weapon'].loot_registry)
        parcels = state.get('parcels')
        containers = getattr(parcels, 'containers', ())
        for container in containers:
            indexes.add(container.fixture.index)
            indexes.update(x.index for x in container.fixture.items)
        if parcels is not None and not containers:
            fixture = parcels.fixture
            indexes.update((fixture.index, fixture.item_index))
        return indexes

    def emit_own_death_inventory(self, d, remote, state):
        world, weapon = state.get('world'), state.get('weapon')
        if (world is None or not getattr(world, 'dead', False) or weapon is None
                or state.get('own_death_inventory_processed')):
            return
        corpses = state.setdefault('death_parcels', DeathParcelState())
        try:
            packets, event = corpses.transfer(weapon, world.position,
                                               self.death_reserved_indexes(state))
        except ValueError as error:
            self.capture.record('death_parcel_probe', synthetic=True, remote=remote,
                                action='own_death_inventory_blocked', reason=str(error))
        else:
            self.send_application_batch(d, remote, state, weapon.reconcile_inventory() + packets)
            if self.shared_loot and self.shared_loot.share_death_parcels:
                self.emit_shared_loot(self.shared_loot.death_outputs(state,packets))
            self.capture.record('death_parcel_probe', synthetic=True, remote=remote, **event)
        state['own_death_inventory_processed'] = True

    def apply_diagnostic_grenade(self, d, remote, state, finished):
        """Opt-in учебный frag: flat damage, радиус и известные AABB стенда.

        Client-list сам по себе не наносит урон. Проверяем принятую grenade
        lifecycle, тип, живую цель, серверную позицию и известные преграды.
        Полный LOS карты и оригинальная формула/световая слепота ещё открыты.
        """
        damage = state['weapon'].fixture.diagnostic_frag_damage
        if finished.definition != 50001 or not damage:
            return
        radius = THROWABLE_EFFECT_PROFILES[50001][1]
        actor = state.get('target_actor')
        boxes = actor.fixture.collision_boxes or () if actor else ()

        def reachable(point):
            if point is None or sum((a-b)**2 for a,b in zip(point, finished.point)) > radius**2:
                return False
            return not any(segment_box_entry(finished.point, point, lower, upper) is not None
                           for lower, upper in boxes)

        reported = set(finished.actors) | {uid for uid, _ in finished.actor_fields}
        if actor and actor.fixture.uid in reported and reachable(actor.position):
            packets, event = actor.diagnostic_area_damage(damage)
            state.setdefault('match_statistics', MatchStatistics()).observe(event)
            self.send_application_batch(d, remote, state, packets)
            self.capture.record('throwable_probe', synthetic=True, remote=remote,
                                identifier=finished.identifier, **event)
            if actor.hp == 0 and 'target_inventory' in state:
                inventory = state['target_inventory']
                if actor.fixture.armor_definition:
                    inventory.equipment_durability[12] = actor.armor_durability
                corpses = state.setdefault('death_parcels', DeathParcelState())
                packets, event = corpses.transfer(inventory, actor.position,
                                                  self.death_reserved_indexes(state))
                self.send_application_batch(d, remote, state, packets)
                self.capture.record('death_parcel_probe', synthetic=True, remote=remote, **event)
        world = state.get('world')
        if world and not world.dead and reachable(world.position):
            hurt, armor_packets, armor_event = state['weapon'].absorb_combat_damage(damage)
            packets, event = world.apply_damage(hurt, state['actor_uid'])
            self.send_application_batch(d, remote, state, armor_packets + packets)
            self.capture.record('weapon_probe', synthetic=True, remote=remote, **armor_event)
            self.capture.record('world_probe', synthetic=True, remote=remote, **event)
            if self.peer_avatars:
                self.emit_peer_avatars(*self.peer_avatars.health(state, time.monotonic()))
            self.emit_own_death_inventory(d, remote, state)
            self.cancel_dead_actions(state, remote)

    def cancel_dead_actions(self, state, remote):
        world = state.get('world')
        if world is None or not getattr(world, 'dead', False):
            return
        statistics = state.get('match_statistics')
        if statistics is not None and statistics.ended_at is None:
            report = statistics.finish(time.monotonic(), rescued=False)
            self.capture.record('match_statistics', synthetic=True, remote=remote,
                                terminal='death', **report)
        vehicles = state.get('vehicles')
        if vehicles is not None:
            event = vehicles.cancel_on_death()
            if event is not None:
                self.capture.record('vehicle_probe', synthetic=True, remote=remote, **event)
        weapon, medical = state.get('weapon'), state.get('medical')
        if weapon is not None and weapon.pending_reload is not None:
            weapon.pending_reload = None
            self.capture.record('weapon_probe', action='reload_cancelled_on_death',
                                synthetic=True, remote=remote)
        if medical is not None:
            if medical.pending is not None:
                medical.pending = None
                self.capture.record('medical_probe', action='medical_cancelled_on_death',
                                    synthetic=True, remote=remote)
            # Не отправлять status27 после смерти: это окончание живого action.
            medical.completed_at = None
        self.send_death_pin(state, time.monotonic())

    def send_death_pin(self, state, now, force=False):
        """Повторная выдача собственного 0x27 на точке смерти.

        Клиент предсказывает своё движение до входа в spectator — серверное
        отклонение 0x2a само по себе модель не останавливает. Позиция фиксируется
        на месте смерти, статус 2 — проверенный prone/lying из LEARNINGS.
        Без этого own-0x27 клиент игнорирует 0x6fbb и продолжает симулировать
        павна (run 154812: 61 × dead_action_rejected 0x2a; с пином — 0).
        """
        world = state.get('world')
        anchor = state.get('peer_avatar_anchor')
        if (self.death_pin_template is None or world is None or anchor is None
                or not getattr(world, 'dead', False) or world.position is None):
            return
        if not force and now - state.get('death_pin_at', -10) < 0.4:
            return
        state['death_pin_at'] = now
        body = bytearray(self.death_pin_template)
        struct.pack_into('>Q', body, 0, state['actor_uid'])
        struct.pack_into('>iii', body, 8, *(int(v) for v in world.position[:3]))
        struct.pack_into('>I', body, 27, 2)
        tick = int((now - self.started) * 25) & 0xffffffff
        struct.pack_into('>I', body, 31, tick)
        self.send_application_batch(anchor[0], anchor[1], state, [(0x27, bytes(body))])
        self.capture.record('death_pin', synthetic=True, account_uid=hex(state['actor_uid']),
                            position=list(world.position))

    def accept_living_action(self, state, message, remote):
        world = state.get('world')
        if (world is not None and getattr(world, 'dead', False) and message.command in (
                0x6f57, 0x6f59, 0x2a, 0x2f, 0x11d5, 0x11d7,
                0x49e, 0x49c, 0x11da, 0x11dd, 0x11de, 0x3b63, 0x3e80, 0x536, 0x1004,
                0x6541, 0x6543, 0x6547, 0x6549, 0x657a, 0x656f, 0xbce)):
            self.capture.record('dead_action_rejected', command=hex(message.command),
                                synthetic=True, remote=remote)
            if message.command == 0x2a:
                self.send_death_pin(state, time.monotonic())
            return False
        return True

    def accept_inventory_action(self, state, message, remote):
        """Один эффект известного gameplay-запроса текущего упорядоченного транспорта.

        Native2022 увеличивает первое BE32 каждого application message.
        Проверяем serial32 для pickup/discard/reload/fire/hand, не payload:
        новый id проходит этот guard; fire отдельно проверяет счётчик выстрела.
        При новом AUTH граница новая; журнал прежнего транспорта не переносим.
        Ответы старого inventory не пересылаем: они откатили бы HUD после
        промежуточного действия. ACK потока уже отправлен datagram_received.
        """
        sequence = message.sequence
        if type(sequence) is not int or not 0 <= sequence <= 0xffffffff:
            self.capture.record('inventory_action_rejected', reason='missing_sequence',
                                command=hex(message.command), remote=remote, synthetic=True)
            return False
        previous = state.get('inventory_action_sequence')
        if previous is not None:
            delta = (sequence - previous) & 0xffffffff
            if delta == 0 or delta >= 0x80000000:
                self.capture.record('inventory_action_rejected',
                                    reason='repeated_sequence' if delta == 0 else 'stale_sequence',
                                    command=hex(message.command), application_sequence=sequence,
                                    previous_sequence=previous, remote=remote, synthetic=True)
                return False
        state['inventory_action_sequence'] = sequence
        return True

    def resume_gameplay(self, identity, state, now, relay_actor_uid=None):
        """Однократный перенос явно закрытой локальной диагностической сессии.

        Ключ, TGCP frame/stream sequence и ACK остаются у нового транспорта.
        Это стенд с synthetic AUTH, не серверная авторизация для LAN.
        """
        if not state.get('auth_sent') or not state.get('identity') or state.get('room_sent'):
            return False
        if (self.peer_avatars or self.shared_loot) and relay_actor_uid is None:
            # В диагностическом лобби identity нескольких аккаунтов совпадает.
            # Ждём UID C2S RELAY, иначе AUTH может забрать состояние другого игрока.
            return False
        candidates = [(key, value) for key, value in self.sessions.items()
                      if value is not state and value.get('room_sent')
                      and value.get('auth_sent') and value.get('identity') == state['identity']
                      and not getattr(value.get('vehicles'), 'evacuation_finished', False)
                      and not value.get('match_finished')
                      and not value.get('transport_retired')
                      and (not (self.peer_avatars or self.shared_loot)
                           or value.get('actor_uid') == relay_actor_uid)
                      and key[0][0] == identity[0][0] and key[2] == identity[2]
                      and (0 <= now-value.get('handoff_at', -1000000) <= 30
                           or (self.idle_handoff and self.reliable_udp
                               and relay_actor_uid == value.get('actor_uid')
                               and relay_actor_uid is not None
                               and 5 <= now-value.get('last_application_at', now) <= 30))]
        if len(candidates) != 1:
            return False
        previous_identity, previous = candidates[0]
        handoff_mode = ('explicit_close' if 0 <= now-previous.get('handoff_at', -1000000) <= 30
                        else 'authenticated_idle_relay')
        idle_seconds = now-previous.get('last_application_at', now)
        fields = ('actor_uid', 'room_uid', 'actor_display_name', 'scene_position', 'scene_position_sync', 'last_movement_position', 'movement_guard', 'scene_rules_sent', 'clock_started',
                  'selection_deadline', 'weapon', 'medical', 'vehicles', 'parcels', 'target_actor',
                  'target_inventory', 'death_parcels', 'throwable_actions',
                  'own_death_inventory_processed',
                  'world', 'match_statistics', 'match_end_policy', 'live_control', 'loot_spawned', 'loot_spawned_uids',
                  'winter_kit_initialized', 'winter_choice', 'born_point', 'loot_catalog', 'environment_initialized',
                  'shared_loot_epoch', 'doors', 'breakables')
        for name in fields:
            if name in previous:
                state[name] = previous.pop(name)
        state['room_sent'] = True
        state['gameplay_resumed'] = True
        previous['room_sent'] = False
        previous.pop('handoff_at', None)
        previous['clock_started'] = False
        previous['transport_retired'] = True
        if 'breakables' in state:
            state['breakables'].resume()
        if 'doors' in state:
            state['doors'].resume()
        if self.shared_doors:
            self.shared_doors.resume(state)
        if self.shared_loot and 'weapon' in state:
            self.shared_loot.attach(state)
        if self.peer_avatars and 'scene_position' in state:
            tick = int((now-self.started)*25) & 0xffffffff
            outputs, events = self.peer_avatars.admit(state, tick, time.monotonic())
            self.emit_peer_avatars(outputs, events)
            state['peer_avatar_admitted'] = True
        sender = previous.get('sender')
        if sender is not None:
            sender.pending.clear()
            sender.bytes = 0
        # Только диагностические игровые поля; ключи/identity не журналируем.
        world, weapon = state.get('world'), state.get('weapon')
        # Клиентский RELAY может сбросить actor к стартовой позиции. Сохраняем
        # последнюю позицию до новых2a и возвращаем её известным27 после7083.
        state['resume_position_snapshot'] = (getattr(world, 'position', None)
                                             or state.get('last_movement_position')
                                             or state.get('scene_position'))
        self.capture.record('gameplay_handoff_complete', synthetic=True,
                            handoff_mode=handoff_mode, previous_idle_seconds=idle_seconds,
                            previous_remote=previous_identity[0], remote=identity[0],
                            experimental=True,
                            world_position=getattr(world, 'position', None),
                            world_hp=getattr(world, 'hp', None),
                            world_started=getattr(world, 'started', None),
                            world_stage_index=getattr(world, 'stage_index', None),
                            world_warning_index=getattr(world, 'warning_index', None),
                            clip=getattr(weapon, 'clip', None), reserve=getattr(weapon, 'reserve', None),
                            ammunition_definition=(weapon.gun_ammunition if isinstance(weapon, WeaponState) else None),
                            ammunition_quantity=(weapon.ammunition_quantity(weapon.gun_ammunition)
                                                 if isinstance(weapon, WeaponState) else None),
                            door_state_changes=({str(identifier): status
                                for identifier, status in state['doors'].status.items()
                                if status != state['doors'].fixture.doors[identifier][1]}
                                if isinstance(state.get('doors'), DoorState) else None),
                            picked_loot_count=len(getattr(weapon, 'picked_loot', ())),
                            available_loot_count=len(getattr(weapon, 'available_loot', ())),
                            frame_sequence=state['frame_seq'])
        return True

    def resume_finished_result(self, identity, d, state, now, relay_actor_uid):
        """Только frozen проигрышный итог после AUTH/RELAY, без новой игры.

        Cold177: C2S blackout закрыл старый канал до timer result; новый
        AUTH дошёл после deadline. Старый retry не достигает нового ключа.
        Граница30с/identity/actor/IP/virtual destination совпадает с local
        handoff. Это не возможность продолжить завершённый матч.
        """
        if (not self.idle_handoff or not self.reliable_udp or not state.get('auth_sent')
                or not state.get('identity') or state.get('room_sent')
                or relay_actor_uid is None):
            return False
        candidates = [(key, value) for key, value in self.sessions.items()
                      if value is not state and value.get('room_sent')
                      and value.get('auth_sent') and value.get('match_finished')
                      and value.get('match_terminal_packets')
                      and not value.get('transport_retired')
                      and value.get('identity') == state['identity']
                      and value.get('actor_uid') == relay_actor_uid
                      and key[0][0] == identity[0][0] and key[2] == identity[2]
                      and 0 <= now-value.get('match_finished_at', -1000000) <= 30]
        if len(candidates) != 1:
            return False
        previous_identity, previous = candidates[0]
        for field in ('actor_uid','room_uid','actor_display_name','match_statistics',
                      'match_end_policy','match_finished_at','match_terminal_packets'):
            if field in previous:
                state[field] = previous[field]
        state.update(room_sent=True, match_finished=True, clock_started=False)
        previous.update(room_sent=False, transport_retired=True)
        sender = previous.get('sender')
        if sender is not None:
            sender.pending.clear()
            sender.bytes = 0
        # Сначала штатный ответ RELAY, затем уже зафиксированный итог.
        for rule in self.scene_rules:
            if rule[0] == 0xdcd:
                for response in rule[1]:
                    if response[0] == 0x7083:
                        self.send_application(d, identity[0], state, 0x7083, response[1])
        self.send_application_batch(d, identity[0], state, state['match_terminal_packets'])
        self.capture.record('match_terminal_replayed', synthetic=True,
                            previous_remote=previous_identity[0], remote=identity[0],
                            actor_uid=hex(relay_actor_uid), restored_gameplay=False,
                            seconds_after_finish=now-state['match_finished_at'])
        return True

    def restore_resume_position(self, d, remote, state):
        """Однократный27 текущей позиции; не повторять сцену/лут/инвентарь."""
        if state.get('resume_position_sent'):
            return
        template = state.get('scene_position_sync')
        position = state.get('resume_position_snapshot')
        if (not isinstance(template, bytes) or len(template) != 40
                or not position or len(position) != 3
                or any(type(n) is not int or not -2**31 <= n < 2**31 for n in position)):
            self.capture.record('gameplay_resume_position_missing', synthetic=True, remote=remote)
            return
        payload = bytearray(template)
        struct.pack_into('>Q3i', payload, 0, state['actor_uid'], *position)
        struct.pack_into('>I', payload, 31, int((time.monotonic()-self.started)*25)&0xffffffff)
        payload = bytes(payload)
        self.send_application(d, remote, state, 0x27, payload)
        state['resume_position_sent'] = True
        self.register_fixture_entity(state, 0x27, payload)
        self.capture.record('gameplay_resume_position_sync', synthetic=True,
                            remote=remote, position=position, command='0x27')

    def restore_resume_world(self, d, remote, state):
        """Стадия и действующее предупреждение481 после смены транспорта."""
        if state.get('resume_world_sent'):
            return
        world = state.get('world')
        if not isinstance(world, WorldState) or not world.fixture.restore_stage_on_resume:
            return
        now = time.monotonic()
        stages_advanced, warnings_advanced = world.catch_up_zone(now)
        if stages_advanced or warnings_advanced:
            self.capture.record('world_probe', action='zone_schedule_catchup', synthetic=True,
                                remote=remote, stages_advanced=stages_advanced,
                                warnings_advanced=warnings_advanced)
        snapshot = world.stage_snapshot(now)
        warning = world.warning_snapshot(now)
        state['resume_world_sent'] = True
        if snapshot is not None:
            body, timing = snapshot
            self.send_application(d, remote, state, 0x481, body)
            self.capture.record('world_probe', action='zone_stage_resume', synthetic=True,
                                remote=remote, **timing)
        if warning is not None:
            body, timing = warning
            self.send_application(d, remote, state, 0x481, body)
            self.capture.record('world_probe', action='zone_warning_resume', synthetic=True,
                                remote=remote, **timing)

    def restore_resume_inventory(self, d, remote, state):
        """Однократное согласование известных слотов/HP после потери финальных RPC."""
        if state.get('resume_inventory_sent'):
            return
        weapon, world, medical = state.get('weapon'), state.get('world'), state.get('medical')
        if not isinstance(weapon, WeaponState):
            return
        self.cancel_dead_actions(state, remote)
        if weapon.pending_reload:
            # Native361: первый финальный4A0 удаляет совпавшее предсказание
            # reload, но не публикует магазин после старого handoff snapshot.
            pending = weapon.pending_reload
            state['resume_reload_reconcile'] = (pending['slot'], pending['uid'])
        now = time.monotonic()
        if medical is not None:
            _, events = medical.poll(now)
            for event in events:
                self.capture.record('medical_probe', synthetic=True, remote=remote, **event)
        packets = weapon.reconcile_inventory()
        if 'death_parcels' in state:
            packets.extend(state['death_parcels'].reconcile(state['actor_uid']))
        if world is not None:
            packets.append((0x1069, encode_health(weapon.actor_uid, world.hp)))
        if (medical is not None and medical.pending is None and medical.completed_at is not None
                and 0 <= now - medical.completed_at <= 30 and not weapon.pending_reload):
            packets.append(medical.finish_status())
        if world is not None and getattr(world, 'dead', False):
            # Новому transport повторяем актуальное состояние смерти, не эффект урона.
            packets.append(world.death_message())
        # Каждая короткая рамка отдельная: не добавляем крупный snapshot при handoff.
        for command, payload in packets:
            self.send_application(d, remote, state, command, payload)
        state['resume_inventory_sent'] = True
        self.capture.record('gameplay_resume_inventory_sync', synthetic=True, remote=remote,
                            gun_count=len(weapon.guns), medical_stacks=dict(weapon.medical),
                            hp=getattr(world, 'hp', None), messages=len(packets))

    def reconcile_resume_reload(self, remote, state, packets, events):
        """Один повтор финального магазина только для reload, пережившей handoff."""
        marker = state.get('resume_reload_reconcile')
        weapon = state.get('weapon')
        if marker is None or not isinstance(weapon, WeaponState):
            return packets
        if weapon.pending_reload is not None:
            return packets
        state.pop('resume_reload_reconcile', None)
        slot, uid = marker
        gun = weapon.guns.get(slot)
        if (gun is None or gun.uid != uid or not any(
                event.get('action') == 'reload' and event.get('slot') == slot
                for event in events)):
            return packets
        final = (0x4a0, weapon.ammunition_sync(slot=slot))
        if final not in packets:
            return packets
        self.capture.record('gameplay_resume_reload_reconcile', synthetic=True,
                            remote=remote, slot=slot, clip=gun.clip, counter=0,
                            repeats=1, experimental=True)
        # Новый application id; транспортный duplicate клиент отбрасывает.
        return packets + [final]

    def accept_movement(self, d, remote, state, payload):
        """Учебный бюджет пути до изменения world/loot-позиции; дефолт отключён."""
        limits = getattr(self.world_fixture, 'movement_limits', None)
        if limits is None:
            return True
        now = time.monotonic()
        movement = decode_move_request(payload)
        guard = state.get('movement_guard')
        if guard is None:
            anchor = state.get('scene_position')
            if anchor is None:
                self.capture.record('movement_validation_awaiting_anchor', remote=remote,
                                    reason='missing_server_anchor', experimental=True)
                return False
            guard = state['movement_guard'] = MovementState(limits, anchor, now)
        accepted, info = guard.accept(movement, now)
        self.capture.record('movement_validation_accepted' if accepted else 'movement_validation_rejected',
                            remote=remote, **info)
        if accepted:
            return True
        # Отказ не меняет anchor/budget. Native27 возвращает позицию, но не
        # регистрируется как новый разрешённый teleport и не выдаёт свежий burst.
        template = state.get('scene_position_sync')
        if isinstance(template, bytes) and len(template) == 40:
            correction = bytearray(template)
            struct.pack_into('>Q3i', correction, 0, state['actor_uid'], *guard.position)
            struct.pack_into('>I', correction, 31, int((now-self.started)*25)&0xffffffff)
            self.send_application(d, remote, state, 0x27, bytes(correction))
            self.capture.record('movement_validation_correction', remote=remote,
                                position=guard.position, command='0x27', experimental=True)
        return False

    def register_fixture_entity(self, state, command, payload):
        """Регистрировать только заранее описанный лут сцены или live-опыта."""
        if command == 0x27 and len(payload) == 40 and struct.unpack_from('>Q', payload)[0] == state['actor_uid']:
            state['scene_position'] = struct.unpack_from('>3i', payload, 8)
            state.setdefault('match_statistics', MatchStatistics()).reset_anchor(state['scene_position'])
            state['scene_position_sync'] = bytes(payload)
            if 'movement_guard' in state:
                state['movement_guard'].reset(state['scene_position'], time.monotonic())
            if 'weapon' in state:
                state['weapon'].update_position(state['scene_position'])
            if 'world' in state:
                event = state['world'].update_position(state['scene_position'], time.monotonic())
                if event:
                    self.capture.record('world_probe', synthetic=True, **event)
        state.setdefault('vehicles', VehicleState(state['actor_uid'],
                         None if getattr(self, 'shared_evacuation', None) else self.evacuation_delay,
                         survivor_name=state.get('actor_display_name', getattr(self, 'room_display_name', 'LOCAL')) if self.evacuation_delay is not None else None,
                         result_fixture=self.evacuation_result_fixture,
                         room_uid=state.get('room_uid', 1),
                         statistics=state.setdefault('match_statistics', MatchStatistics()),
                         diagnostic_pellet_damage=getattr(self, 'diagnostic_vehicle_pellet_damage', None))).register(command, payload)
        if getattr(self,'shared_vehicles',None):
            self.shared_vehicles.register(state,command,payload)
        if self.parcel_fixture:
            if 'parcels' not in state:
                state['parcels'] = make_parcel_state(self.parcel_fixture)
            state['parcels'].register(command, payload)
        if command != 0x821 or self.weapon_fixture is None or len(payload) < 12:
            return
        uid = struct.unpack_from('>Q', payload, 4)[0]
        if uid not in {item.entity_uid for item in self.weapon_fixture.all_loot}:
            return
        state.setdefault('loot_spawned_uids', set()).add(uid)
        primary = self.weapon_fixture.loot
        state['loot_spawned'] = bool(primary and uid == primary.entity_uid) or state.get('loot_spawned', False)
        if 'weapon' in state:
            state['weapon'].loot_available = state['loot_spawned']
            state['weapon'].available_loot.add(uid)

    def emit_peer_avatars(self,outputs,events):
        for recipient,messages in outputs:
            anchor = recipient.get('peer_avatar_anchor')
            if (anchor and recipient.get('auth_sent') and recipient.get('room_sent')
                    and not recipient.get('match_finished') and not recipient.get('transport_retired')
                    and time.monotonic()-anchor[2] <= 5):
                self.send_application_batch(anchor[0],anchor[1],recipient,messages)
        for event in events:
            self.capture.record('peer_avatar_probe',synthetic=True,shared_inventory=False,**event)

    def emit_shared_loot(self, outputs):
        for recipient, messages in outputs:
            anchor = recipient.get('peer_avatar_anchor')
            if (anchor and recipient.get('auth_sent') and recipient.get('room_sent')
                    and not recipient.get('match_finished') and not recipient.get('transport_retired')
                    and time.monotonic()-anchor[2] <= 5):
                self.send_application_batch(anchor[0], anchor[1], recipient, messages)
                self.capture.record('shared_loot_broadcast', synthetic=True,
                                    account_uid=hex(recipient['actor_uid']),
                                    commands=[hex(code) for code, _ in messages])

    def send_application(self,d,remote,state,command,payload):
        if getattr(self, 'shared_loot', None) and command == 0x821:
            payload = self.shared_loot.filter_spawn(state, payload)
            if payload is None:
                return
        stamp=int((time.monotonic()-self.started)*25)&0xffffffff
        plain=struct.pack('>IHI',stamp,command,command)+payload
        state['frame_seq']+=1
        self.send_frame(d,remote,state,0x4013,state['frame_seq'],plain,bytes(4),applications=(command,))

    def send_application_batch(self, d, remote, state, messages):
        """Прикладные сообщения одной команды опыта в одном TGCP-заголовке.

        Первичные4013: extra[3] задаёт количество BE16-длин, последняя
        длина определяется остатком. Предел не даёт разрастаться рамке.
        """
        stamp = int((time.monotonic()-self.started)*25) & 0xffffffff
        packets = []
        for code, payload in messages:
            if getattr(self, 'shared_loot', None) and code == 0x821:
                payload = self.shared_loot.filter_spawn(state, payload)
                if payload is None:
                    continue
            if not 0 <= code <= 65535 or len(payload) > 16384:
                raise ValueError('Прикладное сообщение вне пределов опыта')
            packets.append(struct.pack('>IHI', stamp, code, code) + payload)
        batch, size = [], 0
        # Live2022: клиент молча роняет TGCP-кадры сверх ~3.6КБ — spawn-слепок
        # 20 ботов (22КБ, run20261017T224450) не отрендерился ни разу, а лут
        # в кадрах ≤3.6КБ подбирался. Предел 3000 держит любой всплеск в
        # проверенном диапазоне: дробление на кадры для клиента прозрачно.
        for packet in packets + [None]:
            if batch and (packet is None or size + len(packet) > 3000 or len(batch) == 64):
                extra = bytes([0, 0, 0, len(batch)-1]) + b''.join(
                    struct.pack('>H', len(part)) for part in batch[:-1])
                state['frame_seq'] += 1
                self.send_frame(d, remote, state, 0x4013, state['frame_seq'], b''.join(batch), extra,
                                applications=tuple(struct.unpack_from('>H',part,4)[0] for part in batch))
                batch, size = [], 0
            if packet is not None:
                batch.append(packet)
                size += len(packet)

    def send_own_avatar(self, d, remote, state):
        """0x553 AVATAR_SYNC собственного актора из appearance/<actor_uid>.json.

        Decoder 140c2fb00: {u64 unit, 5×u32 персонаж, u8 applied} — тот же
        формат, что у PeerAvatarRooms; реальный сервер шлёт sync актора
        при входе в сцену, наш стенд раньше не отправлял его для себя.
        """
        if self.appearance_dir is None or not state.get('actor_uid'):
            return
        path = Path(self.appearance_dir)/f"{state['actor_uid']:x}.json"
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        avatar = saved.get('avatar', [0]*5)
        if (len(avatar) != 5
                or any(type(x) is not int or not 0 <= x <= 0xffffffff for x in avatar)
                or not any(avatar)):
            return
        payload = struct.pack('>Q5IB', state['actor_uid'], *avatar,
                              int(bool(saved.get('avatar_applied'))))
        self.send_application(d, remote, state, 0x553, payload)
        self.capture.record('own_avatar_sync', synthetic=True, remote=remote,
                            actor_uid=hex(state['actor_uid']), avatar=avatar)

    def send_clock(self,d,remote,state):
        elapsed=time.monotonic()-self.started
        # Decoder 140bd40c0: четыре BE32; третье слово native задаёт 40 мс/тик.
        payload=struct.pack('>4I',int(elapsed*25)&0xffffffff,int(elapsed*50)&0xffffffff,40,2)
        self.send_application(d,remote,state,0x1f57,payload)
        self.capture.record('clock_tick',synthetic=True,tick_duration_ms=40,remote=remote)


async def main(args):
    capture = Capture(args.output)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    # Proactor loop Windows не поддерживает add_signal_handler; Ctrl+C
    # отменяет main через asyncio.run и выполняет блок finally ниже.
    if sys.platform != 'win32':
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
    scene_pads = {}
    rules = load_scene_rules(args.scene_rules, pads=scene_pads)
    weapon_path = getattr(args, 'weapon_config', None)
    weapon_fixture = WeaponFixture.load(weapon_path) if weapon_path else None
    catalog_path = getattr(args, 'loot_catalog', None)
    loot_catalog = None
    if catalog_path:
        if not weapon_fixture or not weapon_fixture.all_loot:
            raise ValueError('loot-catalog требует weapon-config с проверенным ground821')
        loot_catalog = LootCatalog.load(catalog_path, weapon_fixture.all_loot[0].entity)
        loot_catalog.validate_fixture(weapon_fixture)
    shared_catalog_path = getattr(args, 'shared_loot_catalog', None)
    shared_loot_catalog = None
    if shared_catalog_path:
        if not weapon_fixture or not weapon_fixture.all_loot:
            raise ValueError('shared-loot-catalog требует weapon-config с проверенным ground821')
        shared_loot_catalog = LootCatalog.load(shared_catalog_path, weapon_fixture.all_loot[0].entity)
        shared_loot_catalog.validate_fixture(weapon_fixture)
    world_path = getattr(args, 'world_config', None)
    world_fixture = WorldFixture.load(world_path) if world_path else None
    # Наборы зон для ротации по матчам: каждый набор получает свой fixture
    # поверх базовой конфигурации мира (общие HP/hazard/сетка).
    zone_sets = []
    zone_sets_path = getattr(args, 'zone_sets', None)
    if zone_sets_path:
        if world_path is None:
            raise ValueError('--zone-sets требует --world-config')
        base_world = json.loads(Path(world_path).read_text(encoding='utf-8'))
        for entry in json.loads(Path(zone_sets_path).read_text(encoding='utf-8'))['sets']:
            cfg = dict(base_world)
            cfg['stages'] = entry['stages']
            if 'evacuation_refuge' in entry:
                cfg['evacuation_refuge'] = entry['evacuation_refuge']
            refuge = entry.get('evacuation_refuge')
            zone_sets.append(dict(name=entry['name'], fixture=WorldFixture.from_config(cfg),
                                  refuge=tuple(refuge['position']) if refuge else None))
    actor_path = getattr(args, 'actor_config', None)
    actor_fixture = ActorFixture.load(actor_path) if actor_path else None
    if actor_fixture and actor_fixture.attack and world_fixture is None:
        raise ValueError('Экспериментальная атака бота требует world-config для общих HP')
    parcel_path = getattr(args, 'parcel_config', None)
    parcel_fixture = load_parcel_fixture(parcel_path, weapon_fixture, loot_catalog) if parcel_path else None
    result_path = getattr(args, 'evacuation_result_config', None)
    result_fixture = EvacuationUIFixture.load(result_path) if result_path else None
    environment_path=getattr(args,'environment_config',None)
    environment_fixture=EnvironmentFixture.load(environment_path) if environment_path else None
    peer_avatar_fixture = ActorFixture.load(args.peer_avatar_config) if getattr(args,'peer_avatar_config',None) else None
    transport, endpoint = await loop.create_datagram_endpoint(lambda:Endpoint(capture,args.handshake,args.ident,args.grant,args.auth,args.room_template,rules,args.scene_clock,args.selection_seconds,weapon_fixture,getattr(args,'live_commands',None),world_fixture,actor_fixture,getattr(args,'evacuation_delay_seconds',None),parcel_fixture,result_fixture,loot_catalog,environment_fixture,getattr(args,'reliable_udp',False),getattr(args,'idle_handoff',False),getattr(args,'diagnostic_vehicle_pellet_damage',None),peer_avatar_fixture,getattr(args,'shared_ground_loot',False),getattr(args,'shared_world_clock',False),getattr(args,'shared_peer_health',False),getattr(args,'peer_avatar_trajectories',False),getattr(args,'peer_spectator',False),getattr(args,'shared_evacuation',False),getattr(args,'shared_vehicles',False),getattr(args,'peer_equipment',False),getattr(args,'peer_pellet_damage',None),getattr(args,'shared_death_loot',False),door_fixture=DoorFixture.load(args.door_config) if getattr(args,'door_config',None) else None,breakable_fixture=BreakableFixture.load(args.breakable_config) if getattr(args,'breakable_config',None) else None,shared_loot_catalog=shared_loot_catalog,shared_bots=getattr(args,'shared_bots',0) or 0,bot_pellet_damage=getattr(args,'bot_pellet_damage',None),teleport_command=getattr(args,'teleport_command',None),peer_actor_enter=getattr(args,'peer_actor_enter',False),zone_sets=zone_sets,zone_state=getattr(args,'zone_state',None),scene_pads=scene_pads), local_addr=(getattr(args,'bind','127.0.0.1'),args.port))
    endpoint.appearance_dir = getattr(args,'appearance_dir',None)
    if endpoint.appearance_dir is not None and endpoint.peer_avatars is not None:
        endpoint.peer_avatars.appearance_dir = endpoint.appearance_dir
    try:
        await asyncio.wait_for(stop.wait(),args.duration)
    except asyncio.TimeoutError:
        pass
    finally:
        endpoint.stop_retries()
        endpoint.stop_peer_timer()
        transport.close()
        capture.record('stopped', sessions=len(endpoint.sessions))
        capture.events.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port',type=int,default=18001)
    p.add_argument('--bind',default='127.0.0.1',help='Адрес прослушивания UDP; по умолчанию loopback')
    p.add_argument('--duration',type=float,default=330)
    p.add_argument('--reliable-udp',action='store_true',help='Экспериментальные ACK/retry и ограниченная входная сборка')
    p.add_argument('--diagnostic-vehicle-pellet-damage', type=int, choices=range(1,5001), metavar='1..5000', help='Учебный урон по корпусу102/109; исходная формула не восстановлена')
    p.add_argument('--idle-handoff',action='store_true',help='Экспериментальный перенос послеAUTH/RELAY при5..30с idle; требует reliable-udp')
    p.add_argument('--handshake',action='store_true',help='Экспериментальный ответ 1002 с новым локальным ключом')
    p.add_argument('--ident',action='store_true',help='Экспериментальный 2002 с identity из текущего клиента')
    p.add_argument('--grant',action='store_true',help='Экспериментальный 6002; требует ident и handshake')
    p.add_argument('--auth',action='store_true',help='Экспериментальный успешный CONN_AUTH_RSP после согласованного synthetic запроса')
    p.add_argument('--room-template',type=Path,help='Явное тело room-ответа; локальные идентификаторы подставляются из запроса')
    p.add_argument('--scene-rules',type=Path,help='JSON одноразовых ответов на команды клиента после входа в комнату')
    p.add_argument('--scene-clock',action='store_true',help='Локальные TIME_SYN_CMD/CUR_TICK_TEL после уведомления загрузки; 40 мс/тик')
    p.add_argument('--selection-seconds',type=int,help='Экспериментальная готовность 1/1 и таймер выбора; требует scene-clock')
    p.add_argument('--weapon-config',type=Path,help='Экспериментальная Citori, reload и опциональный pickup12ga; JSON с проверенными fixture')
    p.add_argument('--breakable-config', type=Path, help='Разрушение однокомпонентных сущностей16010; учебное подтверждение')
    p.add_argument('--door-config', type=Path, help='Обратимые двери16010; учебный радиус/одиночное состояние')
    p.add_argument('--loot-catalog',type=Path,help='Явный каталог поддержанного лута; собственная выдача по радиусу, требует weapon-config')
    p.add_argument('--shared-loot-catalog',type=Path,help='Общий каталог EU_LootBox комнаты: общие taken/remaining; требует shared-ground-loot')
    p.add_argument('--live-commands',type=Path,help='Локальный JSON одноразовых команд после AUTH/room; диагностический опыт')
    p.add_argument('--peer-avatar-config',type=Path,help='Аватары комнаты и выбранные позы; общий лут/HP отдельными флагами')
    p.add_argument('--appearance-dir',type=Path,help='Каталог appearance/<uid>.json от лобби-прокси; 0x553 AVATAR_SYNC для пиров')
    p.add_argument('--shared-evacuation',action='store_true',help='Учебные четыре общих места/итог комнаты; all_boarded интерпретация, требует shared-peer-health/evacuation-result-config/delay')
    p.add_argument('--shared-vehicles',action='store_true',help='Учебные общие места/driver ownership/transform102/109, одноместный203; требует shared-peer-health')
    p.add_argument('--peer-spectator',action='store_true',help='Учебное наблюдение живого участника после смерти; требует shared-peer-health')
    p.add_argument('--peer-avatar-trajectories',action='store_true',help='Диагностические плавные SC25; требует peer-avatar-config/scene-clock')
    p.add_argument('--shared-ground-loot',action='store_true',help='Общий наземный лут комнаты; без каталога и контейнеров; диагностический reset после последнего выхода')
    p.add_argument('--shared-world-clock',action='store_true',help='Общий календарь зон текущей комнаты; требует shared-ground-loot/world-config')
    p.add_argument('--shared-peer-health',action='store_true',help='Репликация HP участников через1069/533; требует peer-avatar-config/world-config/shared-ground-loot')
    p.add_argument('--actor-config',type=Path,help='Диагностическая мишень и патруль; требует weapon-config')
    p.add_argument('--parcel-config',type=Path,help='Экспериментальный аирдроп с содержимым 12ga; требует weapon-config')
    p.add_argument('--evacuation-delay-seconds',type=float,help='Одиночный опыт: отлёт через 2..60 секунд после посадки')
    p.add_argument('--evacuation-result-config',type=Path,help='Явный диагностический мост к газете с архивной статистикой; только после посадки')
    p.add_argument('--world-config',type=Path,help='Экспериментальный пресет зимних зон и опасного участка; требует weapon-config')
    p.add_argument('--zone-sets',type=Path,help='Ротация расписаний зон/площадок эвакуации по матчам; требует world-config')
    p.add_argument('--zone-state',type=Path,help='Файл курсора ротации наборов зон (между запусками)')
    p.add_argument('--environment-config',type=Path,help='Явные начальные час суток/температура зимней карты; требует weapon-config')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--shared-death-loot',action='store_true',help='Opt-in общий остаток пакетов погибших; требует shared-peer-health')
    p.add_argument('--peer-pellet-damage',type=int,choices=range(1,101),metavar='1..100',help='Opt-in учебный PvP комнаты; требует shared-peer-health и death_on_zero/hp_floor0')
    p.add_argument('--peer-equipment',action='store_true',help='Опциональная текущая экипировка удалённых игроков через547; требуетshared-peer-health')
    p.add_argument('--peer-actor-enter',action='store_true',help='Экспериментальный 0xbe8 BC_ACTOR_ENTER_MINI при заходе пира; wire-раскладка проверяется трейсером')
    p.add_argument('--shared-bots',type=int,choices=range(1,65),metavar='1..64',help='Общие боты-игроки комнаты: spawn/кит/FSM/лут смерти; требует peer-avatar/shared-peer-health/world-clock/death-loot и карту16010')
    p.add_argument('--bot-pellet-damage',type=int,choices=range(1,101),metavar='1..100',help='Учебный урон ботов по участнику; по умолчанию peer-pellet-damage или40')
    p.add_argument('--teleport-command',help='Диагностика: JSON {x,y,z,seq} → own-0x27 живым участникам (съёмка реальной поверхности)')
    args = p.parse_args()
    if args.peer_equipment and not args.shared_peer_health:
        p.error('peer-equipment требует shared-peer-health')
    if args.shared_vehicles and not args.shared_peer_health:
        p.error('shared-vehicles требует shared-peer-health')
    if args.shared_evacuation and (not args.shared_peer_health or not args.evacuation_result_config or args.evacuation_delay_seconds is None):
        p.error('shared-evacuation требует shared-peer-health/evacuation-result-config/evacuation-delay-seconds')
    if args.peer_spectator and not args.shared_peer_health:
        p.error('peer-spectator требует shared-peer-health')
    if args.peer_avatar_trajectories and (not args.peer_avatar_config or not args.scene_clock):
        p.error('peer-avatar-trajectories требует peer-avatar-config и scene-clock')
    if args.shared_bots and (not args.shared_peer_health or not args.shared_world_clock
            or not args.shared_death_loot or not args.peer_avatar_trajectories):
        p.error('shared-bots требует shared-peer-health/shared-world-clock/shared-death-loot/peer-avatar-trajectories')
    if args.shared_peer_health and (not args.peer_avatar_config or not args.world_config or not args.shared_ground_loot):
        p.error('shared-peer-health требует peer-avatar-config/world-config/shared-ground-loot')
    if args.shared_world_clock and (not args.shared_ground_loot or not args.world_config):
        p.error('shared-world-clock требует shared-ground-loot и world-config')
    if args.shared_ground_loot and (not args.weapon_config or args.loot_catalog or args.parcel_config):
        p.error('shared-ground-loot требует weapon-config без loot-catalog/parcel-config')
    if args.shared_loot_catalog and not args.shared_ground_loot:
        p.error('shared-loot-catalog требует shared-ground-loot')
    if ((args.ident and not args.handshake) or (args.grant and not args.ident)
            or (args.auth and not args.grant) or (args.room_template and not args.auth)
            or ((args.scene_rules or args.scene_clock or args.live_commands) and not args.room_template)):
        p.error('Нужна полная цепочка handshake → ident → grant → auth → room → scene-rules')
    if args.idle_handoff and not args.reliable_udp:
        p.error('idle-handoff требует reliable-udp')
    try:
        import ipaddress;ipaddress.IPv4Address(args.bind)
    except ValueError:
        p.error('--bind требует IPv4-адрес')
    if args.evacuation_delay_seconds is not None and not 2 <= args.evacuation_delay_seconds <= 60:
        p.error('evacuation-delay-seconds должен быть 2..60')
    if args.evacuation_result_config and (args.evacuation_delay_seconds is None or not args.room_template):
        p.error('evacuation-result-config требует room-template и evacuation-delay-seconds')
    if args.actor_config and not args.weapon_config:
        p.error('actor-config требует weapon-config')
    if args.breakable_config and (not args.weapon_config or not args.scene_clock):
        p.error('breakable-config требует weapon-config и scene-clock')
    if args.door_config and (not args.weapon_config or not args.scene_clock):
        p.error('door-config требует weapon-config и scene-clock')
    if args.loot_catalog and not args.weapon_config:
        p.error('loot-catalog требует weapon-config')
    if args.parcel_config and not args.weapon_config:
        p.error('parcel-config требует weapon-config')
    if args.environment_config and not args.weapon_config:
        p.error('environment-config требует weapon-config')
    if args.world_config and (not args.weapon_config or not args.scene_clock):
        p.error('world-config требует weapon-config и scene-clock')
    if args.selection_seconds is not None and (not args.scene_clock or not 1<=args.selection_seconds<=120):
        p.error('selection-seconds требует scene-clock и значение 1..120')
    if args.weapon_config and (not args.scene_rules or args.selection_seconds is None):
        p.error('weapon-config требует scene-rules и selection-seconds')
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
