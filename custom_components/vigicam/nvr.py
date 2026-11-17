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
from .nvr_coordinator import VIGINVRCoordinator, channel_online
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
    Platform.SELECT,
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
    # Devices are created here rather than from the entities' DeviceInfo. Home Assistant
    # 2026.11 deprecates DeviceInfo's via_device, and for camera entities that stops the
    # entity loading at all, so the channel-to-NVR link is set with via_device_id, which
    # every supported version accepts through async_update_device.
    data = hass.data[DOMAIN][entry.entry_id]
    info = data.get("info") or {}
    dev_reg = dr.async_get(hass)
    nvr_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"nvr_{entry.entry_id}")},
        name=data["name"],
        manufacturer=BRAND,
        model=info.get("device_type") or "VIGI NVR",
        sw_version=(info.get("firm_ver") or "").split(" Build")[0] or None,
        serial_number=info.get("serial_number"),
        configuration_url=f"https://{host}",
    )
    for ch, dev in sorted(((coordinator.data or {}).get("devices") or {}).items()):
        channel_device = dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"nvr_{entry.entry_id}_ch{ch}")},
            name=dev.get("name") or dev.get("alias") or f"Channel {ch}",
            manufacturer=BRAND,
            model=f"NVR channel {ch}",
            sw_version=str(dev.get("firmware") or "").split(" Build")[0] or None,
            configuration_url=f"http://{dev['ip']}" if dev.get("ip") else None,
        )
        if channel_device.via_device_id != nvr_device.id:
            dev_reg.async_update_device(channel_device.id, via_device_id=nvr_device.id)
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
    ptz = await _async_read_ptz(api, coordinator)
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
        "ptz": ptz,
    }
    ir.async_delete_issue(hass, DOMAIN, f"{REPAIRS_NVR_EVENT_SERVER}_{entry.entry_id}")
    await _async_finish_setup(hass, entry, coordinator, host)
    return True


async def _async_read_ptz(api: VIGINVRWeb, coordinator: VIGINVRCoordinator) -> dict[int, dict]:
    """{channel: {"capability", "presets"}} for channels whose camera can pan and tilt.

    A camera without PTZ answers with an empty capability block.
    """
    out: dict[int, dict] = {}
    for ch, dev in sorted(((coordinator.data or {}).get("devices") or {}).items()):
        if not channel_online(dev):
            continue
        try:
            cap = await api.ptz_capability(ch)
            if not cap:
                continue
            presets = await api.presets(ch) if cap.get("preset_supported") == "1" else []
        except Exception as exc:  # noqa: BLE001 - PTZ is optional
            _LOGGER.debug("NVR %s ch%s: no PTZ info: %s", api.host, ch, exc)
            continue
        out[ch] = {"capability": cap, "presets": presets}
    _LOGGER.debug("NVR %s: PTZ channels %s", api.host, sorted(out))
    return out


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
