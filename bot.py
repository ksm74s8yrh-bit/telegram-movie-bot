import os
import uuid
import time
import json
import math
import subprocess
import asyncio
import yt_dlp
from flask import Flask, send_from_directory
from threading import Thread, Event

MAX_UPLOAD_BYTES = 1950 * 1024 * 1024  # 1.95 GB — safe margin below Telegram's 2 GB limit

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

import pyrogram
from pyrogram import Client as PyroClient

TOKEN    = os.environ.get("BOT_TOKEN")
API_ID   = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")

if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set")
if not API_ID or not API_HASH:
    raise ValueError("API_ID and API_HASH environment variables are required for large file support")

# Pyrogram client for MTProto uploads (up to 2GB)
pyro = PyroClient(
    "bot_session",
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=TOKEN,
    no_updates=True,
    sleep_threshold=60,
)

DOWNLOAD_DIR = "downloads"
COOKIES_FILE  = "yt_cookies.txt"

def _clean_downloads():
    if os.path.isdir(DOWNLOAD_DIR):
        for f in os.listdir(DOWNLOAD_DIR):
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, f))
            except Exception:
                pass
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

_clean_downloads()

# user_id -> {"cancel": Event, "uid": str, "upload_task": asyncio.Task | None}
ACTIVE_DOWNLOADS: dict = {}

# ---------------- FLASK STREAMING SERVER ----------------
flask_app = Flask(__name__)

@flask_app.route('/file/<path:name>')
def serve_file(name):
    return send_from_directory(DOWNLOAD_DIR, name)

@flask_app.route('/ping')
def ping():
    return "ok", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)

def _keepalive():
    import urllib.request
    while True:
        time.sleep(240)
        try:
            urllib.request.urlopen("http://127.0.0.1:8080/ping", timeout=10)
        except Exception:
            pass

Thread(target=run_flask, daemon=True).start()
Thread(target=_keepalive, daemon=True).start()

# ---------------- UTIL ----------------
def run_cmd(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def is_url(text):
    return text.startswith("http://") or text.startswith("https://")

def make_progress_bar(percent, length=10):
    filled = int(length * percent / 100)
    return "█" * filled + "░" * (length - filled)

async def _safe_edit(msg, text):
    try:
        await msg.edit_text(text)
    except Exception:
        pass

def get_video_metadata(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_streams", "-show_format", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        data = json.loads(result.stdout)
        duration = int(float(data.get("format", {}).get("duration", 0)))
        width = height = 0
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width  = stream.get("width", 0)
                height = stream.get("height", 0)
                if not duration:
                    duration = int(float(stream.get("duration", 0)))
                break
        return {"duration": duration, "width": width, "height": height}
    except Exception:
        return {"duration": 0, "width": 0, "height": 0}

def remux_faststart(src_path):
    dst_path = src_path + "_fs.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", src_path,
         "-c:v", "copy", "-c:a", "copy",
         "-movflags", "+faststart",
         dst_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if r.returncode == 0:
        os.replace(dst_path, src_path)
    else:
        try:
            os.remove(dst_path)
        except Exception:
            pass

def generate_thumbnail(video_path, duration):
    thumb_path = video_path + "_thumb.jpg"
    seek = max(0, duration // 2) if duration else 0
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", str(seek), "-i", video_path,
         "-vframes", "1", "-vf", "scale=320:-1",
         "-q:v", "5", thumb_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if r.returncode == 0 and os.path.exists(thumb_path):
        return thumb_path
    return None

def split_video(file_path, meta, uid):
    """Split file into equal-duration parts each under MAX_UPLOAD_BYTES. Returns list of part paths."""
    file_size = os.path.getsize(file_path)
    num_parts = math.ceil(file_size / MAX_UPLOAD_BYTES)
    total_dur = meta.get("duration", 0)

    if num_parts < 2 or not total_dur:
        return [file_path]

    part_dur = total_dur / num_parts
    parts = []
    for i in range(num_parts):
        start = i * part_dur
        part_path = os.path.join(os.path.dirname(file_path), f"{uid}_part{i + 1}of{num_parts}.mp4")
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(start), "-i", file_path,
             "-t", str(part_dur),
             "-c:v", "copy", "-c:a", "copy",
             "-movflags", "+faststart",
             part_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if r.returncode == 0 and os.path.exists(part_path):
            parts.append(part_path)

    return parts if len(parts) == num_parts else [file_path]

# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📤 رفع ملف",          callback_data="upload")],
        [InlineKeyboardButton("🔗 تحميل من رابط",    callback_data="url_prompt")],
        [InlineKeyboardButton("🎧 MP3 تحويل",         callback_data="mp3")],
        [InlineKeyboardButton("🎬 معلومات",           callback_data="info")],
        [InlineKeyboardButton("✂️ تقسيم",             callback_data="split")],
        [InlineKeyboardButton("⚙️ ضغط/جودة",         callback_data="quality")],
    ]
    await update.message.reply_text(
        "👋 أهلاً بيك في البوت الاحترافي\n\nاختر من الأزرار:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ---------------- /cancel COMMAND ----------------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    entry = ACTIVE_DOWNLOADS.get(user_id)

    if not entry:
        await update.message.reply_text("ℹ️ لا يوجد تحميل جارٍ حالياً.")
        return

    entry["cancel"].set()

    upload_task = entry.get("upload_task")
    if upload_task and not upload_task.done():
        upload_task.cancel()

    uid = entry.get("uid", "")
    for f in os.listdir(DOWNLOAD_DIR):
        if f.startswith(uid):
            try:
                os.remove(os.path.join(DOWNLOAD_DIR, f))
            except Exception:
                pass

    ACTIVE_DOWNLOADS.pop(user_id, None)
    await update.message.reply_text("🛑 تم إلغاء التحميل.")

# ---------------- /url COMMAND ----------------
async def url_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        await handle_url_input(update, context, context.args[0].strip())
    else:
        await update.message.reply_text(
            "🔗 أرسل رابط الفيديو (YouTube، Instagram، إلخ)\n\nمثال:\n/url https://youtube.com/watch?v=..."
        )

# ---------------- URL TEXT HANDLER ----------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if is_url(text):
        await handle_url_input(update, context, text)
    else:
        await update.message.reply_text("ابعت رابط صحيح أو استخدم /url")

async def handle_url_input(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    context.user_data["pending_url"] = url
    keyboard = [
        [
            InlineKeyboardButton("🎬 1080p", callback_data="url_1080"),
            InlineKeyboardButton("📺 720p",  callback_data="url_720"),
        ],
        [
            InlineKeyboardButton("📱 480p",  callback_data="url_480"),
            InlineKeyboardButton("🎧 MP3",   callback_data="url_mp3"),
        ],
        [InlineKeyboardButton("⚡ أفضل جودة", callback_data="url_best")],
    ]
    await update.message.reply_text(
        f"🔗 تم استلام الرابط:\n{url}\n\nاختر جودة التحميل:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ---------------- BUTTONS ----------------
async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "upload":
        await q.edit_message_text("📤 ابعت الفيديو أو الصوت")
    elif q.data == "url_prompt":
        await q.edit_message_text("🔗 أرسل الرابط مباشرة في المحادثة أو استخدم:\n/url https://...")
    elif q.data == "mp3":
        await q.edit_message_text("🎧 ابعت ملف لتحويله MP3")
    elif q.data == "info":
        await q.edit_message_text("🎬 ابعت ملف لمعرفة المعلومات")
    elif q.data == "split":
        await q.edit_message_text("✂️ ابعت ملف لتقسيمه")
    elif q.data == "quality":
        keyboard = [
            [InlineKeyboardButton("1080p", callback_data="q1080")],
            [InlineKeyboardButton("720p",  callback_data="q720")],
            [InlineKeyboardButton("480p",  callback_data="q480")],
        ]
        await q.edit_message_text("⚙️ اختر الجودة:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif q.data in ("url_1080", "url_720", "url_480", "url_mp3", "url_best"):
        await handle_url_download(q, context)

# ---------------- URL DOWNLOAD WITH PROGRESS ----------------
async def handle_url_download(q, context: ContextTypes.DEFAULT_TYPE):
    url = context.user_data.get("pending_url")
    if not url:
        await q.edit_message_text("❌ لم يتم العثور على رابط. أرسل رابطاً أولاً.")
        return

    quality_map = {
        "url_1080": ("bestvideo[vcodec^=avc][height<=1080]+bestaudio[ext=m4a]/bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best", "1080p"),
        "url_720":  ("bestvideo[vcodec^=avc][height<=720]+bestaudio[ext=m4a]/bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo+bestaudio/best",     "720p"),
        "url_480":  ("bestvideo[vcodec^=avc][height<=480]+bestaudio[ext=m4a]/bestvideo[ext=mp4][height<=480]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best[height<=480]/bestvideo+bestaudio/best",     "480p"),
        "url_mp3":  ("bestaudio/best",                                                                                                                                                                             "MP3"),
        "url_best": ("bestvideo[vcodec^=avc]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",                                                                                  "أفضل جودة"),
    }

    fmt, label = quality_map[q.data]
    is_audio = q.data == "url_mp3"
    uid = uuid.uuid4().hex
    out_template = os.path.join(DOWNLOAD_DIR, f"{uid}.%(ext)s")

    user_id = q.from_user.id
    cancel_event = Event()
    ACTIVE_DOWNLOADS[user_id] = {"cancel": cancel_event, "uid": uid, "upload_task": None}

    await q.edit_message_text(f"⏳ جاري التحميل بجودة {label}...\n░░░░░░░░░░ 0%")

    loop = asyncio.get_running_loop()
    last_edit = {"t": 0}

    def progress_hook(d):
        if cancel_event.is_set():
            raise Exception("تم إلغاء التحميل")
        now = time.time()
        if d["status"] == "downloading" and now - last_edit["t"] >= 3:
            last_edit["t"] = now
            downloaded_bytes = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0

            if total:
                percent = min(int(downloaded_bytes / total * 100), 99)
                bar = make_progress_bar(percent)
                dl_mb  = downloaded_bytes / 1024 / 1024
                tot_mb = total / 1024 / 1024
                spd    = f"{speed/1024/1024:.1f} MB/s" if speed else "..."
                text   = (
                    f"⬇️ جاري التحميل بجودة {label}\n"
                    f"{bar} {percent}%\n"
                    f"{dl_mb:.1f} / {tot_mb:.1f} MB  |  {spd}  |  ETA: {eta}s"
                )
            else:
                dl_mb = downloaded_bytes / 1024 / 1024
                text  = f"⬇️ جاري التحميل...\n{dl_mb:.1f} MB تم تحميلها"

            asyncio.run_coroutine_threadsafe(_safe_edit(q.message, text), loop)

        elif d["status"] == "finished":
            asyncio.run_coroutine_threadsafe(
                _safe_edit(q.message, "✅ اكتمل التحميل!\n\n⏫ جاري الرفع إلى تيليغرام..."),
                loop,
            )

    ydl_opts = {
        "format": fmt,
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "progress_hooks": [progress_hook],
        "concurrent_fragment_downloads": 16,
        "http_chunk_size": 10485760,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 3,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android", "web"],
                "player_skip": ["webpage"],
            }
        },
        **({"cookiefile": COOKIES_FILE} if os.path.exists(COOKIES_FILE) else {}),
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    }
    if is_audio:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"

    try:
        await loop.run_in_executor(None, lambda: _do_download(ydl_opts, url))

        if cancel_event.is_set():
            await _safe_edit(q.message, "🛑 تم إلغاء التحميل.")
            return

        downloaded = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(uid)]
        if not downloaded:
            await _safe_edit(q.message, "❌ فشل التحميل. تأكد من صحة الرابط.")
            return

        file_path = os.path.join(DOWNLOAD_DIR, downloaded[0])

        if not is_audio:
            await _safe_edit(q.message, "✅ اكتمل التحميل\n\n🔧 جاري معالجة الملف...")
            await loop.run_in_executor(None, remux_faststart, file_path)

        file_size = os.path.getsize(file_path)
        size_mb   = file_size / 1024 / 1024
        meta      = {} if is_audio else get_video_metadata(file_path)

        # Split into parts if the file exceeds Telegram's 2 GB limit
        if not is_audio and file_size > MAX_UPLOAD_BYTES:
            num_parts = math.ceil(file_size / MAX_UPLOAD_BYTES)
            await _safe_edit(
                q.message,
                f"✅ اكتمل التحميل ({size_mb:.1f} MB)\n\n✂️ الملف كبير — جاري التقسيم إلى {num_parts} أجزاء..."
            )
            parts = await loop.run_in_executor(None, split_video, file_path, meta, uid)
        else:
            parts = [file_path]

        chat_id   = q.message.chat.id
        num_parts = len(parts)

        for idx, part_path in enumerate(parts, start=1):
            if cancel_event.is_set():
                break

            part_meta  = get_video_metadata(part_path) if not is_audio else {}
            part_size  = os.path.getsize(part_path) / 1024 / 1024
            part_label = f" (جزء {idx}/{num_parts})" if num_parts > 1 else ""
            thumb_path = None

            if not is_audio:
                thumb_path = await loop.run_in_executor(
                    None, generate_thumbnail, part_path, part_meta.get("duration", 0)
                )

            await _safe_edit(
                q.message,
                f"⏫ جاري رفع{part_label} ({part_size:.1f} MB) عبر MTProto..."
            )

            last_upload = {"t": 0, "pct": -1}

            def make_progress(lbl):
                def upload_progress(current, total):
                    if cancel_event.is_set():
                        return
                    now = time.time()
                    pct = min(int(current / total * 100), 99)
                    if now - last_upload["t"] >= 3 and pct != last_upload["pct"]:
                        last_upload["t"]   = now
                        last_upload["pct"] = pct
                        bar    = make_progress_bar(pct)
                        cur_mb = current / 1024 / 1024
                        tot_mb = total   / 1024 / 1024
                        text   = (
                            f"📤 جاري الرفع{lbl}\n"
                            f"{bar} {pct}%\n"
                            f"{cur_mb:.1f} / {tot_mb:.1f} MB"
                        )
                        asyncio.run_coroutine_threadsafe(_safe_edit(q.message, text), loop)
                return upload_progress

            progress_cb = make_progress(part_label)

            async def do_upload(pp=part_path, pm=part_meta, tp=thumb_path, cb=progress_cb):
                if is_audio:
                    await pyro.send_audio(chat_id, pp, progress=cb)
                else:
                    await pyro.send_video(
                        chat_id, pp,
                        duration=pm.get("duration", 0),
                        width=pm.get("width", 0),
                        height=pm.get("height", 0),
                        thumb=tp,
                        supports_streaming=True,
                        progress=cb,
                    )

            upload_task = asyncio.ensure_future(do_upload())
            ACTIVE_DOWNLOADS[user_id]["upload_task"] = upload_task
            await upload_task

            if thumb_path:
                try:
                    os.remove(thumb_path)
                except Exception:
                    pass

        if cancel_event.is_set():
            await _safe_edit(q.message, "🛑 تم إلغاء الرفع.")
        else:
            parts_note = f" ({num_parts} أجزاء)" if num_parts > 1 else ""
            await _safe_edit(q.message, f"✅ تم الإرسال بنجاح بجودة {label}{parts_note}! ({size_mb:.1f} MB)")

    except asyncio.CancelledError:
        await _safe_edit(q.message, "🛑 تم إلغاء العملية.")
    except Exception as e:
        if not cancel_event.is_set():
            err = str(e)
            if "403" in err or "HTTP Error 403" in err or "PO Token" in err:
                cookie_status = "✅ الكوكيز محمّلة لكنها منتهية أو غير صالحة." if os.path.exists(COOKIES_FILE) else "❌ لا توجد كوكيز محفوظة."
                await _safe_edit(
                    q.message,
                    "⛔ يوتيوب يرفض التحميل (خطأ 403)\n\n"
                    f"{cookie_status}\n\n"
                    "الحل: يجب رفع ملف كوكيز من حساب يوتيوب مسجّل الدخول:\n\n"
                    "1️⃣ ثبّت امتداد <b>Get cookies.txt LOCALLY</b> في Chrome أو Firefox\n"
                    "2️⃣ افتح youtube.com وأنت مسجّل دخول في حسابك\n"
                    "3️⃣ اضغط الامتداد ← Export ← احفظ الملف\n"
                    "4️⃣ أرسل /setcookies ثم أرسل الملف كمستند",
                    parse_mode="HTML"
                )
            else:
                await _safe_edit(q.message, f"❌ خطأ:\n{err[:500]}")
    finally:
        ACTIVE_DOWNLOADS.pop(user_id, None)
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(uid) or f.endswith(".aria2"):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, f))
                except Exception:
                    pass

def _do_download(ydl_opts, url):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

# ---------------- FILE HANDLER ----------------
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = update.message.document or update.message.video or update.message.audio
    if not file:
        return
    msg      = await update.message.reply_text("⬇️ جاري التحميل...")
    file_obj = await context.bot.get_file(file.file_id)
    name     = f"{uuid.uuid4().hex}.mp4"
    path     = os.path.join(DOWNLOAD_DIR, name)
    await file_obj.download_to_drive(path)
    old_path = context.user_data.get("file")
    if old_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except Exception:
            pass
    context.user_data["file"] = path
    await msg.edit_text("✅ جاهز للمعالجة")

# ---------------- MP3 ----------------
async def to_mp3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = context.user_data.get("file")
    if not path:
        await update.message.reply_text("ابعت ملف الأول")
        return
    out = path + ".mp3"
    run_cmd(["ffmpeg", "-i", path, "-vn", "-ab", "192k", out])
    await update.message.reply_text("🎧 تم التحويل MP3")

# ---------------- INFO ----------------
async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = context.user_data.get("file")
    if not path:
        await update.message.reply_text("ابعت ملف الأول")
        return
    result = run_cmd(["ffprobe", "-v", "error", "-show_format", "-show_streams", path])
    await update.message.reply_text("🎬 معلومات الملف:\n" + result.stdout.decode()[:4000])

# ---------------- SPLIT ----------------
async def split(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = context.user_data.get("file")
    if not path:
        await update.message.reply_text("ابعت ملف الأول")
        return
    out = path + "_part.mp4"
    run_cmd(["ffmpeg", "-i", path, "-t", "30", "-c", "copy", out])
    await update.message.reply_text("✂️ تم تقسيم أول 30 ثانية")

# ---------------- QUALITY ----------------
async def quality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = context.user_data.get("file")
    if not path:
        await update.message.reply_text("ابعت ملف الأول")
        return
    out = path + "_compressed.mp4"
    run_cmd(["ffmpeg", "-i", path, "-vf", "scale=1280:720", "-preset", "fast", out])
    await update.message.reply_text("⚙️ تم ضغط الفيديو 720p")

# ---------------- STREAM LINK ----------------
async def stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = context.user_data.get("file")
    if not path:
        await update.message.reply_text("ابعت ملف الأول")
        return
    filename = os.path.basename(path)
    url = f"http://0.0.0.0:8080/file/{filename}"
    await update.message.reply_text(f"📡 رابط التشغيل:\n{url}")

# ---------------- /setcookies COMMAND ----------------
AWAITING_COOKIES: set = set()   # user_ids waiting to send a cookies file

async def set_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    AWAITING_COOKIES.add(user_id)
    status = "✅ كوكيز محفوظة بالفعل — سيتم استبدالها." if os.path.exists(COOKIES_FILE) else "❌ لا توجد كوكيز حالياً."
    await update.message.reply_text(
        f"🍪 {status}\n\n"
        "أرسل ملف <b>cookies.txt</b> الآن كمستند (ليس صورة).\n\n"
        "كيف تحصل على الملف:\n"
        "1️⃣ ثبّت امتداد <b>Get cookies.txt LOCALLY</b> في Chrome أو Firefox\n"
        "2️⃣ افتح <b>youtube.com</b> وأنت مسجّل دخول\n"
        "3️⃣ اضغط الامتداد ← <b>Export</b> ← احفظ الملف\n"
        "4️⃣ أرسله هنا كمستند",
        parse_mode="HTML"
    )

async def receive_cookies_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in AWAITING_COOKIES:
        return   # not waiting for cookies — let other handlers deal with it

    doc = update.message.document
    if not doc:
        return

    if not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("❌ يجب أن يكون الملف بصيغة .txt — حاول مجدداً.")
        return

    AWAITING_COOKIES.discard(user_id)
    file_obj = await context.bot.get_file(doc.file_id)
    await file_obj.download_to_drive(COOKIES_FILE)
    await update.message.reply_text(
        "✅ تم حفظ ملف الكوكيز بنجاح!\n"
        "سيتم استخدامه تلقائياً في جميع تحميلات يوتيوب من الآن."
    )

# ---------------- MAIN ----------------
async def post_init(application):
    _clean_downloads()
    await pyro.start()

async def post_shutdown(application):
    try:
        await pyro.stop()
    except Exception:
        pass

async def error_handler(update, context):
    print(f"[ERROR] {context.error}")
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ حدث خطأ غير متوقع. يرجى المحاولة مجدداً."
            )
    except Exception:
        pass

def build_app():
    bot_app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(60)
        .pool_timeout(15)
        .build()
    )

    bot_app.add_error_handler(error_handler)

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("url", url_command))
    bot_app.add_handler(CommandHandler("cancel", cancel))
    bot_app.add_handler(CommandHandler("setcookies", set_cookies))
    bot_app.add_handler(MessageHandler(filters.Document.ALL, receive_cookies_file), group=0)
    bot_app.add_handler(CallbackQueryHandler(buttons))
    bot_app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO, handle_file))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    bot_app.add_handler(CommandHandler("mp3", to_mp3))
    bot_app.add_handler(CommandHandler("info", info))
    bot_app.add_handler(CommandHandler("split", split))
    bot_app.add_handler(CommandHandler("quality", quality))
    bot_app.add_handler(CommandHandler("stream", stream))

    return bot_app

def main():
    while True:
        try:
            print("Bot starting...")
            app = build_app()
            app.run_polling(
                drop_pending_updates=True,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as e:
            print(f"[CRASH] Bot stopped with error: {e}. Restarting in 5 seconds...")
            time.sleep(5)

if __name__ == "__main__":
    main()
