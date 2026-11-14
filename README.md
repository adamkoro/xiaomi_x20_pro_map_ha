# Xiaomi X20 Pro Map (Home Assistant)

> [!WARNING]
> This repo is **under development** and not ready for use. The code and docs were
> written by [Claude](https://claude.com/claude-code) (Anthropic's AI assistant).

A HACS custom integration that adds a **live map camera** for the Xiaomi Robot Vacuum
X20 Pro (`xiaomi.vacuum.d102gl`). It runs **on top of** the official Xiaomi Home
integration (`xiaomi_home`). It never logs in itself: it borrows `xiaomi_home`'s
cloud client (OAuth token, region, token refresh) and its device list.

Status: **scaffold, not yet tested on the real robot.** See [Open questions](#open-questions).

## Install (later)

HACS → Custom repositories → this repo (Integration). Restart, then
*Settings → Devices & services → Add → Xiaomi X20 Pro Map*, and pick the vacuum.
The `camera.<vacuum>_map` entity shows up on the existing Xiaomi Home device.

Self-check for the decrypt/render code (no HA needed):

```sh
uv run --no-project --with pillow --with cryptography python tests/test_map.py
```

## Research findings (2026-11-14)

### 1. MIoT spec for d102gl

`urn:miot-spec-v2:device:vacuum:0000A006:xiaomi-d102gl:1` (from
`miot-spec.org/miot-spec-v2/instance?type=…`) has a **`vacuum-map` service, siid 10**:

| iid | name | access | use |
|---|---|---|---|
| p10.1 | `map-obj-name` | read, notify | cloud object name of the current map blob |
| p10.2 | `trajectory-obj-name` | read, notify | cloud object name of the cleaning path |
| p10.3 | `clean-record` | read, notify | |
| p10.4 | `vacuum-position` | read, notify | robot position (string, format unknown) |
| p10.5 | `map-management` | read, notify | |
| p10.6 | `current-map-id` | read, notify | |
| p10.7 | `carpet-obj-name` | read, notify | carpet layer object |
| p10.9 | `map-3d-info` | read, notify | |
| p10.13 | `backup-map-list` | read, notify | |
| a10.1–a10.8 | clear / delete / set / save / auto-room-partition / set-name / **update-properties-data** / restore map | | |

Also siid 20 (`custom`): `a20.1 get-history-obj`, `a20.3 set-stop-upload-map`,
`p20.3 updata-difference`. And `p2.16 room-information` (rooms; already in HA).

There is **no "give me the map image" property**. Like every newer Xiaomi robot, the
robot uploads the map to Xiaomi's cloud file store (FDS) and publishes the object
name; the app mints a short-lived download URL for it.

### 2. Cloud file URL endpoint

All reference projects use the same call:

```
POST https://<region>.api.io.mi.com/app/v2/home/get_interim_file_url   (or …_pro)
     data={"obj_name": "<obj name>"}
→ {"result": {"url": "https://…signed FDS URL…"}}
```

Object name: `xiaomi-vac` and the Cloud Map Extractor guess `"<uid>/<did>/0"` (and
slot `1`); Dreame models read it from a property instead. On d102gl, `p10.1
map-obj-name` is the obvious source. Those projects call it through the **legacy Mi
account API** (serviceToken + ssecurity, RC4-signed).

### 3. How xiaomi-vac does it (letitbe-dull/xiaomi-vac, commit of 2026-10-28)

**Update to the brain note:** the current xiaomi-vac **lists `xiaomi.vacuum.d102gl`**
(SUPPORTED-MODELS.md; its README says zone cleaning was tested on a d102gl). For maps it
puts d102gl in the "xiaomi JSON-map" family (`map_parsers.py`,
`_XIAOMI_JSON_MAP_PROFILES`) "on spec-family grounds only"; the same family is
hardware-confirmed on d109gl and ov42gl. d106gl, by contrast, uses the ijai protobuf
path, so it is the wrong reference for d102gl.

Pipeline for this family:

1. `get_interim_file_url` with `obj_name = "<uid>/<did>/<slot>"` → signed URL.
2. Download → `{"version": 2, "data": "<base64>"}`.
3. Decrypt (`xiaomi_json_decrypt.py`, reverse-engineered from the app's RN plugin):
   `mk = model[-16:]`, `key = md5(AES-CBC(mk, IV="ABCDEF1234123412", pkcs7(mk + did)))`,
   then AES-CBC(key, same IV) → unpad → zlib → JSON.
   Only model and did are needed, no MAC or Wi-Fi SN.
4. JSON: `width`, `height`, `resolution` (mm/px), `origin_x/y` (mm), `pile_x/y` (dock,
   mm), `map_room_info`, and `map_data` = base64+zlib, **one byte per cell**, row 0 =
   origin_y. Cells: 0 unknown, 1–2 floor, 3–63 room grid id, >63 wall.

`custom_components/xiaomi_x20_map/map.py` is a port of steps 3–4 that uses only
`cryptography` + Pillow (both already in HA core), so the integration has no extra
requirements.

### 4. xiaomi_home internals we reuse

- `hass.data["xiaomi_home"]["miot_clients"][entry_id]` → `MIoTClient`.
- `client.device_list[did]` → `{did, uid, name, model, urn, …}`.
- `client.cloud_server` → `"de"`. `client.miot_http` → `MIoTHttpClient`: host
  `de.ha.api.io.mi.com`, header `Authorization: Bearer<token>`, `X-Client-BizId: haapi`,
  OAuth app id `2882303761520251711`. xiaomi_home refreshes the token itself.
- `miot_http.get_prop_async(did, 10, 1)` reads `map-obj-name` (public API).
- `miot_http._MIoTHttpClient__mihome_api_post_async(url_path, data)` sends any
  `/app/v2/...` call with that token (private, name-mangled; it may break when
  xiaomi_home updates).
- Device identifier `("xiaomi_home", f"{cloud_server}_{did}")`, so our camera
  attaches to the existing device.

### 5. Other references

- **Tasshack/dreame-vacuum**: d102gl unsupported (it is not a Dreame-protocol device).
  Useful only for the general `get_interim_file_url` + property object-name pattern.
- **Xiaomi Cloud Map Extractor**: legacy-login only, fails on new `xiaomi.vacuum.*`.
  Its `vacuum-map-parser-xiaomi` decrypt is broken for 20-char model names (see the
  xiaomi-vac notes).

## Live test results

### 2026-11-14, v0.1.0: `get_interim_file_url` is 404 on the HA OAuth host

- Reading `p10.1 map-obj-name` through xiaomi_home works: it returns `<uid>/<did>/0`,
  exactly the name xiaomi-vac guesses (so slot 0 is the current map).
- `POST https://de.ha.api.io.mi.com/app/v2/home/get_interim_file_url` with xiaomi_home's
  Bearer token returns **HTTP 404** for both `/0` and `/1`. A 404 is a "no such route"
  answer, not an auth or body error: the HA OAuth gateway doesn't expose this API
  (or not under this path).

### v0.1.1: probing other routes

The camera now sends the request itself (same host and headers as xiaomi_home) and
tries each route below with two body shapes (`{"obj_name": …}` and the legacy
`{"data": "{\"obj_name\": …}"}`), stopping at the first that returns a `url`.
A route that gives 404 is skipped for the other bodies and names.

| Route | Seen in |
|---|---|
| `/app/v2/home/get_interim_file_url` | xiaomi-vac, Cloud Map Extractor (v2 models) |
| `/app/v2/home/get_interim_file_url_pro` | xiaomi-vac (ijai), dreame-vacuum |
| `/app/home/getmapfileurl` | Cloud Map Extractor (Roborock) |
| `/app/home/getfileurl`, `/app/home/getfileurl_v3` | dreame-vacuum |

Every attempt (route, body, name, HTTP status, first 200 chars of the reply) is
listed in the camera's `attempts` attribute; the working one is shown in
`file_url_endpoint`. After a full failure it waits 10 minutes before probing again.

## Plan

1. ✅ Scaffold: config flow picks a xiaomi_home vacuum; a camera polls every 60 s:
   read p10.1 → try `get_interim_file_url` with p10.1, then `<uid>/<did>/0`, `/1` →
   download → decrypt → render rooms/walls (PNG). The `error`, `map_obj_name` and
   `obj_name_used` attributes show what happened.
2. Install on HA, look at the camera's attributes / log, and answer the open questions.
3. If the OAuth path is refused: fallbacks in order (a) the `_pro` endpoint,
   (b) a different body shape, (c) as a last resort, an optional legacy Mi-account
   login only for the file URL (this breaks the "no second login" goal, so ask first).
4. Once the map renders: robot position (p10.4), dock (`pile_x/y`), path (p10.2 object),
   room names (p2.16), faster polling while cleaning.

## Open questions

1. ~~Will `ha.api.io.mi.com` accept `/app/v2/home/get_interim_file_url` with
   xiaomi_home's OAuth token?~~ **No: HTTP 404** (2026-11-14). Does any other file-URL
   route work there (v0.1.1 probes them)? If none does, the only known path is the
   legacy Mi-account API, which means a second login.
2. Body shape on the OAuth API: `{"obj_name": …}` or the legacy
   `{"data": "{\"obj_name\":…}"}`? (Only matters once a route answers.)
3. ~~What does p10.1 hold?~~ `<uid>/<did>/0`, the FDS object name (2026-11-14).
4. Is the d102gl blob really the version-2 JSON format? (xiaomi-vac assumes so from the
   spec family; it is confirmed only on d109gl and ov42gl.)
5. Is the format of p10.4 `vacuum-position` the same as in d109gl?
6. Does the robot upload fresh maps while idle, or only while cleaning/mapping?
