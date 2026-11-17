"""VIGI NVR web API backend: password encoding, stream framing, live-stream login."""
import asyncio
import datetime
import json
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from vigicam_pure import event_image, nvr_web

USER, PASSWORD = "admin", "S3cret!pw"


def _js_security_encode(d, b, a):
    """Line-for-line port of securityEncode from the NVR's class.js, as a cross-check."""
    c = ""
    g, f, h = len(d), len(b), len(a)
    e = g if g > f else f
    for k in range(e):
        m = l = 187
        if k >= g:
            m = ord(b[k])
        elif k >= f:
            l = ord(d[k])
        else:
            l = ord(d[k])
            m = ord(b[k])
        c += a[(l ^ m) % h]
    return c


def test_security_encode_matches_js():
    for pw in ("", "a", "admin", "S3cret!pw", "a-much-longer-password-than-the-key-123"):
        assert nvr_web.org_auth_pwd(pw) == _js_security_encode(
            pw, nvr_web._ORG_KEY, nvr_web._ORG_DICT
        )
    assert len(nvr_web._ORG_DICT) == 255  # as extracted from class.js


def _frame(ctype: str, body: bytes) -> bytes:
    return (
        f"----device-stream-boundary--\r\nContent-Type: {ctype}\r\n"
        f"Content-Length: {len(body)}\r\nX-If-Encrypt: 0\r\n\r\n"
    ).encode() + body + b"\r\n"


def test_parser_handles_split_and_joined_parts():
    a, b = _frame("video/mp2t", b"A" * 300), _frame("video/mp2t", b"B" * 50)
    blob = a + b
    p = nvr_web.StreamParser()
    got = []
    for i in range(0, len(blob), 37):  # arbitrary split points, boundary split included
        got += p.feed(blob[i:i + 37])
    assert [body for _, _, body in got] == [b"A" * 300, b"B" * 50]
    p2 = nvr_web.StreamParser()
    assert len(p2.feed(blob)) == 2


def test_added_devices_normalised():
    api = nvr_web.VIGINVRWeb("10.0.0.1", USER, PASSWORD)

    async def fake_call(body):
        return {"chm": {"added_dev": [
            {"added_dev_1": {"id": "1", "online": "1", "name": "%e5%ae%a2%e5%8e%85",
                             "alias": "x", "ip": "10.11.100.203", "mac": "98-97-cc-0c-7b-ca",
                             "firmware_version": "1.0.3%20Build%20210304", "connect_prot": "TP-LINK"}},
            {"added_dev_2": {"id": "2", "online": "0", "name": "Yard", "ip": "10.11.100.204"}},
        ]}, "error_code": 0}

    api.call = fake_call
    devs = asyncio.run(api.added_devices())
    assert devs[0]["id"] == 1 and devs[0]["name"] == "客厅" and devs[0]["firmware"] == "1.0.3 Build 210304"
    assert devs[1]["online"] == "0"


# ── Fake stream daemon ────────────────────────────────────────────────────────

def _tls(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nvr")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    return ctx


def _run_stream(tmp_path, password):
    seen = {}

    async def handle(reader, writer):
        while (await reader.readline()) not in (b"\r\n", b""):
            pass
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\n\r\n")
        nonce = "432c66756e55b8ef"
        writer.write(event_image._ws_encode(json.dumps({"type": "notification", "params": {
            "event_type": "websocket_authenticate", "realm": "TP-Link IP-Camera",
            "algorithm": "MD5", "qop": "auth", "nonce": nonce, "opaque": "649"}})))
        _, data = await event_image._ws_recv(reader)
        auth = json.loads(data)["params"]["authenticate"]
        expected = nvr_web.stream_digest(
            USER, PASSWORD, "TP-Link IP-Camera", nonce, auth["nc"], auth["cnonce"], "auth")
        ok = auth["response"] == expected and auth["uri"] == "/stream"
        writer.write(event_image._ws_encode(json.dumps(
            {"type": "response", "seq": 0, "params": {"error_code": 0 if ok else -40401}})))
        if not ok:
            await writer.drain()
            writer.close()
            return
        _, req = await event_image._ws_recv(reader)
        seen["preview"] = json.loads(req.decode().split("\r\n\r\n", 1)[1])
        blob = (_frame("application/json", b'{"type":"response","seq":1,"params":{"error_code":0,"session_id":"16"}}')
                + _frame("video/mp2t", b"\x47" + b"T" * 187) + _frame("video/mp2t", b"\x47" + b"U" * 187))
        writer.write(event_image._ws_encode(blob[:100], opcode=2))
        writer.write(event_image._ws_encode(blob[100:], opcode=2))
        await writer.drain()
        await asyncio.sleep(1)
        writer.close()

    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=_tls(tmp_path))
        port = server.sockets[0].getsockname()[1]
        nvr_web.STREAM_PORT, old = port, nvr_web.STREAM_PORT
        api = nvr_web.VIGINVRWeb("127.0.0.1", USER, password)
        got = []
        try:
            async for chunk in api.preview(3):
                got.append(chunk)
                if len(got) == 2:
                    break
        except nvr_web.VIGINVRStreamError as exc:
            got = exc
        finally:
            nvr_web.STREAM_PORT = old
            server.close()
        return got

    return asyncio.run(main()), seen


def test_preview_login_and_ts(tmp_path):
    got, seen = _run_stream(tmp_path, PASSWORD)
    assert got == [b"\x47" + b"T" * 187, b"\x47" + b"U" * 187]
    # Channels count from 0 in the preview request.
    assert seen["preview"]["params"]["preview"] == {"channels": [2], "resolutions": ["hd"]}


def test_preview_wrong_password(tmp_path):
    got, _ = _run_stream(tmp_path, "wrong")
    assert isinstance(got, nvr_web.VIGINVRStreamError)
