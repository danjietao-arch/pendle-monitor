"""Official NBA.com box scores, data.nba.com schedules, and NBA Communications votes.

stats.nba.com/stats hangs from this environment. These parsers consume the
official payloads that do respond: the league schedule JSON, the game object
embedded in each nba.com box-score page, and NBA Communications vote PDFs.
"""

from __future__ import annotations

import json
import re

from nba_mvp.collect.nba_stats import parse_minutes, parse_number
from nba_mvp.util import DataError

PLACE_POINTS = (10, 7, 5, 3, 1)
RESULTS_LINE = re.compile(
    r"^(?P<name>.+?),\s+(?P<team>.+?)\s+(?P<first>\d+)\s+(?P<second>\d+)\s+(?P<third>\d+)\s+"
    r"(?P<fourth>\d+)\s+(?P<fifth>\d+)\s+(?P<total>\d+)\s*$"
)
NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S,
)
STAT_KEYS = (
    "minutes",
    "fieldGoalsMade",
    "fieldGoalsAttempted",
    "freeThrowsMade",
    "freeThrowsAttempted",
    "reboundsTotal",
    "assists",
    "steals",
    "blocks",
    "turnovers",
    "points",
    "plusMinusPoints",
)


def season_start_year(season: str) -> int:
    match = re.fullmatch(r"(20\d{2})-(\d{2})", season or "")
    if not match:
        raise DataError(f"无法解析赛季: {season}")
    start = int(match.group(1))
    end = int(match.group(2))
    if end != (start + 1) % 100:
        raise DataError(f"赛季年份不连续: {season}")
    return start


def schedule_url(season: str) -> str:
    year = season_start_year(season)
    return (
        "https://data.nba.com/data/10s/v2015/json/mobile_teams/nba/"
        f"{year}/league/00_full_schedule.json"
    )


def boxscore_url(game_id: str) -> str:
    return f"https://www.nba.com/game/{game_id}/box-score"


def _score(value):
    if value in (None, ""):
        return None
    return parse_number(value)


def parse_schedule(payload) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("lscd"), list):
        raise DataError("赛程响应缺少 lscd")
    parsed = []
    seen = set()
    for month in payload["lscd"]:
        games = ((month or {}).get("mscd") or {}).get("g") or []
        if not isinstance(games, list):
            raise DataError("赛程月份缺少 g")
        for game in games:
            game_id = str(game.get("gid") or "")
            if not game_id.startswith("002"):
                continue
            if game_id in seen:
                raise DataError(f"赛程重复: {game_id}")
            seen.add(game_id)
            if game.get("stt") != "Final":
                continue
            game_date = str(game.get("gdte") or "")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", game_date):
                raise DataError(f"赛程日期无法解析: {game_id}")
            home = game.get("h") or {}
            away = game.get("v") or {}
            home_points = _score(home.get("s"))
            away_points = _score(away.get("s"))
            if home_points is None or away_points is None or home_points == away_points:
                home_win = away_win = None
            else:
                home_win = 1 if home_points > away_points else 0
                away_win = 1 - home_win
            if not home.get("tid") or not away.get("tid"):
                raise DataError(f"赛程缺少球队: {game_id}")
            parsed.append(
                {
                    "game_id": game_id,
                    "game_date": game_date,
                    "home": _team_side(home, home_points, away_points, home_win, away),
                    "away": _team_side(away, away_points, home_points, away_win, home),
                }
            )
    if not parsed:
        raise DataError("赛程里没有已结束的常规赛")
    return parsed


def _team_side(side: dict, points_for, points_against, win, opponent: dict) -> dict:
    return {
        "team_id": str(side.get("tid")),
        "team_abbr": None if side.get("ta") in (None, "") else str(side.get("ta")),
        "points_for": points_for,
        "points_against": points_against,
        "win": win,
        "opponent": None if opponent.get("ta") in (None, "") else str(opponent.get("ta")),
    }


def schedule_team_rows(games: list[dict]) -> list[dict]:
    rows = []
    for game in games:
        for side in ("home", "away"):
            team = game[side]
            rows.append(
                {
                    "team_id": team["team_id"],
                    "team_abbr": team["team_abbr"],
                    "game_id": game["game_id"],
                    "game_date": game["game_date"],
                    "win": team["win"],
                    "points_for": team["points_for"],
                    "points_against": team["points_against"],
                }
            )
    return rows


def extract_boxscore_game(html: str) -> dict:
    match = NEXT_DATA.search(html or "")
    if not match:
        raise DataError("箱分页面缺少 __NEXT_DATA__")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise DataError("箱分 __NEXT_DATA__ 无法解析") from exc
    game = ((payload.get("props") or {}).get("pageProps") or {}).get("game")
    if not isinstance(game, dict) or not game.get("gameId"):
        raise DataError("箱分页面缺少 game")
    return trim_boxscore_game(game)


def trim_boxscore_game(game: dict) -> dict:
    """Keep the official game object fields needed to rebuild player logs."""

    def team(side: str) -> dict:
        raw = game.get(side) or {}
        players = []
        for player in raw.get("players") or []:
            stats = player.get("statistics") or {}
            players.append(
                {
                    "personId": player.get("personId"),
                    "firstName": player.get("firstName"),
                    "familyName": player.get("familyName"),
                    "position": player.get("position"),
                    "comment": player.get("comment"),
                    "statistics": {key: stats.get(key) for key in STAT_KEYS},
                }
            )
        return {
            "teamId": raw.get("teamId"),
            "teamTricode": raw.get("teamTricode"),
            "score": raw.get("score"),
            "players": players,
        }

    if not game.get("gameId"):
        raise DataError("箱分缺少 gameId")
    return {
        "gameId": str(game.get("gameId")),
        "gameStatus": game.get("gameStatus"),
        "gameStatusText": game.get("gameStatusText"),
        "gameTimeUTC": game.get("gameTimeUTC"),
        "gameEt": game.get("gameEt"),
        "homeTeam": team("homeTeam"),
        "awayTeam": team("awayTeam"),
    }


def _player_name(player: dict) -> str | None:
    name = " ".join(part for part in (player.get("firstName"), player.get("familyName")) if part)
    name = re.sub(r"\s+", " ", name).strip()
    return name or None


def _started(position) -> int | None:
    if position is None:
        return None
    return 0 if str(position).strip() == "" else 1


def parse_boxscore_players(game: dict, schedule_game: dict) -> tuple[list[dict], list[dict]]:
    """Return played rows and players left unparsed.

    DNP and 0:00 lines are not inserted. Unparseable minutes are reported and
    are not stored as zero.
    """
    if str(game.get("gameId")) != str(schedule_game.get("game_id")):
        raise DataError(f"箱分与赛程 game_id 不一致: {game.get('gameId')}")
    status = game.get("gameStatus")
    status_text = str(game.get("gameStatusText") or "").strip()
    if status in (3, "3"):
        pass
    elif status in (None, "") and status_text.startswith("Final"):
        pass
    else:
        raise DataError(f"比赛未结束，不写入球员数据: {game.get('gameId')}")
    opponents = {
        schedule_game["home"]["team_id"]: schedule_game["away"]["team_abbr"],
        schedule_game["away"]["team_id"]: schedule_game["home"]["team_abbr"],
    }
    played = []
    unparsed = []
    for side in ("homeTeam", "awayTeam"):
        team = game.get(side) or {}
        team_id = None if team.get("teamId") in (None, "") else str(team.get("teamId"))
        if team_id not in opponents:
            raise DataError(f"箱分球队不在赛程中: {game.get('gameId')} {team_id}")
        for player in team.get("players") or []:
            player_id = player.get("personId")
            if player_id in (None, ""):
                unparsed.append({"game_id": schedule_game["game_id"], "reason": "missing player_id"})
                continue
            stats = player.get("statistics") or {}
            minutes_raw = stats.get("minutes")
            comment = str(player.get("comment") or "")
            if minutes_raw in (None, "", "--"):
                continue
            try:
                minutes = parse_minutes(minutes_raw)
            except DataError:
                unparsed.append(
                    {
                        "game_id": schedule_game["game_id"],
                        "player_id": str(player_id),
                        "reason": "minutes",
                    }
                )
                continue
            if minutes is None or minutes <= 0:
                continue
            try:
                row = {
                    "player_id": str(player_id),
                    "player_name": _player_name(player),
                    "game_id": schedule_game["game_id"],
                    "game_date": schedule_game["game_date"],
                    "team_id": team_id,
                    "team_abbr": None if team.get("teamTricode") in (None, "") else str(team.get("teamTricode")),
                    "opponent": opponents[team_id],
                    "minutes": minutes,
                    "pts": parse_number(stats.get("points")),
                    "reb": parse_number(stats.get("reboundsTotal")),
                    "ast": parse_number(stats.get("assists")),
                    "stl": parse_number(stats.get("steals")),
                    "blk": parse_number(stats.get("blocks")),
                    "tov": parse_number(stats.get("turnovers")),
                    "fgm": parse_number(stats.get("fieldGoalsMade")),
                    "fga": parse_number(stats.get("fieldGoalsAttempted")),
                    "ftm": parse_number(stats.get("freeThrowsMade")),
                    "fta": parse_number(stats.get("freeThrowsAttempted")),
                    "started": _started(player.get("position")),
                }
            except DataError:
                unparsed.append(
                    {
                        "game_id": schedule_game["game_id"],
                        "player_id": str(player_id),
                        "reason": "stat",
                    }
                )
                continue
            if comment and row["player_name"] is None:
                unparsed.append(
                    {
                        "game_id": schedule_game["game_id"],
                        "player_id": str(player_id),
                        "reason": "name",
                    }
                )
                continue
            played.append(row)
    return played, unparsed


def _weighted_total(places: list[int]) -> int:
    return sum(count * weight for count, weight in zip(places, PLACE_POINTS))


def parse_voting_results_text(text: str) -> list[dict]:
    rows = []
    seen = set()
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        match = RESULTS_LINE.fullmatch(line)
        if not match:
            continue
        places = [int(match.group(name)) for name in ("first", "second", "third", "fourth", "fifth")]
        total = int(match.group("total"))
        if _weighted_total(places) != total:
            raise DataError(f"投票分与名次票不一致: {match.group('name')}")
        name = match.group("name").strip()
        if name.casefold() in seen:
            raise DataError(f"投票结果出现重复姓名: {name}")
        seen.add(name.casefold())
        rows.append(
            {
                "player_name": name,
                "vote_points": total,
                "first_place_votes": places[0],
                "place_votes": places,
            }
        )
    if not rows:
        raise DataError("投票结果 PDF 没有可解析的球员行")
    return _with_winner(rows)


def parse_voter_ballot_text(text: str) -> list[dict]:
    totals: dict[str, int] = {}
    firsts: dict[str, int] = {}
    display: dict[str, str] = {}
    ballots = 0
    for raw_line in (text or "").splitlines():
        parts = [part.strip() for part in re.split(r"\s{2,}", raw_line.strip()) if part.strip()]
        if len(parts) < 6 or not all("," in part for part in parts[-5:]):
            continue
        if parts[0].lower().startswith("voter"):
            continue
        picks = []
        for part in parts[-5:]:
            name = part.split(",", 1)[0].strip()
            if not name:
                raise DataError("选票姓名为空")
            picks.append(name)
        if len(set(name.casefold() for name in picks)) != 5:
            raise DataError(f"同一张选票出现重复球员: {picks}")
        ballots += 1
        for name, weight in zip(picks, PLACE_POINTS):
            key = name.casefold()
            display.setdefault(key, name)
            totals[key] = totals.get(key, 0) + weight
            if weight == 10:
                firsts[key] = firsts.get(key, 0) + 1
    if ballots == 0:
        raise DataError("选票 PDF 没有可解析的选票")
    rows = [
        {
            "player_name": display[key],
            "vote_points": points,
            "first_place_votes": firsts.get(key, 0),
        }
        for key, points in totals.items()
    ]
    rows.sort(key=lambda row: (-row["vote_points"], row["player_name"]))
    return _with_winner(rows)


def _with_winner(rows: list[dict]) -> list[dict]:
    points = [row["vote_points"] for row in rows]
    best = max(points)
    if points.count(best) != 1:
        raise DataError("最高票并列，拒绝猜测获奖者")
    total = sum(points)
    for row in rows:
        row["is_winner"] = 1 if row["vote_points"] == best else 0
        row["vote_share"] = (row["vote_points"] / total) if total else None
    return rows
