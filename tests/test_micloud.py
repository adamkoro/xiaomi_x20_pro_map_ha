"""RC4 vector, captcha login on one session, secrets never reach exception text.

Run: uv run --no-project --with requests python tests/test_micloud.py
"""
import importlib.util
import json
import pathlib

import requests

_p = pathlib.Path(__file__).parents[1] / "custom_components/xiaomi_x20_map/micloud.py"
_spec = importlib.util.spec_from_file_location("micloud", _p)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

# Classic RC4 test vector (no keystream drop).
assert m.rc4(b"Key", b"Plaintext", drop=0).hex() == "bbf316e8d940af0ad3"
assert m.rc4(b"k" * 16, m.rc4(b"k" * 16, b"round trip")) == b"round trip"
assert m.image_type(b"\x89PNG\r\n") == "image/png" and m.image_type(b"\xff\xd8\xff") == "image/jpeg"


def _resp(text="", content=b"", cookies=None):
    r = requests.Response()
    r.status_code, r._content = 200, content or text.encode()
    for k, v in (cookies or {}).items():
        r.cookies.set(k, v)
    return r


class FakeXiaomi(requests.Session):
    """serviceLogin -> captcha -> wrong code -> new captcha -> right code -> ok."""

    def __init__(self):
        super().__init__()
        self.sessions_seen, self.captcha_fetches = set(), 0

    def request(self, method, url, **kw):
        self.sessions_seen.add(id(self))
        if url.startswith(m._LOGIN):
            return _resp("&&&START&&&" + json.dumps({"_sign": "s"}))
        if url == m._AUTH2:
            code = kw["params"].get("captCode")
            if code == "right":
                return _resp(json.dumps({"ssecurity": "c2VjcmV0", "userId": 1,
                                         "passToken": "pt", "location": "https://sts/x"}))
            return _resp(json.dumps({"code": 87001, "captchaUrl": "/pass/getCode?icodeType=login"}))
        if "getCode" in url:
            self.captcha_fetches += 1
            self.cookies.set("ick", "bound-to-this-session")
            return _resp(content=b"\xff\xd8\xff captcha")
        if url == "https://sts/x":
            self.cookies.set("serviceToken", "tok")
            return _resp("ok")
        raise AssertionError(url)


m.requests.Session = FakeXiaomi
c = m.MiCloud("user@example.com", "hunter2")
assert c.begin_login() == "captcha" and c.captcha_image.startswith(b"\xff\xd8")
assert c.submit_captcha("wrong") == "captcha" and c._s.captcha_fetches == 2  # image refreshed
assert c.submit_captcha(" right ") == "ok"
assert len(c._s.sessions_seen) == 1  # every step on the same cookies (ick)
assert c.session() == {"user_id": 1, "ssecurity": "c2VjcmV0", "service_token": "tok",
                       "pass_token": "pt"}
d = m.MiCloud()
d.restore(c.session())
assert d.logged_in


class Boom(requests.Session):
    def request(self, method, url, **kw):  # every call fails, with the full URL in the text
        raise requests.ConnectionError(f"failed {url} {kw.get('params')}")


m.requests.Session = Boom
c = m.MiCloud("user@example.com", "hunter2")
try:
    c.begin_login()
    raise AssertionError("login must fail")
except m.CloudUnreachable as ex:
    assert ex.__suppress_context__ and "example.com" not in str(ex) and "hunter2" not in str(ex)

c.restore({"user_id": "1", "ssecurity": "c2VjcmV0c2VjcmV0", "service_token": "tok"})
try:
    c.call("de", "/home/getmapfileurl", {"obj_name": "x"})
    raise AssertionError("call must fail")
except m.CloudUnreachable as ex:
    assert ex.__suppress_context__ and "c2VjcmV0" not in str(ex)
print("ok")
