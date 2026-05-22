import os
import uuid
import time
import json
import subprocess
import asyncio
import gc

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import yt_dlp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)

from pyrogram import Client as PyroClient

# ================= CONFIG =================

TOKEN ="8731635445:AAER_lUzaKC21xR31K3EXJN-zUk9t_cr-v4"
API_ID ="39570484"
API_HASH ="79114c616c581109bd61e7b991e595b5"

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "yt_cookies.txt"

MAX_UPLOAD_BYTES = 1700 * 1024 * 1024

EXECUTOR = ThreadPoolExecutor(max_workers=6)

ACTIVE_DOWNLOADS = {}

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ================= PYROGRAM =================

pyro = PyroClient(
    "bot_session",
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=TOKEN,
    no_updates=True
)

# ================= UTILS =================

def make_bar(percent):

    filled = int(percent / 10)

    return "█" * filled + "░" * (10 - filled)

async def safe_edit(msg, text, keyboard=None):

    try:

        await msg.edit_text(
            text,
            reply_markup=keyboard
        )

    except:
        pass

def get_video_info(url):

    ydl_opts = {

        "quiet": True,
        "noplaylist": True,

        "cookiefile":
        COOKIES_FILE
        if os.path.exists(COOKIES_FILE)
        else None,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:

        info = ydl.extract_info(
            url,
            download=False
        )

        title = info.get(
            "title",
            "Unknown"
        )

        formats = []

        used = set()

        for f in info.get("formats", []):

            h = f.get("height")

            size = (
                f.get("filesize")
                or
                f.get("filesize_approx")
            )

            if not h or not size:
                continue

            if h in used:
                continue

            used.add(h)

            formats.append({

                "height": h,

                "size":
                round(size / 1024 / 1024)
            })

        return title, formats

def get_meta(path):

    result = subprocess.run(

        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            path
        ],

        stdout=subprocess.PIPE
    )

    try:

        data = json.loads(result.stdout)

        duration = int(float(
            data["format"]["duration"]
        ))

        width = 0
        height = 0

        for s in data["streams"]:

            if s["codec_type"] == "video":

                width = s.get("width", 0)
                height = s.get("height", 0)

                break

        return {

            "duration": duration,
            "width": width,
            "height": height
        }

    except:

        return {

            "duration": 0,
            "width": 0,
            "height": 0
        }

def split_video(path, uid):

    size = os.path.getsize(path)

    if size <= MAX_UPLOAD_BYTES:
        return [path]

    output = os.path.join(
        DOWNLOAD_DIR,
        f"{uid}_part_%03d.mp4"
    )

    subprocess.run([

        "ffmpeg",
        "-i", path,
        "-c", "copy",
        "-map", "0",
        "-f", "segment",
        "-segment_time", "1800",
        "-reset_timestamps", "1",
        output

    ])

    parts = []

    for f in sorted(os.listdir(DOWNLOAD_DIR)):

        if f.startswith(uid + "_part_"):

            parts.append(
                os.path.join(DOWNLOAD_DIR, f)
            )

    return parts if parts else [path]

def create_thumb(video):

    thumb = video + ".jpg"

    subprocess.run([

        "ffmpeg",
        "-y",
        "-ss", "10",
        "-i", video,
        "-vframes", "1",
        "-q:v", "2",
        thumb

    ])

    return thumb if os.path.exists(thumb) else None

def download_video(opts, url):

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(

        "👋 أهلاً بيك في بوت التحميل...أحمد قابل\n\n"

        "📥 ابعت أي رابط فيديو\n"
        "🎬 يدعم أغلب المواقع\n"
        "⚡ سريع جدًا\n"
        "📦 يدعم تقسيم الملفات الكبيرة\n"
        "🎧 تحميل MP3\n"
        "❌ زر إلغاء أثناء التحميل"
    
    )

# ================= TEXT =================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.strip()

    if not text.startswith("http"):
        return

    context.user_data["url"] = text

    msg = await update.message.reply_text(
        "🔍 جاري استخراج المعلومات..."
    )

    try:

        title, formats = await asyncio.get_running_loop().run_in_executor(

            EXECUTOR,

            lambda: get_video_info(text)
        )

    except Exception as e:

        await msg.edit_text(
            f"❌ فشل استخراج الفيديو\n\n{e}"
        )

        return

    context.user_data["title"] = title

    sizes = {}

    for f in formats:

        h = f["height"]

        if h >= 1080 and "1080" not in sizes:
            sizes["1080"] = f"{f['size']} MB"

        elif h >= 720 and "720" not in sizes:
            sizes["720"] = f"{f['size']} MB"

        elif h >= 480 and "480" not in sizes:
            sizes["480"] = f"{f['size']} MB"

    keyboard = [

        [

            InlineKeyboardButton(
                f"🎬 1080p • {sizes.get('1080', '?')}",
                callback_data="1080"
            ),

            InlineKeyboardButton(
                f"📺 720p • {sizes.get('720', '?')}",
                callback_data="720"
            )
        ],

        [

            InlineKeyboardButton(
                f"📱 480p • {sizes.get('480', '?')}",
                callback_data="480"
            ),

            InlineKeyboardButton(
                "🎧 MP3",
                callback_data="mp3"
            )
        ],

        [

            InlineKeyboardButton(
                "⚡ أعلى جودة",
                callback_data="best"
            )
        ]
    ]

    await msg.edit_text(

        f"🎬 {title}\n\n"
        "اختر الجودة:",

        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================= BUTTONS =================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query

    await q.answer()

    # CANCEL

    if q.data.startswith("cancel_"):

        uid = q.data.replace("cancel_", "")

        for user_id, data in ACTIVE_DOWNLOADS.items():

            if data["uid"] == uid:

                data["cancel"].set()

                await q.edit_message_text(
                    "❌ تم إلغاء التحميل"
                )

                return

    if q.data not in [
        "1080",
        "720",
        "480",
        "best",
        "mp3"
    ]:
        return

    url = context.user_data["url"]

    title = context.user_data.get(
        "title",
        "Video"
    )

    uid = uuid.uuid4().hex

    cancel_event = Event()

    ACTIVE_DOWNLOADS[q.from_user.id] = {

        "uid": uid,
        "cancel": cancel_event
    }

    formats = {

        "1080":
        (
            "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "1080p"
        ),

        "720":
        (
            "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "720p"
        ),

        "480":
        (
            "bestvideo[height<=480]+bestaudio/best[height<=480]",
            "480p"
        ),

        "best":
        (
            "bestvideo+bestaudio/best",
            "أفضل جودة"
        ),

        "mp3":
        (
            "bestaudio/best",
            "MP3"
        )
    }

    fmt, label = formats[q.data]

    output = os.path.join(
        DOWNLOAD_DIR,
        f"{uid}.%(ext)s"
    )

    cancel_keyboard = InlineKeyboardMarkup([

        [

            InlineKeyboardButton(
                "🛑 إلغاء التحميل",
        data=f"cancel_{uid}"
            )
        ]
    ])

    await q.edit_message_text(

        f"🎬 {title}\n\n"
        f"⏳ جاري التحميل {label}",

        reply_markup=cancel_keyboard
    )

    loop = asyncio.get_running_loop()

    last_edit = {"t": 0}

    def hook(d):

        if cancel_event.is_set():
            raise Exception("Cancelled")

        if d["status"] == "downloading":

            now = time.time()

            if now - last_edit["t"] < 2:
                return

            last_edit["t"] = now

            downloaded = d.get(
                "downloaded_bytes",
                0
            )

            total = (
                d.get("total_bytes")
                or
                d.get("total_bytes_estimate")
                or 1
            )

            percent = int(
                downloaded / total * 100
            )

            speed = d.get("speed") or 0
            eta = d.get("eta") or 0

            speed_mb = speed / 1024 / 1024

            d_mb = downloaded / 1024 / 1024
            t_mb = total / 1024 / 1024

            text = (

                f"🎬 {title}\n\n"

                f"⬇️ {label}\n\n"

                f"{make_bar(percent)} {percent}%\n\n"

                f"📦 {d_mb:.1f}/{t_mb:.1f} MB\n"

                f"⚡ {speed_mb:.1f} MB/s\n"

                f"⏳ {eta} ثانية"
            )

            asyncio.run_coroutine_threadsafe(

                safe_edit(
                    q.message,
                    text,
                    cancel_keyboard
                ),

                loop
            )

    ydl_opts = {

        "format": fmt,

        "outtmpl": output,

        "quiet": True,

        "noplaylist": True,

        "progress_hooks": [hook],

        "merge_output_format": "mp4",

        "retries": 10,

        "fragment_retries": 10,

        "socket_timeout": 30,

        "concurrent_fragment_downloads": 16,

        "cookiefile":
        COOKIES_FILE
        if os.path.exists(COOKIES_FILE)
        else None,

        "extractor_args": {
            "youtube": {
                "player_client": [
                    "android",
                    "ios",
                    "web"
                ]
            }
        },

        "http_headers": {

            "User-Agent":

            "Mozilla/5.0"
        }
    }

    if q.data == "mp3":

        ydl_opts["postprocessors"] = [

            {

                "key": "FFmpegExtractAudio",

                "preferredcodec": "mp3",

                "preferredquality": "192"
            }
        ]

    try:

        await loop.run_in_executor(

            EXECUTOR,

            lambda: download_video(
                ydl_opts,
                url
            )
        )

        files = []

        for f in os.listdir(DOWNLOAD_DIR):

            if f.startswith(uid):

                files.append(
                    os.path.join(DOWNLOAD_DIR, f)
                )

        if not files:

            await q.edit_message_text(
                "❌ فشل التحميل"
            )

            return

        path = files[0]

        gc.collect()

        parts = split_video(path, uid)

        total_parts = len(parts)

        for i, part in enumerate(parts, start=1):

            if cancel_event.is_set():
                return

            meta = get_meta(part)

            thumb = None

            if q.data != "mp3":
                thumb = create_thumb(part)

            await safe_edit(

                q.message,

                f"📤 جاري الرفع\n"
                f"الجزء {i}/{total_parts}"
            )

            if q.data == "mp3":

                await pyro.send_audio(

                    q.message.chat.id,

                    part,

                    title=title
                )

            else:

                await pyro.send_video(

                    q.message.chat.id,

                    part,

                    caption=title,

                    duration=meta["duration"],

                    width=meta["width"],

                    height=meta["height"],

                    thumb=thumb,

                    supports_streaming=True
                )

        await safe_edit(

            q.message,

            f"تمت العملية بنجاح✅...تحياتي🫶🏻..أحمد قابل:\n{title}"
        )

    except Exception as e:

        await safe_edit(

            q.message,

            f"❌ خطأ:\n{str(e)[:500]}"
        )

    finally:

        ACTIVE_DOWNLOADS.pop(
            q.from_user.id,
            None
        )

# ================= MAIN =================

async def post_init(app):

    await pyro.start()

async def post_shutdown(app):

    await pyro.stop()

def build_app():

    app = (

    Application.builder()

    .token(TOKEN)

    .concurrent_updates(True)

    .post_init(post_init)

    .post_shutdown(post_shutdown)

    .build()
)
    

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CallbackQueryHandler(buttons)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    return app

def main():

    while True:

        try:

            print("BOT RUNNING")

            app = build_app()

            app.run_polling(
                drop_pending_updates=True
            )

        except Exception as e:

            print(e)

            time.sleep(5)

if __name__ == "__main__":
    main()
