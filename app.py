"""Minimal web remote for an LG webOS TV. Run on a machine in the same LAN."""
import asyncio
import os
import socket
from pathlib import Path

from aiohttp import web
from aiowebostv import WebOsClient

TV_IP = os.environ.get("TV_IP", "192.168.1.99")
PORT = int(os.environ.get("PORT", "8080"))
TV_MAC = os.environ.get("TV_MAC", "")  # e.g. aa:bb:cc:dd:ee:ff, needed for power on
KEY_FILE = Path(__file__).with_name(".client_key")
STATIC = Path(__file__).with_name("static")

BUTTONS = {"UP", "DOWN", "LEFT", "RIGHT", "ENTER", "BACK", "HOME", "MENU", "EXIT",
           "PLAY", "PAUSE", "STOP", "REWIND", "FASTFORWARD"}

client: WebOsClient | None = None
lock = asyncio.Lock()


async def get_client() -> WebOsClient:
    """Return a connected client; the first run needs an 'Accept' on the TV."""
    global client
    if client is None:
        key = KEY_FILE.read_text().strip() if KEY_FILE.exists() else None
        client = WebOsClient(TV_IP, key)
    await client.connect()  # no-op if already connected in current aiowebostv
    if client.client_key:
        KEY_FILE.write_text(client.client_key)
    return client


async def tv_call(method: str, *args):
    """Call a client method; reconnect and retry once if the socket died."""
    global client
    async with lock:
        for attempt in (1, 2):
            try:
                c = await get_client()
                return await getattr(c, method)(*args)
            except Exception:
                if attempt == 2:
                    raise
                if client:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                client = None


def state_of(c: WebOsClient):
    # aiowebostv >= 1.0 keeps state in c.tv_state; older versions on the client itself
    return getattr(c, "tv_state", c)


async def status(request):
    async with lock:
        c = await get_client()
        s = state_of(c)
        channels = getattr(s, "channels", None) or []
        current = getattr(s, "current_channel", None) or {}
        return web.json_response({
            "volume": getattr(s, "volume", None),
            "muted": getattr(s, "muted", None),
            "channel": current,
            "channels": [
                {"id": ch.get("channelId"), "number": ch.get("channelNumber"),
                 "name": ch.get("channelName")} for ch in channels
            ],
        })


async def volume(request):
    body = await request.json()
    if "level" in body:
        await tv_call("set_volume", max(0, min(100, int(body["level"]))))
    else:
        await tv_call("volume_up" if body.get("delta", 1) > 0 else "volume_down")
    return web.json_response({"ok": True})


async def mute(request):
    body = await request.json()
    await tv_call("set_mute", bool(body["muted"]))
    return web.json_response({"ok": True})


async def channel(request):
    body = await request.json()
    if "id" in body:
        await tv_call("set_channel", body["id"])
    else:
        await tv_call("channel_up" if body.get("delta", 1) > 0 else "channel_down")
    return web.json_response({"ok": True})


async def button(request):
    name = (await request.json()).get("name", "").upper()
    if name not in BUTTONS:
        raise web.HTTPBadRequest(text="unknown button")
    await tv_call("button", name)
    return web.json_response({"ok": True})


async def power_off(request):
    await tv_call("power_off")
    return web.json_response({"ok": True})


async def power_on(request):
    mac = bytes.fromhex(TV_MAC.replace(":", "").replace("-", "")) if TV_MAC else b""
    if len(mac) != 6:
        raise web.HTTPBadRequest(text="set TV_MAC=aa:bb:cc:dd:ee:ff")
    packet = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):  # UDP is lossy; resend
            sock.sendto(packet, ("255.255.255.255", 9))
    return web.json_response({"ok": True})


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:  # surface TV errors to the UI instead of a bare 500
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=502)


app = web.Application(middlewares=[errors])
app.add_routes([
    web.get("/", lambda r: web.FileResponse(STATIC / "index.html")),
    web.get("/api/status", status),
    web.post("/api/volume", volume),
    web.post("/api/mute", mute),
    web.post("/api/channel", channel),
    web.post("/api/button", button),
    web.post("/api/power_off", power_off),
    web.post("/api/power_on", power_on),
])

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
