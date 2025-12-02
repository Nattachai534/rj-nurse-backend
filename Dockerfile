# ใช้ Python 3.10
FROM python:3.10-slim

# ตั้งค่า Working Directory
WORKDIR /app

# Copy Requirements และติดตั้ง
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy โค้ดที่เหลือ
COPY . .

# รัน Server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]
