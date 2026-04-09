"""
Amazon Music Telegram Bot
Uses: https://github.com/AmineSoukara/Amazon-Music  (API: amz.dezalty.com)

Required env vars:
  API_ID        – Telegram API ID
  API_HASH      – Telegram API Hash
  BOT_TOKEN     – Telegram Bot Token
  AMZ_TOKEN     – Bearer token from https://amz.dezalty.com/login
  DEV_USERNAME  – Your Telegram username  (shown in /start)
  DEV_URL       – Your Telegram profile URL

Install deps:
  pip install amazon-music pyrogram tgcrypto
"""

import asyncio
import os
import re
import requests
from urllib.parse import parse_qs, urlparse

from amz.api import API
from amz.converter import AudioExtension
from amz.main import AmDownloader

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ── Config ────────────────────────────────────────────────────────────────────
API_ID       = int(os.getenv("API_ID", "0"))
API_HASH     = os.getenv("API_HASH", "")
BOT_TOKEN    = os.getenv("BOT_TOKEN", "")
AMZ_TOKEN    = os.getenv("AMZ_TOKEN", "")       # https://amz.dezalty.com/login
DEV_USERNAME = os.getenv("DEV_USERNAME", "@yourdev")
DEV_URL      = os.getenv("DEV_URL", "https://t.me/yourdev")

DOWNLOAD_DIR    = "./downloads"
DEFAULT_QUALITY = "High"   # FLAC ≤16-bit / 48 kHz  (CD lossless)
AMZ_API_URL     = "https://amz.dezalty.com"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(os.path.join(DOWNLOAD_DIR, "temp"), exist_ok=True)

# ── Library clients ───────────────────────────────────────────────────────────
amz_api = API(api_url=AMZ_API_URL, access_token=AMZ_TOKEN)

amz_downloader = AmDownloader(
    path=DOWNLOAD_DIR,
    path_temp=os.path.join(DOWNLOAD_DIR, "temp"),
    api_url=AMZ_API_URL,
    access_token=AMZ_TOKEN,
    target_extension=AudioExtension.FLAC,
)

# In-memory store for pending download metadata
pending_tracks: dict[str, dict] = {}

# ── Pyrogram bot ──────────────────────────────────────────────────────────────
app = Client("amazon_music_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

AMAZON_MUSIC_RE = re.compile(
    r"https?://music\.amazon\.(in|com|co\.uk|de|jp|fr|ca|com\.au)/\S+"
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def extract_track_asin(url: str) -> str:
    """
    Pull the TRACK ASIN out of any Amazon Music URL.

    Priority:
      1. ?trackAsin=B0XXXXXXXX  ← album page pointing to a specific track
      2. /tracks/B0XXXXXXXX     ← direct track URL
      3. First B-ASIN in URL    ← last resort
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    if "trackAsin" in qs:           # most reliable — catches album+track links
        return qs["trackAsin"][0]

    m = re.search(r"/tracks?/([A-Z0-9]{10})", parsed.path)
    if m:
        return m.group(1)

    m = re.search(r"(B[0-9A-Z]{9})", url)
    if m:
        return m.group(1)

    raise ValueError(
        "Couldn't find a valid track ID in that URL.\n"
        "Tip: for album links, make sure it includes <code>?trackAsin=…</code>."
    )


def fmt_duration(ms) -> str:
    try:
        total = int(ms) // 1000
        m, s  = divmod(total, 60)
        return f"{m}:{s:02d}"
    except Exception:
        return ""


async def run(func, *args):
    return await asyncio.get_event_loop().run_in_executor(None, func, *args)


def save_thumbnail(url: str, asin: str) -> str | None:
    if not url:
        return None
    try:
        path = os.path.join(DOWNLOAD_DIR, f"{asin}_thumb.jpg")
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
        return path
    except Exception:
        return None


# ── /start ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(_client: Client, message: Message):
    await message.reply_text(
        "<blockquote>👋 Welcome to the Amazon Music Downloader!</blockquote>\n\n"
        "Drop any <b>Amazon Music</b> track link and I'll fetch it — "
        "thumbnail, full metadata, and lossless audio.\n\n"
        "<b>Quality:</b> 🎵 <b>High</b> — FLAC, ≤16-bit / 48 kHz (CD lossless)\n\n"
        "<b>Supported regions:</b> .in · .com · .co.uk · .de · .jp · .fr · .ca\n\n"
        "<blockquote>Paste a link below ⬇️</blockquote>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("👨‍💻  Dev", url=DEV_URL),
        ]]),
    )


# ── URL handler → fetch metadata, show preview ────────────────────────────────
@app.on_message(filters.text & ~filters.command(["start"]))
async def handle_url(_client: Client, message: Message):
    text = message.text.strip()

    if not AMAZON_MUSIC_RE.search(text):
        await message.reply_text(
            "<blockquote>❌ That doesn't look like an Amazon Music link.</blockquote>\n\n"
            "Expected format:\n"
            "<code>https://music.amazon.in/tracks/B0XXXXXXXX</code>\n"
            "<code>https://music.amazon.in/albums/B0XXX?trackAsin=B0XXX</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    status = await message.reply_text("🔍 Fetching track info…", parse_mode=ParseMode.HTML)

    # Extract track ASIN
    try:
        asin = extract_track_asin(text)
    except ValueError as e:
        await status.edit_text(
            f"<blockquote>❌ {e}</blockquote>", parse_mode=ParseMode.HTML
        )
        return

    try:
        # ── Call API for metadata (title, artist, album, thumbnail) ───────────
        track_res = await run(amz_api.get_track, asin)

        if not track_res.success:
            await status.edit_text(
                "<blockquote>❌ Track not found.</blockquote>\n\n"
                "• Make sure the link is a <b>track</b> link (not album-only).\n"
                "• Album links must include <code>?trackAsin=…</code>.",
                parse_mode=ParseMode.HTML,
            )
            return

        track     = track_res.data
        title     = track.get("title") or asin
        artist    = (track.get("artist") or {}).get("name") or "Unknown Artist"
        album     = (track.get("album")  or {}).get("title") or ""
        thumb_url = track.get("image") or ""
        duration  = track.get("duration_ms") or 0
        explicit  = "🅴 " if track.get("explicit") else ""

        # Store for download callback
        cb_key = f"{asin}_{message.id}"
        pending_tracks[cb_key] = {
            "asin":      asin,
            "title":     title,
            "artist":    artist,
            "album":     album,
            "thumb_url": thumb_url,
        }

        info_text = (
            f"<blockquote>🎵 Track Found!</blockquote>\n\n"
            f"<b>{explicit}{title}</b>\n"
            f"👤 {artist}\n"
            + (f"💿 {album}\n" if album else "")
            + (f"⏱ {fmt_duration(duration)}\n" if duration else "")
            + "\n<blockquote>Tap Download to get this track 👇</blockquote>"
        )

        buttons = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬇️  Download", callback_data=f"dl:{cb_key}")],
            [InlineKeyboardButton("❌  Cancel",   callback_data=f"cancel:{message.id}")],
        ])

        await status.delete()

        # Send thumbnail from API URL directly — no scraping needed
        if thumb_url:
            await message.reply_photo(
                photo=thumb_url,
                caption=info_text,
                parse_mode=ParseMode.HTML,
                reply_markup=buttons,
            )
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


# ── Download callback ─────────────────────────────────────────────────────────
@app.on_callback_query(filters.regex(r"^dl:"))
async def cb_download(_client: Client, cb: CallbackQuery):
    await cb.answer("Starting download…")

    cb_key = cb.data[3:]
    meta   = pending_tracks.get(cb_key)

    if not meta:
        try:
            await cb.message.edit_caption(
                "<blockquote>⚠️ This request expired. Please send the link again.</blockquote>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    asin      = meta["asin"]
    title     = meta["title"]
    artist    = meta["artist"]
    album     = meta["album"]
    thumb_url = meta["thumb_url"]

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    status = await cb.message.reply_text(
        f"⏳ Downloading <b>{title}</b>…\nThis may take a moment ☕",
        parse_mode=ParseMode.HTML,
    )

    result     = None
    thumb_path = None

    try:
        # ── Download + decrypt track ──────────────────────────────────────────
        result = await run(amz_downloader.download_track, asin, DEFAULT_QUALITY)

        if not result or not result.success:
            await status.edit_text(
                "<blockquote>❌ Download failed.</blockquote>\n\n"
                "The track may not be available in this region or quality.\n"
                "Check that your <code>AMZ_TOKEN</code> is valid.",
                parse_mode=ParseMode.HTML,
            )
            return

        file_path = result.file

        # Save thumbnail locally for the audio message
        thumb_path = await run(save_thumbnail, thumb_url, asin)

        caption = (
            f"<blockquote>🎵 {title}</blockquote>\n\n"
            f"👤 <b>Artist:</b> {artist}\n"
            + (f"💿 <b>Album:</b> {album}\n" if album else "")
            + "🎚 <b>Format:</b> FLAC\n"
            "✨ <b>Quality:</b> High — Lossless"
        )

        await status.edit_text(
            f"📤 Uploading <b>{title}</b>…", parse_mode=ParseMode.HTML
        )

        await cb.message.reply_audio(
            audio=file_path,
            caption=caption,
            title=title,
            performer=artist,
            thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
            parse_mode=ParseMode.HTML,
        )

        await status.delete()
        pending_tracks.pop(cb_key, None)

    except Exception as e:
        await status.edit_text(
            f"<blockquote>❌ Download failed</blockquote>\n\n<code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )

    finally:
        for path in [
            result.file if result else None,
            thumb_path,
        ]:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


# ── Cancel callback ───────────────────────────────────────────────────────────
@app.on_callback_query(filters.regex(r"^cancel:"))
async def cb_cancel(_client: Client, cb: CallbackQuery):
    await cb.answer("Cancelled ✅")
    msg_id = cb.data.split(":")[1]

    for k in [k for k in pending_tracks if k.endswith(f"_{msg_id}")]:
        pending_tracks.pop(k, None)

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.reply_text(
            "<blockquote>❌ Cancelled. Send another link anytime!</blockquote>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🎵 Amazon Music Bot started…")
    app.run()
