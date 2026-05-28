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
    manual_pitchers: str # НОВОЕ ПОЛЕ ДЛЯ ПИТЧЕРОВ

class ChatMessageInput(BaseModel):
    message: str

@app.get("/")
async def root():
    return {"message": "BaseBet VIP Server active."}

@app.get("/fetch-schedule")
async def fetch_schedule():
    us_time = datetime.utcnow() - timedelta(hours=5)
    today_str = us_time.strftime('%Y-%m-%d')
    yesterday_str = (us_time - timedelta(days=1)).strftime('%Y-%m-%d')
    
    mlb_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={yesterday_str}&endDate={today_str}&hydrate=probablePitcher(stats)"
    
    async with httpx.AsyncClient() as client:
        response = await client.get(mlb_url)
        if response.status_code != 200: return {"status": "error"}
            
        data = response.json()
        for date_obj in data.get("dates", []):
            for game in date_obj.get("games", []):
                match_id = str(game["gamePk"])
                home_team = game["teams"]["home"]["team"]["name"]
                away_team = game["teams"]["away"]["team"]["name"]
                
                home_w = game["teams"]["home"].get("leagueRecord", {}).get("wins", "0")
                home_l = game["teams"]["home"].get("leagueRecord", {}).get("losses", "0")
                away_w = game["teams"]["away"].get("leagueRecord", {}).get("wins", "0")
                away_l = game["teams"]["away"].get("leagueRecord", {}).get("losses", "0")
                
                def get_pitcher_str(team_side):
                    p_obj = game["teams"][team_side].get("probablePitcher", {})
                    p_name = p_obj.get("fullName", "TBD")
                    if p_name == "TBD": return "TBD"
                    try:
                        stats = p_obj.get("stats", [{}])[0].get("splits", [{}])[0].get("stat", {})
                        return f"{p_name} ({stats.get('wins', '?')}-{stats.get('losses', '?')}, {stats.get('era', '-.--')} ERA)"
                    except: return p_name
                
                pitchers_text = f"⚾ {get_pitcher_str('away')} vs {get_pitcher_str('home')}"
                status = game.get("status", {}).get("detailedState", "Scheduled")
                away_score = game["teams"]["away"].get("score", "")
                home_score = game["teams"]["home"].get("score", "")
                
                payload = {
                    "pitchers": pitchers_text,
                    "status": status,
                    "score": f"{away_score} - {home_score}" if str(away_score).isdigit() else "",
                    "away_record": f"{away_w}-{away_l}",
                    "home_record": f"{home_w}-{home_l}"
                }
                
                check_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}"
                check_res = await client.get(check_url, headers=DB_HEADERS)
                
                if check_res.status_code == 200 and len(check_res.json()) > 0:
                    await client.patch(check_url, json=payload, headers=DB_HEADERS)
                else:
                    new_match = {"id": match_id, "home_team": home_team, "away_team": away_team, "start_time": game["gameDate"], "chat_history": [], "is_published": False, **payload}
                    await client.post(f"{SUPABASE_URL}/rest/v1/matches", json=[new_match], headers=DB_HEADERS)
        return {"status": "success"}

# УМНАЯ ВЫДАЧА МАТЧЕЙ
@app.get("/matches")
async def get_matches(boss: str = '0'):
    us_time = datetime.utcnow() - timedelta(hours=5)
    today_str = us_time.strftime('%Y-%m-%d')
    yesterday_str = (us_time - timedelta(days=1)).strftime('%Y-%m-%d')
    
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{SUPABASE_URL}/rest/v1/matches?select=*", headers=DB_HEADERS)
        if response.status_code == 200:
            all_matches = response.json()
            today_matches = []
            yesterday_matches = []
            
            for m in all_matches:
                dt_us = datetime.strptime(m.get("start_time", "").replace("T", " ")[:19], '%Y-%m-%d %H:%M:%S') - timedelta(hours=5)
                m_date = dt_us.strftime('%Y-%m-%d')
                if m_date == today_str: today_matches.append(m)
                elif m_date == yesterday_str: yesterday_matches.append(m)
                    
            today_matches.sort(key=lambda x: x.get("start_time", ""))
            yesterday_matches.sort(key=lambda x: x.get("start_time", ""))
            
            is_today_published = any(m.get("is_published", False) for m in today_matches)

            if boss == '1':
                return yesterday_matches + today_matches
            else:
                if is_today_published:
                    return today_matches
                else:
                    # Если сегодня не опубликовано, отдаем вчера и СТИРАЕМ ПИТЧЕРОВ
                    for y in yesterday_matches:
                        y["pitchers"] = ""
                        y["manual_pitchers"] = ""
                    return yesterday_matches
        return []

@app.post("/matches/{match_id}/admin-update")
async def admin_update(match_id: str, data: AdminInput):
    async with httpx.AsyncClient() as client:
        payload = {"ai_analysis": data.ai_analysis, "preview_text": data.preview_text, "manual_pitchers": data.manual_pitchers}
        await client.patch(f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}", json=payload, headers=DB_HEADERS)
        return {"status": "success"}

# КНОПКА "В ЭФИР" - МАССОВАЯ ПУБЛИКАЦИЯ
@app.post("/publish-board")
async def publish_board():
    us_time = datetime.utcnow() - timedelta(hours=5)
    today_str = us_time.strftime('%Y-%m-%d')
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{SUPABASE_URL}/rest/v1/matches?select=id,start_time", headers=DB_HEADERS)
        if res.status_code == 200:
            for m in res.json():
                try:
                    dt_us = datetime.strptime(m.get("start_time", "").replace("T", " ")[:19], '%Y-%m-%d %H:%M:%S') - timedelta(hours=5)
                    if dt_us.strftime('%Y-%m-%d') == today_str:
                        await client.patch(f"{SUPABASE_URL}/rest/v1/matches?id=eq.{m['id']}", json={"is_published": True}, headers=DB_HEADERS)
                except: pass
    return {"status": "success"}

@app.post("/matches/{match_id}/chat")
async def vip_chat(match_id: str, input_data: ChatMessageInput):
    async with httpx.AsyncClient() as client:
        match_data = (await client.get(f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}&select=*", headers=DB_HEADERS)).json()[0]
        
    public_forecast = match_data.get("ai_analysis", "No forecast published yet.")
    raw_stats_tables = match_data.get("preview_text", "No stats yet.")
    history = match_data.get("chat_history", []) or []

    system_instruction = f"""
    You are an elite live MLB betting expert and oddsmaker named 'Buddy'. 
    You speak and write EXCLUSIVELY in flawless American English.
    YOUR PERSONA: Cynical, highly theatrical, slightly snobbish, surgically precise sports philosopher.
    KNOWLEDGE BASE: 1. Forecast: {public_forecast} 2. Raw Stats: {raw_stats_tables}
    RULES: Keep responses under 4-5 sentences. Use Google Search to check live MLB news if asked. Never mention Vasily Utkin.
    """

    contents = [{"role": h["role"], "parts": [{"text": h["text"]}]} for h in history] + [{"role": "user", "parts": [{"text": input_data.message}]}]
    gemini_payload = {"contents": contents, "systemInstruction": {"parts": [{"text": system_instruction}]}, "tools": [{"googleSearch": {}}], "generationConfig": {"temperature": 0.5}}

    async with httpx.AsyncClient(timeout=30.0) as ai_client:
        response = await ai_client.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}", json=gemini_payload)
        ai_response_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]

    history.extend([{"role": "user", "text": input_data.message}, {"role": "model", "text": ai_response_text}])
    async with httpx.AsyncClient() as client:
        await client.patch(f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}", json={"chat_history": history}, headers=DB_HEADERS)

    return {"status": "success", "reply": ai_response_text}