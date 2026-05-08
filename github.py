import os
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from github import upload_to_github, github_configured

# توکن بات را از @BotFather بگیرید
BOT_TOKEN = "توکن_بات_تلگرام_خود_را_وارد_کنید"

logging.basicConfig(level=logging.INFO)

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not github_configured():
        await update.message.reply_text("❌ گیت‌هاب تنظیم نشده (توکن یا مخزن موجود نیست)")
        return

    # دریافت فایل از تلگرام
    file = await update.message.effective_attachment.get_file()
    file_path = f"downloads/{file.file_id}_{update.message.file.file_name}"
    os.makedirs("downloads", exist_ok=True)

    await file.download_to_drive(file_path)

    await update.message.reply_text("⏳ در حال آپلود به گیت‌هاب...")

    success, msg, url = await upload_to_github(file_path)

    if success:
        await update.message.reply_text(f"✅ فایل آپلود شد!\n\n🔗 لینک دانلود مستقیم:\n{url}")
    else:
        await update.message.reply_text(f"❌ خطا: {msg}")

    # پاک کردن فایل موقت
    os.remove(file_path)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_file))
    print("بات راه‌اندازی شد...")
    app.run_polling()

if __name__ == "__main__":
    main()
