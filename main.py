from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import mysql.connector
from pinecone import Pinecone
import google.generativeai as genai
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- ส่วนสำคัญ: ดึง Key จาก Environment Variable ---
# ห้ามใส่รหัสตรงนี้เด็ดขาด! ให้ระบบไปดึงมาจาก Render เอง
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")
DB_NAME = os.getenv("DB_NAME")

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
    index = pc.Index("nursing-kb") # ชื่อ Index

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
    # เพิ่ม check เพื่อกัน error หากลืมใส่ค่า DB Config
    if not all([DB_HOST, DB_USER, DB_NAME]): return ""
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True)
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
        results = index.query(vector=vector, top_k=3, include_metadata=True, namespace="documents")
        contexts = [m['metadata'].get('text', '') for m in results['matches'] if m['score'] > 0.70]
        return "\n".join(contexts)
    except Exception as e:
        print(f"Pinecone Error: {e}")
        return ""

class ChatRequest(BaseModel):
    message: str

@app.get("/")
def read_root():
    return {"status": "Secure Backend Running"}

@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    user_query = request.message
    
    restricted = ["เงินเดือน", "สลิป", "รหัสผ่าน", "admin"]
    if any(w in user_query for w in restricted):
        return {"reply": "⛔ ไม่สามารถเข้าถึงข้อมูลส่วนบุคคลได้ครับ"}

    query_vector = get_embedding(user_query)
    pinecone_context = query_pinecone(query_vector)
    
    mysql_context = ""
    if any(k in user_query for k in ["อบรม", "ตาราง", "วัน"]):
        mysql_context = query_mysql(user_query)
    
    full_context = f"เอกสาร: {pinecone_context}\nข้อมูลตาราง: {mysql_context}"
    prompt = f"ตอบคำถามจากข้อมูลนี้: {full_context}\nคำถาม: {user_query}"
    
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        return {"reply": response.text}
    except Exception as e:
        return {"reply": "ระบบขัดข้องชั่วคราว"}


