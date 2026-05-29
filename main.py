from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict
import requests
from datetime import datetime, timedelta
import time

app = FastAPI(title="MLB Buddy AI Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

matches_db: Dict[str, dict] = {}
LAST_FETCH_TIME = 0  # Время последнего автоматического скачивания данных

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
        
        return {
            "away_innings": away_innings,
            "home_innings": home_innings,
            "away_totals": away_totals,
            "home_totals": home_totals
        }
    except Exception as e:
        print(f"Error fetching linescore for game {game_pk}: {e}")
        return None


def sync_mlb_data():
    """Скачивает матчи за ВЧЕРА, СЕГОДНЯ и ЗАВТРА, чтобы результаты не слетали"""
    global LAST_FETCH_TIME
    try:
        # Формируем окно в 3 дня
        yesterday_str = (datetime.today() - timedelta(days=1)).strftime('%Y-%m-%d')
        tomorrow_str = (datetime.today() + timedelta(days=1)).strftime('%Y-%m-%d')
        
        url = f"https://statsapi.mlb.com/api/v1/schedule/games/?sportId=1&startDate={yesterday_str}&endDate={tomorrow_str}"
        response = requests.get(url, timeout=5).json()
        
        dates = response.get("dates", [])
        for date_obj in dates:
            games = date_obj.get("games", [])
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
        LAST_FETCH_TIME = time.time()
        return len(dates)
    except Exception as e:
        print(f"Error in sync_mlb_data: {e}")
        return 0


@app.get("/fetch-schedule")
def fetch_schedule():
    """Ручная команда загрузки базы из админки"""
    games_count = sync_mlb_data()
    return {"status": "success", "message": "Database synchronized successfully."}


@app.get("/matches")
def get_matches(boss: int = 0):
    """Возвращает список матчей и автоматически обновляет их раз в 60 секунд"""
    global LAST_FETCH_TIME
    # Умный авто-апдейт: если прошло больше 60 секунд — бэкенд сам качает свежий счет из MLB
    if time.time() - LAST_FETCH_TIME > 60:
        sync_mlb_data()
        
    matches_list = list(matches_db.values())
    # Сортируем матчи по ID, чтобы они не прыгали местами на экране
    matches_list.sort(key=lambda x: x.get("id", ""))
    
    if boss == 1:
        return matches_list
    else:
        public_matches = []
        for m in matches_list:
            m_copy = m.copy()
            if not m_copy.get("is_published"):
                m_copy["ai_analysis"] = ""
            public_matches.append(m_copy)
        return public_matches


@app.post("/publish-board")
def publish_board():
    for game_id in matches_db:
        matches_db[game_id]["is_published"] = True
    return {"status": "success", "message": "The premium board is now LIVE."}


@app.post("/matches/{match_id}/admin-update")
def admin_update(match_id: str, data: AdminUpdate):
    if match_id not in matches_db:
        raise HTTPException(status_code=404, detail="Match not found")
    if data.ai_analysis is not None:
        matches_db[match_id]["ai_analysis"] = data.ai_analysis
    if data.preview_text is not None:
        matches_db[match_id]["preview_text"] = data.preview_text
    if data.manual_pitchers is not None:
        matches_db[match_id]["manual_pitchers"] = data.manual_pitchers
    return {"status": "success", "message": "Saved."}


@app.post("/matches/{match_id}/chat")
def chat_with_buddy(match_id: str, data: ChatMessage):
    if match_id not in matches_db:
        raise HTTPException(status_code=404, detail="Match not found")
    match = matches_db[match_id]
    user_msg = data.message
    
    from main import generate_buddy_reply
    ai_reply = generate_buddy_reply(match, user_msg)
    
    if "chat_history" not in match:
        match["chat_history"] = []
    match["chat_history"].append({"role": "user", "text": user_msg})
    match["chat_history"].append({"role": "assistant", "text": ai_reply})
    return {"reply": ai_reply}


def generate_buddy_reply(match: dict, user_msg: str) -> str:
    msg_lower = user_msg.lower()
    away = match['away_team']
    home = match['home_team']
    pitchers = match['manual_pitchers'] if match['manual_pitchers'] else match['pitchers']
    if any(word in msg_lower for word in ["who", "win", "pick", "кто", "побед"]):
        return f"Analyzing the pitching matchup ({pitchers or 'TBA'}), my system detects a clear quantitative edge. Bullpen analytics lean toward the value side. See my complete pick details in the unlocked block above!"
    else:
        return f"Interesting angle on the {away} vs {home} game! Based on starter rotation ({pitchers or 'TBA'}), our mathematical model points to standard variance. Let me know if you need specific player projections!"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)