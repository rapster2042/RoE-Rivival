"""Опциональные аватары подключений; диагностические HP и выбранные позы2022.

Account UID заголовка отделён от назначенного Unit UID отображаемой сущности.
Выбор персонажа подхватывается из appearance/<account>.json (LocalCosmetics);
без файла — профиль фикстуры.
"""
import json
import math
import os
import struct
from pathlib import Path
from world_probe_2022 import encode_health
from peer_motion_2022 import (encode_peer_trajectory, encode_peer_stop,
                             encode_peer_turn, encode_peer_action)
from peer_equipment_2022 import encode_peer_equipment


def encode_peer_ik_sync(unit, payload, tick, tick_offset_ms=0):
    if (type(unit) is not int or not 0 < unit < 2**64 or len(payload) != 12
            or type(tick) is not int or not 0 <= tick <= 0xffffffff
            or type(tick_offset_ms) is not int or not 0 <= tick_offset_ms < 40):
        return None
    angles = struct.unpack('>3f', payload)
    if not all(math.isfinite(value) for value in angles):
        return None
    return 0x11d6, struct.pack('>Q', unit) + payload + struct.pack('>II', tick, tick_offset_ms)


# Remote aim rides 0x2b STATUS_ENTER {rid:u64, status:u32} — the original
# full-match capture shows SCOPE_AIMING(40)/STOP_AIM(41) dominating the
# 0x204 peer stream; EuStatusTable block 03079 names the same ids.
AIM_STATUS_ON = 40
AIM_STATUS_OFF = 41
# Delay between the status change and the pose: the pose in the SAME client
# frame as 0x2b 40 never renders (runs 20261023T214132 / 215347), and the pose
# on its own renders the raise but no aim state. ~3 frames at 25 Hz.
AIM_POSE_DELAY = 0.12
# Every peer in the original enters AIMING(21) at spawn and holds it until
# the first scope transition (median dwell 598 s, n=22). Without it a remote
# avatar never leaves the default status, and the AIM group has no base state.
AIM_STATUS_DEFAULT = 21
# Decomp-proven action channel: 0x27 record byte22=kind selects the branch in
# 141184F80 on the actor's remote flag (actor+0x310+0x68 & 1).
#   * remote actor (flag set): kind 0x12 is a NO-OP; only 0x10 (and 3/0xa/
#     0xc/0xd) reach the transform+FSM path. Kind 0x10 -> 141185AC0 ->
#     141185EE0(record+0x1b) -> 140E88150 action dispatch.
#   * local actor (flag clear): 141185360 handles kind 0x12 as a status-only
#     dispatch at the actor's current position.
# Action 20/AIM (0x14) sets bit 2 of actor+0x7c0, 21/DIS_AIM (0x15) clears it;
# 68/SCOPE_AIM (0x44) sets actor+0x7cc, 69/DIS_AIM (0x45) clears it.
# EuActionTable block 02011 names all four.
AIM_ACTION_KIND = 0x12
AIM_ACTION_KIND_REMOTE = 0x10
AIM_ACTION_ON = 0x14
AIM_ACTION_OFF = 0x15
AIM_ACTION_SCOPE_ON = 0x44
AIM_ACTION_SCOPE_OFF = 0x45
# 140E7D4E0 delta-gates the C2S 0x11d5 IK stream, so silence also occurs while
# the shooter holds still: the c1 tracer shows ~0.13-0.35 s samples broken by
# 0.6-2.6 s gaps inside a single hold. The status/action pair shares this one
# timeout (the aim transition stays latched across those gaps).
AIM_IDLE_TIMEOUT = 3.0
# 2026-10-23 attempt, REVERTED. Remote aim used to reuse the confirmed-visible
# SHOT bundle (0x11d8+0x11d4, the only remote pose the user ever saw) carrying
# the shooter's look ray, re-emitted every poll to hold the pose. That premise
# was wrong: the client streams C2S 0x11d5 continuously (it is the shoot-IK
# stream, not a hold edge), so the bundle fired on every sample and the remote
# avatar played non-stop gunshots in any position (~955 bundles in ~4 min,
# live run 20261023T191945-f276eb). Remote aim/facing is carried by the
# original 0x11d6 relay (`ik_sync`); real shots relay their own bundle. The
# `encode_peer_aim_ray` / `aim_ray` helpers are kept as the documented encoder
# of that bundle shape (tests pin the geometry) and the `peer_point` live probe
# decomposes the bundle into its three parts (ik/motion/fire) for a one-match
# search of a pose that points without firing.
AIM_SIGHT_CM = 12000
AIM_RAY_STATE = (0, 3)


def encode_peer_ammunition(unit, weapon, counter=0):
    """0x4a0 AMMUNITION_SYNC {unit:u64, slot:u32, clip:u32, counter:u32}.

    The original server sends this for peer actors too: 602 of 730 records in
    the full-match capture carry 0x204 player rids, not just the owner echo.
    Without it a remote weapon has no magazine state, so entering
    SCOPE_AIMING(40) degrades into the LOAD_BULLET animation.
    """
    if type(unit) is not int or not 0 < unit < 2**64:
        return None
    slot = getattr(weapon, 'hand_slot', None)
    guns = getattr(weapon, 'guns', None)
    if not isinstance(guns, dict):
        return None
    if slot not in guns:
        # В руках предмет/пусто — отдаём магазин выбранного оружия.
        slot = getattr(weapon, 'selected_slot', None)
    gun = guns.get(slot)
    if gun is None or type(getattr(gun, 'clip', None)) is not int:
        return None
    return 0x4a0, struct.pack('>QIII', unit, slot, gun.clip, counter & 0xffffffff)


def encode_peer_fire_sync(unit, payload, tick=None, tick_offset_ms=0):
    if type(unit) is not int or not 0 < unit < 2**64 or len(payload) != 49:
        return []
    values = struct.unpack('>6fIBIIfII', payload)
    mode = values[7]
    if tick is None:
        tick, tick_offset_ms = values[8:10]
    if not 0 <= tick_offset_ms < 40:
        return []
    if (mode not in (0, 1) or not all(math.isfinite(value) for value in values[:6] + (values[10],))):
        return []
    state = 0 if mode == 0 else 3
    body = struct.pack('>Q6fBII', unit, *values[:6], mode, tick, tick_offset_ms)
    # MOTION_SYNC: state + uid + tick + millisecond offset = 17 bytes.
    # The shot counter remains server-side for hit correlation/deduplication.
    motion = struct.pack('>BQII', state, unit, tick & 0xffffffff, tick_offset_ms)
    return [(0x11d8, motion), (0x11d4, body)]


def encode_peer_aim_ray(unit, origin, target, mode, tick, tick_offset_ms=0):
    """0x11d8 + 0x11d4 from an explicit look ray (scene cm).

    Same 41-byte body as a relayed player shot (`>Q6fBII`), so the remote
    client orients the peer weapon exactly like a confirmed-visible shot.
    mode 0 = open the point pose, mode 1 = close it.

    Any byte value is allowed because the receiving client branches on it: in
    its remote-motion apply (FUN_141184F80) modes 0..2 only stamp the fire
    counters, while mode 4 and mode 0x52 call FUN_140D908F0 (raise the weapon)
    and 0x52 additionally sets actor+0x7c0 bit 1, the flag the per-frame anim
    pass FUN_140E7CF40 reads to keep turning the upper body at the target. The
    0x11d8 state byte stays 0 for every mode but 1 so the pose is opened, not
    closed.
    """
    if (type(unit) is not int or not 0 < unit < 2**64
            or type(tick) is not int or not 0 <= tick <= 0xffffffff
            or type(tick_offset_ms) is not int or not 0 <= tick_offset_ms < 40
            or type(mode) is not int or not 0 <= mode <= 255):
        return []
    values = tuple(float(v) for v in (*origin, *target))
    if not all(math.isfinite(value) for value in values):
        return []
    state = AIM_RAY_STATE[mode] if mode < len(AIM_RAY_STATE) else AIM_RAY_STATE[0]
    body = struct.pack('>Q6fBII', unit, *values, mode, tick, tick_offset_ms)
    motion = struct.pack('>BQII', state, unit, tick & 0xffffffff, tick_offset_ms)
    return [(0x11d8, motion), (0x11d4, body)]


class PeerAvatarRooms:
    def __init__(self,fixture,replicate_health=False,replicate_trajectories=False,replicate_equipment=False,
                 appearance_dir=None,actor_enter_probe=False,avatar_base=None):
        self.fixture = fixture
        self.replicate_health = replicate_health
        self.replicate_trajectories = replicate_trajectories
        self.replicate_equipment = replicate_equipment
        self.actor_enter_probe = actor_enter_probe
        self.members = {}
        # Avatar unit ids are 0x20400001xxxxx; the shared bots live in
        # 0x20800001xxxxx, a different actor class on the client. The base is
        # overridable so a diagnostic run can admit a peer in the bots' class
        # and see whether an aim posture exists for that class at all.
        default_base = 0x2040000100001
        override = avatar_base if avatar_base is not None else os.environ.get('ROE_PEER_AVATAR_BASE')
        if override in (None, ''):
            self.next_avatar = default_base
        elif isinstance(override, int):
            self.next_avatar = override
        else:
            self.next_avatar = int(override, 0)
        self.pose_bootstrap = {}
        self.appearance_dir = Path(appearance_dir) if appearance_dir else None

    def actor_enter_mini(self,member):
        """0xbe8 BC_ACTOR_ENTER_MINI — оригинальный трафик несёт 21 байт
        {x,y,z:i32, u8, rid:u64}; обработчик регистрирует shell актёра."""
        # Wire layout confirmed in the original capture: {x,y,z:i32, u8, rid:u64}.
        x,y,z = member['position']
        return (0xbe8, struct.pack('>3iBQ', x, y, z, 0, member['avatar']))

    def room_live(self, room_uid):
        """Есть ли уже зачисленные участники комнаты (поздний вход не ждёт отбор)."""
        return any(key[0] == room_uid for key in self.members)

    def appearance(self,account):
        """5×u32 выбора персонажа + флаг; без файла — нулевой набор."""
        if self.appearance_dir is None:
            return None
        path = self.appearance_dir/f'{account:x}.json'
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        avatar = saved.get('avatar',[0]*5)
        if (len(avatar)!=5 or any(type(x) is not int or not 0<=x<=0xffffffff for x in avatar)):
            return None
        return avatar, bool(saved.get('avatar_applied'))

    def packets(self,member,tick):
        uid,position = member['avatar'],member['position']
        listing,profile = bytearray(self.fixture.listing),bytearray(self.fixture.profile)
        struct.pack_into('>3i',listing,1,*position)
        struct.pack_into('>Q',listing,14,uid)
        struct.pack_into('>Q',profile,8,uid)
        # Идент @16: u32 len+NUL-строка (в исходнике 13B "elpapu190704\0") —
        # переписываем под участника, иначе у всех пиров одинаковый ник.
        name = (member.get('name') or 'ROE').encode('utf-8')[:60] + b'\x00'
        profile = profile[:16] + struct.pack('>I', len(name)) + name + profile[16 + 4 + 13:]
        delta = len(name) - 13
        struct.pack_into('>3i',profile,1051+delta,*position)
        struct.pack_into('>I',profile,1064+delta,tick)
        packets = [(0x565,bytes(listing)),(0xbe7,bytes(profile))]
        packets.append(self.actor_enter_mini(member))
        avatar = self.appearance(member.get('account',0))
        if avatar is not None and any(avatar[0]):
            ids,applied = avatar
            packets.append((0x553,struct.pack('>Q5IB',uid,*ids,int(applied))))
        world = member['state'].get('world')
        if self.replicate_equipment and 'weapon' in member['state']:
            packets.append((0x547,encode_peer_equipment(member['state']['weapon'],uid,
                                                       bool(world and world.dead))))
            ammunition = (None if (world is not None and world.dead)
                          else encode_peer_ammunition(uid, member['state']['weapon']))
            if ammunition is not None:
                packets.append(ammunition)
        if member.get('look_direction') is not None:
            packets.append(encode_peer_turn(uid, member['look_direction'], tick))
        if world is None or not world.dead:
            # Original spawn order: equipment -> status 21 (AIMING) -> leave.
            # Scope transitions then replace it with 40/41.
            status = AIM_STATUS_ON if member.get('aiming') else AIM_STATUS_DEFAULT
            packets.append((0x2b, struct.pack('>QI', uid, status)))
            if member.get('aiming'):
                # Spawn/late join of an aiming peer needs the pitch too: the
                # 0x11d5 stream only pushes it on change.
                sync = self.ik_packet(member,tick)
                if sync is not None:
                    packets.append(sync)
        if self.replicate_health and world is not None:
            packets.append((0x1069, encode_health(uid, world.hp)))
            if world.dead:
                packets.append((0x533, struct.pack('>IQ', 0, uid)))
        return packets

    def equipment(self, now):
        """Изменения и handoff доставляются только другим живым транспортам комнаты."""
        outputs, events = [], []
        if not self.replicate_equipment:
            return outputs, events
        keys = set(self.members)
        for key, member in self.members.items():
            state = member['state']
            if 'weapon' not in state or state.get('match_finished') or state.get('transport_retired'):
                continue
            world = state.get('world')
            body = encode_peer_equipment(state['weapon'],member['avatar'],bool(world and world.dead))
            ammunition = encode_peer_ammunition(member['avatar'], state['weapon'])
            receipts = member.setdefault('equipment_targets', {})
            ammo_receipts = member.setdefault('ammunition_targets', {})
            for expired in set(receipts)-keys:
                del receipts[expired]
            for expired in set(ammo_receipts)-keys:
                del ammo_receipts[expired]
            recipients = 0
            for other_key,other in self.members.items():
                target = other['state'];anchor = target.get('peer_avatar_anchor')
                if (other_key[0] != key[0] or other_key == key or not anchor
                        or now-anchor[2] > 5 or not target.get('auth_sent')
                        or not target.get('room_sent') or target.get('match_finished')
                        or target.get('transport_retired')):
                    continue
                # Ammo changes must not re-send 0x547: a repeated equipment
                # re-attach is the prime suspect for the peer weapon reloading
                # on its own (reload -> 0x4a0 -> resend 0x547 -> reload again).
                receipt = (id(target),body)
                ammo_receipt = (id(target),ammunition)
                messages = []
                if receipts.get(other_key) != receipt:
                    messages.append((0x547,body))
                    receipts[other_key] = receipt
                if ammo_receipts.get(other_key) != ammo_receipt:
                    if ammunition is not None:
                        messages.append(ammunition)
                    ammo_receipts[other_key] = ammo_receipt
                if messages:
                    outputs.append((target,messages))
                    recipients += 1
            if recipients:
                events.append(dict(action='peer_avatar_equipment',account_uid=hex(key[1]),
                                   avatar_uid=hex(member['avatar']),hand_slot=state['weapon'].hand_slot,
                                   recipients=recipients))
        return outputs, events

    def health(self, state, now=None):
        """Серверные HP одного игрока: own AccountUID и remote UnitUID различны."""
        if not self.replicate_health or 'world' not in state:
            return [], []
        key = (state['room_uid'], state['actor_uid'])
        member = self.members.get(key)
        if member is None or member['state'] is not state:
            return [], []
        world = state['world']
        current = (world.hp, world.dead)
        changed = member.get('last_health') != current
        member['last_health'] = current
        uid = member['avatar']
        packets = [(0x1069, encode_health(uid, world.hp))]
        if world.dead:
            packets.append((0x533, struct.pack('>IQ', 0, uid)))
        receipts = member.setdefault('health_targets', {})
        outputs = []
        for other_key, other in self.members.items():
            if other_key[0] != key[0] or other_key == key:
                continue
            target = other['state']
            # Не считать пропущенный stale transport получателем обновления.
            # После обычного heartbeat, даже без RELAY, вернём текущие HP.
            if now is not None:
                anchor = target.get('peer_avatar_anchor')
                if (not anchor or now-anchor[2] > 5 or not target.get('auth_sent')
                        or not target.get('room_sent') or target.get('match_finished')
                        or target.get('transport_retired')):
                    continue
            receipt = (id(target), *current)
            if receipts.get(other_key) != receipt:
                outputs.append((target, packets))
                receipts[other_key] = receipt
        if not changed and not outputs:
            return [], []
        return outputs, [dict(action='peer_avatar_health', account_uid=hex(key[1]),
                              avatar_uid=hex(uid), hp=world.hp, dead=world.dead,
                              recipients=len(outputs))]

    def admit(self,state,tick,now=None):
        account,room = state['actor_uid'],state['room_uid']
        if not 0 < account < 2**64 or not 0 < room < 2**64:
            raise ValueError('Нужны account UID и room UID')
        key = (room,account)
        previous = self.members.get(key)
        if previous:
            if previous['state'] is state:
                return [],[]
            dead = (previous['state'].get('match_finished')
                    or previous['state'].get('transport_retired'))
            if not state.get('gameplay_resumed') and not dead:
                return [],[dict(action='peer_avatar_duplicate_account',account_uid=hex(account),room_uid=hex(room))]
            previous['state'] = state
            member = previous
        else:
            if sum(k[0]==room for k in self.members) >= 8:
                return [],[dict(action='peer_avatar_room_limit',room_uid=hex(room))]
            member = dict(avatar=self.next_avatar,state=state,account=account,
                          name=f'Player-{account & 0xffffff:x}',
                          position=tuple(state.get('last_movement_position',state['scene_position'])),last_emit=-10,pose=0)
            # Учебная задержка создания remote модели, не native ACK готовности.
            if now is not None:
                member['spectator_ready_at'] = now + 2
            self.next_avatar += 1
            self.members[key] = member
        outputs = []
        for other_key,other in self.members.items():
            if other_key[0] == room and other_key != key:
                outputs.append((other['state'],self.packets(member,tick)))
                outputs.append((state,self.packets(other,tick)))
                if now is not None:
                    # Два ограниченных повтора после async создания моделей.
                    self.pose_bootstrap[(key,other_key)] = (now+1, 2)
                    self.pose_bootstrap[(other_key,key)] = (now+1, 2)
        return outputs,[dict(action='peer_avatar_member',account_uid=hex(account),room_uid=hex(room),
                             avatar_uid=hex(member['avatar']),other_members=len(outputs)//2)]

    def next_sequence(self,member):
        """Advance the movement path ID. Look/IK use scene time, not this ID."""
        sequence = (member.get('motion_sequence',
            struct.unpack_from('>I',self.fixture.movement,23)[0])+1)&0xffffffff
        member['motion_sequence'] = sequence
        return sequence

    def movement_packet(self,member,tick,position=None,status=None,sequence=None):
        payload = bytearray(self.fixture.movement)
        # Restore the discrete locomotion path; aim actions ride the 0x27
        # record on the remote branch (kind 0x10).
        payload[22] = 3
        struct.pack_into('>Q3i',payload,0,member['avatar'],*(position or member['position']))
        struct.pack_into('>I',payload,27,member.get('pose',0) if status is None else status)
        sequence = member.get('motion_sequence') if sequence is None else sequence
        if sequence is not None:
            struct.pack_into('>I',payload,23,sequence)
        struct.pack_into('>I',payload,31,tick)
        return (0x27,bytes(payload))

    def pose_snapshots(self,now,tick):
        """Поздняя модель получает текущую позу, а не потерянный ранний27."""
        outputs,events = [],[]
        for pair,(due,remaining) in list(self.pose_bootstrap.items()):
            source,target = (self.members.get(key) for key in pair)
            if source is None or target is None:
                del self.pose_bootstrap[pair]
                continue
            if now < due:
                continue
            world = source['state'].get('world')
            if world is None or not world.dead:
                way = (None if (world is not None and world.dead)
                   else (AIM_STATUS_ON if source.get('aiming') else AIM_STATUS_DEFAULT))
            packets = [self.movement_packet(source,tick)]
            if way is not None:
                # Late joiner gets the current aim status, not only a pose.
                packets.append((0x2b, struct.pack('>QI', source['avatar'], way)))
                if source.get('aiming'):
                    # Bot recipe: replay magazine + pitch so the joiner sees a
                    # pointing weapon without waiting for a 0x11d5 change.
                    ammunition = self.ammunition_packet(source)
                    if ammunition is not None:
                        packets.insert(len(packets)-1,ammunition)
                    sync = self.ik_packet(source,tick)
                    if sync is not None:
                        packets.append(sync)
                if self.replicate_equipment and 'weapon' in source['state']:
                    packets.insert(0,(0x547,encode_peer_equipment(source['state']['weapon'],source['avatar'])))
                outputs.append((target['state'],packets))
                events.append(dict(action='peer_avatar_pose_snapshot',avatar_uid=hex(source['avatar']),
                                   pose=source.get('pose',0),recipient=hex(pair[1][1])))
            if remaining == 1:
                del self.pose_bootstrap[pair]
            else:
                self.pose_bootstrap[pair] = (now+2,remaining-1)
        return outputs,events

    def ammunition_packet(self,member):
        """0x4a0 with a fresh monotonic counter for this member.

        Shared bots proved the order that opens the remote pose: a magazine
        before SCOPE_AIMING(40), and a *new* counter each time so the client
        applies it. The spawn/equipment snapshot keeps counter 0, so the aim
        transition needs its own announcement.
        """
        state = member['state']
        world = state.get('world')
        if 'weapon' not in state or (world is not None and world.dead):
            return None
        counter = (member.get('ammo_counter',0)+1)&0xffffffff
        packet = encode_peer_ammunition(member['avatar'],state['weapon'],counter)
        if packet is not None:
            member['ammo_counter'] = counter
        return packet

    def ik_packet(self,member,tick):
        """Current aim pitch as 0x11d6 - the channel bots use to raise the gun.

        The client's own 0x11d5 stream is delta-gated, so a late joiner (or a
        peer who holds still) otherwise never receives a pitch at all.
        """
        pitch = member.get('aim_pitch')
        if pitch is None:
            return None
        return encode_peer_ik_sync(member['avatar'],
                                   struct.pack('>3f',float(pitch),0.0,0.0),
                                   tick&0xffffffff,0)

    def aim_action_packet(self,member,tick,action,kind=AIM_ACTION_KIND_REMOTE):
        """0x27 action toggle for a remote peer actor.

        Kind 0x10 is the remote-branch record (141184F80 -> 141185AC0 ->
        141185EE0 -> 140E88150); the default 0x12 only applies to a local
        actor, so the live probe passes kind explicitly.
        """
        return encode_peer_action(self.fixture.movement,member['avatar'],
                                  member['position'],action,tick,
                                  self.next_sequence(member),kind)

    def aim_ray(self,member,pitch=None):
        """Scene-space origin/target of the shooter's current look ray.

        Yaw comes from the relayed 0x2f look direction; the pitch prefers the
        0x11d5 SHoot_IK float (degrees, upward negative) and falls back to the
        look vector's own vertical component. The target is far away because
        only the direction matters to the remote weapon orientation.
        """
        position = member['position']
        direction = member.get('look_direction') or (1.0,0.0,0.0)
        x,y,z = (float(v) for v in direction)
        yaw = math.atan2(y,x)
        vertical = None
        if pitch is not None and -89.0 <= pitch <= 89.0:
            vertical = -math.sin(math.radians(float(pitch)))
        if vertical is None:
            flat = math.hypot(x,y)
            vertical = z/flat if flat > 1e-6 else 0.0
        vertical = max(-1.0,min(1.0,vertical))
        horizontal = math.sqrt(max(0.0,1.0-vertical*vertical))
        origin = (float(position[0]),float(position[1]),float(position[2])+150.0)
        target = (origin[0]+math.cos(yaw)*horizontal*AIM_SIGHT_CM,
                  origin[1]+math.sin(yaw)*horizontal*AIM_SIGHT_CM,
                  origin[2]+vertical*AIM_SIGHT_CM)
        return origin,target

    def motion_pose(self,member,tick,state):
        """0x11d8 MOTION_SYNC state 0/3 - the remote aim pose.

        Proven live between c1 and c2 (2026-10-23): `peer_point --part motion`
        (0x11d8 state 0 ALONE) raised the remote weapon and KEPT it raised;
        0x2b 40 + 0x27 action + 0x11d6 did not, and 0x11d4 fires.

        Delivery matters: the same record packed behind the status bundle never
        rendered (run 20261023T214132, transition frame at 21:46:20.871 =
        [0x4a0,0x2b,0x27,0x30,0x11d6,0x11d8]), while the probe sent it alone.
        It must also NOT be repeated during a hold: state 0 starts the raise
        animation, so re-sending it makes the remote avatar re-play the aim
        transition over and over (live 20261023T220818, 10 Hz re-assert).
        So it goes in its OWN frame, once per transition.
        """
        return 0x11d8, struct.pack('>BQII', state, member['avatar'],
                                   tick&0xffffffff,0)

    def aim_touch(self,state,now,tick,pitch=None):
        """Поток C2S 0x11d5: переход шлёт status 40 + action 0x14 пирам.

        Shot-bundle emission was removed 2026-10-23: 0x11d5 is a continuous
        shoot-IK stream (not a hold edge), so reusing the firing signal as the
        aim pose made the remote avatar play non-stop gunshots in any position.
        """
        key = (state.get('room_uid'),state.get('actor_uid'))
        member = self.members.get(key)
        if member is None or member['state'] is not state:
            return [],[]
        member['aim_until'] = now + AIM_IDLE_TIMEOUT
        if pitch is not None:
            member['aim_pitch'] = float(pitch)
        if member.get('aiming'):
            # Repeated 0x11d5 samples inside a hold must not duplicate the
            # status/action transition.
            return [],[]
        member['aiming'] = True
        member['aim_pose_sent'] = False
        # Stage 1 - the state change the original sends when a player scopes in:
        # a magazine (0x4a0, fresh counter) then SCOPE_AIMING (0x2b 40). The
        # documentation is explicit that entering SCOPE_AIMING with no magazine
        # degrades into LOAD_BULLET, so the magazine goes first.
        # The POSE is deliberately NOT in this frame: [0x4a0,0x2b,...,0x11d8]
        # in one frame never rendered (run 20261023T214132, 21:46:20.871), and
        # neither did the pose 0 ms after the bundle (run 20261023T215347,
        # 21:57:51.310). It goes out one delay later from `aim_pose_sync`.
        packets = []
        ammunition = self.ammunition_packet(member)
        if ammunition is not None:
            packets.append(ammunition)
        packets.append((0x2b,struct.pack('>QI',member['avatar'],AIM_STATUS_ON)))
        outputs = [(other['state'],list(packets)) for other_key,other in self.members.items()
                   if other_key[0]==key[0] and other_key!=key]
        if not outputs:
            return [],[]
        sent = len(outputs)
        member['pose_due_at'] = now + AIM_POSE_DELAY
        events = [dict(action='peer_avatar_aim',account_uid=hex(key[1]),
                       avatar_uid=hex(member['avatar']),aiming=True,
                       pose_deferred=True,recipients=sent)]
        return outputs,events

    def aim_pose_sync(self,now,tick):
        """Stage 2: the single 0x11d8 state 0, one delay after SCOPE_AIMING.

        Live evidence (2026-10-23):
          * pose ALONE -> the raise plays once, then the actor is back to idle
            (no aim state) - user saw "no pose at all" in run 20261023T221520;
          * pose repeated at 10 Hz -> the raise animation re-plays (run
            20261023T220818);
          * pose in the same frame/tick as 0x2b 40 -> nothing at all.
        So the status goes first, the pose follows ~120 ms later, and it is sent
        ONCE per transition: state 0 starts the animation, it does not hold it.
        Called from the 0x11d5 branch (after the IK relay) and from the poll
        timer as a fallback for a tap shorter than one heartbeat.
        """
        outputs,events = [],[]
        for key,member in self.members.items():
            due = member.get('pose_due_at')
            if due is None or now < due or not member.get('aiming'):
                continue
            member.pop('pose_due_at',None)
            state = member['state']
            world = state.get('world')
            if (state.get('match_finished') or state.get('transport_retired')
                    or (world is not None and world.dead)):
                continue
            member['aim_pose_sent'] = True
            packets = [self.motion_pose(member,tick,0)]
            sent = 0
            for other_key,other in self.members.items():
                if other_key[0] == key[0] and other_key != key:
                    outputs.append((other['state'],list(packets)))
                    sent += 1
            if sent:
                events.append(dict(action='peer_avatar_aim_pose',account_uid=hex(key[1]),
                                   avatar_uid=hex(member['avatar']),pose=0,
                                   delay_ms=int(AIM_POSE_DELAY*1000),recipients=sent))
        return outputs,events

    def aim_sync(self,now,tick):
        """Тишина 0x11d5 дольше AIM_IDLE_TIMEOUT -> action 0x15 пирам."""
        outputs,events = [],[]
        for key,member in self.members.items():
            if not member.get('aiming') or now <= member.get('aim_until',0):
                continue
            member['aiming'] = False
            state = member['state']
            world = state.get('world')
            if state.get('match_finished') or state.get('transport_retired') or (world is not None and world.dead):
                continue
            # Release: close the pose (state 3) only if it was ever opened,
            # then STOP_AIM (41) as the original does.
            member.pop('pose_due_at',None)
            packets = []
            if member.pop('aim_pose_sent',False):
                packets.append(self.motion_pose(member,tick,3))
            packets.append((0x2b,struct.pack('>QI',member['avatar'],AIM_STATUS_OFF)))
            sent = 0
            for other_key,other in self.members.items():
                if other_key[0] == key[0] and other_key != key:
                    outputs.append((other['state'],list(packets)))
                    sent += 1
            if sent:
                events.append(dict(action='peer_avatar_aim',account_uid=hex(key[1]),
                                   avatar_uid=hex(member['avatar']),aiming=False,
                                   recipients=sent))
        return outputs,events


    def ammunition(self,state,payload):
        """Собственный 0x4a0 (выстрел/перезарядка) -> пиры с Unit UID стрелка.

        Оригинал рассылает ammo и для чужих актёров; переписываем только
        actor id, слот/магазин/счётчик остаются серверными.
        """
        key = (state.get('room_uid'),state.get('actor_uid'))
        member = self.members.get(key)
        if member is None or member['state'] is not state or len(payload) != 20:
            return [],[]
        world = state.get('world')
        if (state.get('match_finished') or state.get('transport_retired')
                or (world is not None and world.dead)):
            return [],[]
        body = struct.pack('>Q',member['avatar'])+payload[8:]
        outputs = [(other['state'],[(0x4a0,body)]) for other_key,other in self.members.items()
                   if other_key[0]==key[0] and other_key!=key]
        if not outputs:
            return [],[]
        slot,clip = struct.unpack_from('>II',payload,8)
        return outputs,[dict(action='peer_avatar_ammunition',account_uid=hex(key[1]),
                             avatar_uid=hex(member['avatar']),slot=slot,clip=clip,
                             recipients=len(outputs))]

    def live_probe(self,state,payload,tick=0):
        """Живая диагностика: произвольный 0x2b/0x4a0/0x27/0x11d6 пирам от моего аватара.

        Нужна, чтобы в одном матче определить, какой канал (status id,
        0x27 kind 0x10 action, IK pitch) retail-клиент рисует как прицел.
        """
        key = (state.get('room_uid'),state.get('actor_uid'))
        member = self.members.get(key)
        if member is None or member['state'] is not state:
            return [],[dict(action='peer_live_probe',accepted=False,
                            reason='peer_avatar_not_admitted')]
        avatar = member['avatar']
        messages = []
        if payload['action'] == 'peer_status':
            messages.append((0x2b,struct.pack('>QI',avatar,payload['status'])))
            if payload.get('status2') is not None:
                messages.append((0x2b,struct.pack('>QI',avatar,payload['status2'])))
        elif payload['action'] == 'peer_aim':
            # 0x27 action record for a remote peer avatar: kind 0x10 is the
            # remote branch (default); pass kind=0x12 to compare the local
            # branch. Action 20/AIM sets, 21/DIS_AIM clears actor+0x7c0 bit2;
            # 68/69 toggle the scope flag at actor+0x7cc.
            messages.append(self.aim_action_packet(member,tick,payload['aim'],
                                                   payload.get('kind',AIM_ACTION_KIND_REMOTE)))
        elif payload['action'] == 'peer_ik':
            sync = encode_peer_ik_sync(avatar,struct.pack('>3f',
                payload['pitch'],0.0,0.0),tick&0xffffffff,0)
            if sync is None:
                return [],[dict(action='peer_live_probe',accepted=False,
                                reason='invalid_ik_pitch')]
            messages.append(sync)
        elif payload['action'] == 'peer_recipe':
            # The bot aim transition, verbatim: magazine, SCOPE_AIMING status,
            # the 0x27 action on the remote branch, the quantized facing and the
            # IK pitch - all in one frame, exactly like SharedBots._aim_packets.
            # A release (aim 21) sends the status and the action only.
            aim = payload['aim']
            pitch = member.get('aim_pitch')
            if payload.get('pitch') is not None:
                pitch = float(payload['pitch'])
            if aim == 20:
                ammunition = self.ammunition_packet(member)
                if ammunition is not None:
                    messages.append(ammunition)
            messages.append((0x2b,struct.pack('>QI',avatar,
                                               AIM_STATUS_ON if aim == 20
                                               else AIM_STATUS_OFF)))
            messages.append(self.aim_action_packet(member,tick,aim,
                                                   AIM_ACTION_KIND_REMOTE))
            if aim == 20:
                direction = member.get('look_direction')
                if direction is not None:
                    messages.append(encode_peer_turn(avatar,direction,tick))
                if pitch is not None:
                    sync = encode_peer_ik_sync(avatar,struct.pack('>3f',pitch,0.0,0.0),
                                               tick&0xffffffff,0)
                    if sync is not None:
                        messages.append(sync)
        elif payload['action'] == 'peer_point':
            # The confirmed-visible "point" is a 3-message bundle along the
            # shooter's look ray: 0x11d6 IK aim, 0x11d8 MOTION_SYNC (state 0
            # start / 3 stop) and 0x11d4 SHOOT_FIRE_SYNC (the origin->target
            # ray). `part` decomposes it so one match can find a pose that
            # POINTS without FIRING:
            #   all (default) | ik | motion | fire | ik_motion
            # mode 0 opens the pose, mode 1 closes it.
            origin,target = self.aim_ray(member,payload.get('pitch'))
            mode = payload.get('mode',0)
            bundle = encode_peer_aim_ray(avatar,origin,target,mode,tick&0xffffffff)
            if not bundle:
                return [],[dict(action='peer_live_probe',accepted=False,
                                reason='invalid_point_ray')]
            motion,fire = bundle
            pitch = 0.0 if payload.get('pitch') is None else float(payload['pitch'])
            ik = encode_peer_ik_sync(avatar,struct.pack('>3f',pitch,0.0,0.0),
                                     tick&0xffffffff,0)
            selections = {'all':[motion,fire],
                          'ik':[ik] if ik is not None else [],
                          'motion':[motion],
                          'fire':[fire],
                          'ik_motion':([ik] if ik is not None else [])+[motion]}
            part = payload.get('part','all')
            if part not in selections or not selections[part]:
                return [],[dict(action='peer_live_probe',accepted=False,
                                reason='invalid_point_part')]
            messages.extend(selections[part])
        else:
            messages.append((0x4a0,struct.pack('>QIII',avatar,payload['slot'],
                                               payload['clip'],0)))
        outputs = [(other['state'],list(messages)) for other_key,other in self.members.items()
                   if other_key[0]==key[0] and other_key!=key]
        if not outputs:
            return [],[]
        event = dict(action='peer_live_probe',accepted=True,probe=payload['action'],
                     avatar_uid=hex(avatar),recipients=len(outputs),
                     codes=[hex(code) for code,_ in messages])
        for name in ('status','status2','slot','clip','aim','pitch','kind','mode','part'):
            if payload.get(name) is not None:
                event[name] = payload[name]
        return outputs,[event]

    def settle_motion(self,now,tick):
        """Завершить предсказанную очередь одним авторитетным неподвижным якорем.

        Это политика диагностического peer, а не найденный серверный алгоритм.
        Последний узел SC25 имеет prediction marker; без нового якоря скорость
        анимации может остаться ненулевой после прекращения C2S.
        """
        outputs,events = [],[]
        if not self.replicate_trajectories:
            return outputs,events
        for key,member in self.members.items():
            due = member.get('motion_settle_at')
            if due is None or now < due:
                continue
            member.pop('motion_settle_at',None)
            state = member['state']
            world = state.get('world')
            if (state.get('match_finished') or state.get('transport_retired')
                    or (world is not None and world.dead)):
                continue
            sequence = self.next_sequence(member)
            body = encode_peer_stop(member['avatar'],member['position'],sequence,tick,
                                    member.get('pose',0),member['motion_direction'])
            for other_key,other in self.members.items():
                if other_key[0] == key[0] and other_key != key:
                    outputs.append((other['state'],[(0x25,body)]))
            events.append(dict(action='peer_avatar_motion_settle',avatar_uid=hex(member['avatar']),
                               position=member['position'],motion_sequence=sequence))
        return outputs,events

    def move(self,state,position,tick,now,movement=None):
        if len(position)!=3 or any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>10000000 for v in position):
            raise ValueError('Неверная позиция peer avatar')
        key = (state['room_uid'],state['actor_uid'])
        member = self.members.get(key)
        if member is None or member['state'] is not state:
            return [],[]
        member['position'] = tuple(round(v) for v in position)
        packets,transitions,unknown = [],[],[]
        trajectory,trajectory_error = False,None
        # Это подтверждённое подмножество событий C2S2a, а не полная enum игры.
        # bytes[1]==0 означает отсутствие перехода; стоя имеет код1.
        mapping = {1:0,2:1,3:2,22:3,4:9}
        if movement is not None:
            for point in movement['points']:
                event = point['bytes'][1]
                if event in mapping:
                    pose = mapping[event]
                    if member.get('pose',0) != pose:
                        member['pose'] = pose
                        packets.append(self.movement_packet(member,tick,point['position']))
                        transitions.append(dict(client_event=event,server_status=pose))
                elif event:
                    unknown.append(event)
        if now-member['last_emit'] >= .2 or packets:
            member['last_emit'] = now
            final = None
            if self.replicate_trajectories and movement is not None and member.get('pose',0) in (0,1,2):
                sequence = (member.get('motion_sequence',struct.unpack_from('>I',self.fixture.movement,23)[0])+1)&0xffffffff
                try:
                    body = encode_peer_trajectory(member['avatar'],movement,sequence,tick)
                except (ValueError,KeyError,TypeError,OverflowError,struct.error) as error:
                    trajectory_error = str(error)
                else:
                    member['motion_sequence'] = sequence
                    member['motion_direction'] = movement['floats']
                    member['motion_settle_at'] = now+.35
                    final,trajectory = (0x25,body),True
            if final is None:
                member.pop('motion_settle_at',None)
                final = self.movement_packet(member,tick)
            if not packets or packets[-1] != final:
                packets.append(final)
        outputs = [(other['state'],packets) for other_key,other in self.members.items()
                   if other_key[0]==key[0] and other_key!=key] if packets else []
        events = [dict(action='peer_avatar_move',account_uid=hex(key[1]),avatar_uid=hex(member['avatar']),
                       position=member['position'],recipients=len(outputs),pose_replicated=True,
                       server_status=member.get('pose',0),transitions=transitions,unknown_pose_events=unknown,
                       trajectory_replicated=trajectory,trajectory_fallback=trajectory_error)] if outputs or unknown else []
        return outputs,events

    def avatar_uid(self,state):
        key = (state.get('room_uid'),state.get('actor_uid'))
        member = self.members.get(key)
        return member['avatar'] if member is not None and member['state'] is state else None

    def broadcast(self,state,messages,action):
        if not messages:
            return [],[]
        key = (state.get('room_uid'),state.get('actor_uid'))
        member = self.members.get(key)
        if member is None or member['state'] is not state:
            return [],[]
        outputs = [(other['state'],messages) for other_key,other in self.members.items()
                   if other_key[0]==key[0] and other_key!=key]
        events = [dict(action=f'peer_avatar_{action}',account_uid=hex(key[1]),
                       avatar_uid=hex(member['avatar']),recipients=len(outputs),
                       codes=[hex(code) for code,_ in messages],experimental=True)]
        return (outputs,events) if outputs else ([],[])

    def ik_sync(self,state,payload,tick,tick_offset_ms=0):
        """Relay all three IK fields unchanged with a bounded scene-time offset."""
        key = (state.get('room_uid'),state.get('actor_uid'))
        member = self.members.get(key)
        if member is None or member['state'] is not state or len(payload)!=12:
            return [],[]
        sync = encode_peer_ik_sync(member['avatar'],payload,tick,tick_offset_ms)
        if sync is None:
            return [],[]
        return self.broadcast(state,[sync],'ik_sync')

    def motion_sync(self,state,payload,tick,tick_offset_ms=0):
        """Relay the native MOTION_REQ byte independently of firing."""
        uid = self.avatar_uid(state)
        if uid is None or len(payload) != 1 or not 0 <= tick_offset_ms < 40:
            return [],[]
        packet = (0x11d8, struct.pack('>BQII', payload[0], uid,
                                      tick & 0xffffffff, tick_offset_ms))
        return self.broadcast(state,[packet],'motion_sync')

    def turn(self,state,payload,tick,tick_offset_ms=0):
        """Convert the C2S direction to the native quantized look vector."""
        if len(payload) != 12:
            return [],[]
        key = (state.get('room_uid'),state.get('actor_uid'))
        member = self.members.get(key)
        if member is None or member['state'] is not state:
            return [],[]
        direction = struct.unpack('>fff',payload)
        if not all(math.isfinite(v) for v in direction) or not any(direction):
            return [],[]
        yaw = math.atan2(direction[1],direction[0])
        packet = encode_peer_turn(member['avatar'],direction,tick,tick_offset_ms)
        member['look_direction'] = direction
        outputs = [(other['state'],[packet]) for other_key,other in self.members.items()
                   if other_key[0]==key[0] and other_key!=key]
        if not outputs:
            return [],[]
        return outputs,[dict(action='peer_avatar_turn',account_uid=hex(key[1]),
                             avatar_uid=hex(member['avatar']),recipients=len(outputs),
                             yaw=round(yaw,4),vertical=round(direction[2],4),
                             tick_offset_ms=tick_offset_ms)]

    def detach(self, state, reason):
        """SC583/8B удаляет native actor/cache2022; старый RELAY не удаляет новый."""
        key = (state.get('room_uid'), state.get('actor_uid'))
        member = self.members.get(key)
        if member is None or member['state'] is not state:
            return [], []
        del self.members[key]
        payload = struct.pack('>Q', member['avatar'])
        outputs = [(other['state'], [(0x583, payload)]) for other_key, other in self.members.items()
                   if other_key[0] == key[0]]
        return outputs, [dict(action='peer_avatar_leave', account_uid=hex(key[1]),
                              avatar_uid=hex(member['avatar']), reason=reason,
                              recipients=len(outputs))]
