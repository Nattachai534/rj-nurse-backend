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
from linebot.exceptions import InvalidSignatureError
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

def format_zoom(link, mid, pwd):
    if not link and not mid: return ""
    info = ""
    if link: info += f"[Link Zoom: {link}] "
    if mid: info += f"(Meeting ID: {mid}"
    if pwd: info += f" Passcode: {pwd})"
    if mid: info += ")"
    return info

# --- SEARCH LOGIC ---
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
        fetch_meeting = any(k in q for k in ['ประชุม', 'meeting', 'นัดหมาย', 'วาระ', 'ลิงก์'])
        fetch_project = any(k in q for k in ['โครงการ', 'project', 'กิจกรรม'])
        fetch_unit = any(k in q for k in ['หน่วยงาน', 'ตึก', 'ชั้น', 'ward', 'ติดต่อ', 'เบอร์', 'โทร', 'แผนก'])
        fetch_job = any(k in q for k in ['สมัครงาน', 'รับสมัคร', 'ตำแหน่ง', 'ว่าง', 'งาน'])
        fetch_news = any(k in q for k in ['ข่าว', 'ประกาศ', 'ประชาสัมพันธ์', 'แจ้ง'])

        # 1. อบรม (ดึง Zoom ID/Passcode)
        if fetch_training:
            try:
                sql = f"""SELECT course_name, description, date_start, link_register, link_zoom, zoom_meeting_id, zoom_passcode, process_status, visibility 
                          FROM training_courses WHERE (course_name LIKE %s OR description LIKE %s) {access_filter} ORDER BY date_start ASC LIMIT 5"""
                cursor.execute(sql, (f"%{user_query}%", f"%{user_query}%"))
                for t in cursor.fetchall():
                    zoom = format_zoom(t['link_zoom'], t['zoom_meeting_id'], t['zoom_passcode'])
                    lock = "🔒" if t['visibility'] == 'staff' else "🌍"
                    results_text.append(f"- {lock} อบรม: {t['course_name']} ({t['date_start']}) {t['process_status']} {zoom}")
            except: pass

        # 2. ประชุม (ดึง Zoom ID/Passcode)
        if fetch_meeting:
            try:
                sql = f"""SELECT title, meeting_date, start_time, room, link_zoom, zoom_meeting_id, zoom_passcode, visibility 
                          FROM meeting_schedule WHERE (title LIKE %s OR agenda LIKE %s) {access_filter} ORDER BY meeting_date ASC LIMIT 5"""
                cursor.execute(sql, (f"%{user_query}%", f"%{user_query}%"))
                for m in cursor.fetchall():
                    zoom = format_zoom(m['link_zoom'], m['zoom_meeting_id'], m['zoom_passcode'])
                    lock = "🔒" if m['visibility'] == 'staff' else "🌍"
                    results_text.append(f"- {lock} ประชุม: {m['title']} ({m['meeting_date']} {m['start_time']}) @{m['room']} {zoom}")
            except: pass

        # 3. โครงการ (ดึง Zoom ID/Passcode)
        if fetch_project:
            try:
                sql = f"""SELECT project_name, process_status, link_zoom, zoom_meeting_id, zoom_passcode, visibility 
                          FROM nursing_projects WHERE (project_name LIKE %s) {access_filter} LIMIT 5"""
                cursor.execute(sql, (f"%{user_query}%",))
                for p in cursor.fetchall():
                    zoom = format_zoom(p['link_zoom'], p['zoom_meeting_id'], p['zoom_passcode'])
                    lock = "🔒" if p['visibility'] == 'staff' else "🌍"
                    results_text.append(f"- {lock} โครงการ: {p['project_name']} [{p['process_status']}] {zoom}")
            except: pass
        
        # 4. ข่าวสาร (ดึง Zoom ID/Passcode เผื่อเป็น Webinar)
        if fetch_news:
            try:
                sql = f"""SELECT topic, news_date, link_zoom, zoom_meeting_id, zoom_passcode, visibility 
                          FROM nursing_news WHERE (topic LIKE %s) {access_filter} AND status='active' LIMIT 5"""
                cursor.execute(sql, (f"%{user_query}%",))
                for n in cursor.fetchall():
                    zoom = format_zoom(n['link_zoom'], n['zoom_meeting_id'], n['zoom_passcode'])
                    lock = "🔒" if n['visibility'] == 'staff' else "🌍"
                    results_text.append(f"- {lock} ข่าว: {n['topic']} ({n['news_date']}) {zoom}")
            except: pass

        # 4. หน่วยงาน & 5. สมัครงาน (เหมือนเดิม)
        if fetch_unit:
            try:
                cursor.execute(f"SELECT unit_name, floor, phone_number FROM nursing_units WHERE (unit_name LIKE %s) {access_filter} LIMIT 5", (f"%{user_query}%",))
                for u in cursor.fetchall(): results_text.append(f"- {u['unit_name']} ({u['floor']}) โทร {u['phone_number']}")
            except: pass
            
        if fetch_job:
            try:
                cursor.execute(f"SELECT position_name, date_close FROM job_postings WHERE (position_name LIKE %s) {access_filter} AND status='open' LIMIT 5", (f"%{user_query}%",))
                for j in cursor.fetchall(): results_text.append(f"- งาน: {j['position_name']} (ปิด: {j['date_close']})")
            except: pass

        return "\n".join(results_text) if results_text else ""
    except Exception: return ""
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
    context = f"สถานะผู้ถาม: {role_txt}\nเอกสาร:\n{pinecone_data}\n\nฐานข้อมูล:\n{mysql_data}"
    prompt = f"ตอบคำถามพยาบาล: {context}\nคำถาม: {user_query}\n(ปี 2568 = 2025)"
    
    try:
        return genai.GenerativeModel('models/gemini-flash-latest').generate_content(prompt).text
    except: return "ขออภัย ระบบขัดข้องชั่วคราว"

# --- Notification ---
def check_and_send_notifications():
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT line_user_id, first_name FROM line_users WHERE role = 'staff'")
        staff_users = cursor.fetchall()
        if not staff_users: return

        days_to_check = [1, 3, 5]
        for days in days_to_check:
            target_date = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')
            
            cursor.execute("SELECT course_name FROM training_courses WHERE date_start = %s", (target_date,))
            for train in cursor.fetchall():
                for user in staff_users: 
                    try: line_bot_api.push_message(user['line_user_id'], TextSendMessage(text=f"🔔 แจ้งเตือน: อีก {days} วัน มีงานอบรม '{train['course_name']}' ค่ะ"))
                    except: pass
            
            cursor.execute("SELECT title, start_time, room FROM meeting_schedule WHERE meeting_date = %s", (target_date,))
            for meet in cursor.fetchall():
                for user in staff_users:
                    try: line_bot_api.push_message(user['line_user_id'], TextSendMessage(text=f"🔔 นัดหมายประชุม: '{meet['title']}'\nอีก {days} วัน ({target_date}) เวลา {meet['start_time']}"))
                    except: pass
        conn.close()
    except Exception as e: print(f"Scheduler Error: {e}")

@app.get("/tasks/daily_notify")
def trigger_notification(secret: str = Header(None)):
    if secret != ADMIN_SECRET: raise HTTPException(401, "Unauthorized")
    threading.Thread(target=check_and_send_notifications).start()
    return {"status": "Notification task started"}

# --- Admin API ---
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
def root(): return {"status": "RJ Nurse Backend V16.0 Running"}

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
        user_msg = event.message.text.strip()
        user_id = event.source.user_id
        
        if user_msg.startswith("ลงทะเบียน"):
            try:
                content = user_msg.replace("ลงทะเบียน:", "").replace("ลงทะเบียน", "").strip()
                parts = content.split() 
                if len(parts) < 3:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ รูปแบบผิดครับ\nพิมพ์: ลงทะเบียน ชื่อ นามสกุล รหัสลับ"))
                    return
                if parts[-1] != STAFF_REGISTRATION_CODE:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ รหัสลับไม่ถูกต้อง"))
                    return
                fname = parts[0]
                lname = parts[1]
                dept = " ".join(parts[2:-1]) if len(parts) > 3 else "-"
                if register_staff_profile(user_id, fname, lname, dept):
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ ยืนยันตัวตนสำเร็จ!\nยินดีต้อนรับคุณ {fname} {lname} เข้าสู่ระบบครับ"))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ บันทึกข้อมูลล้มเหลว"))
                return
            except Exception:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="❌ เกิดข้อผิดพลาด"))
                return

        role, user_name = get_user_role(user_id)
        reply_text = generate_bot_response(user_msg, role, user_name)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
