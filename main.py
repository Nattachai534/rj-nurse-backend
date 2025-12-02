from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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

if GEMINI_API_KEY: genai.configure(api_key=GEMINI_API_KEY)
pc = Pinecone(api_key=PINECONE_API_KEY) if PINECONE_API_KEY else None
index = pc.Index("nursing-kb") if pc else None

line_bot_api = None
handler = None
if LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)

class ChatRequest(BaseModel): message: str

def get_db_connection(): return mysql.connector.connect(**MYSQL_CONFIG)

def get_embedding(text):
    if not GEMINI_API_KEY: return []
    try:
        return genai.embed_content(model="models/text-embedding-004", content=text, task_type="retrieval_query")['embedding']
    except: return []

# --- [UPDATED] Smart Search Logic (เพิ่มค้นหาหน่วยงาน) ---
def query_mysql(user_query):
    if not all([DB_HOST, DB_USER, DB_NAME]): return ""
    results_text = []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        q = user_query.lower()
        
        # Keyword Detection
        fetch_training = any(k in q for k in ['อบรม', 'ตาราง', 'หลักสูตร', 'เรียน', 'cneu', '2568', '68', 'สมัคร'])
        fetch_meeting = any(k in q for k in ['ประชุม', 'meeting', 'นัดหมาย', 'วาระ'])
        fetch_project = any(k in q for k in ['โครงการ', 'project', 'กิจกรรม'])
        # เพิ่ม keyword สำหรับค้นหาหน่วยงาน
        fetch_unit = any(k in q for k in ['หน่วยงาน', 'ตึก', 'ชั้น', 'ward', 'ติดต่อ', 'เบอร์', 'โทร', 'แผนก'])

        # 1. ตาราง "อบรม"
        try:
            sql_base = "SELECT course_name, date_start, location, link_register, process_status FROM training_courses"
            if fetch_training:
                cursor.execute(f"{sql_base} ORDER BY date_start ASC LIMIT 15")
            else:
                cursor.execute(f"{sql_base} WHERE course_name LIKE %s LIMIT 5", (f"%{user_query}%",))
            
            rows = cursor.fetchall()
            if rows:
                results_text.append(f"--- 📅 ตารางอบรม ---")
                for t in rows:
                    results_text.append(f"- {t['course_name']} ({t['date_start']}) @{t['location']} [{t['process_status']}]")
        except Exception: pass

        # 2. ตาราง "การประชุม"
        try:
            sql_base = "SELECT title, meeting_date, start_time, room, process_status FROM meeting_schedule"
            if fetch_meeting:
                cursor.execute(f"{sql_base} ORDER BY meeting_date ASC LIMIT 10")
            else:
                cursor.execute(f"{sql_base} WHERE title LIKE %s LIMIT 5", (f"%{user_query}%",))
            
            rows = cursor.fetchall()
            if rows:
                results_text.append(f"\n--- 📝 การประชุม ---")
                for m in rows:
                    results_text.append(f"- {m['title']} ({m['meeting_date']} {m['start_time']}) @{m['room']}")
        except Exception: pass

        # 3. ตาราง "โครงการ"
        try:
            sql_base = "SELECT project_name, responsible_unit, process_status FROM nursing_projects"
            if fetch_project:
                cursor.execute(f"{sql_base} ORDER BY id DESC LIMIT 15")
            else:
                cursor.execute(f"{sql_base} WHERE project_name LIKE %s LIMIT 5", (f"%{user_query}%",))
            
            rows = cursor.fetchall()
            if rows:
                results_text.append(f"\n--- 🚀 โครงการ ---")
                for p in rows:
                    results_text.append(f"- {p['project_name']} ({p['responsible_unit']}) [{p['process_status']}]")
        except Exception: pass

        # 4. [เพิ่มใหม่] ตาราง "หน่วยงาน" (nursing_units)
        try:
            if fetch_unit:
                # ถ้าถามหาเบอร์/ตึก ให้ค้นหาชื่อหน่วยงานเลย
                cursor.execute("SELECT unit_name, floor, phone_number, description FROM nursing_units WHERE unit_name LIKE %s OR description LIKE %s LIMIT 5", (f"%{user_query}%", f"%{user_query}%"))
                rows = cursor.fetchall()
                if rows:
                    results_text.append(f"\n--- 🏥 ข้อมูลหน่วยงาน/การติดต่อ ---")
                    for u in rows:
                        results_text.append(f"- {u['unit_name']} : {u['floor']} โทร {u['phone_number']} ({u['description']})")
        except Exception as e: print(f"Unit Error: {e}")

        return "\n".join(results_text) if results_text else ""
    except Exception: return ""
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
    mysql_data = query_mysql(user_query)
    pinecone_data = query_pinecone(vector)
    
    context = f"เอกสาร:\n{pinecone_data}\n\nฐานข้อมูล (MySQL):\n{mysql_data}"
    prompt = f"ตอบคำถามพยาบาลโดยใช้ข้อมูลนี้: {context}\nคำถาม: {user_query}\n(ปี 2568 = 2025)"
    
    models = ['models/gemini-2.0-flash', 'models/gemini-2.5-flash', 'models/gemini-flash-latest']
    for m in models:
        try:
            return genai.GenerativeModel(m).generate_content(prompt).text
        except: continue
    return "ขออภัย ระบบ AI ขัดข้องชั่วคราว"

# --- Admin API ---
@app.get("/api/admin/{table_name}")
def admin_get_data(table_name: str, secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Invalid Admin Secret")
    
    # เพิ่ม nursing_units เข้าไปในรายการที่อนุญาต
    valid_tables = ["training_courses", "meeting_schedule", "nursing_projects", "nursing_units"]
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
                if v is None: row[k] = "" 
        conn.close()
        return rows
    except Exception as e: return {"error": str(e)}

@app.post("/api/admin/{table_name}")
async def admin_add_data(table_name: str, request: Request, secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Invalid Admin Secret")
    data = await request.json()
    for k, v in data.items():
        if v == "": data[k] = None
    columns = ', '.join(data.keys())
    placeholders = ', '.join(['%s'] * len(data))
    values = list(data.values())
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders})", values)
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e: return {"error": str(e)}

@app.delete("/api/admin/{table_name}/{record_id}")
def admin_delete_data(table_name: str, record_id: int, secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Invalid Admin Secret")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM {table_name} WHERE id = %s", (record_id,))
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e: return {"error": str(e)}

@app.get("/")
def root(): return {"status": "RJ Nurse Backend V3.2 Running"}

@app.post("/chat")
def chat(r: ChatRequest): return {"reply": generate_bot_response(r.message)}

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
