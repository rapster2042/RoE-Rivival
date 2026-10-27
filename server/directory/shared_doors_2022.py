"""Общий реестр обратимых дверей комнаты2022.

Одиночный профиль обслуживает DoorState; здесь одно состояние на room_uid,
широковещание 6F55 всем участникам и видимый остаток по члену комнаты.
Членство/эпохи повторяют диагностику SharedLootRooms; исходная серверная
физика не восстановлена.
"""
import math
import struct

from door_state_2022 import DoorState, encode_status


class SharedDoorRooms:
    def __init__(self, fixture):
        self.fixture = fixture
        self.rooms = {}
        self.epochs = {}

    def room(self, state):
        uid = state['room_uid']
        if not 0 < uid < 2**64:
            raise ValueError('Нужен UID комнаты общих дверей')
        if uid not in self.epochs and len(self.epochs) >= 32:
            raise ValueError('Лимит диагностических UID комнат')
        epoch = self.epochs.setdefault(uid, 1)
        if state.get('shared_door_epoch', epoch) != epoch:
            raise ValueError('Истёкшая эпоха общих дверей')
        state['shared_door_epoch'] = epoch
        if uid not in self.rooms:
            if len(self.rooms) >= 32:
                raise ValueError('Лимит диагностических комнат')
            self.rooms[uid] = dict(
                doors=DoorState(self.fixture), members={}, visible={})
        return self.rooms[uid]

    def attach(self, state):
        room = self.room(state)
        account = state['actor_uid']
        old = room['members'].get(account)
        dead = (old is not None and old is not state
                and (old.get('match_finished') or old.get('transport_retired')))
        if old is not None and old is not state and not dead and not state.get('gameplay_resumed'):
            return False
        if dead:
            room['visible'].pop(account, None)
        if old is None and len(room['members']) >= 8:
            return False
        room['members'][account] = state
        room['visible'].setdefault(account, {})
        return True

    def handle(self, state, payload, position):
        """Проверка запроса участника; 6F55 себе и остальным членам комнаты."""
        if not self.attach(state):
            return [], dict(action='rejected', reason='shared_doors_duplicate_or_limit'), []
        room = self.room(state)
        doors = room['doors']
        responses, event = doors.handle(payload, position)
        outputs = []
        if event.get('action') == 'door_status' and event.get('changed'):
            identifier = event['entity_id']
            requested = event['state']
            for account, member in room['members'].items():
                room['visible'][account][identifier] = requested
                if member is not state:
                    outputs.append((member, list(responses)))
        return responses, event, outputs

    def snapshot(self, state, position):
        """Видимый остаток дверей в радиусе члена, только изменения."""
        if not self.attach(state):
            return []
        room = self.room(state)
        doors = room['doors']
        if not doors.valid_position(position):
            return []
        seen = room['visible'][state['actor_uid']]
        nearby = {identifier for identifier, (xyz, _) in self.fixture.doors.items()
                  if math.dist(position, xyz) <= self.fixture.visibility_radius_cm}
        for identifier in list(seen):
            if identifier not in nearby:
                del seen[identifier]
        messages = []
        for identifier in sorted(nearby):
            status = doors.status[identifier]
            if seen.get(identifier) != status:
                messages.append((0x6f55, encode_status(identifier, status)))
                seen[identifier] = status
                if len(messages) >= 16:
                    break
        return messages

    def resume(self, state):
        """Повторный вход в новый native world: сбросить видимость члена."""
        room = self.rooms.get(state.get('room_uid'))
        if room and room['members'].get(state.get('actor_uid')) is state:
            room['visible'][state['actor_uid']] = {}

    def detach(self, state):
        uid = state.get('room_uid')
        room = self.rooms.get(uid)
        if room is None or room['members'].get(state.get('actor_uid')) is not state:
            return False
        del room['members'][state['actor_uid']]
        room['visible'].pop(state['actor_uid'], None)
        if room['members']:
            return False
        del self.rooms[uid]
        self.epochs[uid] = self.epochs.get(uid, 1) + 1
        return True
