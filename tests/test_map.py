"""Round-trip check: encrypt a fake map the way the robot does, decrypt + render it.

Run: uv run --with pillow --with cryptography python tests/test_map.py
"""
import base64
import importlib.util
import io
import json
import pathlib
import zlib

from cryptography.hazmat.primitives import padding
from PIL import Image

_p = pathlib.Path(__file__).parents[1] / "custom_components/xiaomi_x20_map/map.py"
_spec = importlib.util.spec_from_file_location("x20map", _p)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

MODEL, DID = "xiaomi.vacuum.d102gl", "1173077953"

# 4x3 grid: row 0 wall, row 1 floor + room 7, row 2 unknown.
cells = bytes([255, 255, 255, 255, 1, 1, 7, 7, 0, 0, 0, 0])
data = {"width": 4, "height": 3, "resolution": 50,
        "map_data": base64.b64encode(zlib.compress(cells)).decode()}

p = padding.PKCS7(128).padder()
plain = p.update(zlib.compress(json.dumps(data).encode())) + p.finalize()
blob = json.dumps({"version": 2, "data": base64.b64encode(
    m._aes(m.map_key(MODEL, DID), plain, True)).decode()}).encode()

assert m.decrypt(blob, MODEL, DID) == data
img = Image.open(io.BytesIO(m.MapImage(data, scale=1).png()))
assert img.size == (4, 2), img.size  # unknown row cropped
# Flipped: top row is room/floor, bottom row is wall.
assert img.getpixel((3, 0))[:3] == m._ROOM_COLORS[7 % 6] and img.getpixel((0, 1))[:3] == (0x33,) * 3
assert img.getpixel((0, 0))[:3] == (0xcc,) * 3

# Dock + robot: 20x20 floor at 50 mm/cell, origin (-500, -500) mm.
floor = bytes([1]) * 400
big = {"width": 20, "height": 20, "resolution": 50, "origin_x": -500, "origin_y": -500,
       "map_data": base64.b64encode(zlib.compress(floor)).decode(),
       "have_pile": 1, "pile_x": 0, "pile_y": -400, "pile_yaw": 0}
mi = m.MapImage(big, scale=1)
assert mi.to_px(-500, -500) == (0, 20)  # origin = bottom-left after the flip
assert mi.to_px(0, 0) == (10, 10)
assert mi.dock == (0.0, -400.0, 0.0)
pm = m.MapImage({**big, "paths": {"points": [{"x": -400, "y": 300}, {"x": 400, "y": 300}]}}, scale=1)
assert Image.open(io.BytesIO(pm.png())).getpixel((10, 4)) == (255, 255, 255, 255)  # path: solid white
im = Image.open(io.BytesIO(mi.png()))
assert im.getpixel((8, 18))[:3] == m.DOCK_COLOR  # pile (0, -400) mm -> near the bottom (centre = heading tick)
assert im.getpixel((10, 5))[:3] == (0xcc,) * 3
im = Image.open(io.BytesIO(mi.png(robot=(0, 250, 90))))
assert im.getpixel((8, 5))[:3] == m.ROBOT_COLOR  # (0, 250) mm -> 5 rows from the top
assert im.getpixel((10, 1))[:3] == (255, 255, 255)  # heading 90 deg = north = up

# p10.4 parsing: the formats seen in the references; unknown -> None, never raises.
P = m.parse_position
assert P('{"x": 100, "y": -20, "yaw": 9000}') == (100, -20, 90)
assert P({"position": {"x": 1, "y": 2, "phi": 1.5707963}})[:2] == (1, 2)
assert round(P({"position": {"x": 1, "y": 2, "phi": 1.5707963}})[2]) == 90
assert P({"robotPos": [5, 6], "robotPhi": 180}) == (5, 6, 180)
assert P("[10, 20]") == (10, 20, None) and P("10,20,45") == (10, 20, 45)
assert P(None) is None and P("") is None and P("abc") is None and P({"x": "?"}) is None
assert m.yaw_degrees(-90) == 270 and m.yaw_degrees("x") is None
try:
    m.decrypt(blob, MODEL, "999")
    raise AssertionError("wrong did must not decrypt")
except (ValueError, zlib.error):
    pass
D = m.describe({"map_data": "x" * 99, "width": 4, "furniture": [{"x": 1, "type": 3}], "s": "y" * 50,
                "o": {"b": 1, "a": 2}, "e": []})
assert D == {"width": 4, "furniture": "list[1] ['type', 'x']", "s": "str[50]",
             "o": "dict ['a', 'b']", "e": "list[0]"}, D
print("ok")
