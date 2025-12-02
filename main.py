from fastapi import FastAPI, Request, HTTPException
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

# LINE Configuration
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

# --- Database Config ---
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
def get_embedding(text):
    if not GEMINI_API_KEY: return []
    try:
        result = genai.embed_content(
            model="models/text-embedding-004",
            content=text,
            task_type="retrieval_query"
        )
        return result['embedding']
    except Exception as e:
        print(f"Embedding Error: {e}")
        return []

def query_mysql(user_query):
    """
    [Updated] ค้นหาข้อมูลแบบ Smart Retrieval
    แทนที่จะค้นหาด้วย WHERE LIKE '%ประโยคยาวๆ%' ซึ่งมักจะไม่เจอ
    เราจะดึงรายการ 'ทั้งหมด' (ล่าสุด) ออกมาให้ Gemini เป็นคนคัดกรองเอง
    """
    if not all([DB_HOST, DB_USER, DB_NAME]): return ""

    results_text = []
    conn = None
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)
        
        # ตรวจจับ keywords เพื่อตัดสินใจว่าจะดึงตารางไหนบ้าง
        q = user_query.lower()
        fetch_training = any(x in q for x in ['อบรม', 'หลักสูตร', 'เรียน', 'cneu', 'ตาราง', 'ปี', 'schedule', '2568', '68'])
        fetch_meeting = any(x in q for x in ['ประชุม', 'นัด', 'วาระ', 'ตาราง', 'ปี', '2568', '68'])
        fetch_project = any(x in q for x in ['โครงการ', 'project', 'กิจกรรม', 'งาน', 'ปี', '2568', '68'])

        # 1. ตาราง "อบรม" (ดึง 20 รายการ เพื่อให้ครอบคลุมทั้งปี)
        if fetch_training:
            try:
                # ดึงข้อมูลมาเลยไม่ต้อง WHERE LIKE ชื่อ
                cursor.execute("SELECT course_name, date_start, location, cneu_points, status FROM training_courses ORDER BY date_start ASC LIMIT 20")
                trainings = cursor.fetchall()
                if trainings:
                    results_text.append(f"--- 📅 ตารางการอบรม (ทั้งหมด) ---")
                    for t in trainings:
                        results_text.append(f"- {t['course_name']} (วันที่: {t['date_start']}) สถานที่: {t['location']} [CNEU: {t['cneu_points']}]")
            except Exception as e:
                print(f"Table Training Error: {e}")

        # 2. ตาราง "การประชุม"
        if fetch_meeting:
            try:
                cursor.execute("SELECT title, meeting_date, start_time, room FROM meeting_schedule ORDER BY meeting_date ASC LIMIT 15")
                meetings = cursor.fetchall()
                if meetings:
                    results_text.append(f"\n--- 📝 ตารางการประชุม ---")
                    for m in meetings:
                        results_text.append(f"- {m['title']} ({m['meeting_date']} เวลา {m['start_time']}) ห้อง: {m['room']}")
            except Exception as e:
                 print(f"Table Meeting Error: {e}")

        # 3. ตาราง "โครงการ"
        if fetch_project:
            try:
                cursor.execute("SELECT project_name, responsible_unit, status FROM nursing_projects ORDER BY id DESC LIMIT 15")
                projects = cursor.fetchall()
                if projects:
                    results_text.append(f"\n--- 🚀 โครงการต่างๆ ---")
                    for p in projects:
                        results_text.append(f"- {p['project_name']} ({p['responsible_unit']}) สถานะ: {p['status']}")
            except Exception as e:
                 print(f"Table Project Error: {e}")

        if not results_text:
            return ""
            
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
        contexts = [m['metadata'].get('text', '') for m in results['matches'] if m['score'] > 0.60]
        return "\n".join(contexts)
    except Exception as e:
        print(f"Pinecone Error: {e}")
        return ""

# --- Core Logic ---
def generate_bot_response(user_query):
    restricted = ["เงินเดือน", "สลิป", "รหัสผ่าน", "admin", "ตารางเวรของ", "ข้อมูลส่วนตัว"]
    if any(w in user_query for w in restricted):
        return "⛔ ขออภัยครับ ไม่สามารถเข้าถึงข้อมูลส่วนบุคคลได้ครับ"

    query_vector = get_embedding(user_query)
    pinecone_context = query_pinecone(query_vector)
    mysql_context = query_mysql(user_query)
    
    full_context = f"เอกสาร: {pinecone_context}\nฐานข้อมูล: {mysql_context}"
    
    # เพิ่มคำสั่งให้ AI เข้าใจปี พ.ศ.
    prompt = f"""
    คุณคือ Bot RJ Nurse ตอบคำถามโดยใช้ข้อมูลนี้: 
    {full_context}
    
    คำถาม: {user_query}
    
    ข้อควรระวัง:
    - ปี ค.ศ. 2025 ตรงกับ ปี พ.ศ. 2568 (ถ้าผู้ใช้ถามปี 68 ให้ตอบข้อมูลปี 2025)
    - ตอบเป็นภาษาไทย สุภาพ (ใช้ค่ะ/คะ)
    - ถ้าข้อมูลมีเยอะ ให้สรุปเป็นรายการ
    """
    
    models_to_try = [
        'models/gemini-2.5-flash',
        'models/gemini-2.0-flash',
        'models/gemini-flash-latest'
    ]
    
    last_error_msg = ""
    
    for model_name in models_to_try:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            last_error_msg = str(e)
            continue 
            
    return f"⚠️ ระบบขัดข้อง (Debug Info): {last_error_msg}. ลองเข้า /debug/models เพื่อเช็คชื่อโมเดล"

# --- API Endpoints ---
class ChatRequest(BaseModel):
    message: str

@app.get("/")
def read_root():
    return {"status": "RJ Nurse Backend is running!"}

@app.get("/debug/models")
def list_available_models():
    if not GEMINI_API_KEY: return {"error": "No API Key set"}
    try:
        models = []
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                models.append(m.name)
        return {"available_models": models}
    except Exception as e:
        return {"error": str(e)}

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    reply = generate_bot_response(request.message)
    return {"reply": reply}

@app.post("/callback")
async def callback(request: Request):
    if not handler:
        raise HTTPException(status_code=500, detail="LINE config not set")
    
    signature = request.headers['X-Line-Signature']
    body = await request.body()
    body_text = body.decode('utf-8')

    try:
        handler.handle(body_text, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    return 'OK'

if handler:
    @handler.add(MessageEvent, message=TextMessage)
    def handle_message(event):
        user_msg = event.message.text
        reply_text = generate_bot_response(user_msg)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
