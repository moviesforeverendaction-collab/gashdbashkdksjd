import os
import re
import json
import shutil
import asyncio
import requests
import subprocess

from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from pyrogram.enums import ParseMode, ButtonStyle

# ── Config ────────────────────────────────────────────────────────────────────
API_ID    = int(os.getenv("API_ID", "0"))
API_HASH  = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

DEV_USERNAME  = os.getenv("DEV_USERNAME", "@yourdev")
DEV_URL       = os.getenv("DEV_URL", "https://t.me/yourdev")

DOWNLOAD_DIR = "./downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# In-memory store: maps callback key → track metadata
pending_tracks: dict[str, dict] = {}

# ── Downloader ────────────────────────────────────────────────────────────────
class AmazonMusicDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def extract_asin(self, amazon_url: str) -> str:
        match = re.search(r'(B[0-9A-Z]{9})', amazon_url)
        if not match:
            raise Exception(
                "Couldn't find a valid track ID in that URL.\n"
                "Please send a full Amazon Music track link like:\n"
                "`https://music.amazon.in/tracks/B0XXXXXXXX`"
            )
        return match.group(1)

    def detect_codec(self, file_path: str) -> str:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,bits_per_raw_sample,sample_rate",
            "-of", "json",
            file_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception("ffprobe failed to read the audio stream")
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            raise Exception("ffprobe found no audio streams")
        return streams[0]

    def fetch_metadata(self, amazon_url: str, quality: str = "hd") -> dict:
        """Fetch track metadata + stream info without downloading."""
        asin    = self.extract_asin(amazon_url)
        # quality param: 'hd' → CD quality, 'uhd' → Hi-Res
        api_url = f"https://amzn.afkarxyz.qzz.io/api/track/{asin}?quality={quality}"

        r = self.session.get(api_url, timeout=30)
        if r.status_code == 404:
            raise Exception("Track not found — this ASIN might be an album, not a single track.")
        if r.status_code != 200:
            raise Exception(f"API error — HTTP {r.status_code}")

        data = r.json()
        return {
            "asin":       asin,
            "title":      data.get("title") or asin,
            "artist":     data.get("artist") or "Unknown Artist",
            "album":      data.get("album") or "",
            "duration":   data.get("duration") or 0,
            "thumbnail":  data.get("imageUrl") or data.get("thumbnail") or data.get("image") or "",
            "stream_url": data.get("streamUrl") or "",
            "key":        data.get("decryptionKey") or "",
            "quality":    quality,
            "url":        amazon_url,
        }

    def download(self, meta: dict, output_dir: str = DOWNLOAD_DIR) -> dict:
        """Download & decrypt using pre-fetched metadata."""
        asin       = meta["asin"]
        stream_url = meta["stream_url"]
        key        = meta["key"]
        title      = meta["title"]
        artist     = meta["artist"]

        if not stream_url:
            raise Exception("No stream URL found — the track may not be available.")

        os.makedirs(output_dir, exist_ok=True)
        temp_file = os.path.join(output_dir, f"{asin}_enc.m4a")

        with self.session.get(stream_url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(temp_file, "wb") as f:
                shutil.copyfileobj(resp.raw, f)

        # Safe file name: "Artist - Title.ext"
        safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{artist} - {title}")

        if not key:
            final_file = os.path.join(output_dir, f"{safe_name}.m4a")
            os.rename(temp_file, final_file)
            return {**meta, "file_path": final_file, "codec": "m4a",
                    "bit_depth": None, "sample_rate": None}

        stream_info = self.detect_codec(temp_file)
        codec       = stream_info.get("codec_name", "flac")
        bit_depth   = stream_info.get("bits_per_raw_sample")
        sample_rate = stream_info.get("sample_rate")

        ext = "flac" if codec == "flac" else "m4a"
        final_file = os.path.join(output_dir, f"{safe_name}.{ext}")

        cmd = [
            "ffmpeg", "-loglevel", "error",
            "-decryption_key", key.strip(),
            "-i", temp_file,
            "-c", "copy", "-y",
            final_file,
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise Exception(f"Decryption failed:\n{proc.stderr.decode()}")

        if os.path.exists(temp_file):
            os.remove(temp_file)

        if not os.path.exists(final_file) or os.path.getsize(final_file) == 0:
            raise Exception("Decrypted file is empty — something went wrong.")

        return {**meta, "file_path": final_file, "codec": codec,
                "bit_depth": bit_depth, "sample_rate": sample_rate}

    def download_thumbnail(self, url: str, asin: str) -> str | None:
        """Download thumbnail image, return local path or None."""
        if not url:
            return None
        try:
            thumb_path = os.path.join(DOWNLOAD_DIR, f"{asin}_thumb.jpg")
            r = self.session.get(url, timeout=15)
            r.raise_for_status()
            with open(thumb_path, "wb") as f:
                f.write(r.content)
            return thumb_path
        except Exception:
            return None


downloader = AmazonMusicDownloader()

# ── Bot ───────────────────────────────────────────────────────────────────────
app = Client("amazon_music_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

AMAZON_MUSIC_RE = re.compile(
    r'https?://music\.amazon\.(in|com|co\.uk|de|jp|fr|ca|com\.au)/\S+'
)

def is_amazon_music_url(text: str) -> bool:
    return bool(AMAZON_MUSIC_RE.search(text))

def quality_label(quality: str, bit_depth=None, sample_rate=None) -> str:
    if quality == "uhd":
        bd  = f"{bit_depth}-bit" if bit_depth else "24-bit"
        sr  = f"{int(sample_rate)//1000} kHz" if sample_rate else "192 kHz"
        return f"🌟 Ultra HD — {bd} / {sr}"
    else:
        bd  = f"{bit_depth}-bit" if bit_depth else "16-bit"
        sr  = f"{int(sample_rate)//1000} kHz" if sample_rate else "44.1 kHz"
        return f"💿 HD — {bd} / {sr}"

def fmt_duration(secs: int) -> str:
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


# ── /start ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👨‍💻  Dev",
                url=DEV_URL,
                style=ButtonStyle.PRIMARY
            ),
            InlineKeyboardButton(
                "🏅  Credits",
                callback_data="show_credits",
                style=ButtonStyle.SUCCESS
            ),
        ]
    ])

    await message.reply_text(
        "<blockquote>👋 Hey there! Welcome to the Amazon Music Downloader.</blockquote>\n\n"
        "Just drop any <b>Amazon Music</b> track link and I'll handle the rest — "
        "thumbnail, full metadata, and your choice of quality.\n\n"
        "<b>Supported qualities:</b>\n"
        "  💿 <b>HD (Lossless)</b> — 16-bit / 44.1 kHz (CD quality)\n"
        "  🌟 <b>Ultra HD (Hi-Res)</b> — up to 24-bit / 192 kHz\n\n"
        "<b>Supported regions:</b> .in · .com · .co.uk · .de · .jp · .fr · .ca\n\n"
        "<blockquote>Paste a link below to get started ⬇️</blockquote>",
        parse_mode=ParseMode.HTML,
        reply_markup=buttons,
    )


# ── Credits callback ──────────────────────────────────────────────────────────
@app.on_callback_query(filters.regex("^show_credits$"))
async def cb_credits(client: Client, cb: CallbackQuery):
    await cb.answer()
    await cb.message.reply_text(
        "<blockquote>🏅 Credits</blockquote>\n\n"
        "This bot was built with ❤️ using:\n\n"
        "• <b>Pyrogram / Kurigram</b> — Telegram MTProto client\n"
        "• <b>FFmpeg</b> — Audio decryption &amp; stream copying\n"
        "• <b>Amazon Music API</b> — Track metadata &amp; stream URLs\n\n"
        f"<blockquote>Made by {DEV_USERNAME}</blockquote>",
        parse_mode=ParseMode.HTML,
    )


# ── URL handler → show info + quality picker ──────────────────────────────────
@app.on_message(filters.text & ~filters.command(["start", "help"]))
async def handle_url(client: Client, message: Message):
    text = message.text.strip()

    if not is_amazon_music_url(text):
        await message.reply_text(
            "<blockquote>❌ That doesn't look like an Amazon Music link.</blockquote>\n\n"
            "Please send something like:\n"
            "<code>https://music.amazon.in/tracks/B0XXXXXXXX</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    status = await message.reply_text(
        "🔍 Hang on, fetching track details…",
        parse_mode=ParseMode.HTML,
    )

    try:
        loop = asyncio.get_event_loop()
        meta = await loop.run_in_executor(None, downloader.fetch_metadata, text, "hd")

        asin      = meta["asin"]
        title     = meta["title"]
        artist    = meta["artist"]
        album     = meta["album"]
        duration  = meta["duration"]
        thumb_url = meta["thumbnail"]

        # Store for later download
        key_hd  = f"{asin}_hd_{message.id}"
        key_uhd = f"{asin}_uhd_{message.id}"
        pending_tracks[key_hd]  = {**meta, "quality": "hd",  "user_url": text}
        pending_tracks[key_uhd] = {**meta, "quality": "uhd", "user_url": text}

        info_text = (
            f"<blockquote>🎵 Track Found!</blockquote>\n\n"
            f"<b>{title}</b>\n"
            f"👤 {artist}\n"
            + (f"💿 {album}\n" if album else "")
            + (f"⏱ {fmt_duration(duration)}\n" if duration else "")
            + f"\n<blockquote>Pick your preferred quality below 👇</blockquote>"
        )

        buttons = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💿  HD  •  16-bit / 44.1 kHz",
                    callback_data=f"dl:{key_hd}",
                    style=ButtonStyle.SUCCESS
                ),
            ],
            [
                InlineKeyboardButton(
                    "🌟  Ultra HD  •  24-bit / 192 kHz",
                    callback_data=f"dl:{key_uhd}",
                    style=ButtonStyle.PRIMARY
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌  Cancel",
                    callback_data=f"cancel:{message.id}",
                    style=ButtonStyle.DANGER
                ),
            ],
        ])

        thumb_path = await loop.run_in_executor(
            None, downloader.download_thumbnail, thumb_url, asin
        )

        await status.delete()

        if thumb_path and os.path.exists(thumb_path):
            await message.reply_photo(
                photo=thumb_path,
                caption=info_text,
                parse_mode=ParseMode.HTML,
                reply_markup=buttons,
            )
            try:
                os.remove(thumb_path)
            except Exception:
                pass
        else:
            await message.reply_text(
                info_text,
                parse_mode=ParseMode.HTML,
                reply_markup=buttons,
            )

    except Exception as e:
        await status.edit_text(
            f"<blockquote>❌ Something went wrong</blockquote>\n\n<code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )


# ── Quality selection callback → download ─────────────────────────────────────
@app.on_callback_query(filters.regex(r"^dl:"))
async def cb_download(client: Client, cb: CallbackQuery):
    await cb.answer("Starting download…")

    track_key = cb.data[3:]  # strip "dl:"
    meta = pending_tracks.get(track_key)

    if not meta:
        await cb.message.edit_caption(
            "<blockquote>⚠️ This request has expired. Please send the link again.</blockquote>",
            parse_mode=ParseMode.HTML,
        )
        return

    quality   = meta["quality"]
    title     = meta["title"]
    artist    = meta["artist"]
    album     = meta["album"]
    user_url  = meta["user_url"]

    q_label = "💿 HD  •  16-bit / 44.1 kHz" if quality == "hd" \
              else "🌟 Ultra HD  •  24-bit / 192 kHz"

    # Remove quality buttons, show status
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    status = await cb.message.reply_text(
        f"⏳ Downloading in <b>{q_label}</b>…\nThis may take a moment ☕",
        parse_mode=ParseMode.HTML,
    )

    result = None
    thumb_path = None
    try:
        loop = asyncio.get_event_loop()

        # If quality differs from what was fetched, re-fetch with correct quality
        if quality != meta.get("quality_fetched", "hd"):
            meta = await loop.run_in_executor(
                None, downloader.fetch_metadata, user_url, quality
            )

        result = await loop.run_in_executor(None, downloader.download, meta)

        title      = result["title"]
        artist     = result["artist"]
        album      = result["album"]
        codec      = result["codec"]
        bit_depth  = result.get("bit_depth")
        sample_rate = result.get("sample_rate")

        # Re-download thumb for audio message
        if meta.get("thumbnail"):
            thumb_path = await loop.run_in_executor(
                None, downloader.download_thumbnail,
                meta["thumbnail"], meta["asin"]
            )

        await status.edit_text(
            f"📤 Uploading <b>{title}</b>…",
            parse_mode=ParseMode.HTML,
        )

        caption = (
            f"<blockquote>🎵 {title}</blockquote>\n\n"
            f"👤 <b>Artist:</b> {artist}\n"
            + (f"💿 <b>Album:</b> {album}\n" if album else "")
            + f"🎚 <b>Format:</b> {codec.upper()}\n"
            f"✨ <b>Quality:</b> {q_label}"
        )

        await cb.message.reply_audio(
            audio=result["file_path"],
            caption=caption,
            title=title,
            performer=artist,
            thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
            parse_mode=ParseMode.HTML,
        )

        await status.delete()

        # Clean up pending
        pending_tracks.pop(track_key, None)
        # Also clean the other quality key
        other_quality = "uhd" if quality == "hd" else "hd"
        other_key = track_key.replace(f"_{quality}_", f"_{other_quality}_")
        pending_tracks.pop(other_key, None)

    except Exception as e:
        await status.edit_text(
            f"<blockquote>❌ Download failed</blockquote>\n\n<code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )

    finally:
        if result and result.get("file_path") and os.path.exists(result["file_path"]):
            try:
                os.remove(result["file_path"])
            except Exception:
                pass
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass


# ── Cancel callback ───────────────────────────────────────────────────────────
@app.on_callback_query(filters.regex(r"^cancel:"))
async def cb_cancel(client: Client, cb: CallbackQuery):
    await cb.answer("Cancelled ✅")
    msg_id = cb.data.split(":")[1]

    # Clean up any pending keys for this message
    keys_to_remove = [k for k in pending_tracks if k.endswith(f"_{msg_id}")]
    for k in keys_to_remove:
        pending_tracks.pop(k, None)

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.reply_text(
            "<blockquote>❌ Download cancelled. Feel free to send another link anytime!</blockquote>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# ── /help ─────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("help"))
async def cmd_help(client: Client, message: Message):
    await message.reply_text(
        "<blockquote>📖 How to use this bot</blockquote>\n\n"
        "1. Open Amazon Music and copy any track link\n"
        "2. Paste it here in chat\n"
        "3. Pick your quality (HD or Ultra HD)\n"
        "4. Wait for the upload ⏳\n\n"
        "<b>Supported qualities:</b>\n"
        "  💿 <b>HD</b> — 16-bit / 44.1 kHz (CD quality, Lossless)\n"
        "  🌟 <b>Ultra HD</b> — up to 24-bit / 192 kHz (Hi-Res Lossless)\n\n"
        "<b>Commands:</b>\n"
        "/start — Main menu\n"
        "/help — This message\n\n"
        "<blockquote>Files are named as <code>Artist - Title.flac</code> automatically 🎵</blockquote>",
        parse_mode=ParseMode.HTML,
    )


if __name__ == "__main__":
    print("🎵 Amazon Music Bot started…")
    app.run()
