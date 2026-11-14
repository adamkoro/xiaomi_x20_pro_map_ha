"""Decrypt and render a Xiaomi "version 2" JSON cloud map. No Home Assistant imports.

Ported from letitbe-dull/xiaomi-vac (xiaomi_json_decrypt.py, map_vector.py),
which reverse-engineered it from the Xiaomi Home app's robovac plugin.
Hardware-confirmed there on d109gl / ov42gl; d102gl is in the same family.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import zlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

_IV = b"ABCDEF1234123412"


def _aes(key: bytes, data: bytes, encrypt: bool) -> bytes:
    c = Cipher(algorithms.AES(key), modes.CBC(_IV))
    op = c.encryptor() if encrypt else c.decryptor()
    return op.update(data) + op.finalize()


def map_key(model: str, did: str) -> bytes:
    """AES key: md5(AES-CBC(model[-16:], pkcs7(model[-16:] + did)))."""
    model_key = model[-16:].encode("latin1")
    p = padding.PKCS7(128).padder()
    work = p.update(model_key + did.encode("latin1")) + p.finalize()
    return hashlib.md5(_aes(model_key, work, True)).digest()


def decrypt(raw: bytes, model: str, did: str) -> dict:
    """Downloaded blob -> map JSON. Blob is {"version":2,"data":"<b64 AES>"}."""
    env = json.loads(raw)
    if env.get("version") != 2:
        raise ValueError(f"unsupported map blob version {env.get('version')!r}")
    plain = _aes(map_key(model, did), base64.b64decode(env["data"]), False)
    u = padding.PKCS7(128).unpadder()
    return json.loads(zlib.decompress(u.update(plain) + u.finalize()))


# Cell values: 0 unknown, 1-2 floor, 3-63 room grid id, >63 wall.
_ROOM_COLORS = [(0x6f, 0xa8, 0xdc), (0x93, 0xc4, 0x7d), (0xf6, 0xb2, 0x6b),
                (0xc2, 0x7b, 0xa0), (0xff, 0xd9, 0x66), (0x76, 0xa5, 0xaf)]
_PALETTE = bytearray()
for _v in range(256):
    if _v == 0:
        _c = (0, 0, 0)
    elif _v <= 2:
        _c = (0xcc, 0xcc, 0xcc)
    elif _v <= 63:
        _c = _ROOM_COLORS[_v % len(_ROOM_COLORS)]
    else:
        _c = (0x33, 0x33, 0x33)
    _PALETTE += bytes(_c)


def render(data: dict, scale: int = 2) -> bytes:
    """Map JSON -> PNG bytes. Unknown cells are transparent."""
    w, h = int(data["width"]), int(data["height"])
    cells = zlib.decompress(base64.b64decode(data["map_data"]))[: w * h]
    img = Image.frombytes("P", (w, h), cells)
    img.putpalette(bytes(_PALETTE))
    img.info["transparency"] = 0
    # Row 0 is origin_y (south); flip so north is up.
    img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)
    img = img.resize((img.width * scale, img.height * scale), Image.Resampling.NEAREST)
    # ponytail: rooms/walls only; robot (p10.4), dock (pile_x/y), path and labels
    # come after the live blob format is confirmed on d102gl.
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
