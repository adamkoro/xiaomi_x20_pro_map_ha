"""Pick one vacuum from an already set-up xiaomi_home entry. No login of our own."""
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from . import DOMAIN
from .micloud import CloudUnreachable, LoginError, MiCloud, VerificationRequired


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
    """Legacy Mi-account login, used only to get the map file URL. Checked by logging in."""

    async def async_step_init(self, user_input=None):
        errors, info = {}, ""
        if user_input is not None:
            mi = MiCloud(user_input["username"], user_input["password"])
            try:
                await self.hass.async_add_executor_job(mi.login)
            except VerificationRequired as ex:
                errors["base"] = "verify"
                info = f"**{ex}**\n\n{ex.url or ''}"
            except CloudUnreachable:
                errors["base"] = "cannot_connect"
            except LoginError as ex:
                errors["base"] = "invalid_auth"
                info = f"**{ex}**"
            else:
                return self.async_create_entry(data=user_input)

        old = user_input or self.config_entry.options
        return self.async_show_form(
            step_id="init", errors=errors, description_placeholders={"info": info},
            data_schema=vol.Schema({
                vol.Required("username", default=old.get("username", "")): str,
                vol.Required("password"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }),
        )
