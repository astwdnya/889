FROM mcr.microsoft.com/playwright/python:v1.50-noble

# نصب وابستگی‌های سیستم اضافی (برای yt-dlp و ffmpeg)
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

# تنظیم متغیر محیطی برای Playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# کپی و نصب پکیج‌های پایتون
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium --with-deps

# کپی کد اصلی بات
COPY bot.py .

# ایجاد پوشه خروجی
RUN mkdir -p output_files

EXPOSE 10000

# اجرای بات
CMD ["python", "bot.py"]
