"""Legacy Mi-account cloud client, used ONLY to mint the map file URL.

Blocking (requests); run it in an executor. Ported from Xiaomi Cloud Map Extractor's
xiaomi_cloud_connector.py and letitbe-dull/xiaomi-vac's cloud/connector.py.
Never log or raise with the username, password, ssecurity or serviceToken.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import time

import requests

_LOGIN = "https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true"
_AUTH2 = "https://account.xiaomi.com/pass/serviceLoginAuth2"


class LoginError(Exception):
    """Login refused (wrong password, network, unexpected answer)."""


class CloudUnreachable(LoginError):
    """Network trouble reaching Xiaomi; safe to retry later."""


class VerificationRequired(LoginError):
    """Xiaomi wants 2FA or a captcha; a human has to act. `url` is where (may be None)."""

    def __init__(self, msg: str, url: str | None = None):
        super().__init__(msg)
        self.url = url


def rc4(key: bytes, data: bytes, drop: int = 1024) -> bytes:
    s, j = list(range(256)), 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for n in range(drop + len(data)):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        if n >= drop:
            out.append(data[n - drop] ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def _json(text: str) -> dict:
    return json.loads(text.replace("&&&START&&&", ""))


class MiCloud:
    def __init__(self, username: str, password: str):
        self._user, self._pw = username, password
        self._s: requests.Session | None = None
        self._uid = self._ssecurity = self._token = None
        self._agent = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=18)) + "-" + \
            "".join(random.choices("ABCDE", k=13)) + " APP/com.xiaomi.mihome APPV/10.5.201"

    @property
    def logged_in(self) -> bool:
        return bool(self._token)

    def login(self) -> None:
        """Password login. Raises VerificationRequired / LoginError. One attempt, no loops."""
        self._token = None
        s = self._s = requests.Session()
        dev = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=6))
        for dom in ("mi.com", "xiaomi.com"):
            s.cookies.set("sdkVersion", "accountsdk-18.8.15", domain=dom)
            s.cookies.set("deviceId", dev, domain=dom)
        h = {"User-Agent": self._agent, "Content-Type": "application/x-www-form-urlencoded"}
        try:
            r = s.get(_LOGIN, headers=h, cookies={"userId": self._user}, timeout=10)
            sign = _json(r.text)["_sign"]
            r = s.post(_AUTH2, headers=h, timeout=10, params={
                "sid": "xiaomiio", "hash": hashlib.md5(self._pw.encode()).hexdigest().upper(),
                "callback": "https://sts.api.io.mi.com/sts", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
                "user": self._user, "_sign": sign, "_json": "true"})
            j = _json(r.text)
        except (requests.RequestException, ValueError, KeyError) as ex:
            # from None: the exception text would carry the URL with user + password hash.
            raise CloudUnreachable(f"Xiaomi login request failed ({type(ex).__name__})") from None

        if j.get("notificationUrl"):
            raise VerificationRequired(
                "Xiaomi wants identity verification (2FA). Open the URL in a browser on the same "
                "network/public IP as Home Assistant, finish the check, then retry.",
                j["notificationUrl"])
        if j.get("captchaUrl") or j.get("code") == 87001:
            cap = j.get("captchaUrl") or ""
            raise VerificationRequired(
                "Xiaomi asks for a captcha. Sign in once at https://account.xiaomi.com in a browser "
                "on the same network as Home Assistant, then retry.",
                ("https://account.xiaomi.com" + cap) if cap.startswith("/") else (cap or None))
        if not j.get("ssecurity"):
            # 70016 = wrong user/password. desc is Xiaomi's text, carries no secrets.
            raise LoginError(f"Xiaomi refused the login (code {j.get('code')}: {j.get('desc', '')})")

        self._uid, self._ssecurity = j["userId"], j["ssecurity"]
        try:
            r = s.get(j["location"], headers=h, timeout=10)
        except requests.RequestException as ex:
            raise CloudUnreachable(f"Xiaomi login request failed ({type(ex).__name__})") from None
        self._token = r.cookies.get("serviceToken")
        if not self._token:
            raise LoginError("Xiaomi login gave no serviceToken")

    def logout(self) -> None:
        self._token = None

    def _signed_nonce(self, nonce: str) -> str:
        return base64.b64encode(hashlib.sha256(
            base64.b64decode(self._ssecurity) + base64.b64decode(nonce)).digest()).decode()

    @staticmethod
    def _sig(path: str, sn: str, params: dict) -> str:
        parts = ["POST", path, *(f"{k}={v}" for k, v in params.items()), sn]
        return base64.b64encode(hashlib.sha1("&".join(parts).encode()).digest()).decode()

    def call(self, server: str, path: str, data: dict) -> tuple[int, dict | str]:
        """Signed RC4 POST to https://{server}.api.io.mi.com/app{path}. -> (status, json or text)."""
        nonce = base64.b64encode(os.urandom(8) + int(time.time() / 60).to_bytes(4, "big")).decode()
        sn = self._signed_nonce(nonce)
        key = base64.b64decode(sn)
        params = {"data": json.dumps(data)}
        params["rc4_hash__"] = self._sig(path, sn, params)
        params = {k: base64.b64encode(rc4(key, v.encode())).decode() for k, v in params.items()}
        params.update(signature=self._sig(path, sn, params), ssecurity=self._ssecurity, _nonce=nonce)
        host = "api.io.mi.com" if server == "cn" else f"{server}.api.io.mi.com"
        try:
            r = self._s.post(
                f"https://{host}/app{path}", params=params, timeout=10,
                headers={"User-Agent": self._agent, "Accept-Encoding": "identity",
                         "Content-Type": "application/x-www-form-urlencoded",
                         "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
                         "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4"},
                cookies={"userId": str(self._uid), "serviceToken": self._token,
                         "yetAnotherServiceToken": self._token, "locale": "en_GB",
                         "channel": "MI_APP_STORE"})
        except requests.RequestException as ex:
            # from None: the exception text would carry the URL with ssecurity in it.
            raise CloudUnreachable(f"Xiaomi API request failed ({type(ex).__name__})") from None
        if r.status_code != 200:
            return r.status_code, r.text[:200]
        try:
            return 200, json.loads(rc4(key, base64.b64decode(r.text)))
        except ValueError:
            return 200, r.text[:200]
