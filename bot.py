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

TOKEN = "8731635445:AAER_lUzaKC21xR31K3EXJN-zUk9t_cr-v4"
API_ID = "39570484"
API_HASH = "79114c616c581109bd61e7b991e595b5"

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
        await msg.edit_text(text, reply_markup=keyboard)
    except:
        pass


def get_video_info(url):
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "cookiefile": COOKIES_FILE if os.path.exists(COOKIES_FILE) else None,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

        title = info.get("title", "Unknown")

        formats = []
        used = set()

        for f in info.get("formats", []):
            h = f.get("height")
            size = f.get("filesize") or f.get("filesize_approx")

            if not h or not size:
                continue

            if h in used:
                continue

            used.add(h)

            formats.append({
                "height": h,
                "size": round(size / 1024 / 1024)
            })

        return title, formats


def download_video(opts, url):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً بيك في بوت التحميل...أحمد قابل\n\n"
        "📥 ابعت أي رابط فيديو\n"
        "🎬 يدعم أغلب المواقع\n"
        "⚡ سريع جدًا"
    )


# ================= TEXT =================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text.strip()

    if not text.startswith("http"):
        return

    context.user_data["url"] = text

    msg = await update.message.reply_text("🔍 جاري استخراج المعلومات...")

    try:
        title, formats = await asyncio.get_running_loop().run_in_executor(
            EXECUTOR,
            lambda: get_video_info(text)
        )
    except Exception as e:
        await msg.edit_text(f"❌ خطأ في استخراج الفيديو\n{e}")
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
            InlineKeyboardButton(f"🎬 1080p • {sizes.get('1080','?')}", callback_data="1080"),
            InlineKeyboardButton(f"📺 720p • {sizes.get('720','?')}", callback_data="720")
        ],
        [
            InlineKeyboardButton(f"📱 480p • {sizes.get('480','?')}", callback_data="480"),
            InlineKeyboardButton("🎧 MP3", callback_data="mp3")
        ],
        [
            InlineKeyboardButton("⚡ أفضل جودة", callback_data="best")
        ]
    ]

    await msg.edit_text(
        f"🎬 {title}\n\nاختر الجودة:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ================= CALLBACK =================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):

    q = update.callback_query
    await q.answer()

    data = q.data

    # ===== CANCEL =====
    if data.startswith("cancel_"):
        uid = data.replace("cancel_", "")

        for _, d in ACTIVE_DOWNLOADS.items():
            if d["uid"] == uid:
                d["cancel"].set()
                await q.edit_message_text("❌ تم الإلغاء")
                return

        await q.edit_message_text("❌ لم يتم العثور على التحميل")
        return

    # ===== SAFE USER DATA =====
    url = context.user_data.get("url")
    title = context.user_data.get("title", "Video")

    if not url:
        await q.edit_message_text("⚠️ ابعت الرابط تاني الأول")
        return

    formats = {
    "1080": ("best[height<=1080]", "1080p"),
    "720": ("best[height<=720]", "720p"),
    "480": ("best[height<=480]", "480p"),
    "best": ("best", "أفضل جودة"),
    "mp3": ("bestaudio/best", "MP3")
}
    if data not in formats:
        await q.edit_message_text("⚠️ اختيار غير صحيح")
        return

    fmt, label = formats[data]

    uid = uuid.uuid4().hex
    cancel_event = Event()

    ACTIVE_DOWNLOADS[q.from_user.id] = {
        "uid": uid,
        "cancel": cancel_event
    }

    output = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")

    cancel_keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ إلغاء", callback_data=f"cancel_{uid}")
    ]])

    await q.edit_message_text(f"⏳ جاري التحميل {label}", reply_markup=cancel_keyboard)

    loop = asyncio.get_running_loop()

    def hook(d):
        if cancel_event.is_set():
            raise Exception("Cancelled")

        if d["status"] == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or 1
            percent = int(downloaded / total * 100)

            asyncio.run_coroutine_threadsafe(
                safe_edit(q.message, f"⬇️ {percent}%"),
                loop
            )

    ydl_opts = {
        "format": fmt,
        "outtmpl": output,
        "quiet": True,
        "noplaylist": True,
        "progress_hooks": [hook],
        "merge_output_format": "mp4"
    }

    if data == "mp3":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192"
        }]

    try:
        await loop.run_in_executor(
            EXECUTOR,
            lambda: download_video(ydl_opts, url)
        )

        files = [
            os.path.join(DOWNLOAD_DIR, f)
            for f in os.listdir(DOWNLOAD_DIR)
            if f.startswith(uid)
        ]

        if not files:
            await q.edit_message_text("❌ فشل التحميل")
            return

        path = files[0]

        await q.edit_message_text("📤 جاري الرفع...")

        if data == "mp3":
            await pyro.send_audio(q.message.chat.id, path, title=title)
        else:
            await pyro.send_video(q.message.chat.id, path, caption=title)

        await q.edit_message_text(" تم بنجاح✅ ...تحياتي 🫡 أحمد قابل")

    except Exception as e:
        await q.edit_message_text(f"❌ خطأ: {str(e)[:400]}")

    finally:
        ACTIVE_DOWNLOADS.pop(q.from_user.id, None)


# ================= APP =================

async def post_init(app):
    await pyro.start()

async def post_shutdown(app):
    await pyro.stop()


def build_app():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    return app


def main():
    print("BOT RUNNING")

    app = build_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
