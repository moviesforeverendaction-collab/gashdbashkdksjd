import os
import re
import json
import shutil
import asyncio
import requests
import subprocess

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import ParseMode

# ── Config ────────────────────────────────────────────────────────────────────
API_ID    = int(os.getenv("API_ID", "0"))
API_HASH  = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ── Downloader ────────────────────────────────────────────────────────────────
class AmazonMusicDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def extract_asin(self, amazon_url: str) -> str:
        match = re.search(r'(B[0-9A-Z]{9})', amazon_url)
        if not match:
            raise Exception(
                "Could not find an ASIN in that URL.\n"
                "Make sure it's a full Amazon Music link, e.g.\n"
                "`https://music.amazon.in/tracks/B0XXXXXXXX`"
            )
        return match.group(1)

    def detect_codec(self, file_path: str) -> str:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name",
            "-of", "json",
            file_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception("ffprobe failed to detect codec")
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            raise Exception("ffprobe found no audio streams")
        return streams[0]["codec_name"]

    def download(self, amazon_url: str, output_dir: str = DOWNLOAD_DIR) -> dict:
        """Returns dict: file_path, asin, codec, title, artist"""
        asin    = self.extract_asin(amazon_url)
        api_url = f"https://amzn.afkarxyz.qzz.io/api/track/{asin}"

        r = self.session.get(api_url, timeout=30)
        if r.status_code == 404:
            raise Exception("Track not found — the ASIN may be an album, not a single track.")
        if r.status_code != 200:
            raise Exception(f"API returned HTTP {r.status_code}")

        data       = r.json()
        stream_url = data.get("streamUrl")
        key        = data.get("decryptionKey")
        title      = data.get("title") or asin
        artist     = data.get("artist") or "Unknown Artist"

        if not stream_url:
            raise Exception("API returned no streamUrl")

        os.makedirs(output_dir, exist_ok=True)
        temp_file = os.path.join(output_dir, f"{asin}_enc.m4a")

        with self.session.get(stream_url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(temp_file, "wb") as f:
                shutil.copyfileobj(resp.raw, f)

        # No decryption key — return as-is
        if not key:
            return {"file_path": temp_file, "asin": asin, "codec": "m4a",
                    "title": title, "artist": artist}

        codec = self.detect_codec(temp_file)

        if codec == "flac":
            final_file = os.path.join(output_dir, f"{asin}.flac")
        elif codec in ("aac", "alac"):
            final_file = os.path.join(output_dir, f"{asin}.m4a")
        else:
            final_file = os.path.join(output_dir, f"{asin}.{codec}")

        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-decryption_key", key.strip(),
            "-i", temp_file,
            "-c", "copy", "-y",
            final_file,
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise Exception(f"FFmpeg decryption failed:\n{proc.stderr.decode()}")

        if os.path.exists(temp_file):
            os.remove(temp_file)

        if not os.path.exists(final_file) or os.path.getsize(final_file) == 0:
            raise Exception("Decrypted file is empty or missing")

        return {"file_path": final_file, "asin": asin, "codec": codec,
                "title": title, "artist": artist}


downloader = AmazonMusicDownloader()

# ── Bot ───────────────────────────────────────────────────────────────────────
app = Client("amazon_music_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# FIX: removed amzn.to — short URLs don't contain an ASIN and always fail
AMAZON_MUSIC_RE = re.compile(
    r'https?://music\.amazon\.(in|com|co\.uk|de|jp|fr|ca|com\.au)/\S+'
)

def is_amazon_music_url(text: str) -> bool:
    return bool(AMAZON_MUSIC_RE.search(text))


@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    await message.reply_text(
        "🎵 **Amazon Music Downloader Bot**\n\n"
        "Send me any Amazon Music track or album link and I'll download it.\n\n"
        "**Supported formats:** FLAC · M4A (AAC / ALAC)\n\n"
        "**Example:**\n`https://music.amazon.in/tracks/B0XXXXXXXX`",
        parse_mode=ParseMode.MARKDOWN,
    )


@app.on_message(filters.command("help"))
async def cmd_help(client: Client, message: Message):
    await message.reply_text(
        "**How to use:**\n"
        "1. Open Amazon Music and copy a track or album link\n"
        "2. Paste it here\n"
        "3. Wait for the download ⏳\n\n"
        "**Supported domains:**\n"
        "`music.amazon.in` · `.com` · `.co.uk` · `.de` · `.jp` · `.fr`\n\n"
        "**Commands:**\n"
        "/start – Welcome message\n"
        "/help  – This message",
        parse_mode=ParseMode.MARKDOWN,
    )


@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_url(client: Client, message: Message):
    text = message.text.strip()

    if not is_amazon_music_url(text):
        await message.reply_text(
            "❌ That doesn't look like an Amazon Music link.\n"
            "Expected: `https://music.amazon.in/tracks/B0XXXXXXXX`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status_msg = await message.reply_text("⏳ Fetching track info…")
    result = None  # FIX: explicit init so finally block is always safe

    try:
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, downloader.download, text)

        title  = result["title"]
        artist = result["artist"]
        codec  = result["codec"]

        await status_msg.edit_text(
            f"📤 Uploading **{title}**…", parse_mode=ParseMode.MARKDOWN
        )

        caption = f"🎵 **{title}**\n👤 {artist}\n🎚 `{codec.upper()}`"

        await message.reply_audio(
            audio=result["file_path"],
            caption=caption,
            title=title,
            performer=artist,
            parse_mode=ParseMode.MARKDOWN,
        )

        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(
            f"❌ **Error:** `{e}`", parse_mode=ParseMode.MARKDOWN
        )

    finally:
        # FIX: explicit None check instead of fragile locals() trick
        if result and os.path.exists(result["file_path"]):
            try:
                os.remove(result["file_path"])
            except Exception:
                pass


if __name__ == "__main__":
    print("Bot started…")
    app.run()
