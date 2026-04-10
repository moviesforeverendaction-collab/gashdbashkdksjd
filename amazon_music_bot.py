import os, re, json, asyncio, subprocess

import httpx
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ParseMode, ButtonStyle

API_ID    = int(os.getenv("API_ID", "0"))
API_HASH  = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DEV_URL   = os.getenv("DEV_URL", "https://t.me/cantarella_wuwa")
DEV_USER  = os.getenv("DEV_USERNAME", "@cantarella_wuwa")
API_BASE  = os.getenv("API_BASE", "")  #buy the api from the amazon (_:"":_)

DL_DIR = "./downloads"
os.makedirs(DL_DIR, exist_ok=True)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"

AMZN_RE = re.compile(r'https?://music\.amazon\.(in|com|co\.uk|de|jp|fr|ca|com\.au)/\S+')

def is_amzn(text):
    return bool(AMZN_RE.search(text))

def get_asin(url):
    from urllib.parse import urlparse, parse_qs
    p  = urlparse(url)
    qs = parse_qs(p.query)

    if qs.get("trackAsin"):
        a = qs["trackAsin"][0]
        if re.match(r'^B[0-9A-Z]{9}$', a):
            return a

    m = re.search(r'(B[0-9A-Z]{9})', p.path or url)
    if not m:
        raise ValueError("couldn't find a track id in that url")
    return m.group(1)

def probe_codec(path):
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,bits_per_raw_sample,sample_rate",
         "-of", "json", path],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError("ffprobe couldn't read the file")

    streams = json.loads(r.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("ffprobe found no audio streams")

    return streams[0]


async def get_meta(asin):
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=30) as c:
        r = await c.get(f"{API_BASE}/track/{asin}")

    if r.status_code != 200:
        raise RuntimeError(f"api returned {r.status_code}")

    d = r.json()

    stream = d.get("streamUrl") or d.get("stream_url") or d.get("url") or ""
    key    = d.get("decryptionKey") or d.get("decryption_key") or d.get("key") or ""

    if not stream:
        raise RuntimeError("api didn't return a stream url")

    return {
        "asin":      asin,
        "title":     d.get("title")     or asin,
        "artist":    d.get("artist")    or "Unknown Artist",
        "album":     d.get("album")     or "",
        "thumbnail": d.get("thumbnail") or d.get("coverUrl") or "",
        "stream":    stream,
        "key":       key,
    }


async def dl_track(meta):
    asin   = meta["asin"]
    title  = meta["title"]
    artist = meta["artist"]

    enc = os.path.join(DL_DIR, f"{asin}_enc.m4a")

    # download encrypted stream
    async with httpx.AsyncClient(headers={"User-Agent": UA}, timeout=120, follow_redirects=True) as c:
        async with c.stream("GET", meta["stream"]) as resp:
            resp.raise_for_status()
            with open(enc, "wb") as f:
                async for chunk in resp.aiter_bytes(1 << 17):
                    f.write(chunk)

    if not os.path.exists(enc) or os.path.getsize(enc) == 0:
        raise RuntimeError("downloaded file is empty")

    safe = re.sub(r'[\\/*?:"<>|]', "", f"{artist} - {title}")

    # no key = already clear, just rename
    if not meta["key"]:
        out = os.path.join(DL_DIR, f"{safe}.m4a")
        os.rename(enc, out)
        return {**meta, "path": out, "codec": "m4a", "bits": None, "rate": None}

    # detect codec so we pick the right extension
    info  = await asyncio.get_event_loop().run_in_executor(None, probe_codec, enc)
    codec = info.get("codec_name", "m4a")

    if codec == "flac":
        ext = "flac"
    elif codec in ("aac", "alac"):
        ext = "m4a"
    else:
        ext = codec  # mp3, opus, whatever comes back

    out = os.path.join(DL_DIR, f"{safe}.{ext}")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error",
        "-decryption_key", meta["key"].strip(),
        "-i", enc, "-c", "copy", "-y", out,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decryption failed: {err.decode()}")

    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError("output file is empty after decryption")

    try: os.remove(enc)
    except: pass

    return {
        **meta,
        "path":  out,
        "codec": codec,
        "bits":  info.get("bits_per_raw_sample"),
        "rate":  info.get("sample_rate"),
    }


async def dl_thumb(url, asin):
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
        p = os.path.join(DL_DIR, f"{asin}_thumb.jpg")
        with open(p, "wb") as f:
            f.write(r.content)
        return p
    except:
        return None


def cleanup(*paths):
    for p in paths:
        if p and os.path.exists(p):
            try: os.remove(p)
            except: pass


bot = Client("amzn_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


@bot.on_message(filters.command("start"))
async def start(_, msg: Message):
    await msg.reply_text(
        "send me an Amazon Music track link and i'll download it for you.\n\n"
        "just paste it below.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("developer", url=DEV_URL, style=ButtonStyle.PRIMARY),
            InlineKeyboardButton("credits", callback_data="credits", style=ButtonStyle.SUCCESS),
        ]]),
    )


@bot.on_callback_query(filters.regex("^credits$"))
async def credits_cb(_, cb: CallbackQuery):
    await cb.answer()
    await cb.message.reply_text(f"pyrogram + ffmpeg + afkarxyz api\n\nby {DEV_USER}")


@bot.on_callback_query(filters.regex("^dismiss$"))
async def dismiss_cb(_, cb: CallbackQuery):
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except:
        pass


@bot.on_message(filters.text & ~filters.command(["start"]))
async def handle(_, msg: Message):
    text = msg.text.strip()

    if not is_amzn(text):
        await msg.reply_text("that doesn't look like an amazon music link.")
        return

    if "/search/" in text or "/api/" in text:
        await msg.reply_text("send a direct track link, not a search or api url.")
        return

    status = await msg.reply_text("downloading...")

    try:
        asin = get_asin(text)
        meta = await get_meta(asin)

        result, thumb = await asyncio.gather(
            dl_track(meta),
            dl_thumb(meta["thumbnail"], asin),
        )

        await status.edit_text("uploading...")

        fmt = "FLAC 24bit / 192kHz" if result["codec"] == "flac" else result["codec"].upper()
        cap = (
            f"<b>{result['title']}</b>\n"
            f"{result['artist']}"
            + (f" — {result['album']}" if result["album"] else "")
            + f"\n{fmt}"
        )

        await msg.reply_audio(
            audio=result["path"],
            caption=cap,
            title=result["title"],
            performer=result["artist"],
            thumb=thumb if thumb and os.path.exists(thumb) else None,
            parse_mode=ParseMode.HTML,
        )

        await status.delete()
        cleanup(result["path"], thumb)

    except Exception as e:
        await status.edit_text(
            f"something went wrong\n\n<code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )


if __name__ == "__main__":
    print("bot running")
    bot.run()
