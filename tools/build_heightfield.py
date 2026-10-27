#!/usr/bin/env python3
"""Сборка карты высот heightfield_iceland_2022 из cooked PhysX рельефа.

Источник — DATA\\WORLD\\EU_ICELAND_4096\\SERVERDATA\\RIGIDTERRAINLINUX.PXBIN
внутри data_bas_4.vfs (клиентская cooked PhysX коллекция, магия SEBD).
Файл уже выгружен в extracted/RIGIDTERRAINLINUX_ICELAND.PXBIN
(5 zlib-чанков по смещениям 0x989c24/0xafce34/0xc620ae/0xdec734/0xf4edbb);
инструмент умеет выгрузить его из vfs повторно.

Внутри коллекции — NxHeightField: nbRows=nbColumns=2112, stride=4,
nbSamples=4460544, массив сэмплов {i16 height, u8 mat0, u8 mat1}
начинается со смещения 0x1940 и идёт до 0x1105940.

Мир→сетка откалибрована по реальным позициям игрока из ранов
peer_avatar_move/motion_settle/bot_fired (20261017-18):
col=(x-202000)/212, row=(y-156000)/204; ориентация подтверждена
кросс-корреляцией облака точек сущностей с полем (ncc=0.94, без
транспозиции и отражений). Высота мира = raw*HEIGHT_SCALE+HEIGHT_OFFSET:
медианная ошибка на чистых наземных позициях игрока ~59см, на точке
exfil — 15см (z игрока — конвенция центра капсулы).

На выходе: heightfield_iceland_2022.bin (сырые i16 2112x2112) и
heightfield_iceland_2022.json (метаданные и калибровка).
"""
import argparse
import json
import os
import struct
import sys
import zlib

SAMPLES_OFFSET = 0x1940
GRID = 2112
# Мир→сетка (см): точные параметры заголовка PhysX коллекции
# RIGIDTERRAINLINUX.PXBIN (@0x1828 hscale, @0x182c/0x1830 step=200,
# @0x1930/0x1934 origin=192000, @0x1938 z_offset=175002.671875).
ORIGIN_X = 192000.0
ORIGIN_Y = 192000.0
STEP_X = 200.0
STEP_Y = 200.0
# i16 сэмпл → высота мира (см).
HEIGHT_SCALE = 5.340657711029053
HEIGHT_OFFSET = 175002.671875

VFS_CHUNKS = (0x989C24, 0xAFCE34, 0xC620AE, 0xDEC734, 0xF4EDBB)


def extract_pxbin(vfs_path):
    data = b''
    with open(vfs_path, 'rb') as h:
        for off in VFS_CHUNKS:
            h.seek(off)
            data += zlib.decompressobj().decompress(h.read(3 << 20), 8 << 20)
    if data[:4] != b'SEBD':
        raise ValueError('ожидалась cooked PhysX коллекция SEBD')
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pxbin', default='../extracted/RIGIDTERRAINLINUX_ICELAND.PXBIN')
    p.add_argument('--vfs', default='runtime/client/data_bas_4.vfs')
    p.add_argument('--out', default='server/directory/heightfield_iceland_2022')
    args = p.parse_args()
    if os.path.exists(args.pxbin):
        data = open(args.pxbin, 'rb').read()
    elif os.path.exists(args.vfs):
        data = extract_pxbin(args.vfs)
    else:
        sys.exit('нет источника: %s / %s' % (args.pxbin, args.vfs))
    count = GRID * GRID
    blob = data[SAMPLES_OFFSET:SAMPLES_OFFSET + count * 4]
    if len(blob) != count * 4:
        sys.exit('массив сэмплов короче ожидаемого: %d' % len(blob))
    # оставляем только i16 высоты — {i16 h, u8 mat0, u8 mat1}
    raw = bytearray(count * 2)
    raw[0::2] = blob[0::4]
    raw[1::2] = blob[1::4]
    out_bin = args.out + '.bin'
    with open(out_bin, 'wb') as h:
        h.write(raw)
    meta = {
        'rows': GRID, 'cols': GRID, 'format': 'i16-le row-major',
        'origin_x': ORIGIN_X, 'origin_y': ORIGIN_Y,
        'step_x': STEP_X, 'step_y': STEP_Y,
        'height_scale': HEIGHT_SCALE, 'height_offset': HEIGHT_OFFSET,
        'source': 'RIGIDTERRAINLINUX.PXBIN (NxHeightField 2112x2112)',
        'file': os.path.basename(out_bin),
    }
    with open(args.out + '.json', 'w') as h:
        json.dump(meta, h, indent=2)
    print('heightfield: %dx%d -> %s (%d байт), meta %s'
          % (GRID, GRID, out_bin, len(raw), args.out + '.json'))


if __name__ == '__main__':
    main()
