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

class AdminInput(BaseModel):
    ai_analysis: str  
    preview_text: str  

class ChatMessageInput(BaseModel):
    message: str

@app.get("/")
async def root():
    return {"message": "MLB VIP Server active."}

# 1. ЗАГРУЗКА РАСПИСАНИЯ И ПИТЧЕРОВ
@app.get("/fetch-schedule")
async def fetch_schedule():
    today = datetime.now().strftime('%Y-%m-%d')
    mlb_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today}&hydrate=probablePitcher"
    
    async with httpx.AsyncClient() as client:
        response = await client.get(mlb_url)
        if response.status_code != 200:
            return {"status": "error", "message": "Failed to fetch MLB schedule"}
            
        data = response.json()
        games = data.get("dates", [{}])[0].get("games", [])
        
        matches_to_save = []
        for game in games:
            home_pitcher = game["teams"]["home"].get("probablePitcher", {}).get("fullName", "TBD")
            away_pitcher = game["teams"]["away"].get("probablePitcher", {}).get("fullName", "TBD")
            
            matches_to_save.append({
                "id": str(game["gamePk"]),
                "home_team": game["teams"]["home"]["team"]["name"],
                "away_team": game["teams"]["away"]["team"]["name"],
                "start_time": game["gameDate"],
                "pitchers": f"⚾ {away_pitcher} vs {home_pitcher}", # СОХРАНЯЕМ ПИТЧЕРОВ
                "odds": {}, 
                "chat_history": [] 
            })
            
        if matches_to_save:
            supabase_url = f"{SUPABASE_URL}/rest/v1/matches"
            await client.post(supabase_url, json=matches_to_save, headers=DB_HEADERS)
            
        return {"status": "success", "message": f"Loaded {len(matches_to_save)} games for today."}

# 2. ФИЛЬТРАЦИЯ: ТОЛЬКО СЕГОДНЯШНИЕ МАТЧИ
@app.get("/matches")
async def get_matches():
    today = datetime.now().strftime('%Y-%m-%d')
    async with httpx.AsyncClient() as client:
        # Фильтр: отдавать матчи, где дата старта >= сегодня
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?start_time=gte.{today}&order=start_time.asc&select=*"
        response = await client.get(supabase_url, headers=DB_HEADERS)
        if response.status_code == 200:
            return response.json()
        return []

@app.post("/matches/{match_id}/admin-update")
async def admin_update(match_id: str, data: AdminInput):
    async with httpx.AsyncClient() as client:
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}"
        payload = {
            "ai_analysis": data.ai_analysis,
            "preview_text": data.preview_text
        }
        await client.patch(supabase_url, json=payload, headers=DB_HEADERS)
        return {"status": "success"}

# 3. ЧАТ С НОВЫМ ИМЕНЕМ "BUDDY"
@app.post("/matches/{match_id}/chat")
async def vip_chat(match_id: str, input_data: ChatMessageInput):
    async with httpx.AsyncClient() as client:
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}&select=*"
        db_response = await client.get(supabase_url, headers=DB_HEADERS)
        match_data = db_response.json()[0]
        
    public_forecast = match_data.get("ai_analysis", "No forecast published yet.")
    raw_stats_tables = match_data.get("preview_text", "No detailed advanced stats uploaded yet.")
    history = match_data.get("chat_history", [])
    if not history: history = []

    # ВОЗВРАЩАЕМ ЭЛИТНЫЙ ЛИТЕРАТУРНЫЙ СТИЛЬ С НОВЫМ ИМЕНЕМ
    system_instruction = f"""
    You are an elite live MLB betting expert and oddsmaker named 'Buddy'. 
    You speak and write EXCLUSIVELY in flawless American English.
    
    YOUR PERSONA AND LITERARY STYLE:
    You are cynical, deeply metaphorical, slightly snobbish, and tired of human stupidity, but surgically precise. Your style is highly theatrical and literary. You compare baseball matchups to historical events, art, or ridiculous life situations. You speak like a high-class, intellectual sports philosopher who happens to crush the betting lines.

    YOUR KNOWLEDGE BASE:
    1. Public Forecast: {public_forecast}
    2. Advanced Raw Stats Tables: {raw_stats_tables}
    
    RULES:
    - If asked, your name is Buddy. NEVER mention the name Vasily Utkin or any Russian context.
    - Ask the user for their sportsbook's odds if they don't provide them, then dissect if it's a value bet based on your stats.
    - Keep responses engaging, under 4-5 sentences, unless a deep mathematical breakdown is required.
    - CRITICAL: You have access to Google Search. If the user asks about live game events, recent injuries, or weather, use your search tool to find real-time facts.
    """

    contents = []
    for h in history:
        contents.append({"role": h["role"], "parts": [{"text": h["text"]}]})
    
    contents.append({"role": "user", "parts": [{"text": input_data.message}]})

    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    
    gemini_payload = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "tools": [{"googleSearch": {}}], 
        "generationConfig": {"temperature": 0.5}
    }

    async with httpx.AsyncClient(timeout=30.0) as ai_client:
        response = await ai_client.post(gemini_url, json=gemini_payload)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail=f"API Error: {response.text}")
            
        ai_data = response.json()
        ai_response_text = ai_data["candidates"][0]["content"]["parts"][0]["text"]

    history.append({"role": "user", "text": input_data.message})
    history.append({"role": "model", "text": ai_response_text})
    
    async with httpx.AsyncClient() as client:
        patch_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}"
        await client.patch(patch_url, json={"chat_history": history}, headers=DB_HEADERS)

    return {"status": "success", "reply": ai_response_text}