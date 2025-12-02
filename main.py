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
        # ลองใช้ Model embedding ตัวใหม่
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
    if not all([DB_HOST, DB_USER, DB_NAME]): return ""
    results_text = []
    conn = None
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)

        try:
            sql_train = "SELECT course_name, date_start, location, status FROM training_courses WHERE course_name LIKE %s OR description LIKE %s LIMIT 3"
            cursor.execute(sql_train, (f"%{keyword}%", f"%{keyword}%"))
            for t in cursor.fetchall():
                results_text.append(f"- อบรม: {t['course_name']} ({t['date_start']}) {t['location']}")
        except Exception: pass

        try:
            sql_meet = "SELECT title, meeting_date, room FROM meeting_schedule WHERE title LIKE %s OR agenda LIKE %s LIMIT 3"
            cursor.execute(sql_meet, (f"%{keyword}%", f"%{keyword}%"))
            for m in cursor.fetchall():
                results_text.append(f"- ประชุม: {m['title']} ({m['meeting_date']}) ห้อง {m['room']}")
        except Exception: pass

        try:
            sql_proj = "SELECT project_name, status FROM nursing_projects WHERE project_name LIKE %s LIMIT 3"
            cursor.execute(sql_proj, (f"%{keyword}%",))
            for p in cursor.fetchall():
                results_text.append(f"- โครงการ: {p['project_name']} ({p['status']})")
        except Exception: pass

        if not results_text: return ""
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

# --- Core Logic with Model Fallback ---
def generate_bot_response(user_query):
    restricted = ["เงินเดือน", "สลิป", "รหัสผ่าน", "admin", "ตารางเวรของ", "ข้อมูลส่วนตัว"]
    if any(w in user_query for w in restricted):
        return "⛔ ขออภัยครับ ไม่สามารถเข้าถึงข้อมูลส่วนบุคคลได้ครับ"

    query_vector = get_embedding(user_query)
    pinecone_context = query_pinecone(query_vector)
    mysql_context = query_mysql(user_query)
    
    full_context = f"เอกสาร: {pinecone_context}\nฐานข้อมูล: {mysql_context}"
    
    prompt = f"ตอบคำถามพยาบาลสั้นๆ จากข้อมูลนี้: {full_context}\nคำถาม: {user_query}"
    
    # 🌟 จุดแก้ปัญหา 404: ระบบ Retry Model 🌟
    # ลองใช้รุ่น Flash ล่าสุดก่อน -> ถ้าไม่ได้ให้ใช้ Flash 001 -> ถ้าไม่ได้ให้ใช้ Pro 1.0
    models_to_try = ['gemini-1.5-flash-latest', 'gemini-1.5-flash-001', 'gemini-1.5-flash', 'gemini-pro']
    
    for model_name in models_to_try:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(prompt)
            return response.text
        except Exception as e:
            print(f"Model {model_name} failed: {e}")
            continue # ลองรุ่นถัดไป
            
    return "ขออภัย ระบบ AI ขัดข้องชั่วคราว (Model Not Found)"

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
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
