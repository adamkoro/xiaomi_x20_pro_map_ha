# Xiaomi X20 Pro Map (Home Assistant)

> [!WARNING]
> This repo is **under development** and not ready for use. The code and docs were
> written by [Claude](https://claude.com/claude-code) (Anthropic's AI assistant).

A HACS custom integration that adds a **live map camera** for the Xiaomi Robot Vacuum
X20 Pro (`xiaomi.vacuum.d102gl`). It runs **on top of** the official Xiaomi Home
integration (`xiaomi_home`): the device, region and map object name come from
`xiaomi_home`. Xiaomi Home's OAuth API can't hand out the map download link (see
[Live test results](#live-test-results)), so that **one** call uses a second, legacy
Mi-account login that you enter in the integration's options.

Status: **in testing, no map rendered on the real robot yet.** See [Open questions](#open-questions).

## Install

1. HACS → Custom repositories → this repo (Integration), or copy
   `custom_components/xiaomi_x20_map/` into `/config/custom_components/`. Restart HA.
2. *Settings → Devices & services → Add integration → Xiaomi X20 Pro Map*, pick the vacuum.
   The camera (`camera.<vacuum>_map`) shows up on the existing Xiaomi Home device.
3. On that integration's card, choose **Configure** and enter your Mi account
   (email, phone or numeric ID) and password. Saving logs in once to check it, and
   the integration reloads.

### Mi-account login

- Everything happens inside the Configure dialog, no browser detours:
  1. Mi account + password.
  2. **Captcha** (if Xiaomi asks): the image is shown right in the dialog; type the
     characters. A wrong code shows a fresh image.
  3. **Verification code** (if Xiaomi asks for 2FA): Xiaomi e-mails the code (SMS if
     the account has no e-mail; that path is unverified), you type it in.
- The captcha and the 2FA ticket are bound to the login's own cookies (`ick`), so a
  captcha opened in a browser, or a browser sign-in, does **not** clear them. That's
  why the dialog fetches the image itself and keeps one session across the steps.
- Saved in this integration's config entry (`.storage/core.config_entries`): the
  account, the password and the resulting session (`ssecurity`, `serviceToken`,
  `passToken`). Never logged, never put in attributes or error texts.
- The camera only uses the saved session. When it expires it renews it with the
  `passToken` (no password, no captcha). If that fails, it stops and its `error` says
  to log in again under Configure. It never tries a password login on its own, so it
  can't hit a captcha nobody sees or lock the account.

### Self-checks (no HA needed)

```sh
uv run --no-project --with pillow --with cryptography python tests/test_map.py
uv run --no-project --with requests python tests/test_micloud.py
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

### 2026-11-14, v0.1.1: no file-URL route on the OAuth host

All five routes above returned **404** on `de.ha.api.io.mi.com`. The HA OAuth gateway
simply doesn't expose file URLs. Decision (user approved): an optional legacy
Mi-account login only for this call.

### v0.2.0: legacy Mi-account API for the file URL

- `micloud.py`: password login via `account.xiaomi.com/pass/serviceLogin` +
  `serviceLoginAuth2` (sid `xiaomiio`) → `ssecurity`, `serviceToken`; signed RC4
  requests to `https://<region>.api.io.mi.com/app<path>` with `data={"obj_name": …}`.
  Ported from Cloud Map Extractor / xiaomi-vac; RC4 in plain Python, no new requirements.
- Region comes from xiaomi_home (`de`). Routes tried in order:
  `/v2/home/get_interim_file_url` (xiaomi-vac's choice for d102gl's family),
  `/v2/home/get_interim_file_url_pro`, `/home/getmapfileurl`.
- Camera attributes: `attempts` (route, name, HTTP status, Xiaomi's code/message;
  never a returned URL), `file_url_endpoint` (region + route that worked),
  `error`, `map_obj_name`, `obj_name_used`.

### 2026-11-14, v0.2.0: captcha can't be solved in a browser

Xiaomi answered the login with a captcha (`code 87001`, a `/pass/getCode` URL). Opening
that URL in a browser only downloads the image (a JPEG with no extension), and a
browser sign-in doesn't help either: the captcha belongs to HA's own login session.

### v0.2.1: captcha and 2FA inside the options flow

- `micloud.py` is a state machine (`begin_login` / `submit_captcha` / `submit_2fa` →
  `ok` | `captcha` | `2fa`). The options flow keeps one `MiCloud` (one
  `requests.Session`) across its steps.
- Captcha: fetched with that session, shown as a `data:` URI image in the step
  description, `captCode` sent back to `serviceLoginAuth2`. 87001 again = wrong code,
  new image.
- 2FA: `identity/auth/sendEmailTicket` (then `sendPhoneTicket`), code step,
  `verifyEmail`/`verifyPhone`, then the finish redirects to the STS link
  (ported from xiaomi-vac).
- The session is saved in the entry; the camera renews it with the `passToken`.

### 2026-11-15, v0.2.1: the map renders

The captcha step worked, and the map now renders on HA. Working route:
`file_url_endpoint = "de /v2/home/get_interim_file_url_pro"` (not the plain
`get_interim_file_url` xiaomi-vac assumed for this family). The blob is the version-2
JSON format, so open question 4 is answered.

### v0.3.0: dock, live robot, cleaning path

- **Dock**: `have_pile` + `pile_x/pile_y` (mm) from the map JSON, heading tick from
  `pile_yaw`; same transform as the rooms:
  `col = (x − origin_x) / resolution`, `row = height − (y − origin_y) / resolution`
  (north up), minus the crop, times the scale.
- **Cleaning path**: `paths.points[]` (`{x, y}` mm) from the same map JSON, drawn
  into the base image. That's the path as of the last map upload; the separate
  `p10.2 trajectory-obj-name` object is not used yet.
- **Robot**: `p10.4 vacuum-position`, read with xiaomi_home's cloud client (one
  `prop/get` for p10.1 + p10.4) and also through xiaomi_home's property push
  (`MIoTDevice.sub_property`, which keeps xiaomi_home's own handler). The d102gl format is
  unknown, so `parse_position` accepts JSON/dict `{x, y, yaw|phi|angle|…}` (also nested
  under `position`/`pos`), Dreame-style `{robotPos: [x, y], robotPhi}`, a list
  `[x, y(, a)]` or `"x,y(,a)"` text, in map millimetres. Angles: radians, centi-degrees
  or degrees (the vacuum_map_parser_xiaomi heuristic, but kept to 0–360 so the heading
  keeps its direction). If p10.4 can't be read, the robot pose saved in the map file
  (`position`) is drawn instead.
- **Live** (background tick every 2 s, cheap when nothing is due):
  - p10.1 + p10.4 every **4 s** while xiaomi_home's vacuum entity is `cleaning` or
    `returning`, every **5 min** otherwise.
  - Map file: re-downloaded when p10.1 changes, right after a clean finishes, every
    **2 min** while cleaning (so the path grows) and every **10 min** otherwise.
  - Each position change only stamps the robot onto the cached base image (PNG at
    `compress_level=1`). `async_camera_image` just returns the last frame, and
    `frame_interval = 2 s` for the MJPEG "live" camera view.
- New attributes: `robot_position_raw` (the p10.4 value as received),
  `robot_position` (`{x, y, yaw}` or `"unparsed"`), `live`, `position_error`.
  `robot_position*` and `attempts` are kept out of the recorder.

## Plan

1. ✅ Scaffold: config flow picks a xiaomi_home vacuum; a camera polls every 60 s:
   read p10.1 → try `get_interim_file_url` with p10.1, then `<uid>/<did>/0`, `/1` →
   download → decrypt → render rooms/walls (PNG). The `error`, `map_obj_name` and
   `obj_name_used` attributes show what happened.
2. Install on HA, look at the camera's attributes / log, and answer the open questions.
3. ✅ OAuth path refused (404 on every route) → optional legacy Mi-account login
   only for the file URL (v0.2.0).
4. ✅ Session saved in the entry, renewed with the `passToken` (v0.2.1). The renewed
   token isn't written back yet (memory only); write it back if renewals start failing.
5. ✅ Dock, live robot (p10.4), path from the map JSON, faster polling while cleaning
   (v0.3.0). Confirm the p10.4 format and units on the real robot.
6. Next: room names (p2.16), the live trajectory object (p10.2) if the map-file path
   lags too much.

## Open questions

1. ~~Will `ha.api.io.mi.com` accept `/app/v2/home/get_interim_file_url` with
   xiaomi_home's OAuth token?~~ **No: HTTP 404** (2026-11-14). Does any other file-URL
   route work there (v0.1.1 probes them)? If none does, the only known path is the
   legacy Mi-account API, which means a second login.~~ None did (all 404), so v0.2.0
   uses the legacy API.
2. Which legacy route answers for d102gl: `get_interim_file_url`, `_pro` or
   `getmapfileurl`? (v0.2.0 tries all three; `file_url_endpoint` will tell.)
3. ~~What does p10.1 hold?~~ `<uid>/<did>/0`, the FDS object name (2026-11-14).
4. ~~Is the d102gl blob really the version-2 JSON format?~~ Yes (2026-11-15).
5. What is the format of p10.4 `vacuum-position` on d102gl (keys, units, angle), and
   does xiaomi_home push it, or only answer polls? (`robot_position_raw` will tell.)
6. Does the robot upload fresh maps while idle, or only while cleaning/mapping?
