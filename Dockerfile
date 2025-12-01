# ใช้ Python เวอร์ชันเบาที่สุด
FROM python:3.9-slim

# ตั้งค่าโฟลเดอร์ทำงานใน Server
WORKDIR /app

# Copy ไฟล์รายการ library และติดตั้ง
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy โค้ดทั้งหมดเข้า Server
COPY . .

# คำสั่งรัน Server (รับ Port จาก Environment Variable)
CMD sh -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"
