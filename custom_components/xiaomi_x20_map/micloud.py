"""Legacy Mi-account cloud client, used ONLY to mint the map file URL.

Blocking (requests); run it in an executor. Ported from Xiaomi Cloud Map Extractor's
xiaomi_cloud_connector.py and letitbe-dull/xiaomi-vac's cloud/connector.py.

Login is a small state machine driven by the options flow. One MiCloud object (one
requests.Session, so one set of cookies such as `ick`) lives across the flow's steps,
because Xiaomi binds the captcha and the 2FA ticket to that session:

    begin_login() / submit_captcha(code) / submit_2fa(code) -> "ok" | "captcha" | "2fa"

After "ok", session() is stored in the config entry; the camera restore()s it and
renews it with refresh() (passToken, no password, no captcha).

Never log or raise with the username, password, ssecurity, serviceToken or passToken.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import time
from urllib.parse import parse_qs, urlparse

import requests

_ACC = "https://account.xiaomi.com"
_LOGIN = f"{_ACC}/pass/serviceLogin?sid=xiaomiio&_json=true"
_AUTH2 = f"{_ACC}/pass/serviceLoginAuth2"


class LoginError(Exception):
    """Login refused or unexpected answer. The message is safe to show."""


class CloudUnreachable(LoginError):
    """Network trouble reaching Xiaomi; safe to retry later."""


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


def image_type(data: bytes) -> str:
    """MIME type for a captcha image (Xiaomi serves JPEG or PNG without an extension)."""
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


class MiCloud:
    def __init__(self, username: str = "", password: str = ""):
        self._user, self._pw = username, password
        self._s = requests.Session()
        self._uid = self._ssecurity = self._token = self._pass_token = None
        self._fields: dict = {}
        self._2fa: tuple[str, str] | None = None  # (context, "Email" | "Phone")
        self.captcha_image: bytes | None = None
        self._agent = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=18)) + "-" + \
            "".join(random.choices("ABCDE", k=13)) + " APP/com.xiaomi.mihome APPV/10.5.201"
        dev = "".join(random.choices("abcdefghijklmnopqrstuvwxyz", k=6))
        for dom in ("mi.com", "xiaomi.com"):
            self._s.cookies.set("sdkVersion", "accountsdk-18.8.15", domain=dom)
            self._s.cookies.set("deviceId", dev, domain=dom)

    # --- plumbing ---------------------------------------------------------
    def _req(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 10)
        kw.setdefault("headers", {"User-Agent": self._agent,
                                  "Content-Type": "application/x-www-form-urlencoded"})
        try:
            return self._s.request(method, url, **kw)
        except requests.RequestException as ex:
            # from None: the exception text carries the URL (user, password hash, ssecurity).
            raise CloudUnreachable(f"Xiaomi request failed ({type(ex).__name__})") from None

    def _cookie(self, name: str) -> str | None:
        return next((c.value for c in self._s.cookies if c.name == name and c.value), None)

    # --- session ----------------------------------------------------------
    @property
    def logged_in(self) -> bool:
        return bool(self._token)

    def logout(self) -> None:
        self._token = None

    def session(self) -> dict:
        return {"user_id": self._uid, "ssecurity": self._ssecurity,
                "service_token": self._token, "pass_token": self._pass_token}

    def restore(self, sess: dict) -> None:
        self._uid, self._ssecurity = sess.get("user_id"), sess.get("ssecurity")
        self._token, self._pass_token = sess.get("service_token"), sess.get("pass_token")

    def refresh(self) -> bool:
        """New ssecurity + serviceToken from the long-lived passToken. No captcha, no 2FA."""
        if not (self._pass_token and self._uid):
            return False
        self._s.cookies.set("userId", str(self._uid), domain="xiaomi.com")
        self._s.cookies.set("passToken", self._pass_token, domain="xiaomi.com")
        try:
            j = _json(self._req("GET", _LOGIN).text)
        except ValueError:
            return False
        if not (j.get("ssecurity") and j.get("location")):
            return False
        self._ssecurity = j["ssecurity"]
        self._pass_token = j.get("passToken") or self._pass_token
        self._req("GET", j["location"])
        self._token = self._cookie("serviceToken")
        return bool(self._token)

    # --- login state machine ---------------------------------------------
    def begin_login(self) -> str:
        try:
            sign = _json(self._req("GET", _LOGIN, cookies={"userId": self._user}).text)["_sign"]
        except (ValueError, KeyError):
            raise LoginError("Xiaomi login page gave an unexpected answer") from None
        self._fields = {
            "sid": "xiaomiio", "hash": hashlib.md5(self._pw.encode()).hexdigest().upper(),
            "callback": "https://sts.api.io.mi.com/sts", "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "user": self._user, "_sign": sign, "_json": "true"}
        return self._auth2()

    def submit_captcha(self, code: str) -> str:
        self._fields["captCode"] = code.strip()
        return self._auth2()

    def _auth2(self) -> str:
        try:
            j = _json(self._req("POST", _AUTH2, params=self._fields).text)
        except ValueError:
            raise LoginError("Xiaomi login gave an unexpected answer") from None

        if j.get("ssecurity"):
            self._uid, self._ssecurity = j["userId"], j["ssecurity"]
            self._pass_token = j.get("passToken")
            self._req("GET", j["location"])
            self._token = self._cookie("serviceToken")
            if not self._token:
                raise LoginError("Xiaomi login gave no serviceToken")
            return "ok"
        if j.get("captchaUrl") or j.get("code") == 87001:
            # 87001 = captcha needed, or the code just typed was wrong: either way a new image.
            cap = j.get("captchaUrl") or "/pass/getCode?icodeType=login"
            if cap.startswith("/"):
                cap = _ACC + cap
            self.captcha_image = self._req("GET", cap).content  # same session -> same ick cookie
            return "captcha"
        if j.get("notificationUrl"):
            self._start_2fa(j["notificationUrl"])
            return "2fa"
        # 70016 = wrong user/password. desc is Xiaomi's text, carries no secrets.
        raise LoginError(f"Xiaomi refused the login (code {j.get('code')}: {j.get('desc', '')})")

    def _start_2fa(self, notification_url: str) -> None:
        """Ask Xiaomi to send a verification code: e-mail first, then SMS."""
        self._req("GET", notification_url)
        try:
            ctx = parse_qs(urlparse(notification_url).query)["context"][0]
        except (KeyError, IndexError):
            raise LoginError("Xiaomi 2FA link has no context") from None
        q = {"sid": "xiaomiio", "context": ctx, "_locale": "en_US"}
        self._req("GET", f"{_ACC}/identity/list", params=q)
        answers = []
        # ponytail: e-mail path is the one xiaomi-vac verified; SMS uses the same shape, unverified.
        for kind in ("Email", "Phone"):
            r = self._req("POST", f"{_ACC}/identity/auth/send{kind}Ticket",
                          params={**q, "_dc": str(int(time.time() * 1000)), "mask": "0"},
                          data={"retry": "0", "icode": "", "_json": "true",
                                "ick": self._cookie("ick") or ""})
            try:
                code = _json(r.text).get("code")
            except ValueError:
                code = r.status_code
            if code == 0:
                self._2fa = (ctx, kind)
                return
            answers.append(f"{kind.lower()}: {code}")
        raise LoginError(f"Xiaomi wants 2FA but sending the code failed ({', '.join(answers)})")

    @property
    def twofa_channel(self) -> str:
        return "e-mail" if self._2fa and self._2fa[1] == "Email" else "SMS"

    def submit_2fa(self, code: str) -> str:
        """Verify the e-mailed/SMS code and finish the login. "2fa" again = wrong code."""
        ctx, kind = self._2fa
        q = {"sid": "xiaomiio", "context": ctx, "_locale": "en_US"}
        flag = "8" if kind == "Email" else "4"
        r = self._req("POST", f"{_ACC}/identity/auth/verify{kind}",
                      params={**q, "_flag": flag, "_json": "true", "mask": "0"},
                      data={"_flag": flag, "ticket": code.strip(), "trust": "false",
                            "_json": "true", "ick": self._cookie("ick") or ""})
        try:
            j = _json(r.text)
        except ValueError:
            j = {}
        if j.get("code") not in (0, None):
            return "2fa"
        finish = j.get("location")
        if not finish:
            m = re.search(r'https://account\.xiaomi\.com/identity/result/check\?[^"\']+', r.text)
            finish = m.group(0) if m else None
        if not finish:
            r0 = self._req("GET", f"{_ACC}/identity/result/check", params=q, allow_redirects=False)
            finish = r0.headers.get("Location")
        if not finish:
            raise LoginError("Xiaomi accepted the code but gave no way to finish the login")
        if "identity/result/check" in finish:
            finish = self._req("GET", finish, allow_redirects=False).headers.get("Location")
        r = self._req("GET", finish, allow_redirects=False)
        if r.status_code == 200 and "Xiaomi Account - Tips" in r.text:
            r = self._req("GET", finish, allow_redirects=False)
        try:
            self._ssecurity = json.loads(r.headers.get("extension-pragma", "{}")).get("ssecurity")
        except ValueError:
            self._ssecurity = None
        sts = r.headers.get("Location")
        if not sts:
            m = re.search(r'https://sts\.api\.io\.mi\.com/sts[^"\']*', r.text)
            sts = m.group(0) if m else None
        if not (self._ssecurity and sts):
            raise LoginError("Xiaomi 2FA finished without a session (no ssecurity/sts link)")
        self._req("GET", sts)
        self._token = self._cookie("serviceToken")
        self._uid = self._cookie("userId")
        self._pass_token = self._cookie("passToken")
        if not self._token:
            raise LoginError("Xiaomi 2FA finished without a serviceToken")
        return "ok"

    # --- signed API call --------------------------------------------------
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
        r = self._req(
            "POST", f"https://{host}/app{path}", params=params,
            headers={"User-Agent": self._agent, "Accept-Encoding": "identity",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
                     "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4"},
            cookies={"userId": str(self._uid), "serviceToken": self._token,
                     "yetAnotherServiceToken": self._token, "locale": "en_GB",
                     "channel": "MI_APP_STORE"})
        if r.status_code != 200:
            return r.status_code, r.text[:200]
        try:
            return 200, json.loads(rc4(key, base64.b64decode(r.text)))
        except ValueError:
            return 200, r.text[:200]
