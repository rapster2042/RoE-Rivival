"""Общий ROOM-service2022 для customs: список/создание/вход/старт между подключениями.

Чистая логика без asyncio: heartbeat_probe передаёт (conn,uid,code,body) и
исполняет возвращённые replies/broadcasts/joins. Не общий мир/лут/HP — только
лобби-комната и мост к JOIN_INSTANCE для всех участников.

Форматы ROOM_BASE_PROTO/ROOM_ACTOR_PROTO перенесены из исследования
tqq_probe_df1bf67 (decoder FUN_140c50c80, stride0xcc).

Модель присутствия — по нативному клиенту (docs/CUSTOM-ROOM-FLOW-20261016.md):
клиент держит room_id в памяти и шлёт heartbeat 0x7384 на любой conn, пока
room_id!=0 — в т.ч. во время матча. Поэтому membership ключуется по uid,
conn — перепривязываемый атрибут; disconnect сиденье сохраняет, удаление —
только по явному quit/kick/remove или offline-дедлайну.
"""
import struct
import time

ROOM_CODES = frozenset((0x736f, 0x7371, 0x7373, 0x7375, 0x7377, 0x7379, 0x737f, 0x7381, 0x7384, 0x7388, 0x6efb, 0xd78))
# Семейство комнат, которое записываем в журнал даже без обработчика.
ROOM_WATCH = frozenset(range(0x7360, 0x73a0)) - ROOM_CODES

MAX_MEMBERS = 8
MAX_ROOMS = 16
MAX_NAME = 0x80
# Сиденье без conn дольше этого времени удаляется (нативный heartbeat-timeout).
OFFLINE_GRACE = 120.0


def _tdr_str(buf, off=0):
    """sz-строка TDR: u32 len (включая NUL) + bytes."""
    if off + 4 > len(buf):
        raise ValueError('Усечённая строка room-структуры')
    (ln,) = struct.unpack_from('>I', buf, off)
    if not 1 <= ln <= 0x100 or off + 4 + ln > len(buf):
        raise ValueError('Неверная длина строки room-структуры')
    return buf[off + 4:off + 4 + ln - 1].decode('utf-8', 'replace'), off + 4 + ln


def _str(value, limit):
    raw = (value or '').encode('utf-8')[:limit - 1] + b'\x00'
    return struct.pack('>I', len(raw)) + raw


def parse_room_struct(buf, off=0):
    """u64 id + u64 owner + szName<=0x80 + szPass<=0x20 + 7*u32 settings."""
    room_id, owner = struct.unpack_from('>QQ', buf, off)
    name, off = _tdr_str(buf, off + 16)
    pwd, off = _tdr_str(buf, off)
    if off + 28 > len(buf):
        raise ValueError('Усечённые room settings')
    settings = struct.unpack_from('>7I', buf, off)
    return dict(id=room_id, owner=owner, name=name, pwd=pwd, settings=list(settings))


def room_struct(room_id, owner, name, settings):
    return struct.pack('>QQ', room_id, owner) + _str(name, MAX_NAME) + _str('', 0x20) + struct.pack('>7I', *settings)


def member_struct(uid, name, pos_type=1, pos=0):
    """u64 actor + szName + u32 enter_time + u8 pos_type + u8 pos + u8 online + u32 state."""
    return struct.pack('>Q', uid) + _str(name, MAX_NAME) + struct.pack('>IBBBI', 1, pos_type, pos, 1, 0)


class RoomManager:
    """Комнаты, разделённые между всеми lobby-подключениями одного прокси."""

    def __init__(self, seed=True):
        self.rooms = {}
        self.by_conn = {}          # conn -> member dict (только online)
        self.by_uid = {}           # uid -> member dict (все сиденья)
        self.next_room_id = 1
        if seed:
            # Постоянная публичная комната: клиент открывает Create только при
            # count>=1 в LIST_RSP (флаг=1 без комнат недостаточен, проверено
            # в живом прогоне). Первый вошедший становится хостом.
            room = dict(id=self.next_room_id, owner=0, name='ROE Public', pwd='',
                        settings=[1, 3, 0, 16007, 60, 0, 1], state=1,
                        members={}, map_id=16010, seeded=True, started_at=0.0)
            self.rooms[room['id']] = room
            self.next_room_id += 1

    def _result(self, replies=(), broadcasts=None, joins=(), events=()):
        return dict(replies=list(replies), broadcasts=dict(broadcasts or {}),
                    joins=list(joins), events=list(events))

    def _member_name(self, nickname, uid):
        return f'{nickname}-{uid & 0xffffff:x}'[:MAX_NAME - 1]

    def _new_member(self, uid, conn, room_id, nickname):
        return dict(uid=uid, conn=conn, room_id=room_id,
                    name=self._member_name(nickname, uid), seat=(3, 0),
                    offline_at=0.0, hb_seat=(0, 0), needs_resync=False,
                    returned=False, joined_match=False)

    def _room_of(self, conn):
        member = self.by_conn.get(conn)
        if member is None:
            return None, None
        return member, self.rooms.get(member['room_id'])

    def _room_of_uid(self, uid):
        member = self.by_uid.get(uid)
        if member is None:
            return None, None
        return member, self.rooms.get(member['room_id'])

    def _attach(self, conn, uid):
        """Перепривязка сиденья при смене lobby-сокета.

        Клиент не покидает комнату при ротации сокета (тик heartbeat смотрит
        только на in-memory room_id), поэтому любое сообщение от известного
        uid с другой conn — это переподключение, а не новый участник.
        """
        member = self.by_uid.get(uid)
        if member is None:
            return None
        if member['conn'] is conn:
            member['offline_at'] = 0.0
            return member
        old = member['conn']
        if old is not None and self.by_conn.get(old) is member:
            self.by_conn.pop(old, None)
        if self.by_conn.get(conn) is not member:
            self.by_conn.pop(conn, None)
        member['conn'] = conn
        self.by_conn[conn] = member
        member['offline_at'] = 0.0
        member['needs_resync'] = True
        return member

    def _roster(self, room):
        return list(room['members'].values())

    def _enter_ntf(self, room):
        settings = list(room['settings'])
        settings[5] = len(room['members'])        # iCur_actor
        settings[6] = room['state']               # iState
        body = room_struct(room['id'], room['owner'], room['name'], settings)
        body += struct.pack('>I', len(room['members']))
        for member in self._roster(room):
            body += member_struct(member['uid'], member['name'], *member['seat'])
        return body

    def _push_room(self, conn, room, uid):
        """Полный слепок комнаты одному участнику (как push_room исследования)."""
        s = room['settings']
        packets = [
            (0x737b, self._enter_ntf(room)),
        ]
        # OTHER_ENTER_NTF 0x737c = один ROOM_ACTOR (dispatch: FUN_140c50890).
        # Клиент рисует чужие строки ростера только через 0x737c — свой
        # приходит в ENTER_NTF (дублировать его нельзя: покажет лишнюю строку).
        for member in self._roster(room):
            if member['uid'] == uid:
                continue
            packets.append((0x737c, member_struct(member['uid'], member['name'], *member['seat'])))
        for member in self._roster(room):
            packets.append((0x737e, struct.pack('>QIB', member['uid'], 0, 1)))
        # Рабочая версия слала 0x7383 на каждый push — позиционное состояние
        # участника нужно клиенту, чтобы нарисовать его строку ростера.
        for member in self._roster(room):
            packets.append((0x7383, struct.pack('>QBBBB', member['uid'], 0, 0,
                                                member['seat'][0], member['seat'][1])))
        packets += [
            (0x7387, bytes([1 if uid == room['owner'] else 0])),
            (0x7386, struct.pack('>QI', room['id'], room['state'])),
            (0x738a, struct.pack('>5I', s[0], s[1], s[2], s[3], 0)),
        ]
        return packets

    def _leave_ntf(self):
        """0x737b room_id=0 — нативный выход из комнаты.

        FUN_14130b310: payload+4 = u64 room_id; ноль безусловно очищает
        module+0xd0 и account+0xcc и обновляет список комнат. В отличие от
        self-ветки 0x737d (FUN_14130b5c0 сравнивает uid с account+0xd0 и
        молча игнорирует чужой/несовпавший uid) этот путь не зависит от
        идентификаторов — единственный гарантированный способ выгнать
        клиента из комнаты.
        """
        return (0x737b, room_struct(0, 0, '', [0] * 7) + struct.pack('>I', 0))

    def _resync_member(self, conn, room, uid):
        """Wipe+rebuild комнаты для вернувшегося участника.

        0x737c append в клиенте не дедуплируется (FUN_14130b4a0), поэтому
        перед слепком шлём leave+quit — модуль сбрасывается, затем ENTER_NTF
        строит ростер заново без дублей.
        """
        return [self._leave_ntf(),
                (0x737d, struct.pack('>QI', uid, 0))] + self._push_room(conn, room, uid)

    def _remove_member(self, room, uid, reason):
        """Удалить участника по uid; вернуть broadcasts/events оставшимся."""
        member = room['members'].pop(uid, None)
        if member is None:
            return {}, []
        if member['conn'] is not None:
            self.by_conn.pop(member['conn'], None)
        self.by_uid.pop(uid, None)
        events = [dict(action='room_member_left', room_id=room['id'], uid=hex(uid))]
        broadcasts = {}
        if not room['members']:
            if room.get('seeded'):
                room['owner'] = 0
                room['state'] = 1
            else:
                del self.rooms[room['id']]
                events.append(dict(action='room_closed', room_id=room['id']))
            return broadcasts, events
        quit_ntf = (0x737d, struct.pack('>QI', uid, reason))
        if room['owner'] == uid:
            room['owner'] = next(iter(room['members']))
            events.append(dict(action='room_host_migrated', room_id=room['id'], owner=hex(room['owner'])))
        for other in self._roster(room):
            if other['conn'] is None:
                continue
            packets = [quit_ntf, (0x737b, self._enter_ntf(room)),
                       (0x7387, bytes([1 if other['uid'] == room['owner'] else 0]))]
            broadcasts.setdefault(other['conn'], []).extend(packets)
        return broadcasts, events

    def _purge_offline(self):
        """Ленивая вычистка сидений, чей heartbeat молчит дольше OFFLINE_GRACE."""
        now = time.monotonic()
        broadcasts = {}
        events = []
        for room in list(self.rooms.values()):
            for member in list(room['members'].values()):
                if member['conn'] is None and member['offline_at'] \
                        and now - member['offline_at'] > OFFLINE_GRACE:
                    bc, ev = self._remove_member(room, member['uid'], reason=1)
                    for conn, packets in bc.items():
                        broadcasts.setdefault(conn, []).extend(packets)
                    events.extend(ev)
                    events.append(dict(action='room_offline_evicted',
                                       room_id=room['id'], uid=hex(member['uid'])))
        return broadcasts, events

    def handle(self, conn, uid, code, body, nickname='Player'):
        """Вернуть replies/вещание/joins для одного клиентского room-запроса."""
        purge_bc, purge_ev = self._purge_offline()
        attached = self._attach(conn, uid)
        result = self._dispatch(conn, uid, code, body, nickname, attached)
        if purge_bc:
            for target, packets in purge_bc.items():
                result['broadcasts'].setdefault(target, []).extend(packets)
        if purge_ev:
            result['events'].extend(purge_ev)
        return result

    def _dispatch(self, conn, uid, code, body, nickname, attached):
        if code == 0x736f:                        # LIST_REQ
            payload = struct.pack('>III', 0, 1, len(self.rooms))
            for room in self.rooms.values():
                settings = list(room['settings'])
                settings[5] = len(room['members'])
                settings[6] = room['state']
                payload += room_struct(room['id'], room['owner'], room['name'], settings)
            return self._result(replies=[(0x7370, payload)],
                                events=[dict(action='room_list', count=len(self.rooms))])

        if code == 0x7371:                        # CREATE_REQ
            try:
                request = parse_room_struct(body)
            except (ValueError, struct.error):
                request = dict(id=0, owner=0, name='ROOM', pwd='', settings=[0] * 7)
            if len(self.rooms) >= MAX_ROOMS:
                return self._result(replies=[(0x7372, struct.pack('>I', 1))],
                                    events=[dict(action='room_create_denied', reason='limit')])
            member, old_room = self._room_of_uid(uid)
            if member is not None:
                # Клиент ещё считает себя в комнате — нативный сервер отказал
                # бы; ресинкаем его обратно в его комнату.
                replies = [(0x7372, struct.pack('>I', 2))]
                if old_room is not None and member['needs_resync'] \
                        and old_room['state'] != 3:
                    replies += self._resync_member(conn, old_room, uid)
                    member['needs_resync'] = False
                return self._result(replies=replies,
                                    events=[dict(action='room_create_denied', reason='already_member')])
            room_id = self.next_room_id
            self.next_room_id += 1
            settings = list(request['settings'])
            settings[4] = min(settings[4] or 60, 60)   # iMax_actor cap из UI
            settings[5] = 1
            settings[6] = 1                          # state=wait
            member = self._new_member(uid, conn, room_id, nickname)
            room = dict(id=room_id, owner=uid, name=request['name'] or 'ROOM',
                        pwd=request['pwd'] or '', settings=settings, state=1,
                        members={uid: member}, map_id=settings[3], started_at=0.0)
            self.rooms[room_id] = room
            self.by_conn[conn] = member
            self.by_uid[uid] = member
            events = [dict(action='room_created', room_id=room_id, owner=hex(uid),
                           name=room['name'], map_id=settings[3], max_actor=settings[4])]
            return self._result(replies=[(0x7372, struct.pack('>I', 0))] + self._push_room(conn, room, uid),
                                events=events)

        if code == 0x7375:                        # JOIN_REQ: u64 rid + szPass
            (room_id,) = struct.unpack_from('>Q', body, 0)
            try:
                pwd, _ = _tdr_str(body, 8)
            except (ValueError, struct.error):
                pwd = ''
            room = self.rooms.get(room_id)
            events = [dict(action='room_join', room_id=room_id, uid=hex(uid))]
            member = self.by_uid.get(uid)
            if member is not None and member['room_id'] == room_id:
                # Повторный запрос/переподключение в ту же комнату: слепок.
                member['needs_resync'] = False
                return self._result(replies=[(0x7376, struct.pack('>I', 0))] + self._push_room(conn, room, uid),
                                    events=events)
            if room is None:
                events.append(dict(action='room_join_denied', reason='no_room'))
                return self._result(replies=[(0x7376, struct.pack('>I', 1))], events=events)
            if room['state'] != 1:
                events.append(dict(action='room_join_denied', reason='state'))
                return self._result(replies=[(0x7376, struct.pack('>I', 2))], events=events)
            if len(room['members']) >= min(room['settings'][4], MAX_MEMBERS):
                events.append(dict(action='room_join_denied', reason='full'))
                return self._result(replies=[(0x7376, struct.pack('>I', 3))], events=events)
            if room['pwd'] and pwd != room['pwd']:
                events.append(dict(action='room_join_denied', reason='password'))
                return self._result(replies=[(0x7376, struct.pack('>I', 4))], events=events)
            broadcasts = {}
            if member is not None:
                # uid занят другой комнатой: явный уход из неё.
                old_room = self.rooms.get(member['room_id'])
                if old_room is not None:
                    bc, ev = self._remove_member(old_room, uid, reason=0)
                    for target, packets in bc.items():
                        broadcasts.setdefault(target, []).extend(packets)
                    events.extend(ev)
            # chPos_type=3 FREE: клиент рассаживает участников сам через
            # CHANGE_POS_REQ->0x7383 (проверенный рабочий вариант df1bf67).
            member = self._new_member(uid, conn, room_id, nickname)
            room['members'][uid] = member
            self.by_conn[conn] = member
            self.by_uid[uid] = member
            if room['owner'] == 0:
                # Публичная комната без владельца: первый вошедший — хост.
                room['owner'] = uid
                events.append(dict(action='room_host_claimed', room_id=room_id, owner=hex(uid)))
            other_ntf = (0x737c, member_struct(uid, member['name'], *member['seat']))
            for other in self._roster(room):
                if other['conn'] is None or other['conn'] == conn:
                    continue
                packets = [other_ntf,
                           (0x737b, self._enter_ntf(room)),
                           (0x737e, struct.pack('>QIB', uid, 0, 1)),
                           (0x7383, struct.pack('>QBBBB', uid, 0, 0,
                                                member['seat'][0], member['seat'][1]))]
                broadcasts.setdefault(other['conn'], []).extend(packets)
            events.append(dict(action='room_member_added', room_id=room_id, uid=hex(uid),
                               members=len(room['members'])))
            return self._result(replies=[(0x7376, struct.pack('>I', 0))] + self._push_room(conn, room, uid),
                                broadcasts=broadcasts, events=events)

        if code == 0x7377:                        # QUIT_REQ: u64 roomId
            member = self.by_uid.get(uid)
            actor = member['uid'] if member is not None else uid
            broadcasts, events = ({}, [])
            if member is not None:
                room = self.rooms.get(member['room_id'])
                if room is not None:
                    broadcasts, events = self._remove_member(room, uid, reason=0)
            return self._result(replies=[(0x7378, struct.pack('>I', 0)),
                                         (0x737d, struct.pack('>QI', actor, 0)),
                                         self._leave_ntf()],
                                broadcasts=broadcasts, events=events)

        if code == 0x7373:                        # REMOVE_REQ: u64 roomId
            (room_id,) = struct.unpack_from('>Q', body, 0)
            room = self.rooms.get(room_id)
            member, _ = self._room_of_uid(uid)
            if room is None or member is None or member['room_id'] != room_id:
                return self._result(replies=[(0x7374, struct.pack('>I', 1))])
            broadcasts = {}
            if room['owner'] == uid:
                # Хост роспускает комнату: QUIT_NTF + leave каждому участнику.
                leave = self._leave_ntf()
                for m in self._roster(room):
                    if m['conn'] is None or m['conn'] == conn:
                        continue
                    broadcasts.setdefault(m['conn'], []).extend([
                        (0x737d, struct.pack('>QI', m['uid'], 0)), leave])
                    self.by_conn.pop(m['conn'], None)
                for m in list(room['members']):
                    self.by_uid.pop(m, None)
                room['members'].clear()
                self.by_conn.pop(conn, None)
                if room.get('seeded'):
                    room['owner'] = 0
                    room['state'] = 1
                else:
                    del self.rooms[room_id]
                return self._result(replies=[(0x7374, struct.pack('>I', 0)),
                                             (0x737d, struct.pack('>QI', uid, 0)),
                                             leave],
                                    broadcasts=broadcasts,
                                    events=[dict(action='room_removed', room_id=room_id)])
            broadcasts, events = self._remove_member(room, uid, reason=0)
            return self._result(replies=[(0x7374, struct.pack('>I', 0)),
                                         (0x737d, struct.pack('>QI', uid, 0)),
                                         self._leave_ntf()],
                                broadcasts=broadcasts, events=events)

        if code == 0x7379:                        # KICK_REQ: u64 roomId + u64 target
            # Live-2022 sender FUN_14130ad40 пишет {module+0xd0 (room_id),
            # param (target uid)} — 16 байт (run 20261017T175215: хост кликал
            # участника, wire=0000..0002 0005030000140001). 8-байтный
            # target-only вид оставляем как fallback для старых тестов.
            if len(body) >= 16:
                target = struct.unpack_from('>Q', body, 8)[0]
            elif len(body) >= 8:
                target = struct.unpack_from('>Q', body, 0)[0]
            else:
                target = 0
            member, room = self._room_of_uid(uid)
            kicked = None if room is None else room['members'].get(target)
            if room is None or member is None or room['owner'] != uid or kicked is None:
                return self._result(replies=[(0x737a, struct.pack('>I', 1))],
                                    events=[dict(action='room_kick_denied',
                                                 room_id=None if room is None else room['id'],
                                                 uid=hex(uid), target=hex(target))])
            kick_bc, _ = self._remove_member(room, target, reason=1)
            broadcasts = {}
            if kicked['conn'] is not None:
                # kick-ack + проверенная quit-clear тройка: result, QUIT_NTF
                # self и leave(0x737b{0}) — гарантированный сброс модуля.
                broadcasts[kicked['conn']] = [(0x737a, struct.pack('>I', 0)),
                                              (0x7378, struct.pack('>I', 3)),
                                              (0x737d, struct.pack('>QI', target, 1)),
                                              self._leave_ntf()]
            for conn, packets in kick_bc.items():
                broadcasts.setdefault(conn, []).extend(packets)
            return self._result(replies=[(0x737a, struct.pack('>I', 0))],
                                broadcasts=broadcasts,
                                events=[dict(action='room_kick', room_id=room['id'],
                                             target=hex(target))])

        if code == 0x737f:                        # START_REQ: u64 roomId
            (room_id,) = struct.unpack_from('>Q', body, 0)
            room = self.rooms.get(room_id)
            events = [dict(action='room_start', room_id=room_id, uid=hex(uid))]
            if (room is not None and room['owner'] == uid and room['state'] == 3):
                # Клиент не шлёт отдельный "матч окончен": хост может нажать
                # Start, только вернувшись в комнату — сам запрос и есть
                # сигнал возврата. Сбрасываем state и продолжаем старт.
                room['state'] = 1
                events.append(dict(action='room_match_cycle_reset', room_id=room_id))
            if room is None or room['owner'] != uid or room['state'] != 1:
                events.append(dict(action='room_start_denied',
                                   reason='room' if room is None else ('owner' if room['owner'] != uid else 'state')))
                return self._result(replies=[(0x7380, struct.pack('>QI', room_id, 1))], events=events)
            room['state'] = 3
            room['started_at'] = time.monotonic()
            for member in self._roster(room):
                member['needs_resync'] = False
                member['returned'] = False
                member['joined_match'] = False
            broadcasts = {}
            start_packet = [(0x47a, struct.pack('>I', 1)),
                            (0x7386, struct.pack('>QI', room_id, 2)),
                            (0x7386, struct.pack('>QI', room_id, 3))]
            joins = []
            for member in self._roster(room):
                if member['conn'] is None:
                    continue
                broadcasts.setdefault(member['conn'], []).extend(start_packet)
                joins.append(member['conn'])
            events.append(dict(action='room_started', room_id=room_id,
                               members=len(room['members']), map_id=room['map_id']))
            return self._result(replies=[(0x7380, struct.pack('>QI', room_id, 0))],
                                broadcasts=broadcasts, joins=joins, events=events)

        if code == 0x7381:                        # CHANGE_POS_REQ: 4*u8 src/dst
            member, room = self._room_of_uid(uid)
            if member is None or len(body) < 4:
                return self._result(replies=[(0x7382, struct.pack('>I', 1))])
            src, dst = (body[0], body[1]), (body[2], body[3])
            target = member
            if src != member['seat']:
                # src — чужое сиденье: это хост переставляет другого
                # участника по сетке (в UI тянет чужую иконку). Чужой ход
                # разрешён только владельцу комнаты.
                occupant = next((m for m in self._roster(room)
                                 if m['seat'] == src and m['uid'] != uid), None)
                if occupant is not None:
                    if room['owner'] != uid:
                        return self._result(
                            replies=[(0x7382, struct.pack('>I', 1))],
                            events=[dict(action='room_seat_denied',
                                         room_id=room['id'], uid=hex(uid),
                                         target=hex(occupant['uid']))])
                    target = occupant
            target['seat'] = dst
            ntfs = [(0x7383, struct.pack('>QBBBB', target['uid'], *body[:4]))]
            displaced = next((m for m in self._roster(room)
                              if m['seat'] == dst and m is not target), None)
            if displaced is not None:
                # Освобождаем занятое сиденье: вытесненный встаёт на src —
                # иначе двое покажутся в одной ячейке сетки.
                displaced['seat'] = src
                ntfs.append((0x7383, struct.pack('>QBBBB', displaced['uid'],
                                                 dst[0], dst[1], src[0], src[1])))
            # Движителю НЕ шлём полный слепок 0x737b: live-проверка (run
            # 20261011T205324, старый код без refresh) показала, что self-
            # 0x7383 само по себе корректно переставляет иконку и heartbeat
            # отражает новое сиденье, а после 0x737b клиент читает своё
            # сиденье транспонированно (pos,pos_type) и UI показывает
            # непосаженным. 0x737b уходит только пассивной цели host-drag —
            # её клиент не предсказывал ход и нужен перестройка ростера.
            refresh = [(0x737b, self._enter_ntf(room))]
            broadcasts = {}
            for m in self._roster(room):
                if m['conn'] is not None and m['conn'] != conn:
                    broadcasts.setdefault(m['conn'], []).extend(ntfs)
            if target is not member and target['conn'] is not None \
                    and target['conn'] != conn:
                broadcasts.setdefault(target['conn'], []).extend(refresh)
            return self._result(replies=[(0x7382, struct.pack('>I', 0))] + ntfs,
                                broadcasts=broadcasts,
                                events=[dict(action='room_seat', room_id=room['id'],
                                             uid=hex(target['uid']), by=hex(uid),
                                             seat=list(body[:4]))])

        if code == 0x7384:                        # HEART_BEAT_REQ {u64 rid, u16 seat}
            hb_room_id = struct.unpack_from('>Q', body, 0)[0] if len(body) >= 8 else 0
            member, room = self._room_of_uid(uid)
            if member is None or room is None or hb_room_id != room['id']:
                # Сервер не знает такого сиденья/комнаты — клиент бьётся в
                # фантом: гасим его модуль leave+quit, иначе мёртвый UI.
                replies = [(0x7385, struct.pack('>I', 0))]
                if hb_room_id:
                    replies += [self._leave_ntf(),
                                (0x737d, struct.pack('>QI', uid, 0))]
                return self._result(replies=replies,
                                    events=[dict(action='room_heartbeat_orphan',
                                                 room_id=hb_room_id, uid=hex(uid))])
            # Хвост heartbeat = эхо pos_type/pos члена (member+0x90/+0x91);
            # сцена/прогресс в нём не кодируется — для диагностики.
            if len(body) >= 10:
                member['hb_seat'] = (body[8], body[9])
            replies = [(0x7385, struct.pack('>I', 0))]
            events = []
            joins = []
            if member['needs_resync']:
                if room['state'] == 3:
                    # Клиент в матче или разгружается в лобби: wipe+rebuild
                    # посреди смены сцены ломает клиентский приёмник
                    # room-сообщений (run 20261017T170956: ротация lobby-сокета
                    # до 0xd78{04} → resync-бурст → клиент перестал обрабатывать
                    # входящие 0x737d/0x737b/0x7383 при живом исходящем
                    # heartbeat — «rejoin»-тупик). Флаг держим: wipe отработает
                    # на первом heartbeat уже в state==1, а match_finished
                    # _push_room по 0xd78 и так даёт полный слепок.
                    if not member['joined_match']:
                        # Участник отвалился в момент старта и не дошёл до
                        # матча: поздний join через тот же D77-мост. Уже
                        # завершившему d75/d76 повторный D77 слать нельзя —
                        # клиент защёлкивает «join in progress» и следующий
                        # D77 теряет (run 20261016T134205: c2 молчал на 0xd75).
                        joins.append(conn)
                else:
                    # Перепривязка после ротации сокета: wipe + полный слепок
                    # (самовосстановление tqq_probe: push_pending на первый
                    # heartbeat после re-auth).
                    member['needs_resync'] = False
                    replies += self._resync_member(conn, room, uid)
                events.append(dict(action='room_member_kept', room_id=room['id'],
                                   uid=hex(uid), rebound=True,
                                   deferred=room['state'] == 3))
            return self._result(replies=replies, joins=joins, events=events)

        if code == 0x7388:                        # INFO_CHANGE_REQ: szPass + 5*u32
            # vals = {iPass_changed, iZone_id, iTeam_type, iPerspective, iMap_id}
            # NTF 0x738a = {iZone_id, iTeam_type, iPerspective, iMap_id, iPass}
            member, room = self._room_of_uid(uid)
            if member is None or len(body) < 4:
                return self._result(replies=[(0x7389, struct.pack('>I', 1))])
            try:
                _, off = _tdr_str(body, 0)
                values = struct.unpack_from('>5I', body, off)
            except (ValueError, struct.error):
                return self._result(replies=[(0x7389, struct.pack('>I', 2))])
            room['settings'][:4] = list(values[1:5])   # zone/team/persp/map
            room['map_id'] = values[4]
            ntf = (0x738a, struct.pack('>5I', values[1], values[2], values[3], values[4], 0))
            broadcasts = {m['conn']: [ntf, (0x737b, self._enter_ntf(room))]
                          for m in self._roster(room)
                          if m['conn'] is not None and m['conn'] != conn}
            return self._result(replies=[(0x7389, struct.pack('>I', 0)), ntf],
                                broadcasts=broadcasts,
                                events=[dict(action='room_info_change', room_id=room['id'],
                                             map_id=values[4])])

        if code == 0xd78:                         # SCENE_STATE: u8
            # Нативный отчёт о сцене (sender FUN_140d6eea2): 0x18=логин,
            # 0x05=вход в лобби, 0x04=выгрузка из матча обратно в лобби —
            # оба клиента шлют 04 детерминированно после конца матча
            # (run 20261016T132812: 13:35:04/13:35:06). Когда все участники
            # вернулись — воспроизводим состояние "свежая комната, все
            # расселись, state=wait": полный _push_room каждому, как при
            # первичном входе. Offline-участник не блокирует: его клиент
            # не может отчитаться, а при rebind ресинк даст текущий state.
            member, room = self._room_of_uid(uid)
            if member is None or room is None or room['state'] != 3 \
                    or not body or body[0] != 4:
                return self._result()
            member['returned'] = True
            events = [dict(action='room_member_returned', room_id=room['id'],
                           uid=hex(uid))]
            if not all(m['returned'] or m['conn'] is None
                       for m in self._roster(room)):
                return self._result(events=events)
            room['state'] = 1
            broadcasts = {}
            for m in self._roster(room):
                m['returned'] = False
                if m['conn'] is not None:
                    # _push_room и есть полный ресинк — отложенный
                    # needs_resync отрабатывать не нужно. Иначе wipe+rebuild
                    # прилетает через ~4с после reopen и ломает входящий
                    # room-путь клиента ровно как при ротации в state==3
                    # (run 20261017T175215: resync в :34.8 после finished в
                    # :31.0 → c2 не видел свои 0x7383 и проигнорировал join).
                    m['needs_resync'] = False
                    broadcasts.setdefault(m['conn'], []).extend(
                        self._push_room(m['conn'], room, m['uid']))
            events.append(dict(action='room_match_finished', room_id=room['id']))
            return self._result(broadcasts=broadcasts, events=events)

        if code == 0x6efb:                        # WORLD_START_REQ
            return self._result(replies=[(0x6efc, struct.pack('>I', 0))],
                                events=[dict(action='room_world_start', body=body[:64].hex())])

        return self._result()

    def join_completed(self, uid):
        """Прокси отдал 0xd76 — участник завершил JOIN_INSTANCE рукопожатие."""
        member = self.by_uid.get(uid)
        if member is not None:
            member['joined_match'] = True

    def leave(self, conn):
        """Отключение lobby-сокета: сиденье сохраняется (нативная модель).

        Клиент держит room_id в памяти и продолжит heartbeat на новой conn;
        evict только по явному quit/kick/remove или _purge_offline дедлайну.
        """
        member = self.by_conn.pop(conn, None)
        if member is None:
            return self._result()
        if member['conn'] is conn:
            member['conn'] = None
            member['offline_at'] = time.monotonic()
            return self._result(events=[dict(action='room_member_offline',
                                             room_id=member['room_id'],
                                             uid=hex(member['uid']))])
        return self._result()
