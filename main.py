import os
import requests
from datetime import datetime, timedelta
import time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict
import google.generativeai as genai
from supabase import create_client, Client

# === БЕЗОПАСНОСТЬ: ДОСТАЕМ КЛЮЧИ ИЗ ОКРУЖЕНИЯ RENDER.COM ===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
else:
    print("CRITICAL WARNING: GEMINI_API_KEY variable is missing!")
    model = None

if SUPABASE_URL and SUPABASE_ANON_KEY:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
else:
    print("CRITICAL WARNING: Supabase environment variables are missing!")
    supabase = None
# =========================================================

app = FastAPI(title="MLB Buddy AI Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

matches_db: Dict[str, dict] = {}
LAST_FETCH_TIME = 0  

# ДОБАВЛЕН USER_ID ДЛЯ ПРИВАТНОСТИ
class ChatMessage(BaseModel):
    message: str
    user_id: str 

class AdminUpdate(BaseModel):
    ai_analysis: Optional[str] = None
    preview_text: Optional[str] = None
    manual_pitchers: Optional[str] = None

@app.get("/config")
def get_config():
    return {
        "supabase_url": SUPABASE_URL if SUPABASE_URL else "",
        "supabase_anon_key": SUPABASE_ANON_KEY if SUPABASE_ANON_KEY else ""
    }

def load_matches_from_supabase():
    global matches_db
    if not supabase: return
    try:
        response = supabase.table("matches").select("id, data").execute()
        for row in response.data:
            game_id = row["id"]
            matches_db[game_id] = row["data"]
    except Exception as e:
        print(f"Error loading from Supabase: {e}")

def save_match_to_supabase(game_id: str, match_data: dict):
    if not supabase: return
    try:
        supabase.table("matches").upsert({"id": game_id, "data": match_data}).execute()
    except Exception as e:
        print(f"Error saving to Supabase: {e}")

def get_mlb_linescore(game_pk: int) -> Optional[dict]:
    try:
        url = f"https://statsapi.mlb.com/api/v1/game/{game_pk}/linescore"
        res = requests.get(url, timeout=5).json()
        
        away_innings = []
        home_innings = []
        
        for inning in res.get("innings", []):
            away_runs = inning.get("away", {}).get("runs")
            home_runs = inning.get("home", {}).get("runs")
            away_innings.append(str(away_runs) if away_runs is not None else "-")
            
            if home_runs is None:
                if away_runs is not None and inning.get("ordinalNum") == "9th":
                    home_innings.append("X")
                else:
                    home_innings.append("-")
            else:
                home_innings.append(str(home_runs))
        
        while len(away_innings) < 9: away_innings.append("-")
        while len(home_innings) < 9: home_innings.append("-")
            
        teams = res.get("teams", {})
        away_totals = {
            "r": str(teams.get("away", {}).get("runs", "-")),
            "h": str(teams.get("away", {}).get("hits", "-")),
            "e": str(teams.get("away", {}).get("errors", "-"))
        }
        home_totals = {
            "r": str(teams.get("home", {}).get("runs", "-")),
            "h": str(teams.get("home", {}).get("hits", "-")),
            "e": str(teams.get("home", {}).get("errors", "-"))
        }
        return {"away_innings": away_innings, "home_innings": home_innings, "away_totals": away_totals, "home_totals": home_totals}
    except Exception as e:
        return None

def sync_mlb_data():
    global LAST_FETCH_TIME
    try:
        load_matches_from_supabase()
        
        # РЕШЕНИЕ ПРОБЛЕМЫ 1: СМЕЩАЕМ ВРЕМЯ НА АМЕРИКАНСКОЕ (-8 часов от Лондона)
        us_date = (datetime.utcnow() - timedelta(hours=8)).strftime('%Y-%m-%d')
        url = f"https://statsapi.mlb.com/api/v1/schedule/games/?sportId=1&date={us_date}"
        response = requests.get(url, timeout=5).json()
        
        dates = response.get("dates", [])
        if not dates:
            return 0
            
        games = dates[0].get("games", [])
        
        # ЖЕСТКАЯ ОЧИСТКА: Удаляем вчерашние игры из памяти и базы
        active_ids = {str(g.get("gamePk")) for g in games}
        keys_to_remove = [k for k in list(matches_db.keys()) if k not in active_ids]
        for k in keys_to_remove:
            del matches_db[k]
            if supabase:
                try: supabase.table("matches").delete().eq("id", k).execute()
                except: pass
            
        for game in games:
            game_id = str(game.get("gamePk"))
            status_info = game.get("status", {})
            abstract_status = status_info.get("abstractGameState", "Preview")
            detailed_status = status_info.get("detailedState", "")
            
            teams = game.get("teams", {})
            away_team = teams.get("away", {}).get("team", {}).get("name", "Away Team")
            home_team = teams.get("home", {}).get("team", {}).get("name", "Home Team")
            
            away_record = f"{teams.get('away', {}).get('leagueRecord', {}).get('wins', 0)}-{teams.get('away', {}).get('leagueRecord', {}).get('losses', 0)}"
            home_record = f"{teams.get('home', {}).get('leagueRecord', {}).get('wins', 0)}-{teams.get('home', {}).get('leagueRecord', {}).get('losses', 0)}"
            
            if abstract_status != "Preview":
                away_score = teams.get("away", {}).get("score", 0)
                home_score = teams.get("home", {}).get("score", 0)
                score_str = f"{away_score} - {home_score}"
            else:
                score_str = "@"
            
            away_pitcher = game.get("teams", {}).get("away", {}).get("probablePitcher", {}).get("name", "")
            home_pitcher = game.get("teams", {}).get("home", {}).get("probablePitcher", {}).get("name", "")
            pitchers_str = f"{away_pitcher} vs {home_pitcher}" if away_pitcher or home_pitcher else ""
            
            linescore_data = get_mlb_linescore(game.get("gamePk")) if abstract_status != "Preview" else None
            existing_game = matches_db.get(game_id, {})
            
            # АДАПТАЦИЯ ИСТОРИИ ЧАТА В СЛОВАРЬ
            existing_chat = existing_game.get("chat_history", {})
            if isinstance(existing_chat, list): existing_chat = {}
            
            match_object = {
                "id": game_id,
                "away_team": away_team,
                "home_team": home_team,
                "away_record": away_record,
                "home_record": home_record,
                "status": detailed_status if detailed_status else abstract_status,
                "score": score_str,
                "pitchers": pitchers_str,
                "manual_pitchers": existing_game.get("manual_pitchers", ""),
                "ai_analysis": existing_game.get("ai_analysis", ""),
                "preview_text": existing_game.get("preview_text", ""), 
                "is_published": existing_game.get("is_published", False),
                "chat_history": existing_chat, 
                "linescore": linescore_data,
                "game_datetime": game.get("gameDate") # ДОСТАЕМ ТОЧНОЕ ВРЕМЯ МАТЧА ИЗ MLB API
            }
            
            matches_db[game_id] = match_object
            save_match_to_supabase(game_id, match_object)
            
        LAST_FETCH_TIME = time.time()
        return len(games)
    except Exception as e:
        print(f"Error in sync_mlb_data: {e}")
        return 0

@app.get("/fetch-schedule")
def fetch_schedule():
    sync_mlb_data()
    return {"status": "success", "message": "Database synchronized with today's games."}

@app.get("/matches")
def get_matches(boss: int = 0):
    global LAST_FETCH_TIME
    if time.time() - LAST_FETCH_TIME > 60:
        sync_mlb_data()
    else:
        load_matches_from_supabase()
        
    matches_list = list(matches_db.values())
    matches_list.sort(key=lambda x: x.get("id", ""))
    
    public_matches = []
    for m in matches_list:
        m_copy = m.copy()
        # РЕШЕНИЕ ПРОБЛЕМЫ 2: ВЫРЕЗАЕМ ИСТОРИЮ ЧАТА ИЗ ОБЩЕГО ДОСТУПА
        m_copy["chat_history"] = {} 
        if boss != 1 and not m_copy.get("is_published"):
            m_copy["ai_analysis"] = ""
        public_matches.append(m_copy)
    return public_matches

# НОВЫЙ ЭНДПОИНТ: Выдает чат только для конкретного юзера
@app.get("/matches/{match_id}/chat/{user_id}")
def get_user_chat(match_id: str, user_id: str):
    if match_id not in matches_db: return []
    chat_dict = matches_db[match_id].get("chat_history", {})
    if isinstance(chat_dict, list): return []
    return chat_dict.get(user_id, [])

@app.post("/publish-board")
def publish_board():
    load_matches_from_supabase()
    for game_id in matches_db:
        matches_db[game_id]["is_published"] = True
        save_match_to_supabase(game_id, matches_db[game_id])
    return {"status": "success", "message": "The premium board is now LIVE."}

@app.post("/matches/{match_id}/admin-update")
def admin_update(match_id: str, data: AdminUpdate):
    load_matches_from_supabase()
    if match_id not in matches_db:
        raise HTTPException(status_code=404, detail="Match not found")
        
    if data.ai_analysis is not None: matches_db[match_id]["ai_analysis"] = data.ai_analysis
    if data.preview_text is not None: matches_db[match_id]["preview_text"] = data.preview_text
    if data.manual_pitchers is not None: matches_db[match_id]["manual_pitchers"] = data.manual_pitchers
    
    save_match_to_supabase(match_id, matches_db[match_id])
    return {"status": "success", "message": "Saved to Supabase Permanent Storage."}

@app.post("/matches/{match_id}/chat")
def chat_with_buddy(match_id: str, data: ChatMessage):
    load_matches_from_supabase()
    if match_id not in matches_db:
        raise HTTPException(status_code=404, detail="Match not found")
        
    match = matches_db[match_id]
    user_msg = data.message
    user_id = data.user_id # Привязываем к email юзера
    
    ai_reply = generate_buddy_reply(match, user_msg)
    
    if "chat_history" not in match or isinstance(match["chat_history"], list):
        match["chat_history"] = {}
        
    if user_id not in match["chat_history"]:
        match["chat_history"][user_id] = []
        
    match["chat_history"][user_id].append({"role": "user", "text": user_msg})
    match["chat_history"][user_id].append({"role": "assistant", "text": ai_reply})
    
    save_match_to_supabase(match_id, match)
    return {"reply": ai_reply}

def generate_buddy_reply(match: dict, user_msg: str) -> str:
    if not model: return "System error. Gemini API Key is missing."
    away = match['away_team']
    home = match['home_team']
    pitchers = match['manual_pitchers'] if match['manual_pitchers'] else match['pitchers']
    raw_stats = match.get('preview_text', 'No advanced stats provided by admin yet.')
    
    prompt = f"""You are Buddy AI, a highly advanced, sharp MLB sports betting quant and handicapper. 
Your audience is American sports bettors looking for an edge against Vegas sportsbooks.
Your tone is direct, analytical, professional, and slightly cynical about public betting squares. 
NO flowery metaphors. NO poetic language. NO bullshit. Speak strictly in numbers, probabilities, expected value (+EV), and betting odds.

Your goal: Evaluate if the user's bet has VALUE. 
- If the user provides odds (e.g., "Padres at -130"), evaluate if it's a good bet based on the provided stats.
- If the user DOES NOT provide odds (e.g., "San Diego win?"), explicitly tell them to provide the sportsbook odds/lines so you can calculate the expected value (+EV). Explain that you don't guess winners, you play the numbers.
- Argue and debate using the "Raw Advanced Stats" provided below.

Match Context:
Game: {away} @ {home}
Starting Pitchers: {pitchers}
Current Score/Status: {match['score']}
Raw Advanced Stats (Use these to argue your point): {raw_stats}

User Input: {user_msg}

Respond concisely (3-5 sentences). Be sharp and helpful. If the user asks in Russian, answer in Russian (but keep the American sharp bettor vibe). If they ask in English, answer in English."""

    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"System error. Connection to Vegas servers lost. (Error: {e})"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)