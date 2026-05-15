# ================== Dockerfile for Render.com ==================
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

# نصب ffmpeg و وابستگی‌های سیستم
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
    libasound2t64 \
    libxshmfence1 \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# تنظیمات محیطی
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PLAYWRIGHT_SKIP_FFMPEG_INSTALL=1

# محدودیت RAM برای Python GC
ENV PYTHONMALLOC=malloc
ENV MALLOC_TRIM_THRESHOLD_=65536

WORKDIR /app

# نصب پکیج‌های پایتون
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium --with-deps

# کپی کد بات
COPY bot.py .
COPY FastTelethon.py .
COPY github.py .
COPY savep_handler.py .

# ایجاد پوشه خروجی با دسترسی کامل
RUN mkdir -p output_files && chmod -R 777 output_files

EXPOSE 10000

# ulimit -v برای محدود کردن virtual memory به 500MB
# اگه Render از این پشتیبانی نکنه، پایتون خودش resource limit میزنه
CMD ["python", "-u", "bot.py"]
