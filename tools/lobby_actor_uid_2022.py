"""Явная идентичность одного локального клиента2022; без общего мира/аккаунтов."""
import json
import struct
from roe_protocol_2022 import FrameBuffer, dt_plain, encrypt


class LobbyActorUIDStream:
    def __init__(self,key,uid):
        if type(uid) is not int or not 0 < uid < 2**64:
            raise ValueError('Local actor UID требует ненулевой U64')
        self.key = key
        self.uid = uid
        self.frames = FrameBuffer()

    def feed(self,data):
        output, updates = [], []
        for frame in self.frames.feed(data):
            result = frame
            if frame[2:4] == bytes(2):
                plain = dt_plain(frame,self.key)
                # ENTER_GAME_RSP39: payload+12/decoded+22 — UID следующих
                # клиентских заголовков. Не искать UID в произвольных профилях.
                if plain is not None and len(plain) == 39:
                    code,tag = struct.unpack_from('>HI',plain,4)
                    if code == tag == 0x32b and struct.unpack_from('>I',plain,30)[0] == 0:
                        previous = struct.unpack_from('>Q',plain,22)[0]
                        patched = bytearray(plain)
                        struct.pack_into('>Q',patched,22,self.uid)
                        head = struct.unpack_from('>I',frame,4)[0]
                        cipher = encrypt(bytes(patched),self.key)
                        header = bytearray(frame[:head])
                        struct.pack_into('>I',header,8,len(cipher))
                        result = bytes(header)+cipher
                        updates.append(dict(previous_actor_uid=hex(previous),actor_uid=hex(self.uid)))
            output.append(result)
        return b''.join(output),updates

    def finish(self):
        if self.frames.pending:
            raise ValueError('TCP-поток завершился внутри lobby-кадра')


class LobbyAvatarStream:
    """Подмена аватара в карточке актора LOGIN_WORLD_RSP2EF.

    Лобби-манекен строится из карточки актора внутри 0x2ef до первого матча;
    синки косметики (0x73a1/0x73b1) прилетают позже и до первой игры не
    пересобирают модель. Смещения тела wire-проверены на canned-blob
    LobbyPreData (FUN_140bdc9c0/FUN_140bdbc30): аватар 5×U32 по 0x4d6,
    флаг applied U8 по 0x4ea. Проверка длины и имени не даёт трогать чужой blob.
    """
    PLAIN_LEN = 0x8fa
    ACCT_AT = 10 + 0x04
    UID_AT = 10 + 0x14
    AVATAR_AT = 10 + 0x4d6
    APPLIED_AT = 10 + 0x4ea
    NAME_AT = 10 + 0x24
    NAME_LEN_AT = 10 + 0x20
    CANNED_NAME = b'BRING_BACK_ROE!!\x00'

    def __init__(self,key,appearance_dir,uid,account_uid=0x14cc20):
        if type(uid) is not int or not 0 < uid < 2**64:
            raise ValueError('Local actor UID требует ненулевой U64')
        self.key = key
        self.uid = uid
        self.account_uid = account_uid
        self.frames = FrameBuffer()
        self.avatar = None
        self.applied = 0
        path = appearance_dir/f'{uid:x}.json' if appearance_dir is not None else None
        if path is not None and path.exists():
            state = json.loads(path.read_text(encoding='utf-8'))
            avatar = state.get('avatar')
            if isinstance(avatar,list) and len(avatar) == 5:
                avatar = [int(value) & 0xffffffff for value in avatar]
                if any(avatar):
                    self.avatar = avatar
                    self.applied = 1 if state.get('avatar_applied') else 0

    def feed(self,data):
        output, updates = [], []
        for frame in self.frames.feed(data):
            result = frame
            if frame[2:4] == bytes(2):
                plain = dt_plain(frame,self.key)
                if plain is not None and len(plain) == self.PLAIN_LEN:
                    code,tag = struct.unpack_from('>HI',plain,4)
                    if code == tag == 0x2ef \
                            and struct.unpack_from('>I',plain,self.NAME_LEN_AT)[0] == len(self.CANNED_NAME) \
                            and plain[self.NAME_AT:self.NAME_AT+len(self.CANNED_NAME)] == self.CANNED_NAME:
                        previous = list(struct.unpack_from('>5I',plain,self.AVATAR_AT))
                        patched = bytearray(plain)
                        # Карточка отбрасывается клиентом, если uid записи не его —
                        # canned-blob несёт 0x4444.. / чужой аккаунт.
                        struct.pack_into('>Q',patched,self.ACCT_AT,self.account_uid)
                        struct.pack_into('>Q',patched,self.UID_AT,self.uid)
                        if self.avatar is not None:
                            struct.pack_into('>5I',patched,self.AVATAR_AT,*self.avatar)
                            patched[self.APPLIED_AT] = self.applied
                        head = struct.unpack_from('>I',frame,4)[0]
                        cipher = encrypt(bytes(patched),self.key)
                        header = bytearray(frame[:head])
                        struct.pack_into('>I',header,8,len(cipher))
                        result = bytes(header)+cipher
                        updates.append(dict(previous_avatar=previous,avatar=self.avatar,applied=self.applied,actor_uid=hex(self.uid)))
            output.append(result)
        return b''.join(output),updates

    def finish(self):
        if self.frames.pending:
            raise ValueError('TCP-поток завершился внутри lobby-кадра')
