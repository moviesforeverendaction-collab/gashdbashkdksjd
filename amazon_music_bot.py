import os
import re
import json
import shutil
import asyncio
import requests
import subprocess
from urllib.parse import urlparse, parse_qs

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

# ── Downloader ────────────────────────────────────────────────────────────────
class AmazonMusicDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://music.amazon.in/",
        })

    def extract_asin(self, amazon_url: str) -> str:
        parsed = urlparse(amazon_url)
        qs = parse_qs(parsed.query)
        if "trackAsin" in qs and qs["trackAsin"]:
            track_asin = qs["trackAsin"][0]
            if re.match(r'^B[0-9A-Z]{9}$', track_asin):
                return track_asin
        match = re.search(r'(B[0-9A-Z]{9})', parsed.path or amazon_url)
        if not match:
            raise Exception("No valid track ID found in the link.")
        return match.group(1)

    def get_url_type(self, url: str) -> str:
        if "/tracks/" in url:
            return "track"
        elif "/albums/" in url:
            return "album"
        elif "/playlists/" in url:
            return "playlist"
        return "unknown"

    def scrape_amazon_page(self, amazon_url: str) -> dict:
        """Basic scrape (often fails due to JS)"""
        result = {"title": "", "artist": "", "album": "", "thumbnail": ""}
        try:
            r = self.session.get(amazon_url, timeout=15)
            html = r.text

            # og:meta
            for attr in ("property", "name"):
                for tag, key in [("og:title", "title"), ("og:image", "thumbnail"), ("music:musician", "artist")]:
                    m = re.search(rf'<meta[^>]+{attr}=["\']{re.escape(tag)}["\'][^>]*content=["\'](.*?)["\']', html, re.IGNORECASE)
                    if m and not result.get(key):
                        result[key] = m.group(1).strip()

            # <title> tag fallback
            title_tag = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
            if title_tag and not result["title"]:
                text = title_tag.group(1).strip()
                if " on Amazon Music" in text:
                    text = text.split(" on Amazon Music")[0].strip()
                if " by " in text.lower():
                    parts = re.split(r'\s+by\s+', text, 1, re.IGNORECASE)
                    result["title"] = parts[0].strip()
                    result["artist"] = parts[1].strip() if len(parts) > 1 else ""
        except Exception:
            pass
        return result

    def enrich_with_musicbrainz(self, search_query: str) -> dict:
        """MusicBrainz fallback - fixes title, artist, album & thumbnail"""
        if not search_query or search_query.strip() == "":
            return {}

        try:
            # MusicBrainz requires proper User-Agent
            headers = {"User-Agent": f"AmazonMusicBot/1.0[](https://t.me/{DEV_USERNAME.strip('@')})"}
            url = f"https://musicbrainz.org/ws/2/recording?query={search_query}&fmt=json&limit=1"
            r = self.session.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                return {}

            data = r.json()
            recordings = data.get("recordings", [])
            if not recordings:
                return {}

            rec = recordings[0]
            title = rec.get("title", "")
            artist = rec["artist-credit"][0]["artist"]["name"] if rec.get("artist-credit") else ""

            album = ""
            thumbnail = ""
            if rec.get("releases"):
                release = rec["releases"][0]
                album = release.get("title", "")
                mbid = release.get("id")
                if mbid:
                    thumbnail = f"https://coverartarchive.org/release/{mbid}/front-500"

            return {
                "title": title,
                "artist": artist,
                "album": album,
                "thumbnail": thumbnail
            }
        except Exception:
            return {}

    def fetch_metadata(self, amazon_url: str) -> dict:
        asin = self.extract_asin(amazon_url)
        page_meta = self.scrape_amazon_page(amazon_url)

        # MusicBrainz fallback if Amazon scrape is poor
        if page_meta["artist"] in ("", "Unknown Artist") or page_meta["title"] == asin:
            query = f"{page_meta.get('title','')} {page_meta.get('artist','')} {asin}".strip()
            mb_data = self.enrich_with_musicbrainz(query)
            if mb_data:
                for k in ["title", "artist", "album", "thumbnail"]:
                    if mb_data.get(k):
                        page_meta[k] = mb_data[k]

        # Fallback query using ASIN only if still bad
        if page_meta["artist"] in ("", "Unknown Artist"):
            mb_data = self.enrich_with_musicbrainz(asin)
            if mb_data:
                for k in ["title", "artist", "album", "thumbnail"]:
                    if mb_data.get(k):
                        page_meta[k] = mb_data[k]

        # Get stream from AfkArxyz (Ultra HD)
        api_url = f"https://amzn.afkarxyz.qzz.io/api/track/{asin}?quality=uhd"
        r = self.session.get(api_url, timeout=30)
        if r.status_code != 200:
            raise Exception(f"API error — HTTP {r.status_code}")

        data = r.json()
        stream_url = next((data.get(k) for k in ["streamUrl","stream_url","url","audioUrl"] if data.get(k)), "")
        key        = next((data.get(k) for k in ["decryptionKey","decryption_key","key","encKey"] if data.get(k)), "")

        return {
            "asin": asin,
            "title": page_meta.get("title") or asin,
            "artist": page_meta.get("artist") or "Unknown Artist",
            "album": page_meta.get("album") or "",
            "duration": data.get("duration") or 0,
            "thumbnail": page_meta.get("thumbnail") or "",
            "stream_url": stream_url,
            "key": key,
            "url": amazon_url,
        }

    # ── Rest of methods (unchanged) ───────────────────────────────────────
    def detect_codec(self, file_path: str) -> dict:
        cmd = ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
               "-show_entries", "stream=codec_name,bits_per_raw_sample,sample_rate",
               "-of", "json", file_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception("ffprobe failed")
        data = json.loads(result.stdout)
        return data.get("streams", [{}])[0]

    def download(self, meta: dict) -> dict:
        asin = meta["asin"]
        stream_url = meta["stream_url"]
        key = meta["key"]
        title = meta["title"]
        artist = meta["artist"]

        if not stream_url:
            raise Exception("No stream URL found.")

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        temp_file = os.path.join(DOWNLOAD_DIR, f"{asin}_enc.m4a")

        with self.session.get(stream_url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(temp_file, "wb") as f:
                shutil.copyfileobj(resp.raw, f)

        safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{artist} - {title}")

        if not key:
            final_file = os.path.join(DOWNLOAD_DIR, f"{safe_name}.m4a")
            os.rename(temp_file, final_file)
            return {**meta, "file_path": final_file, "codec": "m4a"}

        stream_info = self.detect_codec(temp_file)
        codec = stream_info.get("codec_name", "flac")
        ext = "flac" if codec == "flac" else "m4a"
        final_file = os.path.join(DOWNLOAD_DIR, f"{safe_name}.{ext}")

        cmd = ["ffmpeg", "-loglevel", "error", "-decryption_key", key.strip(),
               "-i", temp_file, "-c", "copy", "-y", final_file]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise Exception(f"Decryption failed: {proc.stderr.decode()}")

        if os.path.exists(temp_file):
            os.remove(temp_file)

        return {**meta, "file_path": final_file, "codec": codec,
                "bit_depth": stream_info.get("bits_per_raw_sample"),
                "sample_rate": stream_info.get("sample_rate")}

    def download_thumbnail(self, url: str, asin: str) -> str | None:
        if not url:
            return None
        try:
            thumb_path = os.path.join(DOWNLOAD_DIR, f"{asin}_thumb.jpg")
            r = self.session.get(url, timeout=15)
            r.raise_for_status()
            with open(thumb_path, "wb") as f:
                f.write(r.content)
            return thumb_path
        except:
            return None


downloader = AmazonMusicDownloader()

# ── Bot ───────────────────────────────────────────────────────────────────────
app = Client("amazon_music_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

AMAZON_MUSIC_RE = re.compile(r'https?://music\.amazon\.(in|com|co\.uk|de|jp|fr|ca|com\.au)/\S+')

def is_amazon_music_url(text: str) -> bool:
    return bool(AMAZON_MUSIC_RE.search(text))


# ── /start ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("👨‍💻 Dev", url=DEV_URL, style=ButtonStyle.PRIMARY),
        InlineKeyboardButton("🏅 Credits", callback_data="show_credits", style=ButtonStyle.SUCCESS),
    ]])

    await message.reply_text(
        "<blockquote>👋 Amazon Music Downloader Ready!</blockquote>\n\n"
        "Send any <b>track</b>, <b>album</b> or <b>playlist</b> link.\n"
        "Auto Ultra HD + MusicBrainz metadata + Cover Art.\n\n"
        "<blockquote>Paste link below ⬇️</blockquote>",
        parse_mode=ParseMode.HTML,
        reply_markup=buttons,
    )


@app.on_callback_query(filters.regex("^show_credits$"))
async def cb_credits(client: Client, cb: CallbackQuery):
    await cb.answer()
    await cb.message.reply_text(
        "<blockquote>🏅 Credits</blockquote>\n\n"
        "Built with ❤️ using Pyrogram + FFmpeg + AfkArxyz + MusicBrainz API\n\n"
        f"<blockquote>Made by {DEV_USERNAME}</blockquote>",
        parse_mode=ParseMode.HTML,
    )


# ── Main Handler ──────────────────────────────────────────────────────────────
@app.on_message(filters.text & ~filters.command(["start"]))
async def handle_url(client: Client, message: Message):
    text = message.text.strip()
    if not is_amazon_music_url(text):
        await message.reply_text("<blockquote>❌ Not an Amazon Music link.</blockquote>", parse_mode=ParseMode.HTML)
        return

    if "/search/" in text.lower() or "/api/" in text.lower():
        await message.reply_text(
            "<blockquote>🔍 Search or API links not supported.</blockquote>\n"
            "Please send a direct track/album/playlist link.",
            parse_mode=ParseMode.HTML
        )
        return

    url_type = downloader.get_url_type(text)
    status = await message.reply_text("🔍 Fetching details…", parse_mode=ParseMode.HTML)

    try:
        loop = asyncio.get_event_loop()
        asin = downloader.extract_asin(text)

        if url_type == "track":
            meta = await loop.run_in_executor(None, downloader.fetch_metadata, text)
            result = await loop.run_in_executor(None, downloader.download, meta)

            thumb_path = await loop.run_in_executor(
                None, downloader.download_thumbnail, meta["thumbnail"], asin
            )

            await status.edit_text(f"📤 Uploading <b>{result['title']}</b>…", parse_mode=ParseMode.HTML)

            caption = (
                f"<blockquote>🎵 {result['title']}</blockquote>\n\n"
                f"👤 <b>Artist:</b> {result['artist']}\n"
                + (f"💿 <b>Album:</b> {result['album']}\n" if result['album'] else "")
                + f"🎚 <b>Format:</b> {result['codec'].upper()}\n"
                f"✨ <b>Quality:</b> 🌟 Ultra HD — 24-bit / 192 kHz"
            )

            await message.reply_audio(
                audio=result["file_path"],
                caption=caption,
                title=result["title"],
                performer=result["artist"],
                thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
                parse_mode=ParseMode.HTML,
            )
            await status.delete()

        else:  # album or playlist
            page_meta = downloader.scrape_amazon_page(text)
            # MusicBrainz fallback for albums too
            if page_meta["artist"] in ("", "Unknown Artist"):
                query = f"{page_meta.get('title','')} {page_meta.get('artist','')}"
                mb = downloader.enrich_with_musicbrainz(query)
                if mb:
                    page_meta.update(mb)

            info_text = (
                f"<blockquote>📀 {url_type.capitalize()} Found!</blockquote>\n\n"
                f"<b>{page_meta.get('title') or 'Album/Playlist'}</b>\n"
                f"👤 {page_meta.get('artist') or 'Unknown Artist'}\n"
                + (f"💿 {page_meta.get('album') or ''}\n" if page_meta.get('album') else "")
                + "\n<blockquote>Multi-track download coming soon™</blockquote>"
            )
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Got it", callback_data="dismiss")]])

            thumb_path = await loop.run_in_executor(
                None, downloader.download_thumbnail, page_meta.get("thumbnail", ""), asin
            )

            await status.delete()
            if thumb_path and os.path.exists(thumb_path):
                await message.reply_photo(photo=thumb_path, caption=info_text, parse_mode=ParseMode.HTML, reply_markup=buttons)
                try: os.remove(thumb_path)
                except: pass
            else:
                await message.reply_text(info_text, parse_mode=ParseMode.HTML, reply_markup=buttons)

    except Exception as e:
        await status.edit_text(f"<blockquote>❌ Error</blockquote>\n\n<code>{e}</code>", parse_mode=ParseMode.HTML)


@app.on_callback_query(filters.regex(r"^dismiss$"))
async def cb_dismiss(client: Client, cb: CallbackQuery):
    await cb.answer("Done ✅")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except:
        pass


if __name__ == "__main__":
    print("🎵 Amazon Music Bot started… (MusicBrainz + Ultra HD)")
    app.run()
