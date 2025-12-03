from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import mysql.connector
from pinecone import Pinecone
import google.generativeai as genai
import os
import sys
import threading
from datetime import datetime, timedelta

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "admin1234")
STAFF_REGISTRATION_CODE = "nurse123"

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

def get_user_role(line_user_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT role, first_name FROM line_users WHERE line_user_id = %s", (line_user_id,))
        result = cursor.fetchone()
        conn.close()
        if result: return result['role'], result['first_name']
        return 'guest', None
    except: return 'guest', None

def register_staff_profile(line_user_id, first_name, last_name, dept):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        sql = """INSERT INTO line_users (line_user_id, first_name, last_name, department, role) 
                 VALUES (%s, %s, %s, %s, 'staff')
                 ON DUPLICATE KEY UPDATE first_name=VALUES(first_name), last_name=VALUES(last_name), department=VALUES(department), role='staff'"""
        cursor.execute(sql, (line_user_id, first_name, last_name, dept))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Reg Error: {e}")
        return False

# --- Helper: Format Data for AI ---
# ฟังก์ชันนี้จะแปลงข้อมูลดิบจาก DB ให้เป็นข้อความสรุป โดยตัดช่องที่ว่างทิ้งไปเลย
def format_db_row(row, title_field):
    lines = []
    # ชื่อหัวข้อหลัก (เช่น ชื่อหลักสูตร)
    if row.get(title_field):
        lines.append(f"🔹 {row[title_field]}")
    
    # แปลงชื่อฟิลด์ให้ AI เข้าใจง่าย
    field_map = {
        "description": "รายละเอียด", "objective": "วัตถุประสงค์", "agenda": "วาระการประชุม", "detail": "เนื้อหาข่าว",
        "date_start": "วันเริ่ม", "date_end": "วันสิ้นสุด", "date_announce": "วันประกาศ",
        "date_exam_written": "วันสอบข้อเขียน", "date_exam_interview": "วันสอบสัมภาษณ์", "date_report": "วันรายงานตัว",
        "meeting_date": "วันที่ประชุม", "start_time": "เวลาเริ่ม", "end_time": "เวลาเลิก",
        "location": "สถานที่", "room": "ห้องประชุม",
        "link_register": "ลิงก์สมัคร/ลงทะเบียน", "link_doc_application": "เอกสารประกอบการสมัคร",
        "link_announce_written": "ประกาศผลข้อเขียน", "link_announce_interview": "ประกาศผลสัมภาษณ์",
        "link_announce_final": "ประกาศผลผู้มีสิทธิ์", "link_poster": "รูปโปสเตอร์/แผนที่", "link_website": "อ่านต่อ",
        "link_zoom": "ลิงก์ Zoom", "zoom_meeting_id": "Meeting ID", "zoom_passcode": "Passcode",
        "responsible_unit": "หน่วยงาน", "unit_phone": "เบอร์หน่วยงาน", "contact_person": "ผู้ติดต่อ", "contact_phone": "เบอร์ผู้ติดต่อ",
        "process_status": "สถานะปัจจุบัน"
    }

    for k, v in row.items():
        # ข้ามฟิลด์ที่ไม่จำเป็น หรือที่เป็นค่าว่าง/None
        if k in [title_field, 'id', 'created_at', 'visibility', 'status'] or v is None or str(v).strip() == "":
            continue
            
        label = field_map.get(k, k) # ใช้ชื่อไทย ถ้าไม่มีใช้ชื่อเดิม
        lines.append(f"   - {label}: {v}")
        
    return "\n".join(lines)

# --- SMART SEARCH LOGIC V18.0 ---
def query_mysql(user_query, role='guest'):
    if not all([DB_HOST, DB_USER, DB_NAME]): return ""
    results_text = []
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        q = user_query.lower()
        access_filter = " AND visibility = 'public'" if role == 'guest' else ""
        
        fetch_training = any(k in q for k in ['อบรม', 'ตาราง', 'หลักสูตร', 'เรียน', 'cneu', '2568', '68', 'สมัคร', 'ลิงก์', 'สอบ'])
        fetch_meeting = any(k in q for k in ['ประชุม', 'meeting', 'นัดหมาย', 'วาระ'])
        fetch_project = any(k in q for k in ['โครงการ', 'project', 'กิจกรรม'])
        fetch_unit = any(k in q for k in ['หน่วยงาน', 'ตึก', 'ชั้น', 'ward', 'ติดต่อ', 'เบอร์', 'โทร', 'แผนก'])
        fetch_job = any(k in q for k in ['สมัครงาน', 'รับสมัคร', 'ตำแหน่ง', 'ว่าง', 'งาน'])
        fetch_news = any(k in q for k in ['ข่าว', 'ประกาศ', 'ประชาสัมพันธ์', 'แจ้ง'])

        def smart_fetch(table, title_col, where_clause, order_clause, limit=5):
            # 1. ลองค้นหาแบบเจาะจงก่อน
            sql = f"SELECT * FROM {table} WHERE ({where_clause}) {access_filter} {order_clause} LIMIT {limit}"
            cursor.execute(sql, (f"%{user_query}%", f"%{user_query}%"))
            rows = cursor.fetchall()
            
            # 2. ถ้าไม่เจอ ให้ดึงข้อมูลล่าสุดมา (Fallback)
            if not rows:
                sql = f"SELECT * FROM {table} WHERE 1=1 {access_filter} {order_clause} LIMIT {limit}"
                cursor.execute(sql)
                rows = cursor.fetchall()
                if rows: results_text.append(f"\n(ไม่พบข้อมูลที่ตรงเป๊ะ แต่พบข้อมูลล่าสุดจาก {table} ดังนี้:)")
            
            for row in rows:
                results_text.append(format_db_row(row, title_col))

        # เรียกใช้ Smart Fetch สำหรับแต่ละตาราง
        if fetch_training: smart_fetch('training_courses', 'course_name', 'course_name LIKE %s OR description LIKE %s', 'ORDER BY date_start ASC')
        if fetch_meeting: smart_fetch('meeting_schedule', 'title', 'title LIKE %s OR agenda LIKE %s', 'ORDER BY meeting_date ASC')
        if fetch_project: smart_fetch('nursing_projects', 'project_name', 'project_name LIKE %s OR objective LIKE %s', 'ORDER BY id DESC')
        if fetch_unit: smart_fetch('nursing_units', 'unit_name', 'unit_name LIKE %s OR description LIKE %s', 'ORDER BY id ASC')
        if fetch_job: smart_fetch('job_postings', 'position_name', 'position_name LIKE %s OR description LIKE %s', 'ORDER BY date_close ASC')
        if fetch_news: smart_fetch('nursing_news', 'topic', 'topic LIKE %s OR detail LIKE %s', 'ORDER BY news_date DESC')

        return "\n\n".join(results_text) if results_text else ""
    except Exception as e: 
        print(f"DB Error: {e}")
        return ""
    finally:
        if conn and conn.is_connected(): conn.close()

def query_pinecone(vector, role='guest'):
    if not index or not vector: return ""
    try:
        filter_dict = {}
        if role == 'guest': filter_dict = {"access": "public"}
        results = index.query(vector=vector, top_k=3, include_metadata=True, namespace="documents", filter=filter_dict)
        return "\n".join([m['metadata'].get('text', '') for m in results['matches'] if m['score'] > 0.60])
    except: return ""

def generate_bot_response(user_query, role='guest', user_name=None):
    restricted = ["เงินเดือน", "สลิป", "รหัสผ่าน", "admin"]
    if any(w in user_query for w in restricted): return "⛔ ไม่สามารถเข้าถึงข้อมูลส่วนบุคคลได้ครับ"

    vector = get_embedding(user_query)
    mysql_data = query_mysql(user_query, role)
    pinecone_data = query_pinecone(vector, role)
    
    role_txt = f"เจ้าหน้าที่ ({user_name})" if role == 'staff' else "บุคคลทั่วไป"
    context = f"สถานะผู้ถาม: {role_txt}\nเอกสารประกอบ:\n{pinecone_data}\n\nข้อมูลจากฐานข้อมูล:\n{mysql_data}"
    
    # ปรับ Prompt ให้ตอบเฉพาะที่มีข้อมูล
    prompt = f"""
    คุณคือ Bot RJ Nurse ตอบคำถามพยาบาลโดยใช้ข้อมูลนี้: 
    {context}
    
    คำถาม: {user_query}
    
    คำสั่ง:
    1. ตอบให้กระชับและตรงประเด็น
    2. **สำคัญ:** แสดงเฉพาะหัวข้อที่มีข้อมูลจริงใน Context เท่านั้น (ถ้าหัวข้อไหนเป็นค่าว่าง หรือไม่มีในข้อมูลที่ให้ไป ไม่ต้องพูดถึงเลย ห้ามบอกว่า "ไม่มีข้อมูลส่วนนี้")
    3. ถ้ามี Zoom Meeting ID และ Passcode ให้แสดงคู่กันเสมอ
    4. ถ้าไม่พบข้อมูลใดๆ เลยใน Context ให้ตอบว่า "ไม่พบข้อมูลในระบบฐานข้อมูลขณะนี้ค่ะ"
    """
    
    try:
        return genai.GenerativeModel('models/gemini-flash-latest').generate_content(prompt).text
    except: return "ขออภัย ระบบขัดข้องชั่วคราว"

# --- Admin & Notification (Same as before) ---
@app.get("/tasks/daily_notify")
def trigger_notification(secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Unauthorized")
    threading.Thread(target=check_and_send_notifications).start()
    return {"status": "Notification task started"}

def check_and_send_notifications():
    # (Logic เดิม)
    pass

@app.get("/api/admin/{table_name}")
def admin_get_data(table_name: str, secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Invalid Admin Secret")
    valid_tables = ["training_courses", "meeting_schedule", "nursing_projects", "nursing_units", "job_postings", "nursing_news", "line_users"]
    if table_name not in valid_tables: raise HTTPException(400, "Invalid table")
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        order_col = "registered_at" if table_name == "line_users" else "id"
        cursor.execute(f"SELECT * FROM {table_name} ORDER BY {order_col} DESC LIMIT 50")
        rows = cursor.fetchall()
        for row in rows:
            for k, v in row.items():
                if hasattr(v, 'strftime'): row[k] = v.strftime('%Y-%m-%d %H:%M:%S') if ':' in str(v) else v.strftime('%Y-%m-%d')
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

@app.put("/api/admin/{table_name}/{record_id}")
async def admin_update_data(table_name: str, record_id: str, request: Request, secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Invalid Admin Secret")
    data = await request.json()
    for k, v in data.items():
        if v == "": data[k] = None
    set_clause = ', '.join([f"{k} = %s" for k in data.keys()])
    values = list(data.values())
    values.append(record_id)
    pk_col = "line_user_id" if table_name == "line_users" else "id"
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        sql = f"UPDATE {table_name} SET {set_clause} WHERE {pk_col} = %s"
        cursor.execute(sql, values)
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e: return {"error": str(e)}

@app.delete("/api/admin/{table_name}/{record_id}")
def admin_delete_data(table_name: str, record_id: str, secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Invalid Admin Secret")
    pk_col = "line_user_id" if table_name == "line_users" else "id"
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"DELETE FROM {table_name} WHERE {pk_col} = %s", (record_id,))
        conn.commit()
        conn.close()
        return {"status": "success"}
    except Exception as e: return {"error": str(e)}

@app.get("/")
def root(): return {"status": "RJ Nurse Backend V18.0 Running"}

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
        try:
            user_msg = event.message.text.strip()
            user_id = event.source.user_id
            
            if user_msg.startswith("ลงทะเบียน"):
                content = user_msg.replace("ลงทะเบียน:", "").replace("ลงทะเบียน", "").strip()
                parts = content.split()
                if len(parts) < 3:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ รูปแบบผิดครับ\nพิมพ์: ลงทะเบียน ชื่อ นามสกุล รหัสลับ"))
                    return
                if parts[-1] != STAFF_REGISTRATION_CODE:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ รหัสลับไม่ถูกต้อง"))
                    return
                fname = parts[0]; lname = parts[1]; dept = " ".join(parts[2:-1]) if len(parts) > 3 else "-"
                if register_staff_profile(user_id, fname, lname, dept):
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ ลงทะเบียนสำเร็จ!\nยินดีต้อนรับคุณ {fname} {lname} ครับ"))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ บันทึกข้อมูลล้มเหลว"))
                return

            role, user_name = get_user_role(user_id)
            reply_text = generate_bot_response(user_msg, role, user_name)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

        except LineBotApiError as e:
            print(f"LINE API Error: {e}")
        except Exception as e:
            print(f"General Error: {e}")
