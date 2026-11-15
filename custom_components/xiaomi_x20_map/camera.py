"""Map camera. xiaomi_home gives the device and map object name; a legacy Mi-account
session (options flow) mints the file URL, which xiaomi_home's OAuth API can't."""
import logging
import time
from datetime import timedelta

from homeassistant.components.camera import Camera
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .map import MapImage, decrypt, parse_position
from .micloud import MiCloud

_LOGGER = logging.getLogger(__name__)
MAP_SIID, MAP_OBJ_PIID, POS_PIID = 10, 1, 4  # d102gl: vacuum-map / map-obj-name / vacuum-position
TICK = timedelta(seconds=2)
MAP_REFRESH_S = 600         # docked/idle: re-download the map file this often
MAP_REFRESH_ACTIVE_S = 120  # cleaning: the map and the path grow
POS_ACTIVE_S, POS_IDLE_S = 4, 300  # p10.4 poll while cleaning/returning, else
ACTIVE_STATES = {"cleaning", "returning"}
FAIL_BACKOFF_S = 600  # map download failed: don't re-probe every minute
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
    _attr_frame_interval = 2  # MJPEG ("live" view): resend the cached frame every 2 s
    # Change every few seconds while cleaning: keep them out of the recorder.
    _unrecorded_attributes = frozenset({"robot_position_raw", "robot_position", "attempts"})
    content_type = "image/png"

    def __init__(self, hass, entry):
        super().__init__()
        self._d = entry.data
        self._attr_unique_id = f"{entry.data['did']}_map"
        self._img: bytes | None = None
        self._map: MapImage | None = None
        self._robot = None
        self._obj: str | None = None  # p10.1, last seen
        self._map_due = self._pos_due = 0.0  # monotonic deadlines
        self._was_active = False
        self._busy = False
        self._vacuum: str | None = None  # xiaomi_home's vacuum entity_id
        self._found: str | None = None  # URL path that worked
        self._mi: MiCloud | None = None
        if sess := entry.options.get("session"):  # saved by the options flow's login
            self._mi = MiCloud()
            self._mi.restore(sess)
        self._blocked: str | None = None  # needs a new login via Configure: stop retrying
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
                               "Xiaomi X20 Pro Map > Configure, log in there")
        if self._blocked:
            raise RuntimeError(self._blocked)
        if self._mi.logged_in:
            return
        # Expired session: renew with the passToken. A password login could need a captcha
        # or 2FA, which only the options flow can show, so never try one here.
        # ponytail: the renewed session lives in memory only; after a restart the saved
        # passToken renews it again.
        if not await self.hass.async_add_executor_job(self._mi.refresh):
            self._blocked = ("Mi session expired and could not be renewed: open Configure on "
                             "Xiaomi X20 Pro Map and log in again")
            raise RuntimeError(self._blocked)

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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        did, entry_id = self._d["did"], self._d["xiaomi_home_entry_id"]
        dev = dr.async_get(self.hass).async_get_device(
            identifiers={("xiaomi_home", f"{self._client().cloud_server}_{did}")})
        if dev:
            self._vacuum = next((e.entity_id for e in er.async_entries_for_device(
                er.async_get(self.hass), dev.id) if e.domain == "vacuum"), None)
        # Push updates, if xiaomi_home's cloud MQTT forwards p10.4. MIoTDevice.sub_property
        # keeps a handler list, so this doesn't replace xiaomi_home's own subscriber.
        # ponytail: lost if xiaomi_home reloads (new device objects); polling still runs.
        miot_dev = next((d for d in self.hass.data["xiaomi_home"]["devices"].get(entry_id, [])
                         if d.did == did), None)
        if miot_dev:
            sub = miot_dev.sub_property(self._on_push, MAP_SIID, POS_PIID)
            self.async_on_remove(lambda: miot_dev.unsub_property(MAP_SIID, POS_PIID, sub))
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, TICK))
        self.hass.async_create_task(self._tick())

    def _on_push(self, params: dict, _ctx) -> None:
        self.hass.add_job(self._push_position, params.get("value"))

    async def _push_position(self, raw) -> None:
        if self._set_robot(raw):
            await self._redraw()
            self.async_write_ha_state()

    def _set_robot(self, raw) -> bool:
        """Store a p10.4 value; True if it changed."""
        attrs = self._attr_extra_state_attributes
        if raw is None or raw == attrs.get("robot_position_raw"):
            return False
        attrs["robot_position_raw"] = raw
        self._robot = parse_position(raw)
        attrs["robot_position"] = (dict(zip(("x", "y", "yaw"), self._robot))
                                   if self._robot else "unparsed")
        return True

    async def _poll_props(self) -> bool:
        """Read p10.1 + p10.4 in one request. True if the robot moved."""
        did = self._d["did"]
        try:
            res = await self._client().miot_http.get_props_async([
                {"did": did, "siid": MAP_SIID, "piid": MAP_OBJ_PIID},
                {"did": did, "siid": MAP_SIID, "piid": POS_PIID}])
        except Exception as ex:  # noqa: BLE001 - next poll tries again
            self._attr_extra_state_attributes["position_error"] = str(ex)
            return False
        self._attr_extra_state_attributes.pop("position_error", None)
        vals = {r.get("piid"): r.get("value") for r in res or [] if r.get("code", 0) == 0}
        if (obj := vals.get(MAP_OBJ_PIID)) and obj != self._obj:
            if self._obj is not None:
                self._map_due = 0  # robot uploaded a different map object
            self._obj = obj
            self._attr_extra_state_attributes["map_obj_name"] = obj
        return self._set_robot(vals.get(POS_PIID))

    async def _fetch_map(self) -> None:
        did, uid = self._d["did"], self._d["uid"]
        # p10.1 first; then the "<uid>/<did>/<slot>" names xiaomi-vac guesses.
        names = list(dict.fromkeys(c for c in (self._obj, f"{uid}/{did}/0", f"{uid}/{did}/1") if c))
        url, name = await self._file_url(names)
        async with async_get_clientsession(self.hass).get(url) as r:
            r.raise_for_status()
            raw = await r.read()
        data = decrypt(raw, self._d["model"], did)
        self._map = await self.hass.async_add_executor_job(MapImage, data)
        self._attr_extra_state_attributes["obj_name_used"] = name

    async def _redraw(self) -> None:
        if self._map:
            self._img = await self.hass.async_add_executor_job(self._map.png, self._robot)

    async def _tick(self, _now=None) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            now = time.monotonic()
            state = self.hass.states.get(self._vacuum) if self._vacuum else None
            active = bool(state and state.state in ACTIVE_STATES)
            if active != self._was_active:
                self._pos_due = 0  # switch poll rate now
                if self._was_active:
                    self._map_due = 0  # clean finished: fetch the final map + path
            self._was_active = active
            self._attr_extra_state_attributes["live"] = active

            changed = False
            if now >= self._pos_due:
                self._pos_due = now + (POS_ACTIVE_S if active else POS_IDLE_S)
                changed = await self._poll_props()
            if now >= self._map_due:
                self._map_due = now + (MAP_REFRESH_ACTIVE_S if active else MAP_REFRESH_S)
                try:
                    await self._fetch_map()
                    self._attr_extra_state_attributes.pop("error", None)
                    self._attr_extra_state_attributes.pop("attempts", None)
                except Exception as ex:  # noqa: BLE001 - keep the last good map
                    _LOGGER.warning("map fetch failed: %s", ex)
                    self._attr_extra_state_attributes["error"] = str(ex)
                    self._map_due = now + FAIL_BACKOFF_S
                changed = True
            if changed:
                await self._redraw()
                self.async_write_ha_state()
        finally:
            self._busy = False

    async def async_camera_image(self, width=None, height=None) -> bytes | None:
        return self._img  # drawn by _tick / pushes; serving it must stay cheap
