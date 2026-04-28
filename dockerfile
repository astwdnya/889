# ================== Dockerfile for Render.com ==================
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

# نصب ffmpeg (برای yt-dlp) + فونت‌ها
RUN apt-get update && apt-get install -y \
    ffmpeg \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# جلوگیری از نصب دوباره ffmpeg توسط yt-dlp (اختیاری اما مفید)
ENV PLAYWRIGHT_SKIP_FFMPEG_INSTALL=1

WORKDIR /app

# نصب وابستگی‌های پایتون
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium --with-deps

# کپی کد بات
COPY bot.py .

# ایجاد پوشه خروجی
RUN mkdir -p output_files && chmod -R 777 output_files

EXPOSE 10000

CMD ["python", "bot.py"]
