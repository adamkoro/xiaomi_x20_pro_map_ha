"""RC4 vector + secrets never reach exception text.

Run: uv run --no-project --with requests python tests/test_micloud.py
"""
import importlib.util
import pathlib

import requests

_p = pathlib.Path(__file__).parents[1] / "custom_components/xiaomi_x20_map/micloud.py"
_spec = importlib.util.spec_from_file_location("micloud", _p)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

# Classic RC4 test vector (no keystream drop).
assert m.rc4(b"Key", b"Plaintext", drop=0).hex() == "bbf316e8d940af0ad3"
assert m.rc4(b"k" * 16, m.rc4(b"k" * 16, b"round trip")) == b"round trip"


class _Boom(requests.Session):
    def request(self, method, url, **kw):  # every call fails, with the full URL in the text
        raise requests.ConnectionError(f"failed {url} {kw.get('params')}")


m.requests.Session = _Boom
c = m.MiCloud("user@example.com", "hunter2")
try:
    c.login()
    raise AssertionError("login must fail")
except m.CloudUnreachable as ex:
    assert ex.__cause__ is None and ex.__suppress_context__
    assert "example.com" not in str(ex) and "hunter2" not in str(ex)

c._s, c._ssecurity, c._token, c._uid = _Boom(), "c2VjcmV0c2VjcmV0", "tok", "1"
try:
    c.call("de", "/home/getmapfileurl", {"obj_name": "x"})
    raise AssertionError("call must fail")
except m.CloudUnreachable as ex:
    assert ex.__suppress_context__ and "c2VjcmV0" not in str(ex)
print("ok")
