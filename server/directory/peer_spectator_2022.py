"""Учебный выбор наблюдаемого живого участника комнаты после смерти.

6FBB проверен в EXE2022; правила команд/режимов оригинального сервера неизвестны.
Account UID получателя и Unit UID наблюдаемой модели имеют разные роли.
"""
import struct
import math


class PeerSpectators:
    def __init__(self, diagnostics=False, delay=0.0):
        self.receipts = {}
        self.camera_ticks = {}
        self.skip_reported = {}
        self.diagnostics = diagnostics
        # Окно смерти: при delay>0 клиент держит замороженный prone заданное
        # число секунд, затем назначается цель. По умолчанию отрыв сразу —
        # владелец отменил frozen prone: пин мешал observer-камере.
        self.delay = delay
        # Ограниченный пробник формы 0x6fbc: selector/тики первых кадров каждой
        # разновидности — нужен для диагностики отброшенных селекторов.
        self._probe_shapes = {}

    def spectating(self, state):
        """Назначена ли мёртвому участнику живая цель наблюдения прямо сейчас."""
        key = (state.get('room_uid'), state.get('actor_uid'))
        receipt = self.receipts.get(key)
        return bool(receipt and receipt[0] == id(state) and receipt[1])

    @staticmethod
    def connected(state, now):
        anchor = state.get('peer_avatar_anchor')
        return bool(anchor and now - anchor[2] <= 5
                    and state.get('auth_sent') and state.get('room_sent')
                    and not state.get('match_finished')
                    and not state.get('transport_retired'))

    @staticmethod
    def gate_reason(state, now):
        anchor = state.get('peer_avatar_anchor')
        if not anchor:
            return 'no_anchor'
        if now - anchor[2] > 5:
            return 'anchor_stale'
        if not state.get('auth_sent'):
            return 'auth'
        if not state.get('room_sent'):
            return 'room'
        if state.get('match_finished'):
            return 'match_finished'
        if state.get('transport_retired'):
            return 'transport_retired'
        return 'unknown'

    def poll(self, rooms, now):
        outputs, events = [], []
        members = rooms.members
        for key in list(self.receipts):
            if key not in members:
                del self.receipts[key]
        for key in list(self.skip_reported):
            if key not in members:
                del self.skip_reported[key]
        for key, member in members.items():
            state = member['state']
            world = state.get('world')
            if world is not None and not world.dead:
                self.receipts.pop(key, None)
                self.skip_reported.pop(key, None)
                member.pop('dead_since', None)
            if world is not None and world.dead:
                member.setdefault('dead_since', now)
            if world is None or not world.dead or not self.connected(state, now):
                # Мёртвый участник, который не попал в выдачу: причина один раз
                # на смену сигнатуры, чтобы видеть живой сценарий в журнале.
                if world is not None and world.dead and self.diagnostics:
                    sig = ('member', self.gate_reason(state, now))
                    if self.skip_reported.get(key) != sig:
                        self.skip_reported[key] = sig
                        events.append(dict(action='peer_spectator_skip', reason=sig[1],
                                           account_uid=hex(key[1]), room_uid=hex(key[0])))
                continue
            if now - member['dead_since'] < self.delay:
                # Окно смерти: клиент держит замороженный prone, наблюдение
                # назначается по истечении задержки — камера отрывается позже.
                sig = ('member', 'observe_delay')
                if self.diagnostics and self.skip_reported.get(key) != sig:
                    self.skip_reported[key] = sig
                    events.append(dict(action='peer_spectator_skip', reason=sig[1],
                                       account_uid=hex(key[1]), room_uid=hex(key[0])))
                continue
            eligible = {other['avatar'] for other_key, other in members.items()
                        if other_key[0] == key[0] and other_key != key
                        and now >= other.get('spectator_ready_at', 0)
                        and self.connected(other['state'], now)
                        and other['state'].get('world') is not None
                        and not other['state']['world'].dead}
            previous = self.receipts.get(key)
            target = previous[1] if previous and previous[1] in eligible else min(eligible, default=0)
            receipt = (id(state), target)
            # Нулевой UID очищает цель, но native active остаётся 1.
            # Завершение матча — отдельное событие, не этот ответ.
            if previous == receipt or (previous is None and target == 0):
                if target == 0 and previous is None and self.diagnostics:
                    detail = []
                    for other_key, other in members.items():
                        if other_key[0] != key[0] or other_key == key:
                            continue
                        other_state = other['state']
                        other_world = other_state.get('world')
                        if now < other.get('spectator_ready_at', 0):
                            reason = 'not_ready'
                        elif not self.connected(other_state, now):
                            reason = 'not_connected:' + self.gate_reason(other_state, now)
                        elif other_world is None:
                            reason = 'world_missing'
                        elif other_world.dead:
                            reason = 'world_dead'
                        else:
                            reason = 'eligible'
                        detail.append(dict(account_uid=hex(other_key[1]), reason=reason))
                    sig = ('no_eligible', tuple((d['account_uid'], d['reason']) for d in detail))
                    if self.skip_reported.get(key) != sig:
                        self.skip_reported[key] = sig
                        events.append(dict(action='peer_spectator_no_eligible',
                                           account_uid=hex(key[1]), room_uid=hex(key[0]),
                                           candidates=detail))
                continue
            self.receipts[key] = receipt
            self.skip_reported.pop(key, None)
            if target:
                # Наблюдаемый клиент сам производит camera state через 6FBC.
                target_state = next(other['state'] for other_key,other in members.items()
                                    if other_key[0] == key[0] and other['avatar'] == target)
                outputs.append((target_state, [(0x6fbe,struct.pack('>Q',key[1]))]))
            outputs.append((state, [(0x6fbb, struct.pack('>Q', target))]))
            events.append(dict(action='peer_spectator_target',
                               account_uid=hex(key[1]), room_uid=hex(key[0]),
                               target_unit_uid=hex(target)))
        return outputs, events

    def relay_camera(self, rooms, state, payload, now):
        """Проверенный selector1: 61B LE внутри bounded BE byte-array.

        Ограничение опыта: оба внутренних UID равны собственному AccountUID.
        Другие камеры/селекторы и transport input events пока не пересылаются.
        """
        probe = None
        if self.diagnostics:
            shape = (len(payload), payload[4] if len(payload) > 4 else -1)
            seen = self._probe_shapes.get(shape, 0)
            if seen < 12:
                self._probe_shapes[shape] = seen + 1
                probe = dict(action='peer_camera_probe',
                             account_uid=hex(state.get('actor_uid', 0)),
                             length=len(payload),
                             selector=payload[4] if len(payload) > 4 else None)
                if len(payload) >= 29:
                    probe['count'] = struct.unpack_from('>I', payload)[0]
                    probe['tick_a'] = struct.unpack_from('<I', payload, 21)[0]
                    probe['tick_b'] = struct.unpack_from('<I', payload, 25)[0]
        if (len(payload) != 66 or struct.unpack_from('>I',payload)[0] != 62
                or payload[4] != 1):
            return [], ([probe] if probe else [])
        key = (state['room_uid'],state['actor_uid'])
        member = rooms.members.get(key)
        world = state.get('world')
        if (member is None or member['state'] is not state or world is None
                or world.dead or not self.connected(state,now)):
            return [], ([probe] if probe else [])
        if struct.unpack_from('<QQ',payload,5) != (key[1],key[1]):
            return [], []
        if not all(math.isfinite(value) for value in struct.unpack_from('<9f',payload,29)):
            return [], []
        tick = struct.unpack_from('<I',payload,21)[0]
        tick_key = (*key,id(state))
        previous = self.camera_ticks.get(tick_key)
        if previous is not None and not 0 < (tick-previous)&0xffffffff < 0x80000000:
            return [], []
        body = bytearray(payload)
        struct.pack_into('<QQ',body,5,member['avatar'],member['avatar'])
        outputs = []
        for observer_key, receipt in self.receipts.items():
            observer = rooms.members.get(observer_key)
            if observer is None or observer_key[0] != key[0] or receipt[1] != member['avatar']:
                continue
            target = observer['state']
            if (receipt[0] != id(target) or not self.connected(target,now)
                    or target.get('world') is None or not target['world'].dead):
                continue
            outputs.append((target,[(0x6fbd,bytes(body))]))
        if not outputs:
            return [], ([probe] if probe else [])
        self.camera_ticks[tick_key] = tick
        # Ограничить историю старыми транспортами этой небольшой комнаты.
        active = {(*member_key,id(value['state'])) for member_key,value in rooms.members.items()}
        self.camera_ticks = {k:v for k,v in self.camera_ticks.items() if k in active}
        return outputs, ([probe] if probe else [])
