from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import mysql.connector
from pinecone import Pinecone
import google.generativeai as genai
import os

app = FastAPI()

# --- CORS Setup (อนุญาตให้เว็บเรียกใช้งานได้) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration (ดึงจาก Environment Variables) ---
# เพื่อความปลอดภัย เราจะไม่ใส่ Key ในโค้ดตรงๆ แต่จะไปตั้งค่าใน Render/Cloud Run แทน
PINECONE_API_KEY = os.getenv("pcsk_4quqFC_5caa8Nve71zuGHp4KXYtUCkKiTrMuVswzvb5mAa8TRvHSqiyQfs8SSzHFLZAX8q")
GEMINI_API_KEY = os.getenv("AIzaSyB5_6NUVcxB-NwyKosHVkty69JjgvlwXqU")

# ดึงค่า MySQL Connection
DB_HOST = os.getenv("DB_HOST", "118.27.146.16")
DB_USER = os.getenv("DB_USER", "zzjpszw1_nursing_db")
DB_PASS = os.getenv("DB_PASS", "NattachaiOat@25341799")
DB_NAME = os.getenv("DB_NAME", "zzjpszw1_nursing")

MYSQL_CONFIG = {
    'user': DB_USER,
    'password': DB_PASS,
    'host': DB_HOST,
    'database': DB_NAME
}

# --- Initialization ---
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

pc = None
index = None
if PINECONE_API_KEY:
    pc = Pinecone(api_key=PINECONE_API_KEY)
    # เชื่อมต่อ Index ชื่อ 'nursing-kb' (ต้องสร้างใน Pinecone ก่อน)
    index = pc.Index("nursing-kb")

# --- Helper Functions ---
def get_embedding(text):
    if not GEMINI_API_KEY: return []
    try:
        result = genai.embed_content(
            model="models/embedding-001",
            content=text,
            task_type="retrieval_query"
        )
        return result['embedding']
    except Exception as e:
        print(f"Embedding Error: {e}")
        return []

def query_mysql(keyword):
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)
        # ตัวอย่าง Query ค้นหาตารางอบรม
        sql = "SELECT course_name, date, location, cneu_points FROM training_schedule WHERE course_name LIKE %s"
        cursor.execute(sql, (f"%{keyword}%",))
        results = cursor.fetchall()
        conn.close()
        return str(results) if results else ""
    except Exception as e:
        print(f"MySQL Error: {e}")
        return ""

def query_pinecone(vector):
    if not index or not vector: return ""
    try:
        # ค้นหาเอกสารที่เกี่ยวข้อง
        results = index.query(vector=vector, top_k=3, include_metadata=True, namespace="documents")
        contexts = [m['metadata'].get('text', '') for m in results['matches'] if m['score'] > 0.70]
        return "\n".join(contexts)
    except Exception as e:
        print(f"Pinecone Error: {e}")
        return ""

# --- API Endpoints ---

class ChatRequest(BaseModel):
    message: str

@app.get("/")
def read_root():
    return {"status": "RJ Nurse Backend is running!"}

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    user_query = request.message
    
    # 1. Security Filter: กรองคำถามต้องห้าม
    restricted = ["เงินเดือน", "สลิป", "รหัสผ่าน", "admin", "ตารางเวรของ", "ข้อมูลส่วนตัว"]
    if any(w in user_query for w in restricted):
        return {"reply": "⛔ ขออภัยครับ ไม่สามารถเข้าถึงข้อมูลส่วนบุคคลหรือความลับทางราชการได้ครับ"}

    # 2. Retrieval Process
    query_vector = get_embedding(user_query)
    
    # ดึงข้อมูลจาก Pinecone (เอกสาร)
    pinecone_context = query_pinecone(query_vector)
    
    # ดึงข้อมูลจาก MySQL (ถ้าถามเรื่องอบรม/ตาราง)
    mysql_context = ""
    if any(k in user_query for k in ["อบรม", "ตาราง", "วัน", "หลักสูตร"]):
        mysql_context = query_mysql(user_query)
    
    # 3. Generate Answer with Gemini
    full_context = f"ข้อมูลเอกสารวิชาการ: {pinecone_context}\nข้อมูลตารางอบรม: {mysql_context}"
    
    prompt = f"""
    คุณคือ Bot RJ Nurse ตอบคำถามพยาบาลโดยใช้ข้อมูลนี้เท่านั้น: 
    {full_context}
    
    คำถาม: {user_query}
    
    ข้อควรระวัง:
    - ถ้าไม่พบข้อมูล ให้ตอบว่า "ไม่พบข้อมูลในระบบฐานข้อมูลภารกิจด้านการพยาบาลครับ"
    - ตอบอย่างมืออาชีพ สุภาพ
    """
    
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        return {"reply": response.text}
    except Exception as e:
        print(f"Gemini Error: {e}")
        return {"reply": "ขออภัย ระบบขัดข้องชั่วคราวครับ (AI Error)"}
