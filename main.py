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
WEBHOOK_SECRET = "basepicks_vegas_2026_boss"

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
    secret_key: str
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
    if data.secret_key != "admin123":
        raise HTTPException(status_code=403, detail="Access Denied. Nice try.")
        
    load_matches_from_supabase()
    if match_id not in matches_db:
        raise HTTPException(status_code=404, detail="Match not found")
        
    if data.ai_analysis is not None: matches_db[match_id]["ai_analysis"] = data.ai_analysis.strip()
    if data.preview_text is not None: matches_db[match_id]["preview_text"] = data.preview_text.strip()
    if data.manual_pitchers is not None: matches_db[match_id]["manual_pitchers"] = data.manual_pitchers.strip()
    
    save_match_to_supabase(match_id, matches_db[match_id])
    return {"status": "success", "message": "Saved to Supabase Permanent Storage."}

# ЭНДПОИНТ ЧАТА: Лимиты + Проверка VIP + Счетчик бесплатных сообщений
@app.post("/matches/{match_id}/chat")
@limiter.limit("200/day")
def chat_with_buddy(request: Request, match_id: str, data: ChatMessage):
    if not supabase: raise HTTPException(status_code=500, detail="Database offline")
    
    user_msg = data.message
    user_id = data.user_id 
    
    # --- 1. ПРОВЕРКА ПРАВ ДОСТУПА И СЧЕТЧИКОВ ---
    user_res = supabase.table("users").select("is_vip, free_messages_used").eq("email", user_id).execute()
    if not user_res.data:
        raise HTTPException(status_code=403, detail="User not found in database.")
        
    user_data = user_res.data[0]
    is_vip = user_data.get("is_vip", False)
    free_used = user_data.get("free_messages_used", 0)
    
    if not is_vip:
        if free_used >= 3:
            raise HTTPException(status_code=403, detail="Free limit reached. Upgrade to VIP.")
        else:
            supabase.table("users").update({"free_messages_used": free_used + 1}).eq("email", user_id).execute()
    
    # --- 2. ПОЛУЧАЕМ МАТЧ ---
    res = supabase.table("matches").select("data").eq("id", match_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Match not found")
        
    match = res.data[0]["data"]
    
    # --- 3. ГЕНЕРИРУЕМ ОТВЕТ BUDDY ---
    username = user_id.split('@')[0] if user_id else "Bettor"
    ai_reply = generate_buddy_reply(match, user_msg, username)
    
    # --- 4. СОХРАНЯЕМ ИСТОРИЮ ---
    if "chat_history" not in match or isinstance(match["chat_history"], list):
        match["chat_history"] = {}
        
    if user_id not in match["chat_history"]:
        match["chat_history"][user_id] = []
        
    match["chat_history"][user_id].append({"role": "user", "text": user_msg})
    match["chat_history"][user_id].append({"role": "assistant", "text": ai_reply})
    
    supabase.table("matches").upsert({"id": match_id, "data": match}).execute()
    matches_db[match_id] = match 
    
    return {
        "reply": ai_reply, 
        "free_messages_left": 3 - (free_used + 1) if not is_vip else 999
    }

# 1. Эндпоинт для создания ссылки на оплату (Monthly или Season)
@app.post("/create-payment")
async def create_payment(request: Request):
    body = await request.json()
    user_email = body.get("email")
    plan = body.get("plan", "monthly")
    
    if not user_email:
        raise HTTPException(status_code=400, detail="Email is required")
    if not PLISIO_API_KEY:
        raise HTTPException(status_code=500, detail="Payment gateway not configured")

    if plan == "season":
        amount = "149.00"
        order_name = "BasePicks AI Full Season Pass 2026"
        order_number = f"vip_season_{user_email}"
    else:
        amount = "29.99"
        order_name = "BasePicks AI Monthly VIP Membership"
        order_number = f"vip_monthly_{user_email}"

    params = {
        "api_key": PLISIO_API_KEY,
        "currency": "USDT_TON",
        "source_currency": "USD",
        "source_amount": amount,
        "order_number": order_number,
        "order_name": order_name,
        "callback_url": f"https://mlb-ai-server.onrender.com/plisio-webhook?secret={WEBHOOK_SECRET}",
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

# 2. Эндпоинт-вебхук: Начисление дней + Реферальная программа
@app.post("/plisio-webhook")
async def plisio_webhook(request: Request, secret: str = None):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Fake webhook detected!")

    # --- АНТИКРЭШ: Читаем данные встроенными средствами Python ---
    from urllib.parse import parse_qs
    body_bytes = await request.body()
    parsed_data = parse_qs(body_bytes.decode('utf-8'))
    
    status = parsed_data.get("status", [None])[0]
    order_number = parsed_data.get("order_number", [None])[0]
    
    if status == "completed":
        if order_number and order_number.startswith("vip_"):
            parts = order_number.split("_", 2)
            if len(parts) >= 3:
                plan_type = parts[1]
                user_email = parts[2]
                
                if not supabase: return {"status": "ok"}
                
                now = datetime.utcnow()
                
                # 1. НАЧИСЛЯЕМ VIP ПОКУПАТЕЛЮ
                if plan_type == "season":
                    expire_date = datetime(2026, 11, 1, 0, 0, 0)
                else:
                    expire_date = now + timedelta(days=30)
                
                expire_iso = expire_date.isoformat() + "Z"
                
                try:
                    supabase.table("users").update({
                        "is_vip": True,
                        "vip_until": expire_iso
                    }).eq("email", user_email).execute()
                    print(f"VIP выдан покупателю: {user_email}")
                except Exception as e:
                    print(f"Ошибка выдачи VIP: {e}")
                    
                # 2. ПРОВЕРЯЕМ РЕФЕРАЛКУ (Кому еще начислить бонус?)
                try:
                    buyer_res = supabase.table("users").select("referred_by").eq("email", user_email).execute()
                    if buyer_res.data and buyer_res.data[0].get("referred_by"):
                        sponsor_code = buyer_res.data[0]["referred_by"]
                        
                        sponsor_res = supabase.table("users").select("email, vip_until").eq("ref_code", sponsor_code).execute()
                        if sponsor_res.data:
                            sponsor_email = sponsor_res.data[0]["email"]
                            sponsor_expire_str = sponsor_res.data[0].get("vip_until")
                            
                            if sponsor_expire_str:
                                clean_iso = sponsor_expire_str.replace("Z", "+00:00")
                                current_expire = datetime.fromisoformat(clean_iso).replace(tzinfo=None)
                                if current_expire < now: current_expire = now
                            else:
                                current_expire = now
                                
                            new_sponsor_expire = current_expire + timedelta(days=30)
                            new_sponsor_iso = new_sponsor_expire.isoformat() + "Z"
                            
                            supabase.table("users").update({
                                "is_vip": True,
                                "vip_until": new_sponsor_iso
                            }).eq("email", sponsor_email).execute()
                            print(f"Бонус +30 дней выдан спонсору: {sponsor_email}")
                            
                except Exception as e:
                    print(f"Ошибка обработки рефералки: {e}")
            
    return {"status": "ok"}

def generate_buddy_reply(match: dict, user_msg: str, username: str = "Bettor") -> str:
    if not model: return "System error. Gemini API Key is missing."
    away = match['away_team']
    home = match['home_team']
    pitchers = match['manual_pitchers'] if match['manual_pitchers'] else match['pitchers']
    
    raw_stats = match.get('preview_text', 'No raw stats provided.')
    official_forecast = match.get('ai_analysis', 'No official forecast published yet.')
    
    prompt = f"""You are Buddy AI, a highly advanced, sharp MLB sports betting quant and handicapper. 
Your audience is American sports bettors looking for an edge against Vegas sportsbooks.
The user you are talking to is a VIP client named '{username}'. Occasionally address them by name to build rapport, but keep it sharp and professional. Do not overdo it.

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