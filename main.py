import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel
from datetime import datetime

app = FastAPI(title="MLB VIP AI Analytics Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

DB_HEADERS = {
    "apikey": SUPABASE_KEY or "",
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates"
}

# Модели для приема данных от тебя (Админка)
class AdminInput(BaseModel):
    ai_analysis: str  # Публичный прогноз Уткина из домашнего Gemini
    preview_text: str  # Скрытая сырая статистика для VIP-чата (Ctrl+C с B-R)

# Модель для приема сообщений от VIP-пользователя в чат
class ChatMessageInput(BaseModel):
    message: str

@app.get("/")
async def root():
    return {"message": "MLB VIP Server active. Gemini Search Grounding deployed."}

# 1. ПОЛУЧЕНИЕ МАТЧЕЙ (Чистое расписание из MLB API, без букмекеров)
@app.get("/fetch-schedule")
async def fetch_schedule():
    today = datetime.now().strftime('%Y-%m-%d')
    mlb_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today}"
    
    async with httpx.AsyncClient() as client:
        response = await client.get(mlb_url)
        if response.status_code != 200:
            return {"status": "error", "message": "Failed to fetch MLB schedule"}
            
        data = response.json()
        games = data.get("dates", [{}])[0].get("games", [])
        
        matches_to_save = []
        for game in games:
            matches_to_save.append({
                "id": str(game["gamePk"]),
                "home_team": game["teams"]["home"]["team"]["name"],
                "away_team": game["teams"]["away"]["team"]["name"],
                "start_time": game["gameDate"],
                "odds": {}, # Больше не используем API букмекеров
                "chat_history": [] # Чистая история чата
            })
            
        if matches_to_save:
            supabase_url = f"{SUPABASE_URL}/rest/v1/matches"
            await client.post(supabase_url, json=matches_to_save, headers=DB_HEADERS)
            
        return {"status": "success", "message": f"Loaded {len(matches_to_save)} games for today."}

@app.get("/matches")
async def get_matches():
    async with httpx.AsyncClient() as client:
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?select=*"
        response = await client.get(supabase_url, headers=DB_HEADERS)
        if response.status_code == 200:
            return response.json()
        return []

# 2. АДМИНКА: Загрузка твоих ручных прогнозов и таблиц в базу
@app.post("/matches/{match_id}/admin-update")
async def admin_update(match_id: str, data: AdminInput):
    async with httpx.AsyncClient() as client:
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}"
        payload = {
            "ai_analysis": data.ai_analysis,
            "preview_text": data.preview_text
        }
        response = await client.patch(supabase_url, json=payload, headers=DB_HEADERS)
        if response.status_code in [200, 204]:
            return {"status": "success", "message": "Match analysis and VIP database updated."}
        return {"status": "error", "details": response.text}

# 3. СВЕРХУМНЫЙ VIP-ЧАТ С GOOGLE GEMINI + ИНТЕРНЕТ ПОИСК
@app.post("/matches/{match_id}/chat")
async def vip_chat(match_id: str, input_data: ChatMessageInput):
    async with httpx.AsyncClient() as client:
        # Достаем матч, прогноз и скрытые таблицы из БД
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}&select=*"
        db_response = await client.get(supabase_url, headers=DB_HEADERS)
        
        if db_response.status_code != 200 or not db_response.json():
            raise HTTPException(status_code=404, detail="Match not found")
            
        match_data = db_response.json()[0]
        
    # Извлекаем контекст, который ты подготовил
    public_forecast = match_data.get("ai_analysis", "No forecast published yet.")
    raw_stats_tables = match_data.get("preview_text", "No detailed advanced stats uploaded yet.")
    history = match_data.get("chat_history", [])
    if not history: history = []

    # Конструируем системные инструкции (Промпт)
    system_instruction = f"""
    You are an elite live MLB betting expert in the sharp, cynical, and metaphorical literary style of Vasily Utkin, speaking in American English.
    You are inside a premium, paid live chat room talking to a VIP client.
    
    YOUR KNOWLEDGE BASE FOR THIS GAME:
    1. Public Forecast you already wrote: {public_forecast}
    2. Advanced Raw Stats Tables (from Baseball-Reference): {raw_stats_tables}
    
    YOUR CHALLENGE & ROLES:
    - The user might give you live betting odds from their local sportsbook. Compare them against our Fair Line and tell them if it's a value bet or a scam.
    - The user can argue with you, share their thoughts, or ask deep questions about pitchers, counts, or bullpen availability. Maintain a live, intellectual, slightly snobbish, but deeply analytical conversation.
    - Use correct baseball terminology. Keep your responses engaging, under 4-5 sentences, unless a deep mathematical breakdown of odds is required.
    - CRITICAL: You have access to Google Search. If the user asks about live game events, recent injuries (e.g., player incidents, sudden lineups changes), or weather updates, use your search tool to find real-time facts from the last 48 hours.
    """

    # Собираем историю сообщений для формата Gemini API
    contents = []
    # Добавляем прошлую историю (если она есть)
    for h in history:
        contents.append({"role": h["role"], "parts": [{"text": h["text"]}]})
    
    # Добавляем текущее сообщение пользователя
    contents.append({"role": "user", "parts": [{"text": input_data.message}]})

    # Ссылка на официальный эндпоинт Google Gemini 1.5 Pro
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    # Конфигурация запроса с включенным ИНТЕРНЕТ ПОИСКОМ (Google Search Grounding)
    gemini_payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "tools": [{"googleSearch": {}}], # <--- ВКЛЮЧИЛИ ЖИВОЙ ГУГЛ-ПОИСК СЕРВЕРА
        "generationConfig": {
            "temperature": 0.5
        }
    }

    async with httpx.AsyncClient(timeout=30.0) as ai_client:
        response = await ai_client.post(gemini_url, json=gemini_payload)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Gemini API Error: {response.text}")
            
        ai_data = response.json()
        
        try:
            ai_response_text = ai_data["candidates"][0]["content"]["parts"][0]["text"]
        except KeyError:
            ai_response_text = "I am processing the data, but the stadium security is blocking my view. Let's look at the numbers again."

    # Обновляем историю переписки в нашей базе данных
    history.append({"role": "user", "text": input_data.message})
    history.append({"role": "model", "text": ai_response_text})
    
    async with httpx.AsyncClient() as client:
        patch_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}"
        await client.patch(patch_url, json={"chat_history": history}, headers=DB_HEADERS)

    return {
        "status": "success",
        "reply": ai_response_text
    }