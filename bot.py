from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import yt_dlp
import os
import glob

BOT_TOKEN = "8731635445:AAER_lUzaKC21xR31K3EXJN-zUk9t_cr-v4"

# حذف الملفات القديمة
def cleanup():
    files = glob.glob("video.*")
    for f in files:
        try:
            os.remove(f)
        except:
            pass

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    await update.message.reply_text("⏳ جاري التحميل...")

    cleanup()

    ydl_opts = {
        "format": "best[filesize<1900M]/best",
        "outtmpl": "video.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "merge_output_format": "mp4",
    }

    try:
        # تحميل الفيديو
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # البحث عن الملف
        files = glob.glob("video.*")

        if not files:
            await update.message.reply_text("❌ فشل تحميل الفيديو")
            return

        file_name = files[0]

        file_size = os.path.getsize(file_name)

        # لو الملف أكبر من 1.9GB
        if file_size > 1900 * 1024 * 1024:
            await update.message.reply_text(
                "❌ حجم الفيديو كبير جدًا لرفعه على تيليجرام"
            )
            cleanup()
            return

        await update.message.reply_text("📤 جاري رفع الفيديو...")

        # رفع الفيديو بمهلة أكبر
        with open(file_name, "rb") as video:
            await update.message.reply_document(
                document=video,
                filename=file_name,
                read_timeout=600,
                write_timeout=600,
                connect_timeout=600,
                pool_timeout=600
            )

        await update.message.reply_text("✅ تم الإرسال بنجاح")

        cleanup()

    except Exception as e:
        await update.message.reply_text(f"❌ حصل خطأ:\n{e}")

        cleanup()

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, handle)
)

print("🔥 Bot Running...")

app.run_polling()
