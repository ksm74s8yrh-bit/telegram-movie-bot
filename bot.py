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

# تذكير: قم بتغيير التوكن والهاش إذا تم كشفهم علناً لسلامة بوتك وحسابك
TOKEN = "8731635445:AAER_lUzaKC21xR31K3EXJN-zUk9t_cr-v4"
API_ID = "39570484"
API_HASH = "79114c616c581109bd61e7b991e595b5"

if not TOKEN:
    raise ValueError("BOT_TOKEN missing")
if not API_ID or not API_HASH:
    raise ValueError("API_ID / API_HASH missing")

DOWNLOAD_DIR = "downloads"
COOKIES_FILE = "yt_cookies.txt"

MAX_UPLOAD_BYTES = 1900 * 1024 * 1024  # تم رفعها قليلاً لتناسب حد تليجرام الآمن

EXECUTOR = ThreadPoolExecutor(max_workers=10)
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

# ========================= FLASK =========================

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
        await msg.edit_text(text, reply_markup=keyboard)
    except Exception as e:
        pass

def get_video_info(url):
    ydl_opts = {
        "quiet": True,
        "noplaylist": True,
        "nocheckcertificate": True,
    }
    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        title = info.get("title", "Unknown")
        formats = []
        seen = set()

        for f in info.get("formats", []):
            height = f.get("height")
            filesize = f.get("filesize") or f.get("filesize_approx")

            if not height or not filesize:
                continue
            if height in seen:
                continue

            seen.add(height)
            size_mb = filesize / 1024 / 1024
            formats.append({"height": height, "size": size_mb})

        return title, formats

def get_meta(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    try:
        data = json.loads(result.stdout)
        duration = int(float(data.get("format", {}).get("duration", 0)))
        width, height = 0, 0
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                width = s.get("width", 0)
                height = s.get("height", 0)
                break
        return {"duration": duration, "width": width, "height": height}
    except:
        return {"duration": 0, "width": 0, "height": 0}

def split_video(path, uid):
    size = os.path.getsize(path)
    if size <= MAX_UPLOAD_BYTES:
        return [path]

    meta = get_meta(path)
    duration = meta["duration"]
    if duration <= 0:
        return [path]

    # حساب عدد الأجزاء المطلوبة تقريبياً بناءً على الحجم
    num_parts = math.ceil(size / MAX_UPLOAD_BYTES)
    part_duration = math.ceil(duration / num_parts)

    parts = []
    output_pattern = os.path.join(DOWNLOAD_DIR, f"{uid}_part_%03d.mp4")

    subprocess.run([
        "ffmpeg", "-i", path,
        "-c", "copy",
        "-map", "0",
        "-segment_time", str(part_duration),
        "-f", "segment",
        "-reset_timestamps", "1",
        output_pattern
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for f in sorted(os.listdir(DOWNLOAD_DIR)):
        if f.startswith(uid + "_part_") and f.endswith(".mp4"):
            parts.append(os.path.join(DOWNLOAD_DIR, f))

    return parts if parts else [path]

def thumbnail(video):
    thumb = video + ".jpg"
    subprocess.run([
        "ffmpeg", "-y", "-ss", "00:00:10", "-i", video,
        "-vframes", "1", "-q:v", "2", thumb
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return thumb if os.path.exists(thumb) else None

def do_download(opts, url):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

# ========================= START =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً بيك في بوت التحميل...بواسطة: أحمد قابل \n\n"
        "📥 إبعت أي رابط فيديو مباشر\n"
        "🎬 يدعم أغلب المواقع والمنصات\n"
        "⚡ سريع ويدعم أكثر من مستخدم في نفس الوقت\n"
        "📦 تقسيم تلقائي للفيديوهات الكبيرة"
    )

# ========================= TEXT HANDLER =========================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not (text.startswith("http://") or text.startswith("https://")):
        return

    context.user_data["url"] = text
    msg = await update.message.reply_text("🔍 جاري فحص الرابط واستخراج معلومات الفيديو...")

    try:
        title, formats = await asyncio.get_running_loop().run_in_executor(
            EXECUTOR, lambda: get_video_info(text)
        )
        context.user_data["title"] = title
    except Exception as e:
        await msg.edit_text(f"❌ فشل جلب معلومات الرابط. تأكد من صحته أو جرب لاحقاً.\nالخطأ: {str(e)[:100]}")
        return

    sizes = {}
    for f in formats:
        h = f["height"]
        if h >= 1080 and "1080" not in sizes:
            sizes["1080"] = f"{f['size']:.0f} MB"
            
        elif h >= 720 and "720" not in sizes:
            sizes["720"] = f"{f['size']:.0f} MB"
            
        elif h >= 480 and "480" not in sizes:
            sizes["480"] = f"{f['size']:.0f} MB"

    keyboard = [
        [
            InlineKeyboardButton(f"🎬 1080p • {sizes.get('1080', '?')}", callback_data="1080"),
            InlineKeyboardButton(f"📺 720p • {sizes.get('720', '?')}", callback_data="720")
        ],
        [
            InlineKeyboardButton(f"📱 480p • {sizes.get('480', '?')}", callback_data="480"),
            InlineKeyboardButton("🎧 MP3", callback_data="mp3")
        ],
        [
            InlineKeyboardButton("⚡ أعلى جودة متاحة", callback_data="best")
        ]
    ]

    await msg.edit_text(
        f"🎬 **{title}**\n\nاختر الجودة المطلوبة للتحميل:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ========================= BUTTONS =========================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data.startswith("cancel_"):
        uid = q.data.replace("cancel_", "")
        for user_id, data in list(ACTIVE_DOWNLOADS.items()):
            if data["uid"] == uid:
                data["cancel"].set()
                task = data.get("task")
                if task and not task.done():
                    task.cancel()
                await q.edit_message_text("🛑 تم إلغاء عملية التحميل بنجاح.")
                return
        return

    if q.data not in ["1080", "720", "480", "mp3", "best"]:
        return

    url = context.user_data.get("url")
    title = context.user_data.get("title", "Video")
    uid = uuid.uuid4().hex
    cancel_event = Event()

    ACTIVE_DOWNLOADS[q.from_user.id] = {
        "uid": uid,
        "cancel": cancel_event,
        "task": None
    }

    quality_map = {
        "1080": ("bestvideo[vcodec^=avc][height<=1080]+bestaudio/best[height<=1080]", "1080p"),
        "720": ("bestvideo[vcodec^=avc][height<=720]+bestaudio/best[height<=720]", "720p"),
        "480": ("bestvideo[vcodec^=avc][height<=480]+bestaudio/best[height<=480]", "480p"),
        "best": ("bestvideo[vcodec^=avc]+bestaudio/best", "أفضل جودة"),
        "mp3": ("bestaudio/best", "MP3")
    }

    fmt, label = quality_map[q.data]
    output = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")

    cancel_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 إلغاء التحميل", callback_data=f"cancel_{uid}")]
    ])

    await q.edit_message_text(
        f"🎬 {title}\n\n⏳ جاري بدء التحميل [{label}]...\n░░░░░░░░░░ 0%",
        reply_markup=cancel_keyboard
    )

    loop = asyncio.get_running_loop()
    last_edit = {"t": 0}

    def hook(d):
        if cancel_event.is_set():
            raise Exception("Cancelled")

        if d["status"] == "downloading":
            now = time.time()
            if now - last_edit["t"] < 3:  # زيادة المهلة لـ 3 ثوانٍ لتجنب حظر تليجرام للـ Flood
                return
            last_edit["t"] = now

            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            percent = int(downloaded / total * 100)

            speed = d.get("speed") or 0
            eta = d.get("eta") or 0
            speed_mb = speed / 1024 / 1024 if speed else 0
            downloaded_mb = downloaded / 1024 / 1024
            total_mb = total / 1024 / 1024
            bar = make_bar(percent)

            text = (
                f"🎬 {title}\n\n"
                f"⬇️ جاري التحميل {label}\n\n"
                f"{bar} {percent}%\n\n"
                f"📦 {downloaded_mb:.1f} / {total_mb:.1f} MB\n"
                f"⚡ {speed_mb:.1f} MB/s\n"
                f"⏳ الوقت المتبقي: {eta} ثانية"
            )
            # استدعاء آمن متوافق مع الأسينك
            loop.call_soon_threadsafe(
                asyncio.create_task, safe_edit(q.message, text, cancel_keyboard)
            )

    ydl_opts = {
        "format": fmt,
        "outtmpl": output,
        "quiet": True,
        "noplaylist": True,
        "progress_hooks": [hook],
        "concurrent_fragment_downloads": 16, # تخفيضها قليلاً للثبات
        "extractor_retries": 5,
        "retries": 10,
        "nocheckcertificate": True,
        "socket_timeout": 30,
    }
    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookiefile"] = COOKIES_FILE

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
            await loop.run_in_executor(EXECUTOR, lambda: do_download(ydl_opts, url))

        files = [os.path.join(DOWNLOAD_DIR, f) for f in os.listdir(DOWNLOAD_DIR) if f.startswith(uid)]
        if not files:
            await q.edit_message_text("❌ فشل تحميل الملف من الخادم.")
            return

        # فرز الملف الفعلي والابتعاد عن المخلفات
        path = files[0]
        for f in files:
            if f.endswith(".mp4") or f.endswith(".mp3"):
                path = f
                break

        gc.collect()
        parts = split_video(path, uid)
        total_parts = len(parts)

        if not pyro.is_connected:
            await pyro.start()

        for i, part in enumerate(parts, start=1):
            if cancel_event.is_set():
                return

            meta = get_meta(part)
            thumb = None if q.data == "mp3" else thumbnail(part)

            await safe_edit(q.message, f"🎬 {title}\n\n📤 جاري الرفع إلى تيليجرام...\nالجزء {i} من {total_parts}")

            if q.data == "mp3":
                await pyro.send_audio(chat_id=q.message.chat.id, audio=part, title=title)
            else:
                await pyro.send_video(
                    chat_id=q.message.chat.id,
                    video=part,
                    caption=f"{title} - Part {i}/{total_parts}" if total_parts > 1 else title,
                    duration=meta["duration"],
                    width=meta["width"],
                    height=meta["height"],
                    thumb=thumb,
                    supports_streaming=True
                )

            if thumb and os.path.exists(thumb):
                try: os.remove(thumb)
                except: pass

        await safe_edit(q.message, f"✅ تم تحميل وإرسال الملف بنجاح:\n{title}")

    except Exception as e:
        await safe_edit(q.message, f"❌ حدث خطأ غير متوقع:\n{str(e)[:300]}")
    finally:
        ACTIVE_DOWNLOADS.pop(q.from_user.id, None)
        # تنظيف نهائي للملفات الخاصة بهذا الـ UID فقط لعدم مسح تحميلات مستخدمين آخرين
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(uid):
                try: os.remove(os.path.join(DOWNLOAD_DIR, f))
                except: pass

# ========================= MAIN =========================

async def post_init(app):
    if not pyro.is_connected:
        await pyro.start()

async def post_shutdown(app):
    if pyro.is_connected:
        await pyro.stop()

def build_app():
    app = (
        Application.builder()
        .concurrent_updates(True)
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(60)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return app

def main():
    while True:
        try:
            print("=== BOT IS RUNNING NOW ===")
            app = build_app()
            app.run_polling(drop_pending_updates=True)
        except Exception as e:
            print(f"Crash detected: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
