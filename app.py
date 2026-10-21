"""
Telegram voice sender for Salebot (regular Telegram account).

Salebot sends attachments from a TG account as files. This service logs in to the
same account through the Telegram API (Telethon) and sends audio as a real voice
message (voice_note). Salebot calls it from an "API-запрос" block.

Env vars:
  API_ID, API_HASH  - from https://my.telegram.org -> API development tools
  SECRET            - any long random string; required in every request
  TG_SESSION        - session string (shown once after /login)
"""
import asyncio
import html
import io
import os
import subprocess
import tempfile

import aiohttp
import imageio_ffmpeg
from aiohttp import web
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

API_ID = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "")
SECRET = os.environ.get("SECRET", "")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

client = (TelegramClient(StringSession(os.environ.get("TG_SESSION", "")), API_ID, API_HASH)
          if API_ID and API_HASH else None)
login_state = {}  # phone -> phone_code_hash (only during login)
voice_cache = {}  # source url -> converted ogg bytes


def page(body):
    return web.Response(
        text="<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<body style='font-family:sans-serif;max-width:560px;margin:40px auto;padding:0 16px'>"
        + body + "</body>",
        content_type="text/html",
    )


def check_key(key):
    if not SECRET or key != SECRET:
        raise web.HTTPForbidden(text="bad key")


async def ensure_connected():
    if client is None:
        raise web.HTTPServiceUnavailable(text="set API_ID and API_HASH env vars")
    if not client.is_connected():
        await client.connect()


# ---------- health ----------
async def health(_):
    await ensure_connected()
    return web.json_response({"ok": True, "authorized": await client.is_user_authorized()})


# ---------- one-time login ----------
async def login_form(req):
    check_key(req.query.get("key"))
    k = html.escape(SECRET)
    return page(
        "<h3>Вход в Telegram-аккаунт</h3>"
        "<form method=post action='/login/phone'>"
        f"<input type=hidden name=key value='{k}'>"
        "Телефон (в формате +7...):<br><input name=phone style='width:100%;padding:8px'><br><br>"
        "<button>Получить код</button></form>"
    )


async def login_phone(req):
    data = await req.post()
    check_key(data.get("key"))
    phone = data.get("phone", "").strip()
    await ensure_connected()
    sent = await client.send_code_request(phone)
    login_state[phone] = sent.phone_code_hash
    k, p = html.escape(SECRET), html.escape(phone)
    return page(
        "<h3>Код отправлен в Telegram</h3>"
        "<form method=post action='/login/code'>"
        f"<input type=hidden name=key value='{k}'><input type=hidden name=phone value='{p}'>"
        "Код из Telegram:<br><input name=code style='width:100%;padding:8px'><br><br>"
        "Пароль 2FA (если включён):<br><input name=password type=password style='width:100%;padding:8px'><br><br>"
        "<button>Войти</button></form>"
    )


async def login_code(req):
    data = await req.post()
    check_key(data.get("key"))
    phone, code, pwd = data.get("phone", ""), data.get("code", "").strip(), data.get("password", "")
    await ensure_connected()
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=login_state.get(phone))
    except SessionPasswordNeededError:
        if not pwd:
            return page("Нужен пароль 2FA. Вернитесь назад и введите его вместе с кодом.")
        await client.sign_in(password=pwd)
    login_state.pop(phone, None)
    me = await client.get_me()
    session = client.session.save()
    return page(
        f"<h3>Готово: вошли как {html.escape(me.first_name or '')} (id {me.id})</h3>"
        "<p>Скопируйте строку ниже в переменную <b>TG_SESSION</b> сервиса на Render. "
        "Это ключ доступа к аккаунту — храните как пароль и никому не пересылайте.</p>"
        f"<textarea style='width:100%;height:160px' readonly>{html.escape(session)}</textarea>"
    )


# ---------- sending ----------
async def to_voice(url):
    if url in voice_cache:
        return voice_cache[url]
    async with aiohttp.ClientSession() as s:
        async with s.get(url) as r:
            r.raise_for_status()
            raw = await r.read()
    with tempfile.TemporaryDirectory() as d:
        src, dst = os.path.join(d, "in"), os.path.join(d, "voice.ogg")
        with open(src, "wb") as f:
            f.write(raw)
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
            "-vn", "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "48k",
            "-application", "voip", "-f", "ogg", dst,
            stderr=subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg: " + err.decode(errors="ignore"))
        with open(dst, "rb") as f:
            out = f.read()
    voice_cache[url] = out
    return out


async def resolve(peer, username):
    if username:
        return await client.get_input_entity(username.lstrip("@"))
    pid = int(peer)
    try:
        return await client.get_input_entity(pid)
    except ValueError:
        await client.get_dialogs(limit=500)  # fill entity cache, then retry
        return await client.get_input_entity(pid)


async def send_voice(req):
    data = await req.json() if req.content_type == "application/json" else dict(await req.post())
    data = {**dict(req.query), **data}
    check_key(data.get("key"))
    url = data.get("url")
    peer, username = data.get("peer"), data.get("username")
    if not url or not (peer or username):
        return web.json_response({"ok": False, "error": "need url and peer or username"}, status=400)
    await ensure_connected()
    if not await client.is_user_authorized():
        return web.json_response({"ok": False, "error": "not logged in, open /login"}, status=503)
    try:
        entity = await resolve(peer, username)
        audio = await to_voice(url)
        f = io.BytesIO(audio)
        f.name = "voice.ogg"
        msg = await client.send_file(entity, f, voice_note=True)
        return web.json_response({"ok": True, "message_id": msg.id})
    except Exception as e:  # noqa: BLE001 - report any failure back to Salebot
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)


async def on_startup(_):
    try:
        await ensure_connected()
    except Exception as e:  # noqa: BLE001 - keep the web server up for /health and /login
        print("telegram connect failed at startup:", e)


app = web.Application()
app.on_startup.append(on_startup)
app.add_routes([
    web.get("/", health),
    web.get("/health", health),
    web.get("/login", login_form),
    web.post("/login/phone", login_phone),
    web.post("/login/code", login_code),
    web.post("/send_voice", send_voice),
    web.get("/send_voice", send_voice),
])

if __name__ == "__main__":
    web.run_app(app, port=int(os.environ.get("PORT", "10000")))
