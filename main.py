from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict
import requests
from datetime import datetime

app = FastAPI(title="MLB Buddy AI Server")

# Настройка CORS, чтобы фронтенд на Vercel мог беспрепятственно делать запросы
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Временная база данных в оперативной памяти сервера.
# При перезапуске сервера бесплатного тарифа Render данные обнуляются,
# поэтому перед релизом мы прикрутим сюда таблицы Supabase.
matches_db: Dict[str, dict] = {}

# Модели данных Pydantic для валидации входящих запросов
class ChatMessage(BaseModel):
    message: str

class AdminUpdate(BaseModel):
    ai_analysis: Optional[str] = None
    preview_text: Optional[str] = None
    manual_pitchers: Optional[str] = None


def get_mlb_linescore(game_pk: int) -> Optional[dict]:
    """Скачивает живые иннинги и статистику из официального API MLB"""
    try:
        url = f"https://statsapi.mlb.com/api/v1/game/{game_pk}/linescore"
        res = requests.get(url, timeout=5).json()
        
        away_innings = []
        home_innings = []
        
        # Парсим каждый сыгранный иннинг
        for inning in res.get("innings", []):
            away_runs = inning.get("away", {}).get("runs")
            home_runs = inning.get("home", {}).get("runs")
            
            away_innings.append(str(away_runs) if away_runs is not None else "-")
            
            # В бейсболе, если хозяева ведут в счете к концу 9 иннинга, они не бьют. Ставится "X"
            if home_runs is None:
                if away_runs is not None and inning.get("ordinalNum") == "9th":
                    home_innings.append("X")
                else:
                    home_innings.append("-")
            else:
                home_innings.append(str(home_runs))
        
        # Автоматически добиваем таблицу до стандартных 9 иннингов прочерками
        while len(away_innings) < 9: away_innings.append("-")
        while len(home_innings) < 9: home_innings.append("-")
            
        # Забираем суммарные Runs (Очки), Hits (Хиты), Errors (Ошибки)
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
        
        return {
            "away_innings": away_innings,
            "home_innings": home_innings,
            "away_totals": away_totals,
            "home_totals": home_totals
        }
    except Exception as e:
        print(f"Error fetching linescore for game {game_pk}: {e}")
        return None


@app.get("/fetch-schedule")
def fetch_schedule():
    """Скачивает расписание матчей MLB на сегодня и обновляет базу данных"""
    try:
        # Автоматически берем текущую дату
        today = datetime.today().strftime('%Y-%m-%d')
        url = f"https://statsapi.mlb.com/api/v1/schedule/games/?sportId=1&date={today}"
        response = requests.get(url, timeout=5).json()
        
        games = response.get("dates", [{}])[0].get("games", [])
        
        for game in games:
            game_id = str(game.get("gamePk"))
            status_info = game.get("status", {})
            abstract_status = status_info.get("abstractGameState", "Preview") # Preview, Live, Final
            detailed_status = status_info.get("detailedState", "")
            
            # Парсим названия команд
            teams = game.get("teams", {})
            away_team = teams.get("away", {}).get("team", {}).get("name", "Away Team")
            home_team = teams.get("home", {}).get("team", {}).get("name", "Home Team")
            
            # Извлекаем текущие рекорды побед/поражений
            away_record = f"{teams.get('away', {}).get('leagueRecord', {}).get('wins', 0)}-{teams.get('away', {}).get('leagueRecord', {}).get('losses', 0)}"
            home_record = f"{teams.get('home', {}).get('leagueRecord', {}).get('wins', 0)}-{teams.get('home', {}).get('leagueRecord', {}).get('losses', 0)}"
            
            # Формируем отображение счета
            if abstract_status != "Preview":
                away_score = teams.get("away", {}).get("score", 0)
                home_score = teams.get("home", {}).get("score", 0)
                score_str = f"{away_score} - {home_score}"
            else:
                score_str = "@"
            
            # Стартовые питчеры по умолчанию от MLB
            away_pitcher = game.get("teams", {}).get("away", {}).get("probablePitcher", {}).get("name", "")
            home_pitcher = game.get("teams", {}).get("home", {}).get("probablePitcher", {}).get("name", "")
            pitchers_str = f"{away_pitcher} vs {home_pitcher}" if away_pitcher or home_pitcher else ""
            
            # Наш новый живой Linescore
            linescore_data = get_mlb_linescore(game.get("gamePk")) if abstract_status != "Preview" else None

            # Если этот матч уже был в нашей памяти, сохраняем аналитику, которую админ писал вручную
            existing_game = matches_db.get(game_id, {})
            
            matches_db[game_id] = {
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
                "chat_history": existing_game.get("chat_history", []),
                "linescore": linescore_data
            }
            
        return {"status": "success", "message": f"Successfully parsed {len(games)} games for today."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/matches")
def get_matches(boss: int = 0):
    """Возвращает список матчей на главную страницу сайта"""
    matches_list = list(matches_db.values())
    if boss == 1:
        return matches_list
    else:
        # Для обычных пользователей скрываем неопубликованные прогнозы
        public_matches = []
        for m in matches_list:
            m_copy = m.copy()
            if not m_copy.get("is_published"):
                m_copy["ai_analysis"] = ""  # Текст прогноза сотрется до публикации админом
            public_matches.append(m_copy)
        return public_matches


@app.post("/publish-board")
def publish_board():
    """Админ-команда: сделать все текущие прогнозы видимыми для всех пользователей"""
    for game_id in matches_db:
        matches_db[game_id]["is_published"] = True
    return {"status": "success", "message": "The premium board is now LIVE."}


@app.post("/matches/{match_id}/admin-update")
def admin_update(match_id: str, data: AdminUpdate):
    """Эндпоинт Контрольной Комнаты (Админки) для сохранения прогнозов и таблиц"""
    if match_id not in matches_db:
        raise HTTPException(status_code=404, detail="Match not found in local DB")
    
    if data.ai_analysis is not None:
        matches_db[match_id]["ai_analysis"] = data.ai_analysis
    if data.preview_text is not None:
        matches_db[match_id]["preview_text"] = data.preview_text
    if data.manual_pitchers is not None:
        matches_db[match_id]["manual_pitchers"] = data.manual_pitchers
        
    return {"status": "success", "message": "Admin changes saved successfully."}


@app.post("/matches/{match_id}/chat")
def chat_with_buddy(match_id: str, data: ChatMessage):
    """Закрытый Пейволом VIP-чат с ИИ Buddy по конкретной игре"""
    if match_id not in matches_db:
        raise HTTPException(status_code=404, detail="Match not found")
        
    match = matches_db[match_id]
    user_msg = data.message
    
    # Умный симулятор ответов Buddy AI на основе контекста игры
    ai_reply = generate_buddy_reply(match, user_msg)
    
    # Записываем диалог в историю матча
    if "chat_history" not in match:
        match["chat_history"] = []
        
    match["chat_history"].append({"role": "user", "text": user_msg})
    match["chat_history"].append({"role": "assistant", "text": ai_reply})
    
    return {"reply": ai_reply}


def generate_buddy_reply(match: dict, user_msg: str) -> str:
    """Алгоритм симуляции экспертных спортивных ответов Buddy AI"""
    msg_lower = user_msg.lower()
    away = match['away_team']
    home = match['home_team']
    pitchers = match['manual_pitchers'] if match['manual_pitchers'] else match['pitchers']
    
    if any(word in msg_lower for word in ["who", "win", "pick", "кто", "побед", "выиграет"]):
        return f"Analyzing the pitching matchup ({pitchers or 'TBA'}), my system detects a clear quantitative edge. In our premium forecast above, we noticed severe bullpen vulnerabilities for the road team. Backing the value side within acceptable market lines is the sharpest move here!"
    elif any(word in msg_lower for word in ["odds", "line", "total", "коэф", "тотал", "спред"]):
        return f"The current total line for {away} @ {home} is heavily reflecting public betting trends. Looking at the Sabermetric configurations, wind conditions, and umpire data, historical models lean against the consensus. I'd wait closer to the first pitch to see if we can catch an extra half-run of value."
    else:
        return f"That is an excellent angle on the {away} vs {home} game! Taking the starting pitchers ({pitchers or 'TBA'}) into account, my current projection gives an explicit performance variance advantage to the home field rotation strategy. Let me know if you need specific player prop insights!"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)