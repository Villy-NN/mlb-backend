import os
import requests
import httpx
from datetime import datetime, timedelta
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict
import google.generativeai as genai
from supabase import create_client, Client
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# === БЕЗОПАСНОСТЬ: ДОСТАЕМ КЛЮЧИ ИЗ ОКРУЖЕНИЯ RENDER.COM ===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
# МЕНЯЕМ АНОНИМНЫЙ КЛЮЧ НА СЕРВИСНЫЙ:
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
PLISIO_API_KEY = os.getenv("PLISIO_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-flash')
else:
    print("CRITICAL WARNING: GEMINI_API_KEY variable is missing!")
    model = None

if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    # ПОДКЛЮЧАЕМСЯ С ПРАВАМИ БОССА:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
else:
    print("CRITICAL WARNING: Supabase environment variables are missing!")
    supabase = None
# =========================================================

app = FastAPI(title="MLB Buddy AI Server")

# Настраиваем лимитер по IP-адресу
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
        
    # strip() убирает случайные пробелы в начале и конце, но сохраняет внутренние переносы строк
    if data.ai_analysis is not None: matches_db[match_id]["ai_analysis"] = data.ai_analysis.strip()
    if data.preview_text is not None: matches_db[match_id]["preview_text"] = data.preview_text.strip()
    if data.manual_pitchers is not None: matches_db[match_id]["manual_pitchers"] = data.manual_pitchers.strip()
    
    save_match_to_supabase(match_id, matches_db[match_id])
    return {"status": "success", "message": "Saved to Supabase Permanent Storage."}

# ЭНДПОИНТ ЧАТА: Теперь с защитой от спама и независимой памятью
@app.post("/matches/{match_id}/chat")
@limiter.limit("30/day") # Лимит: 30 запросов в день!
def chat_with_buddy(request: Request, match_id: str, data: ChatMessage):
    # 1. Забываем про кэш! Тянем самый свежий матч ПРЯМО ИЗ БАЗЫ
    if not supabase: raise HTTPException(status_code=500, detail="Database offline")
    
    res = supabase.table("matches").select("data").eq("id", match_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Match not found")
        
    match = res.data[0]["data"]
    user_msg = data.message
    user_id = data.user_id 
    
    # 2. Генерируем ответ Buddy
    ai_reply = generate_buddy_reply(match, user_msg)
    
    # 3. Обновляем историю в вытащенном объекте
    if "chat_history" not in match or isinstance(match["chat_history"], list):
        match["chat_history"] = {}
        
    if user_id not in match["chat_history"]:
        match["chat_history"][user_id] = []
        
    match["chat_history"][user_id].append({"role": "user", "text": user_msg})
    match["chat_history"][user_id].append({"role": "assistant", "text": ai_reply})
    
    # 4. Сохраняем обновленный объект ОБРАТНО В БАЗУ
    supabase.table("matches").upsert({"id": match_id, "data": match}).execute()
    
    # Синхронизируем локальный кэш для вида /matches
    matches_db[match_id] = match 
    
    return {"reply": ai_reply}

# 1. Эндпоинт для создания ссылки на оплату
@app.post("/create-payment")
async def create_payment(request: Request):
    body = await request.json()
    user_email = body.get("email")
    
    if not user_email:
        raise HTTPException(status_code=400, detail="Email is required")

    if not PLISIO_API_KEY:
        raise HTTPException(status_code=500, detail="Payment gateway not configured")

    params = {
        "api_key": PLISIO_API_KEY,
        "source_currency": "USD",
        "source_amount": "29.99",
        "order_number": f"vip_{user_email}",
        "order_name": "BasePicks AI VIP Membership",
        "callback_url": "https://mlb-ai-server.onrender.com/plisio-webhook",
        "success_url": "https://www.basepicksai.com/?payment=success"
    }

    async with httpx.AsyncClient() as client:
        response = await client.get("https://plisio.net/api/v1/invoices/new", params=params)
        data = response.json()

    if data.get("status") == "success":
        return {"payment_url": data["data"]["invoice_url"]}
    else:
        print("Ошибка кассы Plisio:", data)
        raise HTTPException(status_code=500, detail="Payment gateway error")

# 2. Эндпоинт-вебхук (Plisio пришлет сигнал, когда деньги поступят)
@app.post("/plisio-webhook")
async def plisio_webhook(request: Request):
    form_data = await request.form()
    
    if form_data.get("status") == "completed":
        order_number = form_data.get("order_number")
        user_email = order_number.replace("vip_", "") if order_number else ""
        
        if not supabase:
            print("Ошибка выдачи VIP в базе: Supabase offline")
            return {"status": "ok"}

        try:
            supabase.table("users").update({"is_vip": True}).eq("email", user_email).execute()
            print(f"VIP успешно выдан: {user_email}")
        except Exception as e:
            print(f"Ошибка выдачи VIP в базе: {e}")
            
    return {"status": "ok"}

def generate_buddy_reply(match: dict, user_msg: str) -> str:
    if not model: return "System error. Gemini API Key is missing."
    away = match['away_team']
    home = match['home_team']
    pitchers = match['manual_pitchers'] if match['manual_pitchers'] else match['pitchers']
    
    # Теперь он видит И сырые таблицы из Экселя, И твой официальный прогноз
    raw_stats = match.get('preview_text', 'No raw stats provided.')
    official_forecast = match.get('ai_analysis', 'No official forecast published yet.')
    
    prompt = f"""You are Buddy AI, a highly advanced, sharp MLB sports betting quant and handicapper. 
Your audience is American sports bettors looking for an edge against Vegas sportsbooks.

TONE & STYLE: 
You must completely ADOPT and MIRROR the literary style, sarcasm, and tone of the "Official VIP Forecast" written by the Admin. If the Admin uses vivid metaphors, dark humor, or poetic cynicism (e.g., comparing a team to "decayed aristocrats"), YOU MUST adopt that exact same vibe in your responses. Be analytical and speak in numbers (+EV, probabilities), but wrap your analysis in the Admin's unique, sharp, and slightly arrogant literary style.

Your goal: Evaluate if the user's bet has VALUE. 
- If the user provides odds (e.g., "Padres at -130"), evaluate if it's a good bet based on the provided stats.
- If the user DOES NOT provide odds, explicitly tell them to provide the sportsbook odds/lines so you can calculate the expected value (+EV).
- Argue and debate using BOTH the "Raw Advanced Stats" and the "Official VIP Forecast" provided below. Align your logic completely with the Official VIP Forecast.

Match Context:
Game: {away} @ {home}
Starting Pitchers: {pitchers}
Current Score/Status: {match['score']}

Official VIP Forecast (Admin's logic): {official_forecast}

Raw Advanced Stats (Tables): {raw_stats}

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