from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any
import mysql.connector
from pinecone import Pinecone
import google.generativeai as genai
import os
import sys

# --- LINE SDK Import ---
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = FastAPI()

# --- CORS Setup ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration ---
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "admin1234") 

# Database Config
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASS", "")
DB_NAME = os.getenv("DB_NAME", "test")
DB_PORT = os.getenv("DB_PORT", "4000")

MYSQL_CONFIG = {
    'user': DB_USER,
    'password': DB_PASS,
    'host': DB_HOST,
    'database': DB_NAME,
    'port': int(DB_PORT),
    'ssl_disabled': False
}

# --- Initialization ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

pc = None
index = None
if PINECONE_API_KEY:
    pc = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index("nursing-kb")

line_bot_api = None
handler = None
if LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Data Models ---
class ChatRequest(BaseModel):
    message: str

# --- Helper Functions ---
def get_db_connection():
    return mysql.connector.connect(**MYSQL_CONFIG)

def get_embedding(text):
    if not GEMINI_API_KEY: return []
    try:
        result = genai.embed_content(model="models/text-embedding-004", content=text, task_type="retrieval_query")
        return result['embedding']
    except Exception as e:
        print(f"Embedding Error: {e}")
        return []

# --- [UPDATED] Smart Search Logic ---
def query_mysql(user_query):
    """
    ระบบค้นหาอัจฉริยะ: ตรวจจับ Keyword เพื่อดึงข้อมูลที่เกี่ยวข้องมาทั้งหมด
    แทนการค้นหาแบบระบุชื่อตรงๆ
    """
    if not all([DB_HOST, DB_USER, DB_NAME]): return ""
    results_text = []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        
        q = user_query.lower()
        # ตรวจจับ Keyword ว่าผู้ใช้ถามเรื่องอะไร
        fetch_training = any(k in q for k in ['อบรม', 'ตาราง', 'หลักสูตร', 'เรียน', 'cneu', '2568', '68'])
        fetch_meeting = any(k in q for k in ['ประชุม', 'meeting', 'นัดหมาย', 'วาระ'])
        fetch_project = any(k in q for k in ['โครงการ', 'project', 'กิจกรรม'])

        # 1. ค้นหาตาราง "อบรม"
        try:
            if fetch_training:
                # ถ้าถามกว้างๆ ให้ดึงรายการล่าสุดมา 15 รายการเลย
                sql = "SELECT course_name, date_start, location, cneu_points, status FROM training_courses ORDER BY date_start ASC LIMIT 15"
                cursor.execute(sql)
            else:
                # ถ้าไม่ถามเจาะจง ลองค้นหาแบบ LIKE เผื่อฟลุ๊ค
                sql = "SELECT course_name, date_start, location, cneu_points, status FROM training_courses WHERE course_name LIKE %s LIMIT 5"
                cursor.execute(sql, (f"%{user_query}%",))
            
            rows = cursor.fetchall()
            if rows:
                results_text.append(f"--- 📅 ตารางอบรมที่พบ ({len(rows)} รายการ) ---")
                for t in rows:
                    results_text.append(f"- {t['course_name']} (วันที่: {t['date_start']}) @{t['location']} [CNEU: {t['cneu_points']}]")
        except Exception as e: print(f"Training Error: {e}")

        # 2. ค้นหาตาราง "การประชุม"
        try:
            if fetch_meeting:
                sql = "SELECT title, meeting_date, start_time, room FROM meeting_schedule ORDER BY meeting_date ASC LIMIT 10"
                cursor.execute(sql)
            else:
                sql = "SELECT title, meeting_date, start_time, room FROM meeting_schedule WHERE title LIKE %s LIMIT 5"
                cursor.execute(sql, (f"%{user_query}%",))
            
            rows = cursor.fetchall()
            if rows:
                results_text.append(f"\n--- 📝 การประชุม ---")
                for m in rows:
                    results_text.append(f"- {m['title']} ({m['meeting_date']} {m['start_time']}) @{m['room']}")
        except Exception as e: print(f"Meeting Error: {e}")

        # 3. ค้นหาตาราง "โครงการ"
        try:
            if fetch_project:
                sql = "SELECT project_name, status, responsible_unit FROM nursing_projects ORDER BY id DESC LIMIT 15"
                cursor.execute(sql)
            else:
                sql = "SELECT project_name, status, responsible_unit FROM nursing_projects WHERE project_name LIKE %s LIMIT 5"
                cursor.execute(sql, (f"%{user_query}%",))
            
            rows = cursor.fetchall()
            if rows:
                results_text.append(f"\n--- 🚀 โครงการ ---")
                for p in rows:
                    results_text.append(f"- {p['project_name']} ({p['responsible_unit']}) [{p['status']}]")
        except Exception as e: print(f"Project Error: {e}")

        if not results_text: return ""
        return "\n".join(results_text)

    except Exception as e:
        print(f"DB Connection Error: {e}")
        return ""
    finally:
        if conn and conn.is_connected(): conn.close()

def query_pinecone(vector):
    if not index or not vector: return ""
    try:
        results = index.query(vector=vector, top_k=3, include_metadata=True, namespace="documents")
        return "\n".join([m['metadata'].get('text', '') for m in results['matches'] if m['score'] > 0.60])
    except: return ""

def generate_bot_response(user_query):
    restricted = ["เงินเดือน", "สลิป", "รหัสผ่าน", "admin"]
    if any(w in user_query for w in restricted): return "⛔ ไม่สามารถเข้าถึงข้อมูลส่วนบุคคลได้ครับ"

    vector = get_embedding(user_query)
    
    # ดึงข้อมูลด้วย Logic ใหม่
    mysql_data = query_mysql(user_query)
    pinecone_data = query_pinecone(vector)
    
    context = f"เอกสาร:\n{pinecone_data}\n\nฐานข้อมูล (MySQL):\n{mysql_data}"
    
    # Model Fallback
    models = ['models/gemini-2.0-flash', 'models/gemini-1.5-flash', 'gemini-1.5-flash']
    for m in models:
        try:
            model = genai.GenerativeModel(m)
            # เพิ่ม Prompt ให้ AI ฉลาดเรื่องปี พ.ศ./ค.ศ.
            prompt = f"ตอบคำถามพยาบาลโดยใช้ข้อมูลนี้: {context}\nคำถาม: {user_query}\n(หมายเหตุ: ปี 2568 = 2025)"
            return model.generate_content(prompt).text
        except: continue
    return "ขออภัย ระบบ AI ขัดข้องชั่วคราว"

# ==========================================
# 🌟 ADMIN API ENDPOINTS 🌟
# ==========================================

@app.get("/api/admin/{table_name}")
def admin_get_data(table_name: str, secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Invalid Admin Secret")
    
    valid_tables = ["training_courses", "meeting_schedule", "nursing_projects"]
    if table_name not in valid_tables: raise HTTPException(400, "Invalid table")

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT 50")
        rows = cursor.fetchall()
        for row in rows:
            for k, v in row.items():
                if hasattr(v, 'strftime'): row[k] = v.strftime('%Y-%m-%d')
                if hasattr(v, 'total_seconds'): row[k] = str(v)
        conn.close()
        return rows
    except Exception as e:
        return {"error": str(e)}

@app.post("/api/admin/{table_name}")
async def admin_add_data(table_name: str, request: Request, secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Invalid Admin Secret")
    
    data = await request.json()
    columns = ', '.join(data.keys())
    placeholders = ', '.join(['%s'] * len(data))
    values = list(data.values())

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        sql = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})"
        cursor.execute(sql, values)
        conn.commit()
        conn.close()
        return {"status": "success", "message": "Data added"}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/api/admin/{table_name}/{record_id}")
def admin_delete_data(table_name: str, record_id: int, secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Invalid Admin Secret")
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM {table_name} WHERE id = %s", (record_id,))
        conn.commit()
        conn.close()
        return {"status": "success", "message": "Data deleted"}
    except Exception as e:
        return {"error": str(e)}

# --- Standard Endpoints ---
@app.get("/")
def root(): return {"status": "RJ Nurse Backend Running"}

@app.post("/chat")
def chat(r: ChatRequest): 
    return {"reply": generate_bot_response(r.message)}

@app.post("/callback")
async def callback(request: Request):
    if not handler: raise HTTPException(500, "Line not set")
    try: handler.handle((await request.body()).decode('utf-8'), request.headers['X-Line-Signature'])
    except InvalidSignatureError: raise HTTPException(400, "Invalid signature")
    return 'OK'

if handler:
    @handler.add(MessageEvent, message=TextMessage)
    def handle_message(event):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=generate_bot_response(event.message.text)))
