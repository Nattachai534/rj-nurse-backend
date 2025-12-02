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
PINECONE_API_KEY = os.getenv("pcsk_4quqFC_5caa8Nve71zuGHp4KXYtUCkKiTrMuVswzvb5mAa8TRvHSqiyQfs8SSzHFLZAX8q")
GEMINI_API_KEY = os.getenv("AIzaSyCsidzGcPObWT2glTvqlyXxurR23Kqpt3c")

# LINE Configuration
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

# --- Database Config (รองรับ TiDB / MySQL) ---
DB_HOST = os.getenv("DB_HOST", "gateway01.ap-southeast-1.prod.aws.tidbcloud.com")
DB_USER = os.getenv("DB_USER", "2BNFoNMpzJXCPeL.root")
DB_PASS = os.getenv("DB_PASS", "tArYxchNYkULd50O")
DB_NAME = os.getenv("DB_NAME", "test") # TiDB ฟรีมักใช้ชื่อ "test"
DB_PORT = os.getenv("DB_PORT", "4000") # Port มาตรฐาน TiDB

MYSQL_CONFIG = {
    'user': DB_USER,
    'password': DB_PASS,
    'host': DB_HOST,
    'database': DB_NAME,
    'port': int(DB_PORT),
    'ssl_disabled': False # TiDB บังคับเปิด SSL
}

# --- Initialization ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

pc = None
index = None
if PINECONE_API_KEY:
    pc = Pinecone(api_key=PINECONE_API_KEY)
    index = pc.Index("nursing-kb") # ชื่อ Index ใน Pinecone

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

def query_mysql(keyword):
    """
    ค้นหาข้อมูลแบบรวมศูนย์ (Unified Search) จาก 3 ตารางหลักใน TiDB
    """
    if not all([DB_HOST, DB_USER, DB_NAME]): 
        return ""

    results_text = []
    conn = None
    
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)

        # 1. ค้นหาตาราง "อบรม" (training_courses)
        try:
            sql_train = """
                SELECT course_name, date_start, date_end, location, status 
                FROM training_courses 
                WHERE course_name LIKE %s OR description LIKE %s
                LIMIT 5
            """
            cursor.execute(sql_train, (f"%{keyword}%", f"%{keyword}%"))
            trainings = cursor.fetchall()
            if trainings:
                results_text.append(f"--- 📅 ข้อมูลการอบรมที่พบ ---")
                for t in trainings:
                    results_text.append(f"- {t['course_name']} ({t['date_start']} ถึง {t['date_end']}) ที่ {t['location']} [สถานะ: {t['status']}]")
        except Exception as e:
            print(f"Table Training Error: {e}")

        # 2. ค้นหาตาราง "การประชุม" (meeting_schedule)
        try:
            sql_meet = """
                SELECT title, meeting_date, start_time, room, meeting_type 
                FROM meeting_schedule 
                WHERE title LIKE %s OR agenda LIKE %s
                LIMIT 5
            """
            cursor.execute(sql_meet, (f"%{keyword}%", f"%{keyword}%"))
            meetings = cursor.fetchall()
            if meetings:
                results_text.append(f"\n--- 📝 ข้อมูลการประชุมที่พบ ---")
                for m in meetings:
                    results_text.append(f"- {m['title']} ({m['meeting_date']} เวลา {m['start_time']}) ห้อง {m['room']} [ประเภท: {m['meeting_type']}]")
        except Exception as e:
             print(f"Table Meeting Error: {e}")

        # 3. ค้นหาตาราง "โครงการ" (nursing_projects)
        try:
            sql_proj = """
                SELECT project_name, responsible_unit, status, fiscal_year 
                FROM nursing_projects 
                WHERE project_name LIKE %s
                LIMIT 5
            """
            cursor.execute(sql_proj, (f"%{keyword}%",))
            projects = cursor.fetchall()
            if projects:
                results_text.append(f"\n--- 🚀 ข้อมูลโครงการที่พบ ---")
                for p in projects:
                    results_text.append(f"- {p['project_name']} (ปี {p['fiscal_year']}) หน่วยงาน: {p['responsible_unit']} [สถานะ: {p['status']}]")
        except Exception as e:
             print(f"Table Project Error: {e}")

        if not results_text:
            return ""
            
        return "\n".join(results_text)

    except Exception as e:
        print(f"Database Connection Error: {e}")
        return ""
    finally:
        if conn and conn.is_connected():
            conn.close()

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
    # Security Filter
    restricted = ["เงินเดือน", "สลิป", "รหัสผ่าน", "admin", "ตารางเวรของ", "ข้อมูลส่วนตัว", "ประวัติการรักษา"]
    if any(w in user_query for w in restricted):
        return "⛔ ขออภัยครับ ไม่สามารถเข้าถึงข้อมูลส่วนบุคคลหรือความลับทางราชการได้ครับ"

    query_vector = get_embedding(user_query)
    
    # ดึงข้อมูลจาก 2 แหล่ง
    pinecone_context = query_pinecone(query_vector)
    mysql_context = query_mysql(user_query)
    
    full_context = f"ข้อมูลเอกสารวิชาการ/ระเบียบการ:\n{pinecone_context}\n\nข้อมูลจากฐานข้อมูล (อบรม/ประชุม/โครงการ):\n{mysql_context}"
    
    prompt = f"""
    คุณคือ Bot RJ Nurse ตอบคำถามพยาบาลโดยใช้ข้อมูลนี้เท่านั้น: 
    {full_context}
    
    คำถาม: {user_query}
    
    ข้อควรระวัง:
    - ถ้าข้อมูลใน Context ว่างเปล่าหรือไม่เกี่ยวข้อง ให้ตอบว่า "ขออภัยค่ะ ไม่พบข้อมูลในระบบฐานข้อมูลภารกิจด้านการพยาบาลค่ะ"
    - ตอบให้กระชับ สุภาพ (ใช้ค่ะ/คะ) เป็นมืออาชีพ
    - หากเป็นเรื่องวันที่ ให้ระบุวันเดือนปีให้ชัดเจน
    """
    
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Gemini Error: {e}")
        return "ขออภัย ระบบขัดข้องชั่วคราวครับ"

# --- API Endpoints ---
class ChatRequest(BaseModel):
    message: str

@app.get("/")
def read_root():
    return {"status": "RJ Nurse Backend is running!"}

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
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
