from __future__ import annotations

import asyncio
import logging

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CHANNEL, CONF_REOLINK_ENTRY_IDS, DEFAULT_CHANNEL, DOMAIN
from .talk_live_view import async_register_views

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["media_player"]

FRONTEND_URL = "/reolink_talk/reolink-talk-button.js"
FRONTEND_FILE = "frontend/reolink-talk-button.js"


async def _async_register_lovelace_resource(hass: HomeAssistant) -> None:
    """Register the element as a real Lovelace resource.

    add_extra_js_url() injects the script as a deferred module, which means a
    card can finish rendering before customElements.define() has run -- the
    card then reports "Custom element doesn't exist". Lovelace resources, by
    contrast, are awaited before dashboards render, so registering there
    removes the race entirely.
    """
    async with hass.data.setdefault(DOMAIN, {}).setdefault("resource_lock", asyncio.Lock()):
        try:
            lovelace = hass.data.get("lovelace")
            resources = getattr(lovelace, "resources", None)
            if resources is None:
                _LOGGER.warning("Lovelace resources unavailable; manually add %s as a module resource", FRONTEND_URL)
                return
            if not resources.loaded:
                await resources.async_load()
                resources.loaded = True
            for item in resources.async_items():
                if item.get("url", "").split("?")[0] == FRONTEND_URL:
                    return
            create = getattr(resources, "async_create_item", None)
            if create is None:
                _LOGGER.warning(
                    "YAML/read-only Lovelace resources: add to configuration.yaml: "
                    "lovelace: {resources: [{url: '%s', type: module}]}", FRONTEND_URL,
                )
                return
            await create({"res_type": "module", "url": FRONTEND_URL})
            _LOGGER.info("Registered Lovelace resource %s", FRONTEND_URL)
        except Exception as err:
            # Frontend enhancement must never prevent media players loading.
            _LOGGER.warning("Cannot register Lovelace resource (%s); manually add %s as a module resource", type(err).__name__, FRONTEND_URL)


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the Lovelace element from the integration itself.

    Registered once per HA start, so multiple config entries don't try to
    claim the same static path.
    """
    if hass.data.setdefault(DOMAIN, {}).get("frontend_registered"):
        await _async_register_lovelace_resource(hass)
        return

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                FRONTEND_URL,
                hass.config.path(f"custom_components/{DOMAIN}/{FRONTEND_FILE}"),
                True,
            )
        ]
    )
    await _async_register_lovelace_resource(hass)
    hass.data[DOMAIN]["frontend_registered"] = True
    if not hass.is_running:
        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

        async def register_when_started(_event):
            await _async_register_lovelace_resource(hass)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, register_when_started)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Default options: all current Reolink config entries, channel 0.
    if not entry.options:
        reolink_entry_ids = [e.entry_id for e in hass.config_entries.async_entries("reolink")]
        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_REOLINK_ENTRY_IDS: reolink_entry_ids,
                CONF_CHANNEL: DEFAULT_CHANNEL,
            },
        )
    else:
        reolink_entry_ids = entry.options.get(CONF_REOLINK_ENTRY_IDS) or [
            e.entry_id for e in hass.config_entries.async_entries("reolink")
        ]

    # Let talk_live_view discover cameras/channels from these same Reolink
    # entries (instead of a hardcoded host/channel map that would need
    # per-user source edits and get clobbered on every HACS update).
    hass.data.setdefault(DOMAIN, {})["reolink_entry_ids"] = reolink_entry_ids

    await _async_register_frontend(hass)
    async_register_views(hass)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
