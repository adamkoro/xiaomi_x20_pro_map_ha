"""Pick one vacuum from an already set-up xiaomi_home entry. No login of our own."""
import base64

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from . import DOMAIN
from .micloud import CloudUnreachable, LoginError, MiCloud, image_type


def _vacuums(hass) -> dict[str, tuple[str, dict]]:
    """did -> (xiaomi_home entry_id, cloud device info) for every vacuum."""
    clients = hass.data.get("xiaomi_home", {}).get("miot_clients", {})
    return {
        did: (entry_id, dev)
        for entry_id, client in clients.items()
        for did, dev in client.device_list.items()
        if dev.get("model", "").startswith("xiaomi.vacuum.")
    }


class XiaomiX20MapFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return MiAccountOptions()

    async def async_step_user(self, user_input=None):
        if "xiaomi_home" not in self.hass.data:
            return self.async_abort(reason="no_xiaomi_home")
        vacs = _vacuums(self.hass)
        if not vacs:
            return self.async_abort(reason="no_vacuums")

        if user_input is not None:
            did = user_input["did"]
            await self.async_set_unique_id(did)
            self._abort_if_unique_id_configured()
            entry_id, dev = vacs[did]
            return self.async_create_entry(
                title=f"{dev['name']} map",
                data={"xiaomi_home_entry_id": entry_id, "did": did,
                      "model": dev["model"], "uid": dev.get("uid")},
            )

        choices = {did: f"{dev['name']} ({dev['model']})" for did, (_, dev) in vacs.items()}
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required("did"): vol.In(choices)})
        )


class MiAccountOptions(config_entries.OptionsFlow):
    """Legacy Mi-account login, used only to get the map file URL.

    init (user + password) -> [captcha] -> [2fa] -> saves the session. One MiCloud object
    lives across the steps: Xiaomi binds the captcha and the 2FA ticket to its cookies.
    """

    def __init__(self):
        self._mi: MiCloud | None = None
        self._creds: dict = {}

    async def _run(self, step_id: str, fn, *args):
        """Run one login step in the executor and show whatever Xiaomi wants next."""
        try:
            state = await self.hass.async_add_executor_job(fn, *args)
        except CloudUnreachable:
            return self._form(step_id, {"base": "cannot_connect"})
        except LoginError as ex:  # refused: back to the credentials form with Xiaomi's reason
            return self._form("init", {"base": "invalid_auth"}, info=f"**{ex}**")
        if state == "ok":
            return self.async_create_entry(data={**self._creds, "session": self._mi.session()})
        nxt = "captcha" if state == "captcha" else "twofa"
        # Same step again after a code was typed = that code was wrong (captcha: new image).
        return self._form(nxt, {"base": "wrong_code"} if nxt == step_id else {})

    def _form(self, step_id: str, errors: dict, info: str = ""):
        if step_id == "captcha":
            img = self._mi.captcha_image or b""
            uri = f"data:{image_type(img)};base64,{base64.b64encode(img).decode()}"
            ph, schema = {"image": f"![captcha]({uri})"}, {vol.Required("code"): str}
        elif step_id == "twofa":
            ph, schema = {"channel": self._mi.twofa_channel}, {vol.Required("code"): str}
        else:
            ph = {"info": info}
            schema = {
                vol.Required("username", default=self._creds.get(
                    "username", self.config_entry.options.get("username", ""))): str,
                vol.Required("password"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema(schema),
                                    errors=errors, description_placeholders=ph)

    async def async_step_init(self, user_input=None):
        if user_input is None:
            return self._form("init", {})
        self._creds = {"username": user_input["username"], "password": user_input["password"]}
        self._mi = MiCloud(**self._creds)
        return await self._run("init", self._mi.begin_login)

    async def async_step_captcha(self, user_input=None):
        return await self._run("captcha", self._mi.submit_captcha, user_input["code"])

    async def async_step_twofa(self, user_input=None):
        return await self._run("twofa", self._mi.submit_2fa, user_input["code"])
