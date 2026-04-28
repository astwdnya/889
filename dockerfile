# ================== Dockerfile for Render.com - Full Version ==================
# این فایل برای اجرای بات با پشتیبانی کامل از Playwright، yt-dlp و فشرده‌سازی ویدیو با ffmpeg ساخته شده

FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

# نصب ffmpeg (برای فشرده‌سازی ویدیو) و سایر وابستگی‌های سیستم
RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# تنظیمات محیطی مهم
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# جلوگیری از نصب دوباره ffmpeg توسط yt-dlp (بهینه‌سازی)
ENV PLAYWRIGHT_SKIP_FFMPEG_INSTALL=1

WORKDIR /app

# نصب پکیج‌های پایتون
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium --with-deps

# کپی کد اصلی بات
COPY bot.py .

# ایجاد پوشه خروجی با دسترسی کامل (مهم برای Render)
RUN mkdir -p output_files && chmod -R 777 output_files

EXPOSE 10000

# اجرای بات
CMD ["python", "bot.py"]
