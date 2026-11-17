"""VIGI NVR through its own web API (port 443) and stream daemon (port 8443).

For NVR firmware without OpenAPI. Worked out from a browser capture of a VIGI
NVR1008H(UN) 2.20 on firmware 1.1.2 Build 240815:

Login is the camera web login api.py already does (get_encrypt_info, RSA-encrypted
MD5 password, stok), then JSON calls to /stok=<stok>/ds:

  {"system": {"name": ["basic", "basic_info"]}, "method": "get"}   model, firmware, serial
  {"chm": {"table": "added_dev"}, "method": "get"}                 one entry per channel
  {"ptz": {"get_ptz_capability": {"chn_id": 1}}, "method": "do"}
  {"ptz": {"get_preset": {"chn_id": 1}}, "method": "do"}

Live view is MPEG-TS over a WebSocket at wss://<nvr>:8443/stream, framed in
"----device-stream-boundary--" parts. The NVR signs it with HTTP Digest (MD5, qop=auth,
HA2 = MD5("GET:/stream")), and the digest password is the account password run through
TP-Link's securityEncode (orgAuthPwd in the NVR's class.js), not the plain password.
Channels in the preview request count from 0.

This module has no Home Assistant imports so the tests can load it on its own.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import ssl
import urllib.parse
import warnings
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .api import VIGICamera
from .event_image import _ws_encode, _ws_recv

_LOGGER = logging.getLogger(__name__)

STREAM_PORT = 8443
STREAM_PATH = "/stream"
_CLIENT_BOUNDARY = "----client-stream-boundary--\r\n"
_DEVICE_BOUNDARY = b"----device-stream-boundary--"
_SUBPROTOCOL = urllib.parse.quote(
    json.dumps({"Content-Type": "multipart/mixed;boundary=--client-stream-boundary--"},
               separators=(",", ":"))
)

# orgAuthPwd(): securityEncode(password, _ORG_KEY, _ORG_DICT) in the NVR's class.js.
_ORG_KEY = "RDpbLfCPsJZ7fiv"
_ORG_DICT = (
    "yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4rBL3cD02KZciXTysVXiV8n"
    "gg6vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro510qXeU07kV57fQMc8L6aLgMLwygtc0F10a0Dg70TO"
    "oouyFhdysuRMO51yY5ZlOZZLEal1h0t9YQW0Ko7oBwmCAHoic4HYbUyVeU3sfQ1xtXcPcf1aT303wAQhv66qzW"
)


def security_encode(a: str, b: str, c: str) -> str:
    """TP-Link's securityEncode, as in the NVR's bundled jquery: XOR, then index a dict."""
    out = []
    la, lb, lc = len(a), len(b), len(c)
    for p in range(max(la, lb)):
        l = n = 187
        if p >= la:
            n = ord(b[p])
        elif p >= lb:
            l = ord(a[p])
        else:
            l, n = ord(a[p]), ord(b[p])
        out.append(c[(l ^ n) % lc])
    return "".join(out)


def org_auth_pwd(password: str) -> str:
    return security_encode(password, _ORG_KEY, _ORG_DICT)


def stream_digest(
    username: str, password: str, realm: str, nonce: str, nc: str, cnonce: str, qop: str,
    method: str = "GET",
) -> str:
    """The stream login's response hash (MD5 digest over the encoded password)."""
    def md5(s: str) -> str:
        return hashlib.md5(s.encode()).hexdigest()

    ha1 = md5(f"{username}:{realm}:{org_auth_pwd(password)}")
    ha2 = md5(f"{method}:{STREAM_PATH}")
    return md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")


def _part(obj: dict) -> str:
    body = json.dumps(obj, separators=(",", ":"))
    return (
        _CLIENT_BOUNDARY
        + f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        + body
    )


class StreamParser:
    """Split the device's multipart byte stream into (content_type, headers, body).

    A WebSocket frame can hold several parts, or end part-way through one, so bytes are
    buffered until a whole part (by Content-Length) is available.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, dict[str, str], bytes]]:
        self._buf += data
        parts: list[tuple[str, dict[str, str], bytes]] = []
        while True:
            start = self._buf.find(_DEVICE_BOUNDARY)
            if start < 0:
                # Keep a tail in case the boundary itself was split.
                if len(self._buf) > len(_DEVICE_BOUNDARY):
                    del self._buf[: len(self._buf) - len(_DEVICE_BOUNDARY)]
                return parts
            if start:
                del self._buf[:start]
            hdr_end = self._buf.find(b"\r\n\r\n")
            if hdr_end < 0:
                return parts
            headers: dict[str, str] = {}
            for line in bytes(self._buf[len(_DEVICE_BOUNDARY):hdr_end]).decode(
                "utf-8", "replace"
            ).split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            try:
                length = int(headers.get("content-length", ""))
            except ValueError:
                # No length: drop this boundary and resync on the next one.
                del self._buf[: len(_DEVICE_BOUNDARY)]
                continue
            body_start = hdr_end + 4
            if len(self._buf) < body_start + length:
                return parts
            body = bytes(self._buf[body_start: body_start + length])
            del self._buf[: body_start + length]
            parts.append((headers.get("content-type", ""), headers, body))


# The NVR1008H's web server (firmware 1.1.2) rejects every cipher suite Python offers by
# default: the handshake fails with SSLV3_ALERT_HANDSHAKE_FAILURE. These older suites
# (static RSA, CBC) are added on top of the defaults, and TLS 1.0 is allowed. Fine for a
# box on the LAN whose certificate isn't verified anyway.
_NVR_CIPHERS = (
    "DEFAULT:AES256-GCM-SHA384:AES128-GCM-SHA256:AES256-SHA256:AES128-SHA256:"
    "AES256-SHA:AES128-SHA:DES-CBC3-SHA:@SECLEVEL=0"
)


def nvr_ssl_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers(_NVR_CIPHERS)
    except ssl.SSLError:  # an OpenSSL build without some of them
        ctx.set_ciphers("DEFAULT:AES256-GCM-SHA384:AES128-GCM-SHA256:AES256-SHA:AES128-SHA:@SECLEVEL=0")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = ssl.TLSVersion.TLSv1
    except (ValueError, ssl.SSLError):
        pass
    return ctx


class VIGINVRStreamError(Exception):
    pass


class VIGINVRWeb:
    """The NVR's web API (443) and stream daemon (8443)."""

    def __init__(self, host: str, username: str, password: str) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._ssl: ssl.SSLContext = nvr_ssl_context()
        self._session: aiohttp.ClientSession | None = None
        self._cam = VIGICamera(host, username, password, session=None)

    def _ensure_session(self) -> None:
        """Hand VIGICamera a session with the wider TLS settings (it never closes it)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=self._ssl),
                timeout=aiohttp.ClientTimeout(total=15),
            )
            self._cam._ha_session = self._session

    @property
    def host(self) -> str:
        return self._host

    async def login(self) -> None:
        self._ensure_session()
        await self._cam.authenticate()

    async def close(self) -> None:
        await self._cam.close()
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def call(self, body: dict) -> dict:
        self._ensure_session()
        return await self._cam._request(body)

    async def system_info(self) -> dict:
        reply = await self.call({"system": {"name": ["basic", "basic_info"]}, "method": "get"})
        system = reply.get("system") or {}
        info: dict[str, Any] = {}
        for section in ("basic", "basic_info"):
            for k, v in (system.get(section) or {}).items():
                if not k.startswith("."):
                    info[k] = urllib.parse.unquote(v) if isinstance(v, str) else v
        return info

    async def added_devices(self) -> list[dict]:
        """Channels in the same shape as the OpenAPI's added_devices (id, name, online, ip, mac)."""
        reply = await self.call({"chm": {"table": "added_dev"}, "method": "get"})
        out: list[dict] = []
        for row in (reply.get("chm") or {}).get("added_dev") or []:
            if not isinstance(row, dict) or not row:
                continue
            dev = next(iter(row.values()))
            if not isinstance(dev, dict) or "id" not in dev:
                continue
            out.append({
                "id": int(dev["id"]),
                "name": urllib.parse.unquote(dev.get("name") or dev.get("alias") or ""),
                "alias": urllib.parse.unquote(dev.get("alias") or ""),
                "online": dev.get("online", "0"),
                "ip": dev.get("ip"),
                "mac": dev.get("mac"),
                "firmware": urllib.parse.unquote(dev.get("firmware_version") or ""),
                "protocol": dev.get("connect_prot"),
                "manufacturer": dev.get("manufacturers"),
            })
        return out

    async def ptz_capability(self, channel: int) -> dict:
        reply = await self.call(
            {"ptz": {"get_ptz_capability": {"chn_id": int(channel)}}, "method": "do"}
        )
        block = reply.get(f"chn{int(channel)}_ptz_capability") or {}
        return block.get("ptz_capability") or {}

    async def presets(self, channel: int) -> list[dict]:
        reply = await self.call({"ptz": {"get_preset": {"chn_id": int(channel)}}, "method": "do"})
        return [
            {**p, "name": urllib.parse.unquote(str(p.get("name", "")))}
            for p in reply.get("preset") or [] if isinstance(p, dict)
        ]

    # ── Live stream ──────────────────────────────────────────────────────────

    async def preview(
        self, channel: int, resolution: str = "hd", *, connect_timeout: float = 10
    ) -> AsyncIterator[bytes]:
        """Yield the live MPEG-TS of one channel (1-based) until the caller stops.

        Raises VIGINVRStreamError if the NVR refuses the login or the preview.
        """
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, STREAM_PORT, ssl=self._ssl),
            timeout=connect_timeout,
        )
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            writer.write((
                f"GET {STREAM_PATH} HTTP/1.1\r\n"
                f"Host: {self._host}:{STREAM_PORT}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Protocol: {_SUBPROTOCOL}\r\n"
                f"Origin: https://{self._host}\r\n\r\n"
            ).encode())
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), timeout=connect_timeout)
            if b" 101 " not in status:
                raise VIGINVRStreamError(f"WebSocket upgrade refused: {status[:80]!r}")
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=connect_timeout)
                if not line:
                    raise VIGINVRStreamError("stream closed during handshake")
                if line in (b"\r\n", b"\n"):
                    break

            parser = StreamParser()
            authed = requested = False
            while True:
                timeout = 15 if requested else connect_timeout
                opcode, data = await asyncio.wait_for(_ws_recv(reader), timeout=timeout)
                if opcode == 8:
                    raise VIGINVRStreamError("NVR closed the stream")
                if opcode == 9:  # ping
                    writer.write(_ws_encode(data, opcode=10))
                    continue
                if opcode == 1:
                    msg = json.loads(data.decode("utf-8", "replace"))
                    params = msg.get("params") or {}
                    if params.get("event_type") == "websocket_authenticate":
                        nc, cnonce = "00000001", os.urandom(8).hex()
                        qop = params.get("qop", "auth")
                        writer.write(_ws_encode(json.dumps({
                            "type": "request", "seq": 0,
                            "params": {"method": "do", "authenticate": {
                                "username": self._username,
                                "realm": params.get("realm", ""),
                                "nonce": params.get("nonce", ""),
                                "uri": STREAM_PATH,
                                "response": stream_digest(
                                    self._username, self._password, params.get("realm", ""),
                                    params.get("nonce", ""), nc, cnonce, qop,
                                ),
                                "opaque": params.get("opaque", ""),
                                "qop": qop, "nc": nc, "cnonce": cnonce,
                            }},
                        }, separators=(",", ":"))))
                        await writer.drain()
                    elif msg.get("type") == "response" and msg.get("seq") == 0:
                        if params.get("error_code") not in (0, "0"):
                            raise VIGINVRStreamError(
                                f"stream login refused (error_code={params.get('error_code')})"
                            )
                        authed = True
                        writer.write(_ws_encode(_part({
                            "type": "request", "seq": 1,
                            "params": {"method": "get", "preview": {
                                "channels": [int(channel) - 1], "resolutions": [resolution],
                            }},
                        })))
                        await writer.drain()
                        requested = True
                    continue
                if opcode in (0, 2) and authed:
                    for ctype, _headers, body in parser.feed(data):
                        if "json" in ctype:
                            reply = json.loads(body.decode("utf-8", "replace") or "{}")
                            code = (reply.get("params") or {}).get("error_code")
                            if code not in (None, 0, "0"):
                                raise VIGINVRStreamError(f"preview refused (error_code={code})")
                            continue
                        yield body
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
