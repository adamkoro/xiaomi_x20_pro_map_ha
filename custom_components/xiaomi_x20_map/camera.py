"""Map camera. xiaomi_home gives the device and map object name; a legacy Mi-account
session (options flow) mints the file URL, which xiaomi_home's OAuth API can't."""
import logging
import time

from homeassistant.components.camera import Camera
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN
from .map import decrypt, render
from .micloud import CloudUnreachable, LoginError, MiCloud, VerificationRequired

_LOGGER = logging.getLogger(__name__)
REFRESH_S = 60  # ponytail: fixed poll; faster while cleaning once the fetch is proven
MAP_SIID, MAP_OBJ_PIID = 10, 1  # d102gl spec: vacuum-map / map-obj-name
FAIL_BACKOFF_S = 600  # all endpoints failed: don't re-probe every minute
# Legacy Mi-account API routes (https://<region>.api.io.mi.com/app<path>), tried in order.
# The same routes on xiaomi_home's OAuth host (de.ha.api.io.mi.com) all answer 404 (v0.1.1).
URL_PATHS = [
    "/v2/home/get_interim_file_url",      # xiaomi-vac: JSON-map family (d102gl)
    "/v2/home/get_interim_file_url_pro",  # xiaomi-vac: ijai
    "/home/getmapfileurl",                # Cloud Map Extractor: older Xiaomi/Roborock
]

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
        self._found: str | None = None  # URL path that worked
        opts = entry.options
        self._mi = MiCloud(opts["username"], opts["password"]) if opts.get("password") else None
        self._blocked: str | None = None  # login needs a human: stop retrying until reload
        self._attr_extra_state_attributes = {}

    def _client(self):
        # Look up every time: a xiaomi_home reload replaces the client object.
        return self.hass.data["xiaomi_home"]["miot_clients"][self._d["xiaomi_home_entry_id"]]

    @property
    def device_info(self) -> DeviceInfo:
        # Same identifier xiaomi_home uses, so the camera lands on the existing device.
        server = self._client().cloud_server
        return DeviceInfo(identifiers={("xiaomi_home", f"{server}_{self._d['did']}")})

    async def _login(self) -> None:
        if self._mi is None:
            raise RuntimeError("No Mi account set: Settings > Devices & services > "
                               "Xiaomi X20 Pro Map > Configure, enter it there")
        if self._blocked:
            raise RuntimeError(self._blocked)
        if self._mi.logged_in:
            return
        try:
            await self.hass.async_add_executor_job(self._mi.login)
        except CloudUnreachable:
            raise  # network: retry after the backoff
        except VerificationRequired as ex:
            self._attr_extra_state_attributes["verification_url"] = ex.url
            self._blocked = f"{ex} Then reload the Xiaomi X20 Pro Map integration."
            raise RuntimeError(self._blocked) from None
        except LoginError as ex:  # wrong password etc.: retrying only risks an account lock
            self._blocked = f"{ex}. Fix it under Configure, which reloads the integration."
            raise RuntimeError(self._blocked) from None
        self._attr_extra_state_attributes.pop("verification_url", None)

    async def _file_url(self, names) -> tuple[str, str]:
        """First (url, name) that works; every attempt is kept in the 'attempts' attribute."""
        await self._login()
        server = self._client().cloud_server
        attempts = []
        for path in [self._found] if self._found else URL_PATHS:
            for name in names:
                status, res = await self.hass.async_add_executor_job(
                    self._mi.call, server, path, {"obj_name": name})
                url = (res.get("result") or {}).get("url") if isinstance(res, dict) else None
                if url:
                    self._found = path
                    self._attr_extra_state_attributes["file_url_endpoint"] = f"{server} {path}"
                    return url, name
                if isinstance(res, dict):  # never echo a result: it may hold a signed URL
                    res = f"code {res.get('code')} {res.get('message', '')}"
                attempts.append(f"{path} {name}: {status} {res}")
                if status == 401:  # session expired: log in again next cycle
                    self._mi.logout()
                if status in (401, 404):
                    break
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
        url, name = await self._file_url(names)
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
