"""Turn the NVR's WebSocket live stream into a plain HTTP MPEG-TS stream for HA.

Home Assistant's stream component (PyAV) and ffmpeg can't speak the NVR's framed
WebSocket protocol, so each camera entity points them at
http://127.0.0.1:<port>/api/vigicam_nvr_stream/<token>/<channel>, served here. Each
request opens its own session on the NVR and pipes the TS bytes through.

The view needs no HA login (PyAV can't send one), so every NVR entry gets a random
token at setup and requests without a known token get 404.
"""
from __future__ import annotations

import asyncio
import logging
import secrets

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

RELAY_PATH = "/api/vigicam_nvr_stream"
_REGISTERED = f"{DOMAIN}_nvr_relay_registered"


def new_token() -> str:
    return secrets.token_urlsafe(24)


def relay_url(hass: HomeAssistant, token: str, channel: int) -> str:
    scheme = "https" if hass.config.api and hass.config.api.use_ssl else "http"
    port = hass.http.server_port if hass.http else 8123
    return f"{scheme}://127.0.0.1:{port}{RELAY_PATH}/{token}/{int(channel)}"


def async_register_relay(hass: HomeAssistant) -> None:
    if hass.data.get(_REGISTERED):
        return
    hass.http.register_view(NVRStreamRelayView())
    hass.data[_REGISTERED] = True


class NVRStreamRelayView(HomeAssistantView):
    url = RELAY_PATH + "/{token}/{channel}"
    name = "api:vigicam:nvr_stream"
    requires_auth = False

    async def get(self, request: web.Request, token: str, channel: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        entry_data = next(
            (
                d for d in (hass.data.get(DOMAIN) or {}).values()
                if isinstance(d, dict) and d.get("relay_token")
                and secrets.compare_digest(d["relay_token"], token)
            ),
            None,
        )
        if entry_data is None or not channel.isdigit():
            raise web.HTTPNotFound
        api = entry_data["api"]
        response = web.StreamResponse(headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"})
        await response.prepare(request)
        sent = 0
        try:
            async for chunk in api.preview(int(channel), entry_data.get("resolution", "hd")):
                await response.write(chunk)
                sent += len(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            pass  # the player went away
        except Exception as exc:  # noqa: BLE001 - log and end the stream
            _LOGGER.warning("NVR %s ch%s: live stream ended: %s", api.host, channel, exc)
        _LOGGER.debug("NVR %s ch%s: relay closed after %d bytes", api.host, channel, sent)
        return response
