"""Map camera. Borrows xiaomi_home's MIoTHttpClient (OAuth token, region, refresh)."""
import json
import logging
import time

from homeassistant.components.camera import Camera
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN
from .map import decrypt, render

_LOGGER = logging.getLogger(__name__)
REFRESH_S = 60  # ponytail: fixed poll; faster while cleaning once the fetch is proven
MAP_SIID, MAP_OBJ_PIID = 10, 1  # d102gl spec: vacuum-map / map-obj-name
FAIL_BACKOFF_S = 600  # all endpoints failed: don't re-probe every minute
# File-URL routes seen in xiaomi-vac, Cloud Map Extractor and dreame-vacuum, under the /app
# prefix every xiaomi_home path uses. Tried in order until one returns a url.
URL_PATHS = [
    "/app/v2/home/get_interim_file_url",
    "/app/v2/home/get_interim_file_url_pro",
    "/app/home/getmapfileurl",
    "/app/home/getfileurl",
    "/app/home/getfileurl_v3",
]
BODY_SHAPES = {
    "json": lambda n: {"obj_name": n},
    "data": lambda n: {"data": json.dumps({"obj_name": n})},  # legacy API's form-style wrapper
}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([MapCamera(hass, entry)])


class MapCamera(Camera):
    _attr_has_entity_name = True
    _attr_name = "Map"
    content_type = "image/png"

    def __init__(self, hass, entry):
        super().__init__()
        self._d = entry.data
        self._attr_unique_id = f"{entry.data['did']}_map"
        self._img: bytes | None = None
        self._last = 0.0
        self._found: tuple[str, str] | None = None  # (path, body shape) that worked
        self._attr_extra_state_attributes = {}

    def _client(self):
        # Look up every time: a xiaomi_home reload replaces the client object.
        return self.hass.data["xiaomi_home"]["miot_clients"][self._d["xiaomi_home_entry_id"]]

    @property
    def device_info(self) -> DeviceInfo:
        # Same identifier xiaomi_home uses, so the camera lands on the existing device.
        server = self._client().cloud_server
        return DeviceInfo(identifiers={("xiaomi_home", f"{server}_{self._d['did']}")})

    async def _post(self, http, path: str, body: dict) -> tuple[int, str]:
        # Same host + headers (Bearer token, app id) xiaomi_home itself sends; private attrs.
        headers = http._MIoTHttpClient__api_request_headers
        async with async_get_clientsession(self.hass).post(
                f"{http._base_url}{path}", json=body, headers=headers, timeout=15) as r:
            return r.status, (await r.text())[:200]

    async def _file_url(self, http, names) -> tuple[str, str, str]:
        """First (url, path, name) that works; every attempt is kept in the 'attempts' attribute."""
        if self._found:
            path, shape = self._found
            combos = [(path, shape)]
        else:
            combos = [(p, s) for p in URL_PATHS for s in BODY_SHAPES]
        attempts, dead = [], set()
        for path, shape in combos:
            if path in dead:
                continue
            for name in names:
                status, text = await self._post(http, path, BODY_SHAPES[shape](name))
                attempts.append(f"{path} [{shape}] {name}: {status} {text}")
                if status == 404:  # no such route: the body/name can't fix it
                    dead.add(path)
                    break
                try:
                    url = json.loads(text)["result"]["url"]
                except (ValueError, KeyError, TypeError):
                    continue
                self._found = (path, shape)
                self._attr_extra_state_attributes["file_url_endpoint"] = f"{path} [{shape}]"
                return url, path, name
        self._attr_extra_state_attributes["attempts"] = attempts
        self._found = None
        raise RuntimeError(f"no file URL endpoint worked ({len(attempts)} attempts, see 'attempts')")

    async def _fetch(self) -> bytes:
        http = self._client().miot_http
        did, uid = self._d["did"], self._d["uid"]
        obj = await http.get_prop_async(did, MAP_SIID, MAP_OBJ_PIID, immediately=True)
        self._attr_extra_state_attributes["map_obj_name"] = obj
        # p10.1 first; then the "<uid>/<did>/<slot>" names xiaomi-vac guesses.
        names = list(dict.fromkeys(c for c in (obj, f"{uid}/{did}/0", f"{uid}/{did}/1") if c))
        url, _, name = await self._file_url(http, names)
        async with async_get_clientsession(self.hass).get(url) as r:
            r.raise_for_status()
            raw = await r.read()
        data = decrypt(raw, self._d["model"], did)
        self._attr_extra_state_attributes["obj_name_used"] = name
        return await self.hass.async_add_executor_job(render, data)

    async def async_camera_image(self, width=None, height=None) -> bytes | None:
        if time.monotonic() - self._last > REFRESH_S:
            self._last = time.monotonic()
            try:
                self._img = await self._fetch()
                self._attr_extra_state_attributes.pop("error", None)
                self._attr_extra_state_attributes.pop("attempts", None)
            except Exception as ex:  # noqa: BLE001 - keep the last good image
                _LOGGER.warning("map fetch failed: %s", ex)
                self._attr_extra_state_attributes["error"] = str(ex)
                self._last += FAIL_BACKOFF_S - REFRESH_S
            self.async_write_ha_state()
        return self._img
