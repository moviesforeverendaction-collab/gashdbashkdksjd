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

pending_tracks: dict[str, dict] = {}

# ── Downloader ────────────────────────────────────────────────────────────────
class AmazonMusicDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/134.0.0.0 Safari/537.36"
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
            raise Exception("Couldn't find a valid ASIN in the URL.")
        return match.group(1)

    def get_url_type(self, url: str) -> str:
        if "/tracks/" in url:
            return "track"
        elif "/albums/" in url:
            return "album"
        elif "/playlists/" in url:
            return "playlist"
        return "track"

    def scrape_amazon_page(self, amazon_url: str) -> dict:
        result = {"title": "", "artist": "", "album": "", "thumbnail": ""}

        try:
            r = self.session.get(amazon_url, timeout=15)
            html = r.text

            # og:meta
            for attr in ("property", "name"):
                for tag, key in [("og:title", "title"), ("og:image", "thumbnail"), ("music:musician", "artist")]:
                    m = re.search(
                        rf'<meta[^>]+{attr}=["\']{re.escape(tag)}["\'][^>]*content=["\'](.*?)["\']',
                        html, re.IGNORECASE
                    )
                    if m and not result.get(key):
                        result[key] = m.group(1).strip()

            # Strong <title> tag parser (fixes "Unknown Artist" on albums)
            title_tag = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
            if title_tag and not result["title"]:
                title_text = title_tag.group(1).strip()
                if " on Amazon Music" in title_text:
                    title_text = title_text.split(" on Amazon Music")[0].strip()

                # Album format: "Voicenotes by Charlie Puth"
                if " by " in title_text.lower():
                    parts = re.split(r'\s+by\s+', title_text, 1, re.IGNORECASE)
                    result["title"] = parts[0].strip()
                    result["artist"] = parts[1].strip() if len(parts) > 1 else ""
                # Track format fallback
                elif " song by " in title_text.lower():
                    parts = re.split(r'\s+song by\s+', title_text, 1, re.IGNORECASE)
                    result["title"] = parts[0].strip()
                    if len(parts) == 2:
                        remaining = parts[1]
                        if " from " in remaining.lower():
                            sub = re.split(r'\s+from\s+', remaining, 1, re.IGNORECASE)
                            result["artist"] = sub[0].strip()
                            result["album"] = sub[1].strip() if len(sub) > 1 else ""
                        else:
                            result["artist"] = remaining.strip()

            # JSON-LD backup
            for ld_raw in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.DOTALL):
                try:
                    ld = json.loads(ld_raw)
                    if isinstance(ld, dict):
                        if not result["title"]: result["title"] = ld.get("name", "")
                        if not result["thumbnail"]: result["thumbnail"] = ld.get("image", "")
                        if not result["album"]:
                            album = ld.get("inAlbum") or ld.get("album")
                            if isinstance(album, dict): result["album"] = album.get("name", "")
                        if not result["artist"]:
                            artist = ld.get("byArtist") or ld.get("artist")
                            if isinstance(artist, list) and artist: artist = artist[0]
                            if isinstance(artist, dict): result["artist"] = artist.get("name", "")
                except:
                    pass

        except Exception:
            pass

        return result

    def fetch_metadata(self, amazon_url: str, quality: str = "hd") -> dict:
        asin = self.extract_asin(amazon_url)
        page_meta = self.scrape_amazon_page(amazon_url)

        api_url = f"https://amzn.afkarxyz.qzz.io/api/track/{asin}?quality={quality}"
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
            "quality": quality,
            "url": amazon_url,
        }

    def detect_codec(self, file_path: str) -> dict:
        cmd = ["ffprobe", "-v", "quiet", "-select_streams", "a:0",
               "-show_entries", "stream=codec_name,bits_per_raw_sample,sample_rate",
               "-of", "json", file_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception("ffprobe failed")
        data = json.loads(result.stdout)
        return data.get("streams", [{}])[0]

    def download(self, meta: dict, output_dir: str = DOWNLOAD_DIR) -> dict:
        asin = meta["asin"]
        stream_url = meta["stream_url"]
        key = meta["key"]
        title = meta["title"]
        artist = meta["artist"]

        if not stream_url:
            raise Exception("No stream URL found.")

        os.makedirs(output_dir, exist_ok=True)
        temp_file = os.path.join(output_dir, f"{asin}_enc.m4a")

        with self.session.get(stream_url, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(temp_file, "wb") as f:
                shutil.copyfileobj(resp.raw, f)

        safe_name = re.sub(r'[\\/*?:"<>|]', "", f"{artist} - {title}")

        if not key:
            final_file = os.path.join(output_dir, f"{safe_name}.m4a")
            os.rename(temp_file, final_file)
            return {**meta, "file_path": final_file, "codec": "m4a"}

        stream_info = self.detect_codec(temp_file)
        codec = stream_info.get("codec_name", "flac")
        ext = "flac" if codec == "flac" else "m4a"
        final_file = os.path.join(output_dir, f"{safe_name}.{ext}")

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

def quality_label(quality: str) -> str:
    return "🌟 Ultra HD — 24-bit / 192 kHz" if quality == "uhd" else "💿 HD — 16-bit / 44.1 kHz"

def fmt_duration(secs: int) -> str:
    m, s = divmod(int(secs), 60)
    return f"{m}:{s:02d}"


# ── /start ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(client: Client, message: Message):
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("👨‍💻 Dev", url=DEV_URL, style=ButtonStyle.PRIMARY),
        InlineKeyboardButton("🏅 Credits", callback_data="show_credits", style=ButtonStyle.SUCCESS),
    ]])

    await message.reply_text(
        "<blockquote>👋 Amazon Music Downloader Ready!</blockquote>\n\n"
        "Send any <b>track</b>, <b>album</b> or <b>playlist</b> link.\n\n"
        "✅ Tracks → HD / Ultra HD download\n"
        "✅ Albums & Playlists → full metadata + thumbnail\n\n"
        "<blockquote>Paste link below ⬇️</blockquote>",
        parse_mode=ParseMode.HTML,
        reply_markup=buttons,
    )


# ── Credits ───────────────────────────────────────────────────────────────────
@app.on_callback_query(filters.regex("^show_credits$"))
async def cb_credits(client: Client, cb: CallbackQuery):
    await cb.answer()
    await cb.message.reply_text(
        "<blockquote>🏅 Credits</blockquote>\n\n"
        "Built with ❤️ using Pyrogram + FFmpeg + AfkArxyz API\n\n"
        f"<blockquote>Made by {DEV_USERNAME}</blockquote>",
        parse_mode=ParseMode.HTML,
    )


# ── URL Handler (FIXED) ───────────────────────────────────────────────────────
@app.on_message(filters.text & ~filters.command(["start"]))
async def handle_url(client: Client, message: Message):
    text = message.text.strip()
    if not is_amazon_music_url(text):
        await message.reply_text("<blockquote>❌ Not an Amazon Music link.</blockquote>", parse_mode=ParseMode.HTML)
        return

    url_type = downloader.get_url_type(text)
    status = await message.reply_text("🔍 Fetching details…", parse_mode=ParseMode.HTML)

    try:
        loop = asyncio.get_event_loop()
        asin = downloader.extract_asin(text)          # ← always extract first

        if url_type == "track":
            meta = await loop.run_in_executor(None, downloader.fetch_metadata, text, "hd")
            page_meta = {                                 # reuse for thumbnail
                "title": meta["title"],
                "artist": meta["artist"],
                "album": meta["album"],
                "thumbnail": meta["thumbnail"]
            }
            key_hd = f"{asin}_hd_{message.id}"
            key_uhd = f"{asin}_uhd_{message.id}"

            pending_tracks[key_hd] = {**meta, "quality": "hd", "user_url": text}
            pending_tracks[key_uhd] = {**meta, "quality": "uhd", "user_url": text}

            info_text = (
                f"<blockquote>🎵 Track Found!</blockquote>\n\n"
                f"<b>{meta['title']}</b>\n"
                f"👤 {meta['artist']}\n"
                + (f"💿 {meta['album']}\n" if meta['album'] else "")
                + (f"⏱ {fmt_duration(meta['duration'])}\n" if meta['duration'] else "")
                + "\n<blockquote>Pick quality below 👇</blockquote>"
            )

            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("💿 HD • 16-bit / 44.1 kHz", callback_data=f"dl:{key_hd}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton("🌟 Ultra HD • 24-bit / 192 kHz", callback_data=f"dl:{key_uhd}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{message.id}", style=ButtonStyle.DANGER)],
            ])

        else:  # album or playlist
            page_meta = downloader.scrape_amazon_page(text)
            info_text = (
                f"<blockquote>📀 {url_type.capitalize()} Found!</blockquote>\n\n"
                f"<b>{page_meta.get('title') or 'Album/Playlist'}</b>\n"
                f"👤 {page_meta.get('artist') or 'Unknown Artist'}\n"
                + (f"💿 {page_meta.get('album') or ''}\n" if page_meta.get('album') else "")
                + "\n<blockquote>Multi-track download coming soon™</blockquote>\n"
                f"<i>Send individual track links for now.</i>"
            )
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Got it", callback_data="dismiss")]])

        # Thumbnail (now safe in both branches)
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


# ── Download callback ─────────────────────────────────────────────────────────
@app.on_callback_query(filters.regex(r"^dl:"))
async def cb_download(client: Client, cb: CallbackQuery):
    await cb.answer("Starting download…")
    track_key = cb.data[3:]
    meta = pending_tracks.get(track_key)
    if not meta:
        await cb.message.edit_caption("<blockquote>⚠️ Request expired.</blockquote>", parse_mode=ParseMode.HTML)
        return

    quality = meta["quality"]
    q_label = quality_label(quality)

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except:
        pass

    status = await cb.message.reply_text(f"⏳ Downloading <b>{q_label}</b>…", parse_mode=ParseMode.HTML)

    result = None
    thumb_path = None
    try:
        loop = asyncio.get_event_loop()
        if quality == "uhd":
            uhd_meta = await loop.run_in_executor(None, downloader.fetch_metadata, meta["user_url"], "uhd")
            meta = {**meta, "stream_url": uhd_meta["stream_url"], "key": uhd_meta["key"], "quality": "uhd"}

        result = await loop.run_in_executor(None, downloader.download, meta)

        if meta.get("thumbnail"):
            thumb_path = await loop.run_in_executor(None, downloader.download_thumbnail, meta["thumbnail"], meta["asin"])

        await status.edit_text(f"📤 Uploading <b>{result['title']}</b>…", parse_mode=ParseMode.HTML)

        caption = (
            f"<blockquote>🎵 {result['title']}</blockquote>\n\n"
            f"👤 <b>Artist:</b> {result['artist']}\n"
            + (f"💿 <b>Album:</b> {result['album']}\n" if result['album'] else "")
            + f"🎚 <b>Format:</b> {result['codec'].upper()}\n"
            f"✨ <b>Quality:</b> {q_label}"
        )

        await cb.message.reply_audio(
            audio=result["file_path"],
            caption=caption,
            title=result["title"],
            performer=result["artist"],
            thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
            parse_mode=ParseMode.HTML,
        )

        await status.delete()

        pending_tracks.pop(track_key, None)
        other_key = track_key.replace(f"_{quality}_", f"_{'uhd' if quality == 'hd' else 'hd'}_")
        pending_tracks.pop(other_key, None)

    except Exception as e:
        await status.edit_text(f"<blockquote>❌ Download failed</blockquote>\n\n<code>{e}</code>", parse_mode=ParseMode.HTML)
    finally:
        if result and result.get("file_path") and os.path.exists(result["file_path"]):
            try: os.remove(result["file_path"])
            except: pass
        if thumb_path and os.path.exists(thumb_path):
            try: os.remove(thumb_path)
            except: pass


@app.on_callback_query(filters.regex(r"^cancel:|^dismiss$"))
async def cb_cancel(client: Client, cb: CallbackQuery):
    await cb.answer("Done ✅")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except:
        pass


if __name__ == "__main__":
    print("🎵 Amazon Music Bot started… (album + playlist support)")
    app.run()
