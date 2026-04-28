FROM python:3.11-slim

# نصب Chromium و وابستگی‌های سیستمی
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libgbm-dev \
    fonts-liberation \
    libxkbcommon-x11-0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt/lists/*

# تنظیم متغیر محیطی Chromium
ENV CHROMIUM_PATH=/usr/bin/chromium

# پوشه کار
WORKDIR /app

# کپی فایل requirements و نصب پکیج‌های پایتون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی بقیه فایل‌ها
COPY pdf_bot.py .

# پورت health check
EXPOSE 10000

# اجرای ربات
CMD ["python", "pdf_bot.py"]
