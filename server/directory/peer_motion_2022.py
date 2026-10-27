"""SC25 для диагностического peer: decoder140C59870, consumer141194CA0.

Дельты узлов знаковые и отсчитываются от общего якоря. Это перенос
подтверждённых полей C2S2a; исходная серверная физика не восстановлена.
"""
import math
import struct


def encode_peer_turn(uid, direction, tick, tick_offset_ms=0):
    """BC_TURN_AROUND_SIMPLE carries scene time, not a path counter.

    140BAFC90 computes (tick * 40 + offset) / 1000. 141184B20 decodes yaw
    and normalized vertical direction from two u16s.
    """
    if not 0 <= tick_offset_ms < 40:
        raise ValueError('Scene tick offset must be in [0, 40) milliseconds')
    yaw, vertical = packed_direction(direction)
    return 0x30, struct.pack('>QHHII', uid, yaw, vertical,
                            tick & 0xffffffff, tick_offset_ms)


def encode_peer_action(template, uid, position, action, tick, path_id,
                       kind=0x12):
    """Apply an action at the remote actor's current position.

    141184F80 splits on the actor's remote flag (actor+0x310+0x68 & 1):
    * flag CLEAR (local)  -> 141185360: kind 0x12 dispatches the action with
      the actor's current position and leaves the trajectory queue intact.
    * flag SET (remote)   -> only kinds 0x10/3/0xa/0xc/0xd are handled; kind
      0x10 calls 141185AC0 whose 141185EE0(record+0x1b) -> 140E88150 action
      dispatch (bit2 +0x7c0 for 20/AIM, +0x7cc for 68/SCOPE_AIM). Any other
      kind, including 0x12, returns early writing only speed/timestamp, so a
      peer avatar never sees it.

    We therefore default to kind 0x12 (the previously shipped shape) and pass
    kind=0x10 for remote peer avatars.
    """
    body = bytearray(template)
    struct.pack_into('>Q3i', body, 0, uid, *map(round, position))
    body[22] = kind
    struct.pack_into('>III', body, 23, path_id & 0xffffffff,
                     action, tick & 0xffffffff)
    body[35] = 0
    return 0x27, bytes(body)


def target_ik_pitch(origin, target):
    """IK pitch is in degrees, upward negative (140E7CCD0/140E7D4E0)."""
    dx, dy, dz = (b-a for a, b in zip(origin, target))
    return -math.degrees(math.atan2(dz, math.hypot(dx, dy)))


# Движ-режимы C2S 0x2a (хвостовой byte_0), допущенные в диагностический SC25.
# 0..8/33 — verified upstream; 17/19/21 — stance-окна, засвеченные
# move_bytes_probe вживую (run 20261016T225303: tail_bytes 17/19/21 при
# отклонении кадров и slide). Прочие значения остаются fallback027.
MOVEMENT_MODES = (*range(9), 17, 19, 21, 33)


def packed_direction(direction):
    if len(direction) != 3 or any(not math.isfinite(v) for v in direction):
        raise ValueError('Неверное направление движения')
    length = math.sqrt(sum(v*v for v in direction))
    if length < 1e-6:
        return 32768, 32768
    x, y, z = (v/length for v in direction)
    return (round((math.atan2(y, x)/math.pi+1)*32767.5),
            round((max(-1, min(1, z))+1)*32767.5))


def encode_peer_trajectory(uid, movement, sequence, tick):
    """Обычное движение; неизвестные режимы/дальние узлы требуют fallback27.

    Позы передаются отдельным27: здесь event=0 не переключает FSM.
    Часы исходных точек сохраняются; клиент получает общий1f57/40мс.
    """
    points = movement['points']
    if not 1 <= len(points) <= 20:
        raise ValueError('Нужно от1 до20 точек движения')
    speed, flags = movement['word_1'], movement['bytes'][0]
    if type(speed) is not int or not 0 <= speed <= 2000 or flags not in MOVEMENT_MODES:
        raise ValueError('Режим движения вне проверенного подмножества')
    anchor = points[0]['position']
    entries = []
    previous = None
    previous_fraction = None
    for point in points:
        position, stamp, fraction = point['position'], point['value'], point['bytes'][2]
        if (len(position) != 3 or any(type(v) is not int or abs(v) > 10000000 for v in position)
                or type(stamp) is not int or not 0 <= stamp < 2**32
                or type(fraction) is not int or not 0 <= fraction < 40):
            raise ValueError('Неверная точка движения')
        delta_tick = ((stamp-tick+2**31) % 2**32)-2**31
        if not -125 <= delta_tick <= 25:
            raise ValueError('Точка движения вне окна общих часов')
        if previous is not None and (((stamp-previous) & 0xffffffff) > 125
                                     or (stamp == previous and fraction < previous_fraction)):
            raise ValueError('Точки движения не упорядочены')
        previous, previous_fraction = stamp, fraction
        delta = tuple(v-a for v, a in zip(position, anchor))
        if any(abs(v) > 3000 for v in delta):
            raise ValueError('Траектория выходит за диагностический предел')
        entries.append((delta, fraction, stamp))
    u, v = packed_direction(movement['floats'])
    payload = struct.pack('>QHiiiBBIB', uid, speed, *anchor, 0,
                          entries[0][1], entries[0][2], len(entries)-1)
    payload += b''.join(struct.pack('>hhhBBI', *delta, 0, fraction, stamp)
                        for delta, fraction, stamp in entries[1:])
    return payload + struct.pack('>BHHI', flags, u, v, sequence)


def encode_peer_stop(uid, position, sequence, tick, pose, direction):
    """Fast commit141194D5A: speed0/N1/event!=0/node.tick==base.tick+1.

    Native255: fast commit сбросил attr134; 256/257 подтвердили автоматическую
    остановку. Опыт обычного N0 осложнён ошибкой журнала и не окончателен.
    Поддержанные события соответствуют выбранным стоянию/приседу/лежанию.
    """
    events = {0:1, 1:2, 2:3}
    if pose not in events:
        raise ValueError('Поза вне проверенного подмножества остановки')
    movement = dict(points=[dict(position=position,bytes=(0,0,0),value=tick),
                            dict(position=position,bytes=(0,0,0),value=(tick+1)&0xffffffff)],
                    word_1=0,bytes=(0,0,0),floats=direction)
    payload = bytearray(encode_peer_trajectory(uid,movement,sequence,tick))
    payload[22] = events[pose]
    return bytes(payload)
