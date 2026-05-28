import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel
from datetime import datetime, timedelta

app = FastAPI(title="BaseBet VIP AI Backend")

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
    "Content-Type": "application/json"
}

class AdminInput(BaseModel):
    ai_analysis: str  
    preview_text: str  

class ChatMessageInput(BaseModel):
    message: str

@app.get("/")
async def root():
    return {"message": "BaseBet VIP Server active."}

# 1. ЗАГРУЗКА РАСПИСАНИЯ, СЧЕТА И СТАТИСТИКИ 
@app.get("/fetch-schedule")
async def fetch_schedule():
    us_time = datetime.utcnow() - timedelta(hours=5)
    today_str = us_time.strftime('%Y-%m-%d')
    yesterday_str = (us_time - timedelta(days=1)).strftime('%Y-%m-%d')
    
    # Запрашиваем 2 дня и гидратируем стату питчеров (stats)
    mlb_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={yesterday_str}&endDate={today_str}&hydrate=probablePitcher(stats)"
    
    async with httpx.AsyncClient() as client:
        response = await client.get(mlb_url)
        if response.status_code != 200:
            return {"status": "error", "message": "Failed to fetch MLB schedule"}
            
        data = response.json()
        dates = data.get("dates", [])
        
        for date_obj in dates:
            games_list = date_obj.get("games", [])
            for game in games_list:
                match_id = str(game["gamePk"])
                home_team = game["teams"]["home"]["team"]["name"]
                away_team = game["teams"]["away"]["team"]["name"]
                start_time = game["gameDate"]
                
                # --- КОМАНДНАЯ СТАТИСТИКА (W-L) ---
                home_w = game["teams"]["home"].get("leagueRecord", {}).get("wins", "0")
                home_l = game["teams"]["home"].get("leagueRecord", {}).get("losses", "0")
                away_w = game["teams"]["away"].get("leagueRecord", {}).get("wins", "0")
                away_l = game["teams"]["away"].get("leagueRecord", {}).get("losses", "0")
                
                away_record = f"{away_w}-{away_l}"
                home_record = f"{home_w}-{home_l}"
                
                # --- ЛИЧНАЯ СТАТИСТИКА ПИТЧЕРОВ (W-L, ERA) ---
                def get_pitcher_str(team_side):
                    p_obj = game["teams"][team_side].get("probablePitcher", {})
                    p_name = p_obj.get("fullName", "TBD")
                    if p_name == "TBD": return "TBD"
                    try:
                        # MLB API прячет стату глубоко
                        stats = p_obj.get("stats", [{}])[0].get("splits", [{}])[0].get("stat", {})
                        w = stats.get("wins", "?")
                        l = stats.get("losses", "?")
                        era = stats.get("era", "-.--")
                        return f"{p_name} ({w}-{l}, {era} ERA)"
                    except:
                        return p_name # Если статы нет, возвращаем просто имя
                
                away_pitcher_str = get_pitcher_str("away")
                home_pitcher_str = get_pitcher_str("home")
                pitchers_text = f"⚾ {away_pitcher_str} vs {home_pitcher_str}"
                
                # Достаем Статус и Счет
                status = game.get("status", {}).get("detailedState", "Scheduled")
                away_score = game["teams"]["away"].get("score", "")
                home_score = game["teams"]["home"].get("score", "")
                score_text = f"{away_score} - {home_score}" if str(away_score).isdigit() else ""
                
                check_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}"
                check_res = await client.get(check_url, headers=DB_HEADERS)
                
                payload = {
                    "pitchers": pitchers_text,
                    "status": status,
                    "score": score_text,
                    "away_record": away_record,
                    "home_record": home_record
                }
                
                if check_res.status_code == 200 and len(check_res.json()) > 0:
                    patch_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}"
                    await client.patch(patch_url, json=payload, headers=DB_HEADERS)
                else:
                    new_match = {
                        "id": match_id,
                        "home_team": home_team,
                        "away_team": away_team,
                        "start_time": start_time,
                        "chat_history": [],
                        **payload
                    }
                    post_url = f"{SUPABASE_URL}/rest/v1/matches"
                    await client.post(post_url, json=[new_match], headers=DB_HEADERS)
                
        return {"status": "success"}

# 2. ВЫДАЧА МАТЧЕЙ (ВЧЕРА + СЕГОДНЯ)
@app.get("/matches")
async def get_matches():
    us_time = datetime.utcnow() - timedelta(hours=5)
    today_str = us_time.strftime('%Y-%m-%d')
    yesterday_str = (us_time - timedelta(days=1)).strftime('%Y-%m-%d')
    
    async with httpx.AsyncClient() as client:
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?select=*"
        response = await client.get(supabase_url, headers=DB_HEADERS)
        
        if response.status_code == 200:
            all_matches = response.json()
            today_matches = []
            yesterday_matches = []
            
            for m in all_matches:
                st = m.get("start_time", "").replace("T", " ")[:19]
                try:
                    dt_utc = datetime.strptime(st, '%Y-%m-%d %H:%M:%S')
                    dt_us = dt_utc - timedelta(hours=5)
                    match_date = dt_us.strftime('%Y-%m-%d')
                    
                    if match_date == today_str:
                        today_matches.append(m)
                    elif match_date == yesterday_str:
                        yesterday_matches.append(m)
                except:
                    pass
                    
            today_matches.sort(key=lambda x: x.get("start_time", ""))
            yesterday_matches.sort(key=lambda x: x.get("start_time", ""))
            
            # Возвращаем ВСЕ матчи: сначала вчерашние (результаты), потом сегодняшние!
            return yesterday_matches + today_matches
            
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