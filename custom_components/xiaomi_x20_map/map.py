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
import math
import re
import zlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image, ImageDraw

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


DOCK_COLOR = (0x2e, 0xa0, 0x43)
ROBOT_COLOR = (0x1e, 0x6f, 0xd9)
PATH_COLOR = (0xff, 0xff, 0xff, 0xff)  # opaque: alpha shows the card background (grey)
ROBOT_RADIUS_MM = 175  # X20 Pro is ~35 cm across


def yaw_degrees(v) -> float | None:
    """Heading in degrees, counter-clockwise from +x. Xiaomi mixes radians,
    centi-degrees and degrees across models (see vacuum_map_parser_xiaomi)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if abs(v) <= 2 * math.pi + 0.001:
        return math.degrees(v) % 360
    if abs(v) > 360:
        return (v / 100) % 360
    return v % 360


_YAW_KEYS = ("yaw", "phi", "angle", "a", "theta", "robotPhi", "direction")


def parse_position(raw) -> tuple[float, float, float | None] | None:
    """p10.4 vacuum-position -> (x_mm, y_mm, yaw_deg | None), or None if unreadable.

    The d102gl format is not documented. Accepted: JSON or plain dict
    {"x","y","yaw"|"phi"|...}, nested under "position"/"pos"; Dreame-style
    {"robotPos": [x, y], "robotPhi": a}; a list [x, y(, a)]; or "x,y(,a)" text.
    Units are assumed to be the map's millimetres.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            nums = re.findall(r"-?\d+(?:\.\d+)?", raw)
            raw = [float(n) for n in nums]
    if isinstance(raw, dict):
        for k in ("position", "pos", "robotPos", "robot_pos"):
            inner = raw.get(k)
            if isinstance(inner, dict):
                return parse_position({**raw, **inner, k: None})
            if isinstance(inner, (list, tuple)) and len(inner) >= 2:
                yaw = next((raw[y] for y in _YAW_KEYS if y in raw), None)
                return parse_position([*inner[:2], yaw] if yaw is not None else inner)
        try:
            x, y = float(raw["x"]), float(raw["y"])
        except (KeyError, TypeError, ValueError):
            return None
        return x, y, yaw_degrees(next((raw[k] for k in _YAW_KEYS if k in raw), None))
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        try:
            x, y = float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            return None
        return x, y, yaw_degrees(raw[2]) if len(raw) > 2 else None
    return None


class MapImage:
    """Base image (rooms, walls, path, dock) drawn once per map download;
    png(robot) stamps only the robot on a copy, cheap enough for every position update."""

    def __init__(self, data: dict, scale: int = 2):
        w, h = int(data["width"]), int(data["height"])
        cells = zlib.decompress(base64.b64decode(data["map_data"]))[: w * h]
        img = Image.frombytes("P", (w, h), cells)
        img.putpalette(bytes(_PALETTE))
        img.info["transparency"] = 0
        # Row 0 is origin_y (south); flip so north is up.
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        self._left, self._top = 0, 0
        if bbox := img.getbbox():
            img = img.crop(bbox)
            self._left, self._top = bbox[0], bbox[1]
        self._h, self._scale = h, scale
        self._res = float(data.get("resolution") or 50)  # mm per cell
        self._ox, self._oy = float(data.get("origin_x", 0)), float(data.get("origin_y", 0))
        img = img.convert("RGBA").resize((img.width * scale, img.height * scale),
                                         Image.Resampling.NEAREST)

        draw = ImageDraw.Draw(img)
        paths = data.get("paths")
        pts = paths.get("points") if isinstance(paths, dict) else paths
        line = [self.to_px(p["x"], p["y"]) for p in pts or ()
                if isinstance(p, dict) and "x" in p and "y" in p]
        if len(line) > 1:
            draw.line(line, fill=PATH_COLOR, width=max(1, scale // 2))
        self.dock = None
        if data.get("have_pile", "pile_x" in data) and "pile_x" in data:
            self.dock = (float(data["pile_x"]), float(data.get("pile_y", 0)),
                         yaw_degrees(data.get("pile_yaw")))
            self._marker(draw, self.dock, DOCK_COLOR, square=True)
        # Robot pose saved in the map file: the fallback when p10.4 can't be parsed.
        self.map_robot = parse_position(data.get("position"))
        self.base = img

    def to_px(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        col = (x_mm - self._ox) / self._res
        row = self._h - (y_mm - self._oy) / self._res  # flipped: north up
        return (col - self._left) * self._scale, (row - self._top) * self._scale

    def _marker(self, draw, pose, color, square=False) -> None:
        x, y = self.to_px(pose[0], pose[1])
        r = max(3.0, ROBOT_RADIUS_MM / self._res * self._scale)
        box = (x - r, y - r, x + r, y + r)
        if square:
            draw.rectangle(box, fill=color, outline="white", width=2)
        else:
            draw.ellipse(box, fill=color, outline="white", width=2)
        if pose[2] is not None:  # heading tick; image y grows downward
            a = math.radians(pose[2])
            draw.line([(x, y), (x + math.cos(a) * r * 1.6, y - math.sin(a) * r * 1.6)],
                      fill="white", width=max(2, int(r / 3)))

    def png(self, robot: tuple[float, float, float | None] | None = None) -> bytes:
        img = self.base
        if robot := robot or self.map_robot:
            img = img.copy()
            self._marker(ImageDraw.Draw(img), robot, ROBOT_COLOR)
        out = io.BytesIO()
        img.save(out, "PNG", compress_level=1)  # speed over size: redrawn every few seconds
        return out.getvalue()


def describe(data: dict) -> dict:
    """Shape of every map-JSON key except the grid: finds where furniture etc. live."""
    def short(v):
        if isinstance(v, list):
            first = v[0] if v else None
            return f"list[{len(v)}]" + (f" {sorted(first)[:15]}" if isinstance(first, dict)
                                        else f" e.g. {short(first)}" if v else "")
        if isinstance(v, dict):
            return f"dict {sorted(v)[:15]}"
        return v if not isinstance(v, str) or len(v) <= 40 else f"str[{len(v)}]"
    return {k: short(v) for k, v in data.items() if k != "map_data"}
