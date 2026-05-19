# ========================= IMPORTS =========================

import os
import uuid
import time
import json
import math
import subprocess
import asyncio
import gc
from concurrent.futures import ThreadPoolExecutor
import yt_dlp

from flask import Flask, send_from_directory
from threading import Thread, Event

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

# ========================= CONFIG =========================

TOKEN = os.environ.get("BOT_TOKEN")
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")

if not TOKEN:
    raise ValueError("BOT_TOKEN missing")

if not API_ID or not API_HASH:
    raise ValueError("API_ID / API_HASH missing")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "yt_cookies.txt"

# 1.7 GB
MAX_UPLOAD_BYTES = 1700 * 1024 * 1024

# Multi users support
EXECUTOR = ThreadPoolExecutor(max_workers=6)
USER_SEMAPHORE = asyncio.Semaphore(6)

ACTIVE_DOWNLOADS = {}

# ========================= PYROGRAM =========================

pyro = PyroClient(
    "bot_session",
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=TOKEN,
    no_updates=True,
    sleep_threshold=60,
)

# ========================= CLEANUP =========================

def clean_downloads():

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    for f in os.listdir(DOWNLOAD_DIR):
        try:
            os.remove(os.path.join(DOWNLOAD_DIR, f))
        except:
            pass

clean_downloads()

# ========================= FLASK KEEPALIVE =========================

flask_app = Flask(__name__)

@flask_app.route('/ping')
def ping():
    return "ok"

@flask_app.route('/file/<path:name>')
def stream_file(name):
    return send_from_directory(DOWNLOAD_DIR, name)

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)

Thread(target=run_flask, daemon=True).start()

# ========================= UTILS =========================

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
            data.get("format", {}).get("duration", 0)
        ))

        width = 0
        height = 0

        for s in data.get("streams", []):

            if s.get("codec_type") == "video":

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

    parts = []

    output_pattern = os.path.join(
        DOWNLOAD_DIR,
        f"{uid}_part_%03d.mp4"
    )

    subprocess.run([
        "ffmpeg",
        "-i", path,
        "-c", "copy",
        "-map", "0",
        "-fs", str(MAX_UPLOAD_BYTES),
        "-f", "segment",
        "-reset_timestamps", "1",
        output_pattern
    ])

    for f in sorted(os.listdir(DOWNLOAD_DIR)):

        if f.startswith(uid + "_part_"):
            parts.append(
                os.path.join(DOWNLOAD_DIR, f)
            )

    return parts if parts else [path]

def thumbnail(video):

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

def do_download(opts, url):

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

# ========================= START =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [
            InlineKeyboardButton(
                "🎬 تحميل فيديو",
                callback_data="video"
            )
        ],
        [
            InlineKeyboardButton(
                "🎧 تحميل MP3",
                callback_data="audio"
            )
        ]
    ]

    await update.message.reply_text(
        "👋 أهلاً بيك في بوت التحميل الاحترافي\n\n"
        "📥 ابعت أي رابط فيديو\n"
        "🎬 يدعم YouTube و مواقع كثيرة\n"
        "📦 تقسيم تلقائي للفيديوهات الكبيرة\n"
        "⚡ سريع و يدعم أكثر من مستخدم",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ========================= URL =========================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.strip()

    if not (
        text.startswith("http://")
        or
        text.startswith("https://")
    ):

        return

    context.user_data["url"] = text

    keyboard = [

        [
            InlineKeyboardButton(
                "1080p 🎬",
                callback_data="1080"
            ),

            InlineKeyboardButton(
                "720p 📺",
                callback_data="720"
            )
        ],

        [
            InlineKeyboardButton(
                "480p 📱",
                callback_data="480"
            ),

            InlineKeyboardButton(
                "MP3 🎧",
                callback_data="mp3"
            )
        ]
    ]

    await update.message.reply_text(
        "اختر الجودة:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ========================= BUTTONS =========================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query

    await q.answer()

    # CANCEL

    if q.data.startswith("cancel_"):

        uid = q.data.replace("cancel_", "")

        for user_id, data in ACTIVE_DOWNLOADS.items():

            if data["uid"] == uid:

                data["cancel"].set()

                task = data.get("task")

                if task and not task.done():
                    task.cancel()

                await q.edit_message_text(
                    "🛑 تم إلغاء التحميل"
                )

                return

    # QUALITY

    if q.data not in [
        "1080",
        "720",
        "480",
        "mp3"
    ]:
        return

    url = context.user_data.get("url")

    if not url:

        await q.edit_message_text(
            "❌ الرابط غير موجود"
        )

        return

    uid = uuid.uuid4().hex

    cancel_event = Event()

    ACTIVE_DOWNLOADS[q.from_user.id] = {
        "uid": uid,
        "cancel": cancel_event,
        "task": None
    }

    quality_map = {

        "1080":
        (
            "bestvideo[vcodec^=avc][height<=1080]+bestaudio/best[height<=1080]",
            "1080p"
        ),

        "720":
        (
            "bestvideo[vcodec^=avc][height<=720]+bestaudio/best[height<=720]",
            "720p"
        ),

        "480":
        (
            "bestvideo[vcodec^=avc][height<=480]+bestaudio/best[height<=480]",
            "480p"
        ),

        "mp3":
        (
            "bestaudio/best",
            "MP3"
        )
    }

    fmt, label = quality_map[q.data]

    output = os.path.join(
        DOWNLOAD_DIR,
        f"{uid}.%(ext)s"
    )

    cancel_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🛑 إلغاء التحميل",
                callback_data=f"cancel_{uid}"
            )
        ]
    ])

    await q.edit_message_text(
        f"⏳ جاري التحميل {label}\n░░░░░░░░░░ 0%",
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
                or
                1
            )

            percent = int(
                downloaded / total * 100
            )

            bar = make_bar(percent)

            text = (
                f"⬇️ جاري التحميل {label}\n"
                f"{bar} {percent}%"
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

        "concurrent_fragment_downloads": 32,

        "extractor_retries": 5,

        "retries": 10,

        "fragment_retries": 10,

        "nocheckcertificate": True,

        "socket_timeout": 30,
    }

    if q.data == "mp3":

        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    else:

        ydl_opts["merge_output_format"] = "mp4"

    try:

        async with USER_SEMAPHORE:

            await loop.run_in_executor(
                EXECUTOR,
                lambda: do_download(
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
                thumb = thumbnail(part)

            part_text = (
                f"📤 جاري الرفع\n"
                f"الجزء {i}/{total_parts}"
            )

            await safe_edit(
                q.message,
                part_text
            )

            async def upload():

                if q.data == "mp3":

                    await pyro.send_audio(
                        q.message.chat.id,
                        part
                    )

                else:

                    await pyro.send_video(
                        q.message.chat.id,
                        part,
                        duration=meta["duration"],
                        width=meta["width"],
                        height=meta["height"],
                        thumb=thumb,
                        supports_streaming=True
                    )

            task = asyncio.create_task(
                upload()
            )

            ACTIVE_DOWNLOADS[q.from_user.id]["task"] = task

            await task

            if thumb and os.path.exists(thumb):
                os.remove(thumb)

        await safe_edit(
            q.message,
            "✅ تم الإرسال بنجاح"
        )

    except Exception as e:

        await safe_edit(
            q.message,
            f"❌ خطأ:\n{str(e)[:300]}"
        )

    finally:

        ACTIVE_DOWNLOADS.pop(
            q.from_user.id,
            None
        )

        for f in os.listdir(DOWNLOAD_DIR):

            if f.startswith(uid):

                try:
                    os.remove(
                        os.path.join(
                            DOWNLOAD_DIR,
                            f
                        )
                    )
                except:
                    pass

# ========================= MAIN =========================

async def post_init(app):

    await pyro.start()

async def post_shutdown(app):

    await pyro.stop()

def build_app():

    app = (
        Application.builder()
        .concurrent_updates(True)
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(60)
        .pool_timeout(15)
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
