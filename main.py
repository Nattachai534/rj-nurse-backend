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

# --- CORS Setup (สำคัญมากสำหรับหน้า Admin) ---
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

# Admin Secret (รหัสผ่านง่ายๆ กันคนนอกมากดเล่น)
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

# Setup LINE Bot
line_bot_api = None
handler = None
if LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)

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

# --- Smart Search Logic ---
def query_mysql(keyword):
    if not all([DB_HOST, DB_USER, DB_NAME]): return ""
    results_text = []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        try:
            cursor.execute("SELECT course_name, date_start, status FROM training_courses WHERE course_name LIKE %s LIMIT 5", (f"%{keyword}%",))
            for t in cursor.fetchall(): results_text.append(f"- อบรม: {t['course_name']} ({t['date_start']}) [{t['status']}]")
        except: pass

        try:
            cursor.execute("SELECT title, meeting_date, room FROM meeting_schedule WHERE title LIKE %s LIMIT 5", (f"%{keyword}%",))
            for m in cursor.fetchall(): results_text.append(f"- ประชุม: {m['title']} ({m['meeting_date']}) {m['room']}")
        except: pass

        try:
            cursor.execute("SELECT project_name, status FROM nursing_projects WHERE project_name LIKE %s LIMIT 5", (f"%{keyword}%",))
            for p in cursor.fetchall(): results_text.append(f"- โครงการ: {p['project_name']} [{p['status']}]")
        except: pass

        return "\n".join(results_text)
    except Exception as e:
        print(f"DB Error: {e}")
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
    context = f"เอกสาร:\n{query_pinecone(vector)}\nฐานข้อมูล:\n{query_mysql(user_query)}"
    
    # Model Fallback
    models = ['models/gemini-2.0-flash', 'models/gemini-1.5-flash', 'gemini-1.5-flash']
    for m in models:
        try:
            model = genai.GenerativeModel(m)
            return model.generate_content(f"ตอบสั้นๆจากข้อมูลนี้: {context}\nคำถาม: {user_query}").text
        except: continue
    return "ขออภัย ระบบ AI ขัดข้องชั่วคราว"

# ==========================================
# 🌟 ADMIN API ENDPOINTS (เพิ่มใหม่) 🌟
# ==========================================

# 1. ดูข้อมูลทั้งหมด (Read)
@app.get("/api/admin/{table_name}")
def admin_get_data(table_name: str, secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Invalid Admin Secret")
    
    valid_tables = ["training_courses", "meeting_schedule", "nursing_projects"]
    if table_name not in valid_tables: raise HTTPException(400, "Invalid table")

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # แปลงวันที่ให้เป็น String เพื่อป้องกัน JSON Error
        cursor.execute(f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT 50")
        rows = cursor.fetchall()
        for row in rows:
            for k, v in row.items():
                if hasattr(v, 'strftime'): row[k] = v.strftime('%Y-%m-%d')
                if hasattr(v, 'total_seconds'): row[k] = str(v) # For TIME type
        conn.close()
        return rows
    except Exception as e:
        return {"error": str(e)}

# 2. เพิ่มข้อมูล (Create)
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

# 3. ลบข้อมูล (Delete)
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
def chat(r: ChatRequest): return {"reply": generate_bot_response(r.message)}

class ChatRequest(BaseModel): message: str

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
