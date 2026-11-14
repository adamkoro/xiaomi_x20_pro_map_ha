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
img = Image.open(io.BytesIO(m.render(data, scale=1)))
assert img.size == (4, 2), img.size  # unknown row cropped
# Flipped: top row is room/floor, bottom row is wall.
assert img.getpixel((3, 0)) == 7 and img.getpixel((0, 1)) == 255
try:
    m.decrypt(blob, MODEL, "999")
    raise AssertionError("wrong did must not decrypt")
except (ValueError, zlib.error):
    pass
print("ok")
