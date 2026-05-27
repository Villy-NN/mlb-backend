import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel # <--- ДОБАВИЛИ ДЛЯ ПРИЕМА ТЕКСТА

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

# Модель для приема текста превью
class PreviewInput(BaseModel):
    text: str

@app.get("/")
async def root():
    return {"message": "Сервер MLB Analytics работает. Режим: Ручной ввод статистики."}

@app.get("/matches")
async def get_matches():
    async with httpx.AsyncClient() as client:
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?select=*"
        response = await client.get(supabase_url, headers=DB_HEADERS)
        if response.status_code == 200:
            return response.json()
        return []

# ---> НОВЫЙ МЕТОД: ПРИНИМАЕТ ТЕКСТ ПРЕВЬЮ И СОХРАНЯЕТ В БАЗУ <---
@app.post("/matches/{match_id}/preview")
async def save_preview(match_id: str, data: PreviewInput):
    async with httpx.AsyncClient() as client:
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}"
        # Обновляем колонку preview_text для конкретного матча
        response = await client.patch(supabase_url, json={"preview_text": data.text}, headers=DB_HEADERS)
        if response.status_code in [200, 204]:
            return {"status": "success", "message": "Preview text saved successfully."}
        return {"status": "error", "details": response.text}

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
            
            if db_response.status_code not in [200, 201]:
                return {"error": "Ошибка БД", "details": db_response.text}

            return {"status": "success", "message": f"Сохранено матчей: {len(matches_to_save)}"}
        except Exception as e:
            return {"error": "Ошибка", "details": str(e)}

@app.get("/analyze/{match_id}")
async def analyze_match(match_id: str):
    async with httpx.AsyncClient() as client:
        supabase_url = f"{SUPABASE_URL}/rest/v1/matches?id=eq.{match_id}&select=*"
        db_response = await client.get(supabase_url, headers=DB_HEADERS)
        
        if db_response.status_code != 200 or not db_response.json():
            return {"error": "Матч не найден в базе данных"}
            
        match_data = db_response.json()[0]
        
    home = match_data["home_team"]
    away = match_data["away_team"]
    odds = match_data.get("odds", {})
    # Достаем наш сохраненный текст превью из базы
    preview_text = match_data.get("preview_text", "No detailed statistics provided yet.")
    
    prompt = f"""
    Ты — глубочайший эксперт и аналитик Главной лиги бейсбола (MLB). Твоя задача — дать прогноз на матч для американской аудитории СТРОГО НА АМЕРИКАНСКОМ АНГЛИЙСКОМ ЯЗЫКЕ (American English).

    КРИТИЧЕСКИ ВАЖНО: Используй ТОЛЬКО правильную бейсбольную терминологию (питчеры, иннинги, страйкауты, базы). Никаких баскетбольных терминов или футбольных словечек!

    ТВОЙ СТИЛЬ: Литературная и аналитическая манера легендарного комментатора Василия Уткина: 
    1. Невероятно остроумные, неожиданные метафоры.
    2. Легкий интеллектуальный снобизм и скрытая ирония.
    3. Глубокое понимание логики игры, сочетающее сухие цифры статистики и букмекерские линии.
    
    Матч: {home} (хозяева) против {away} (гости).
    Коэффициенты букмекеров: {odds}
    
    ДОПОЛНИТЕЛЬНАЯ БЕЙСБОЛЬНАЯ СТАТИСТИКА (с сайта Baseball-Reference):
    {preview_text}
    
    ЗАДАЧА:
    Внимательно изучи предоставленный текст статистики (найди там стартовых питчеров, их форму, ERA и другие важные маркеры) и букмекерские коэффициенты.
    Напиши аналитический монолог (4-6 предложений) на АНГЛИЙСКОМ ЯЗЫКЕ, объясняя, где здесь математическая ценность ставки (value bet), основываясь на синергии статистики и коэффициентов. Заверши прогноз четкой рекомендацией ставки.
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
                    "temperature": 0.7
                }
            )
            
            if response.status_code != 200:
                return {"error": f"Ошибка на сервере ИИ: {response.status_code}", "details": response.text}
                
            ai_data = response.json()
            
        return {
            "status": "success",
            "match": f"{home} vs {away}",
            "ai_analysis": ai_data["choices"][0]["message"]["content"]
        }
    except Exception as e:
        return {"error": "Превышено время ожидания ИИ", "details": str(e)}