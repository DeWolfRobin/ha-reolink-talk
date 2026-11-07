from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CHANNEL,
    CONF_REOLINK_ENTRY_IDS,
    CONF_VERIFY_SSL,
    DEFAULT_CHANNEL,
    DOMAIN,
)

from .talk import PlaybackOptions


class ReolinkTalkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        # One-click setup: we don't ask for credentials; we piggyback on the official
        # Reolink integration config entries.
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_create_entry(title="Reolink Talk", data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return ReolinkTalkOptionsFlowHandler(config_entry)


class ReolinkTalkOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        hass = self.hass
        reolink_entries = hass.config_entries.async_entries("reolink")
        entry_map = {e.entry_id: e.title for e in reolink_entries}

        errors = {}
        if user_input is not None:
            try:
                PlaybackOptions.from_options(user_input)
            except (ValueError, TypeError):
                errors["base"] = "invalid_diagnostics"
            else:
                return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_REOLINK_ENTRY_IDS,
                    default=self._config_entry.options.get(
                        CONF_REOLINK_ENTRY_IDS, list(entry_map.keys())
                    ),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=eid, label=title)
                            for eid, title in entry_map.items()
                        ],
                        multiple=True,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_VERIFY_SSL,
                    default=self._config_entry.options.get(CONF_VERIFY_SSL, True),
                ): bool,
                vol.Optional(
                    CONF_CHANNEL,
                    default=self._config_entry.options.get(
                        CONF_CHANNEL, DEFAULT_CHANNEL
                    ),
                ): vol.Coerce(int),
            }
        )

        # File-playback diagnostics only. Defaults intentionally preserve behavior.
        validators = {
            "blocks_per_packet": vol.All(vol.Coerce(int), vol.Range(min=1, max=16)),
            "initial_buffer_ms": vol.All(vol.Coerce(int), vol.Range(min=0, max=500)),
            "pacing_factor": vol.All(vol.Coerce(float), vol.Range(min=0.9, max=1.1)),
            "pacing_mode": vol.In(["deadline", "interval"]),
            "final_drain_ms": vol.All(vol.Coerce(int), vol.Range(min=0, max=2000)),
            "wait_for_ack": bool,
            "logging": bool,
            "attenuation_db": vol.All(vol.Coerce(float), vol.Range(min=-18, max=0)),
        }
        schema = schema.extend(
            {
                vol.Optional(
                    f"diagnostic_{key}",
                    default=self._config_entry.options.get(
                        f"diagnostic_{key}", field.default
                    ),
                ): validators[key]
                for key, field in PlaybackOptions.__dataclass_fields__.items()
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
