from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlencode

from nba_mvp.config import project_path
from nba_mvp.db import audit, transaction
from nba_mvp.http_client import HttpError, get_json
from nba_mvp.util import DataError, isoformat, utcnow

NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; nba-mvp-research/0.1)",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nba.com/",
    "Origin": "https://www.nba.com",
    "x-nba-stats-origin": "stats",
    "x-nba-stats-token": "true",
}

PLAYER_FIELDS = {
    "PLAYER_ID": "player_id",
    "PLAYER_NAME": "player_name",
    "TEAM_ID": "team_id",
    "TEAM_ABBREVIATION": "team_abbr",
    "GAME_ID": "game_id",
    "GAME_DATE": "game_date",
    "MATCHUP": "opponent",
    "MIN": "minutes",
    "PTS": "pts",
    "REB": "reb",
    "AST": "ast",
    "STL": "stl",
    "BLK": "blk",
    "TOV": "tov",
    "FGM": "fgm",
    "FGA": "fga",
    "FTM": "ftm",
    "FTA": "fta",
}

NUMBER_FIELDS = {"minutes", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "ftm", "fta", "points_for", "plus_minus"}


_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def parse_nba_date(value) -> str:
    if value is None or str(value).strip() == "":
        raise DataError("缺少比赛日期")
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", text)
    if match:
        month = _MONTHS.get(match.group(1)[:3].upper())
        if month is None:
            raise DataError(f"无法解析日期: {value}")
        return datetime(int(match.group(3)), month, int(match.group(2))).date().isoformat()
    raise DataError(f"无法解析日期: {value}")


def parse_minutes(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if text in {"", "--", "None"}:
        return None
    if ":" in text:
        minutes, seconds = text.split(":", 1)
        return float(minutes) + float(seconds) / 60.0
    try:
        return float(text)
    except ValueError as exc:
        raise DataError(f"无法解析分钟: {value}") from exc


def parse_number(value):
    if value is None or value == "" or value == "--":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise DataError(f"无法解析数值: {value}") from exc


def result_sets(payload) -> list[dict]:
    if not isinstance(payload, dict):
        raise DataError("NBA 响应不是 JSON 对象")
    sets = payload.get("resultSets")
    if sets is None and isinstance(payload.get("resultSet"), dict):
        sets = [payload["resultSet"]]
    if not isinstance(sets, list) or not sets:
        raise DataError("NBA 响应缺少 resultSets")
    return sets


def _rows_from_set(result_set: dict, required: list[str]) -> tuple[list[str], list[list]]:
    headers = result_set.get("headers") or []
    missing = [name for name in required if name not in headers]
    if missing:
        raise DataError(f"缺少字段: {', '.join(missing)}")
    return headers, result_set.get("rowSet") or []


def parse_player_gamelogs(payload, *, fetched_on: str | None = None) -> list[dict]:
    chosen = None
    for result_set in result_sets(payload):
        headers = result_set.get("headers") or []
        if "PLAYER_ID" in headers and "GAME_ID" in headers:
            chosen = result_set
            break
    if chosen is None:
        raise DataError("未找到球员逐场结果集")
    headers, raw_rows = _rows_from_set(chosen, ["PLAYER_ID", "GAME_ID", "GAME_DATE"])
    index = {name: pos for pos, name in enumerate(headers)}
    parsed = []
    for raw in raw_rows:
        row = {"started": None}
        for header, field in PLAYER_FIELDS.items():
            if header not in index:
                row[field] = None
                continue
            value = raw[index[header]]
            if field == "game_date":
                row[field] = parse_nba_date(value)
            elif field == "minutes":
                row[field] = parse_minutes(value)
            elif field in NUMBER_FIELDS:
                row[field] = parse_number(value)
            elif field in {"player_id", "team_id"} and value is not None:
                row[field] = str(value)
            else:
                row[field] = None if value is None else str(value)
        if fetched_on and row["game_date"] > fetched_on:
            continue
        parsed.append(row)
    return parsed


def parse_team_gamelogs(payload, *, fetched_on: str | None = None) -> list[dict]:
    chosen = None
    for result_set in result_sets(payload):
        headers = result_set.get("headers") or []
        if "TEAM_ID" in headers and "GAME_ID" in headers and "PLAYER_ID" not in headers:
            chosen = result_set
            break
    if chosen is None:
        raise DataError("未找到球队逐场结果集")
    headers, raw_rows = _rows_from_set(chosen, ["TEAM_ID", "GAME_ID", "GAME_DATE"])
    index = {name: pos for pos, name in enumerate(headers)}
    parsed = []
    for raw in raw_rows:
        def cell(name):
            if name not in index:
                return None
            return raw[index[name]]

        game_date = parse_nba_date(cell("GAME_DATE"))
        if fetched_on and game_date > fetched_on:
            continue
        win_raw = cell("WL")
        if win_raw in (None, ""):
            win = None
        elif str(win_raw).upper() == "W":
            win = 1
        elif str(win_raw).upper() == "L":
            win = 0
        else:
            win = None
        points_for = parse_number(cell("PTS"))
        plus_minus = parse_number(cell("PLUS_MINUS"))
        points_against = None
        if points_for is not None and plus_minus is not None:
            points_against = points_for - plus_minus
        parsed.append(
            {
                "team_id": str(cell("TEAM_ID")),
                "team_abbr": None if cell("TEAM_ABBREVIATION") is None else str(cell("TEAM_ABBREVIATION")),
                "game_id": str(cell("GAME_ID")),
                "game_date": game_date,
                "win": win,
                "points_for": points_for,
                "points_against": points_against,
            }
        )
    return parsed


def _url(base: str, endpoint: str, params: dict) -> str:
    return f"{base.rstrip('/')}/{endpoint}?{urlencode(params)}"


def player_gamelogs_url(base: str, season: str, date_to: str | None = None) -> str:
    params = {
        "LeagueID": "00",
        "Season": season,
        "SeasonType": "Regular Season",
        "PlayerID": "",
        "DateFrom": "",
        "DateTo": date_to or "",
    }
    return _url(base, "playergamelogs", params)


def team_gamelogs_url(base: str, season: str, date_to: str | None = None) -> str:
    params = {
        "LeagueID": "00",
        "Season": season,
        "SeasonType": "Regular Season",
        "PlayerOrTeam": "T",
        "Direction": "ASC",
        "Sorter": "DATE",
        "Counter": "0",
        "DateFrom": "",
        "DateTo": date_to or "",
    }
    return _url(base, "leaguegamelog", params)


def _upsert_players(conn, games: list[dict], fetched_at: str) -> None:
    for game in games:
        conn.execute(
            """
            INSERT INTO players (player_id, player_name, team_id, team_abbr, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(player_id) DO UPDATE SET
              player_name = COALESCE(excluded.player_name, players.player_name),
              team_id = COALESCE(excluded.team_id, players.team_id),
              team_abbr = COALESCE(excluded.team_abbr, players.team_abbr),
              updated_at = excluded.updated_at
            """,
            (game["player_id"], game.get("player_name"), game.get("team_id"), game.get("team_abbr"), fetched_at),
        )


def _upsert_games(conn, season: str, games: list[dict], meta: dict) -> None:
    for game in games:
        conn.execute(
            """
            INSERT INTO game_logs (
              season, player_id, player_name, game_id, game_date, team_id, team_abbr, opponent,
              minutes, pts, reb, ast, stl, blk, tov, fgm, fga, ftm, fta, started,
              source_url, source_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season, player_id, game_id) DO UPDATE SET
              player_name = COALESCE(excluded.player_name, game_logs.player_name),
              game_date = excluded.game_date,
              team_id = COALESCE(excluded.team_id, game_logs.team_id),
              team_abbr = COALESCE(excluded.team_abbr, game_logs.team_abbr),
              opponent = COALESCE(excluded.opponent, game_logs.opponent),
              minutes = COALESCE(excluded.minutes, game_logs.minutes),
              pts = COALESCE(excluded.pts, game_logs.pts),
              reb = COALESCE(excluded.reb, game_logs.reb),
              ast = COALESCE(excluded.ast, game_logs.ast),
              stl = COALESCE(excluded.stl, game_logs.stl),
              blk = COALESCE(excluded.blk, game_logs.blk),
              tov = COALESCE(excluded.tov, game_logs.tov),
              fgm = COALESCE(excluded.fgm, game_logs.fgm),
              fga = COALESCE(excluded.fga, game_logs.fga),
              ftm = COALESCE(excluded.ftm, game_logs.ftm),
              fta = COALESCE(excluded.fta, game_logs.fta),
              source_url = excluded.source_url,
              source_timestamp = excluded.source_timestamp
            """,
            (
                season,
                game["player_id"],
                game.get("player_name"),
                game["game_id"],
                game["game_date"],
                game.get("team_id"),
                game.get("team_abbr"),
                game.get("opponent"),
                game.get("minutes"),
                game.get("pts"),
                game.get("reb"),
                game.get("ast"),
                game.get("stl"),
                game.get("blk"),
                game.get("tov"),
                game.get("fgm"),
                game.get("fga"),
                game.get("ftm"),
                game.get("fta"),
                game.get("started"),
                meta["url"],
                meta["fetched_at"],
            ),
        )


def _upsert_teams(conn, season: str, games: list[dict], meta: dict) -> None:
    for game in games:
        conn.execute(
            """
            INSERT INTO team_results (
              season, team_id, team_abbr, game_id, game_date, win, points_for, points_against,
              source_url, source_timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season, team_id, game_id) DO UPDATE SET
              team_abbr = COALESCE(excluded.team_abbr, team_results.team_abbr),
              game_date = excluded.game_date,
              win = COALESCE(excluded.win, team_results.win),
              points_for = COALESCE(excluded.points_for, team_results.points_for),
              points_against = COALESCE(excluded.points_against, team_results.points_against),
              source_url = excluded.source_url,
              source_timestamp = excluded.source_timestamp
            """,
            (
                season,
                game["team_id"],
                game.get("team_abbr"),
                game["game_id"],
                game["game_date"],
                game.get("win"),
                game.get("points_for"),
                game.get("points_against"),
                meta["url"],
                meta["fetched_at"],
            ),
        )


def collect_season_stats(
    session,
    cfg: dict,
    conn,
    season: str,
    *,
    date_to: str | None = None,
    fetched_on: str | None = None,
) -> dict:
    """Fetch and archive one season. On failure, keep existing rows and mark STALE.

    date_to is the NBA API query value. fetched_on is an ISO date; games after it are dropped.
    """
    raw_dir = project_path(cfg, "raw")
    base = cfg["nba"]["stats_base"]
    headers = dict(NBA_HEADERS)
    headers["Referer"] = cfg["nba"].get("referer", headers["Referer"])
    try:
        player_payload, player_meta = get_json(
            session, player_gamelogs_url(base, season, date_to), raw_dir=raw_dir, headers=headers
        )
        team_payload, team_meta = get_json(
            session, team_gamelogs_url(base, season, date_to), raw_dir=raw_dir, headers=headers
        )
        player_rows = parse_player_gamelogs(player_payload, fetched_on=fetched_on)
        team_rows = parse_team_gamelogs(team_payload, fetched_on=fetched_on)
    except (HttpError, DataError) as exc:
        audit(conn, "nba_fetch_failed", season, str(exc), isoformat(utcnow()))
        return {"status": "stale", "season": season, "error": str(exc), "games": 0, "team_games": 0}
    with transaction(conn):
        _upsert_players(conn, player_rows, player_meta["fetched_at"])
        _upsert_games(conn, season, player_rows, player_meta)
        _upsert_teams(conn, season, team_rows, team_meta)
        audit(
            conn,
            "nba_fetch_ok",
            season,
            f"players={len(player_rows)} teams={len(team_rows)}",
            player_meta["fetched_at"],
        )
    status = "empty" if not player_rows and not team_rows else "ok"
    return {
        "status": status,
        "season": season,
        "games": len(player_rows),
        "team_games": len(team_rows),
        "fetched_at": player_meta["fetched_at"],
    }


def nba_date_param(day: datetime | None = None) -> str:
    current = day or datetime.now(timezone.utc)
    return current.strftime("%m/%d/%Y")
