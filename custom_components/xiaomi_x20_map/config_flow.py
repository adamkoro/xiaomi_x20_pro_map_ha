"""Pick one vacuum from an already set-up xiaomi_home entry. No login of our own."""
import voluptuous as vol
from homeassistant import config_entries

from . import DOMAIN


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
