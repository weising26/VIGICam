"""Set up, unload and remove a VIGI NVR config entry.

An NVR entry is a separate path through the integration: one NVR device, one child
device per channel, all reached through the NVR's OpenAPI. The camera code paths are
untouched. Platform modules hand NVR entries to nvr_entities.py.
"""
from __future__ import annotations

import logging

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .const import (
    BRAND,
    CONF_DEVICE_TYPE,
    CONF_NVR_API,
    CONF_NVR_EVENT_PUSH,
    CONF_WEBHOOK_ID,
    DEFAULT_NVR_EVENT_PUSH,
    DEVICE_TYPE_NVR,
    DOMAIN,
    NVR_API_OPENAPI,
    NVR_API_WEB,
    REPAIRS_NVR_EVENT_SERVER,
)
from .api import VIGIAuthError, VIGIError
from .nvr_relay import async_register_relay, new_token
from .nvr_web import VIGINVRWeb
from .nvr_api import VIGINVR, VIGINVRAuthError, VIGINVRError
from .nvr_coordinator import VIGINVRCoordinator
from .nvr_events import (
    async_configure_event_server,
    async_register_webhook,
    async_remove_event_server,
)

_LOGGER = logging.getLogger(__name__)

NVR_PLATFORMS = [
    Platform.CAMERA,
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
]


def is_nvr_entry(entry: ConfigEntry) -> bool:
    return entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_NVR


async def async_setup_nvr_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host = entry.data[CONF_HOST]
    name = entry.data.get(CONF_NAME) or entry.title or host
    if entry.data.get(CONF_NVR_API) == NVR_API_WEB:
        return await _async_setup_web(hass, entry, host, name)

    api = VIGINVR(host, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    entry.async_on_unload(api.close)

    try:
        await api.login()
        modules = await api.module_list()
    except VIGINVRAuthError as exc:
        raise ConfigEntryAuthFailed(str(exc)) from exc
    except (VIGINVRError, aiohttp.ClientError, TimeoutError) as exc:
        raise ConfigEntryNotReady(f"{type(exc).__name__}: {exc}") from exc
    _LOGGER.debug("NVR %s modules: %s", host, modules)

    coordinator = VIGINVRCoordinator(hass, api, modules, name)
    await coordinator.async_config_entry_first_refresh()

    events_on = entry.options.get(CONF_NVR_EVENT_PUSH, DEFAULT_NVR_EVENT_PUSH)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "device_type": DEVICE_TYPE_NVR,
        "api": api,
        "coordinator": coordinator,
        "modules": modules,
        "name": name,
        "ip": host,
        "events": events_on,
        "backend": NVR_API_OPENAPI,
        "info": {},
    }

    if events_on:
        async_register_webhook(hass, entry, name)
        await async_configure_event_server(hass, entry, api, name)
    else:
        ir.async_delete_issue(hass, DOMAIN, f"{REPAIRS_NVR_EVENT_SERVER}_{entry.entry_id}")

    await _async_finish_setup(hass, entry, coordinator, host)
    return True


async def _async_finish_setup(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: VIGINVRCoordinator, host: str
) -> None:
    # Register the NVR device before any entity names it as via_device: newer Home
    # Assistant refuses a via_device link to a device that doesn't exist yet, which
    # dropped every channel's camera except whichever loaded after the NVR device.
    data = hass.data[DOMAIN][entry.entry_id]
    info = data.get("info") or {}
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"nvr_{entry.entry_id}")},
        name=data["name"],
        manufacturer=BRAND,
        model=info.get("device_type") or "VIGI NVR",
        sw_version=(info.get("firm_ver") or "").split(" Build")[0] or None,
        serial_number=info.get("serial_number"),
        configuration_url=f"https://{host}",
    )
    await hass.config_entries.async_forward_entry_setups(entry, NVR_PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Entities are built per channel at setup, so a camera added to or removed from the
    # NVR needs a reload to appear or go.
    known = set((coordinator.data or {}).get("devices") or {})

    @callback
    def _on_update() -> None:
        now = set((coordinator.data or {}).get("devices") or {})
        if coordinator.last_update_success and now and now != known:
            _LOGGER.info("NVR %s: channels changed (%s -> %s), reloading", host, sorted(known), sorted(now))
            known.clear()
            known.update(now)
            hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(coordinator.async_add_listener(_on_update))


async def _async_setup_web(
    hass: HomeAssistant, entry: ConfigEntry, host: str, name: str
) -> bool:
    """NVR without OpenAPI: its web API on 443 and stream daemon on 8443."""
    api = VIGINVRWeb(host, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    entry.async_on_unload(api.close)
    try:
        await api.login()
        info = await api.system_info()
    except VIGIAuthError as exc:
        raise ConfigEntryAuthFailed(str(exc)) from exc
    except (VIGIError, aiohttp.ClientError, TimeoutError) as exc:
        raise ConfigEntryNotReady(f"{type(exc).__name__}: {exc}") from exc

    # Only the channel list is polled; the OpenAPI-only entities are never created.
    coordinator = VIGINVRCoordinator(hass, api, {"channel_management": 1}, name)
    await coordinator.async_config_entry_first_refresh()

    async_register_relay(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "device_type": DEVICE_TYPE_NVR,
        "api": api,
        "coordinator": coordinator,
        "modules": coordinator.modules,
        "name": name,
        "ip": host,
        "events": False,
        "backend": NVR_API_WEB,
        "info": info,
        "relay_token": new_token(),
        "resolution": "hd",
    }
    ir.async_delete_issue(hass, DOMAIN, f"{REPAIRS_NVR_EVENT_SERVER}_{entry.entry_id}")
    await _async_finish_setup(hass, entry, coordinator, host)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_nvr_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, NVR_PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def async_remove_nvr_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give the event-server slot back to the NVR, and clear this entry's repairs."""
    ir.async_delete_issue(hass, DOMAIN, f"{REPAIRS_NVR_EVENT_SERVER}_{entry.entry_id}")
    if not entry.data.get(CONF_WEBHOOK_ID) or entry.data.get(CONF_NVR_API) == NVR_API_WEB:
        return
    api = VIGINVR(entry.data[CONF_HOST], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    try:
        await async_remove_event_server(api, entry.data[CONF_WEBHOOK_ID])
    finally:
        await api.close()
