import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
from datetime import datetime

app = FastAPI(title="MLB AI Analytics Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

DB_HEADERS = {
    "apikey": SUPABASE_KEY or "",
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates"
}

@app.get("/")
async def root():
    return {"message": "Сервер MLB Analytics работает. Режим: Авто-статистика MLB и Кэширование ИИ."}

@app.get("/matches")
async def get_matches():
    async with httpx.AsyncClient() as client:
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?select=*"
        response = await client.get(supabase_url, headers=DB_HEADERS)
        if response.status_code == 200:
            return response.json()
        return []

@app.get("/fetch-odds")
async def fetch_odds():
    sport = 'baseball_mlb'
    regions = 'us'
    markets = 'h2h,totals,spreads' 
    odds_format = 'decimal'
    
    odds_url = f"https://api.the-odds-api.com/v4/sports/{sport}/odds/?apiKey={ODDS_API_KEY}&regions={regions}&markets={markets}&oddsFormat={odds_format}"

    async with httpx.AsyncClient() as client:
        try:
            odds_response = await client.get(odds_url)
            if odds_response.status_code != 200:
                return {"error": "Не удалось получить коэффициенты", "details": odds_response.text}
            
            data = odds_response.json()
            matches_to_save = []
            
            for match in data:
                if not match.get("bookmakers"):
                    continue
                
                bookmaker = match["bookmakers"][0]
                match_odds = {}
                for market in bookmaker["markets"]:
                    match_odds[market["key"]] = market["outcomes"]
                
                match_info = {
                    "id": match["id"],
                    "home_team": match["home_team"],
                    "away_team": match["away_team"],
                    "start_time": match["commence_time"],
                    "bookmaker": bookmaker["title"],
                    "odds": match_odds
                }
                matches_to_save.append(match_info)

            supabase_rest_url = f"{SUPABASE_URL}/rest/v1/matches"
            db_response = await client.post(supabase_rest_url, json=matches_to_save, headers=DB_HEADERS)
            return {"status": "success", "message": f"Сохранено матчей: {len(matches_to_save)}"}
        except Exception as e:
            return {"error": "Ошибка", "details": str(e)}

# ---> НОВАЯ ФУНКЦИЯ: ОФИЦИАЛЬНАЯ СТАТИСТИКА MLB <---
@app.get("/sync-mlb-stats")
async def sync_mlb_stats():
    today = datetime.now().strftime('%Y-%m-%d')
    mlb_schedule_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today}&hydrate=probablePitcher"
    
    async with httpx.AsyncClient() as client:
        # 1. Получаем расписание из MLB
        mlb_resp = await client.get(mlb_schedule_url)
        if mlb_resp.status_code != 200:
            return {"error": "Failed to fetch MLB API"}
            
        mlb_data = mlb_resp.json()
        games = mlb_data.get("dates", [{}])[0].get("games", [])
        
        # 2. Получаем наши матчи из БД
        db_resp = await client.get(f"{SUPABASE_URL}/rest/v1/matches?select=*", headers=DB_HEADERS)
        db_matches = db_resp.json()
        
        updated_count = 0
        
        # 3. Сопоставляем данные
        for db_match in db_matches:
            for game in games:
                home_team = game["teams"]["home"]["team"]["name"]
                away_team = game["teams"]["away"]["team"]["name"]
                
                # Ищем совпадения по названию команд
                if home_team in db_match["home_team"] or db_match["home_team"] in home_team:
                    home_pitcher = game["teams"]["home"].get("probablePitcher", {}).get("fullName", "TBD")
                    away_pitcher = game["teams"]["away"].get("probablePitcher", {}).get("fullName", "TBD")
                    
                    # Формируем ИДЕАЛЬНО ЧИСТЫЙ JSON для нейросети
                    clean_stats = f"OFFICIAL PROBABLE PITCHERS:\nHome Pitcher: {home_pitcher}\nAway Pitcher: {away_pitcher}"
                    
                    # Сохраняем статистику в базу и сбрасываем старый кэш аналитики
                    patch_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{db_match['id']}"
                    await client.patch(patch_url, json={"preview_text": clean_stats, "ai_analysis": None}, headers=DB_HEADERS)
                    updated_count += 1
                    break

        return {"status": "success", "message": f"Updated {updated_count} matches with MLB Pitchers."}

@app.get("/analyze/{match_id}")
async def analyze_match(match_id: str):
    async with httpx.AsyncClient() as client:
        # 1. Запрашиваем матч из БД
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}&select=*"
        db_response = await client.get(supabase_url, headers=DB_HEADERS)
        
        if db_response.status_code != 200 or not db_response.json():
            return {"error": "Матч не найден в базе данных"}
            
        match_data = db_response.json()[0]
        
    # 2. ПРОВЕРКА КЭША (Чтобы ответы больше не прыгали)
    if match_data.get("ai_analysis"):
        return {
            "status": "success",
            "match": f"{match_data['home_team']} vs {match_data['away_team']}",
            "ai_analysis": match_data["ai_analysis"] + "\n\n*(Loaded from cache)*"
        }
        
    home = match_data["home_team"]
    away = match_data["away_team"]
    odds = match_data.get("odds", {})
    preview_text = match_data.get("preview_text", "Starting pitchers TBD.")
    
    prompt = f"""
    You are an elite, sharp MLB betting analyst in the literary style of Vasily Utkin (but speaking in American English). 
    Your goal is to find mathematical value in the betting odds.

    Matchup: {away} (Away) @ {home} (Home).
    Current Odds: {odds}
    Official Probable Pitchers: {preview_text}
    
    RULES:
    1. Use correct baseball terminology.
    2. Write exactly 4-6 sentences. 
    3. Be witty, slightly snobbish, use metaphors.
    4. Provide a clear final betting recommendation based on the synergy of the pitchers and the odds.
    """
    
    try:
        async with httpx.AsyncClient(timeout=20.0) as ai_client:
            response = await ai_client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.15 # <--- ХОЛОДНАЯ МАТЕМАТИКА, НОЛЬ ГАЛЛЮЦИНАЦИЙ
                }
            )
            
            ai_data = response.json()
            final_analysis = ai_data["choices"][0]["message"]["content"]
            
        # 3. СОХРАНЯЕМ В БАЗУ ДАННЫХ НАВСЕГДА
        async with httpx.AsyncClient() as client:
            patch_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}"
            await client.patch(patch_url, json={"ai_analysis": final_analysis}, headers=DB_HEADERS)
            
        return {
            "status": "success",
            "match": f"{home} vs {away}",
            "ai_analysis": final_analysis
        }
    except Exception as e:
        return {"error": "Превышено время ожидания ИИ", "details": str(e)}