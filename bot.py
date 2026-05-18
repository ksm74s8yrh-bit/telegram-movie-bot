from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
import yt_dlp
import os

BOT_TOKEN = os.getenv("8731635445:AAER_lUzaKC21xR31K3EXJN-zUk9t_cr-v4")

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    await update.message.reply_text("جاري التحميل...")

    ydl_opts = {
        "format": "best",
        "outtmpl": "video.%(ext)s",
        "noplaylist": True
    }

    try:
        for f in os.listdir():
            if f.startswith("video."):
                os.remove(f)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        file_name = None

        for f in os.listdir():
            if f.startswith("video."):
                file_name = f
                break

        if not file_name:
            await update.message.reply_text("فشل التحميل")
            return

        with open(file_name, "rb") as video:
            await update.message.reply_document(video)

        os.remove(file_name)

    except Exception as e:
        await update.message.reply_text(str(e))

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, handle)
)

app.run_polling()
