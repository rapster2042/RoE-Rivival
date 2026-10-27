"""Явные административные действия одиночного стенда2022, не оригинальная console."""
from copy import deepcopy
import struct

from world_probe_2022 import encode_health
from death_parcel_2022 import DeathParcelState


def validate_state_action(row):
    action = row.get('action')
    parameter = {'set_own_health':'hp','give_ammo':'quantity','damage_npc':'damage',
                 'peer_status':'status','peer_ammo':'slot','peer_aim':'aim',
                 'peer_ik':'pitch','peer_point':'mode',
                 'peer_recipe':'aim'}.get(action)
    if parameter is None:
        raise ValueError('Неизвестное state action')
    allowed = {'id','action','recipient_actor_uid',parameter}
    if action == 'give_ammo':allowed.add('definition')
    if action == 'peer_status':allowed.add('status2')
    if action == 'peer_ammo':allowed.update(('clip',))
    if action == 'peer_aim':allowed.add('kind')
    if action == 'peer_point':allowed.update(('pitch','part'))
    if action == 'peer_recipe':allowed.add('pitch')
    if set(row)-allowed:
        raise ValueError('State action не принимает wire payload/неизвестные поля')
    value = row.get(parameter)
    if action == 'peer_ik':
        # 0x11d6 first float: degrees, upward negative (target_ik_pitch).
        if type(value) not in (int,float) or not -90 <= value <= 90:
            raise ValueError('State action: неверный pitch')
        return dict(action=action,pitch=float(value))
    if action == 'peer_recipe':
        # The bot aim TRANSITION as one burst: 0x4a0 magazine, 0x2b status 40,
        # 0x27 action 20 kind 0x10, 0x30 turn, 0x11d6 IK. A peer has never been
        # sent this exact set in one frame; the aim pipeline only sends the
        # first two and relies on relayed 0x2f/0x11d5 for the rest.
        # aim 20 = AIM, 21 = DIS_AIM (release).
        if value not in (20,21):
            raise ValueError('State action: неверный aim')
        result = dict(action=action,aim=value)
        pitch = row.get('pitch')
        if pitch is not None:
            if type(pitch) not in (int,float) or not -90 <= pitch <= 90:
                raise ValueError('peer_recipe: неверный pitch')
            result['pitch'] = float(pitch)
        return result
    if action == 'peer_point':
        # The confirmed-visible "point" is a 3-message bundle (0x11d6 IK aim,
        # 0x11d8 MOTION_SYNC state, 0x11d4 SHOOT_FIRE_SYNC ray). `part`
        # decomposes it so one match can find a pose that points WITHOUT
        # firing. mode 0 opens the pose, 1 closes it. Optional pitch in
        # degrees (upward negative), like peer_ik.
        #
        # The whole mode byte is reachable on purpose: the client's own
        # remote-motion apply (FUN_141184F80) does nothing for modes 0..2 but
        # raises the weapon for mode 4 and for mode 0x52, which also sets
        # actor+0x7c0 bit 1 - the bit the per-frame anim pass reads to keep
        # turning the upper body toward the target (FUN_140E7CF40 ->
        # FUN_140E7CCD0). So 4 and 82 are the two values worth probing.
        if type(value) is not int or not 0 <= value <= 255:
            raise ValueError('State action: неверный mode')
        result = dict(action=action,mode=value)
        pitch = row.get('pitch')
        if pitch is not None:
            if type(pitch) not in (int,float) or not -90 <= pitch <= 90:
                raise ValueError('peer_point: неверный pitch')
            result['pitch'] = float(pitch)
        part = row.get('part')
        if part is not None:
            if part not in ('all','ik','motion','fire','ik_motion'):
                raise ValueError('peer_point: неверный part')
            result['part'] = part
        return result
    maximum = 100 if action in ('give_ammo','peer_status') else (
        5 if action == 'peer_ammo' else 255 if action == 'peer_aim' else 1000)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError('State action: неверный '+parameter)
    result = dict(action=action,**{parameter:value})
    # Диагностика прицела: произвольный EuStatus id пирам (21/40/42...).
    if action == 'peer_status' and row.get('status2') is not None:
        second = row['status2']
        if type(second) is not int or not 1 <= second <= 100:
            raise ValueError('peer_status: неверный status2')
        result['status2'] = second
    if action == 'peer_ammo':
        clip = row.get('clip')
        if type(clip) is not int or not 0 <= clip <= 999:
            raise ValueError('peer_ammo: неверный clip')
        result['clip'] = clip
    if action == 'peer_aim' and row.get('kind') is not None:
        # 0x27 record kind byte: 0x10 remote actor branch, 0x12 local branch.
        kind = row['kind']
        if kind not in (0x10,0x12):
            raise ValueError('peer_aim: неверный kind')
        result['kind'] = kind
    if action == 'give_ammo':
        definition = row.get('definition')
        if type(definition) is not int or definition not in (30004,30001,30002,30005):
            raise ValueError('give_ammo: поддержаны12ga/5.56/7.62/.45')
        result['definition'] = definition
    return result


def apply_state_action(state, request, reserved_indexes=()):
    action = request['action']
    event = dict(action=action,accepted=False,administrative=True,experimental=True)
    if action in ('peer_status','peer_ammo','peer_aim','peer_ik','peer_point'):
        # Рассылку пирам делает endpoint через PeerAvatarRooms.live_probe.
        return [],dict(event,reason='peer_probe_handled_by_endpoint')
    world,weapon = state.get('world'),state.get('weapon')
    if world is None or weapon is None or getattr(world,'dead',False):
        return [],dict(event,reason='active_alive_world_required')
    if action == 'set_own_health':
        hp = request['hp']
        if not 1 <= hp <= world.fixture.hp:
            return [],dict(event,reason='outside_profile_health')
        body = encode_health(state['actor_uid'],hp)
        before = world.hp;world.hp = hp
        return [(0x1069,body)],dict(event,accepted=True,before=before,hp=hp)
    if action == 'give_ammo':
        definition,quantity = request['definition'],request['quantity']
        if weapon.ammunition_room(definition) < quantity:
            return [],dict(event,reason='insufficient_bag_capacity')
        staged = deepcopy(weapon)
        before = staged.ammunition_quantity(definition)
        staged.set_ammunition_quantity(definition,before+quantity)
        packets = [staged.ammunition_slot_sync(definition),*staged.magazine_syncs(),staged.capacity_sync()]
        weapon.set_ammunition_quantity(definition,before+quantity)
        return packets,dict(event,accepted=True,definition=definition,before=before,quantity=before+quantity)
    actor = state.get('target_actor')
    if actor is None or not actor.started or actor.hp <= 0:
        return [],dict(event,reason='active_npc_required')
    staged_actor = deepcopy(actor)
    packets,result = staged_actor.diagnostic_area_damage(request['damage'])
    # Пакет смерти готовится до commitHP и inventory; malformed fixture
    # не оставляет NPC мёртвым с невыданными предметами.
    corpse_event = None
    inventory = state.get('target_inventory')
    if staged_actor.hp == 0 and inventory is not None:
        staged_inventory = deepcopy(inventory)
        if staged_actor.fixture.armor_definition:
            staged_inventory.equipment_durability[12] = staged_actor.armor_durability
        corpses = deepcopy(state.get('death_parcels',DeathParcelState()))
        loot,corpse_event = corpses.transfer(staged_inventory,actor.position,reserved_indexes)
        packets.extend(loot)
    actor.__dict__.update(staged_actor.__dict__)
    if corpse_event is not None:
        inventory.__dict__.update(staged_inventory.__dict__)
        state['death_parcels'] = corpses
    return packets,dict(event,accepted=True,target=hex(actor.fixture.uid),hp=actor.hp,
                        damage=result['damage'],corpse=corpse_event)
