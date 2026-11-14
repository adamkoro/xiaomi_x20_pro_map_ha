"""Map camera. Borrows xiaomi_home's MIoTHttpClient (OAuth token, region, refresh)."""
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
        self._attr_extra_state_attributes = {}

    def _client(self):
        # Look up every time: a xiaomi_home reload replaces the client object.
        return self.hass.data["xiaomi_home"]["miot_clients"][self._d["xiaomi_home_entry_id"]]

    @property
    def device_info(self) -> DeviceInfo:
        # Same identifier xiaomi_home uses, so the camera lands on the existing device.
        server = self._client().cloud_server
        return DeviceInfo(identifiers={("xiaomi_home", f"{server}_{self._d['did']}")})

    async def _file_url(self, http, obj_name: str) -> str | None:
        # Private helper of MIoTHttpClient: same host, headers and Bearer token xiaomi_home uses.
        # UNVERIFIED: whether the HA OAuth app may call this path, and the body shape.
        res = await http._MIoTHttpClient__mihome_api_post_async(
            url_path="/app/v2/home/get_interim_file_url", data={"obj_name": obj_name})
        return (res.get("result") or {}).get("url")

    async def _fetch(self) -> bytes:
        http = self._client().miot_http
        did, uid = self._d["did"], self._d["uid"]
        obj = await http.get_prop_async(did, MAP_SIID, MAP_OBJ_PIID, immediately=True)
        self._attr_extra_state_attributes["map_obj_name"] = obj
        # p10.1 first; then the "<uid>/<did>/<slot>" names xiaomi-vac guesses.
        candidates = dict.fromkeys(c for c in (obj, f"{uid}/{did}/0", f"{uid}/{did}/1") if c)
        errors = []
        for name in candidates:
            try:
                url = await self._file_url(http, name)
            except Exception as ex:  # noqa: BLE001 - try the next name, report all at the end
                errors.append(f"{name}: {ex}")
                continue
            if not url:
                errors.append(f"{name}: no url")
                continue
            async with async_get_clientsession(self.hass).get(url) as r:
                r.raise_for_status()
                raw = await r.read()
            data = decrypt(raw, self._d["model"], did)
            self._attr_extra_state_attributes["obj_name_used"] = name
            return await self.hass.async_add_executor_job(render, data)
        raise RuntimeError("; ".join(errors))

    async def async_camera_image(self, width=None, height=None) -> bytes | None:
        if time.monotonic() - self._last > REFRESH_S:
            self._last = time.monotonic()
            try:
                self._img = await self._fetch()
                self._attr_extra_state_attributes.pop("error", None)
            except Exception as ex:  # noqa: BLE001 - keep the last good image
                _LOGGER.warning("map fetch failed: %s", ex)
                self._attr_extra_state_attributes["error"] = str(ex)
            self.async_write_ha_state()
        return self._img
