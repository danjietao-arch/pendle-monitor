from __future__ import annotations

import json

import pandas as pd

from nba_mvp.db import transaction
from nba_mvp.eligibility import eligibility_for
from nba_mvp.util import dumps

OTHER_ID = "OTHER"
OTHER_NAME = "其他球员"
COUNT_FIELDS = ["minutes", "pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga", "ftm", "fta"]


def snapshot_keys_from_config(cfg: dict) -> list[str]:
    keys = []
    for item in cfg["snapshot_team_games"]:
        if item in (0, "0", "preseason"):
            keys.append("preseason")
        elif item in ("end", "season_end"):
            keys.append("end")
        else:
            keys.append(f"tg_{int(item)}")
    return keys


def _none_if_na(value):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    return value


def aggregate_player_games(games: list[dict]) -> dict:
    base = {"games_played": 0, "game_ids": [], "qualifying_games": None}
    for field in COUNT_FIELDS:
        base[field] = None
        base[f"{field}_per_game"] = None
    if not games:
        return base
    frame = pd.DataFrame(games)
    base["games_played"] = int(len(frame))
    base["game_ids"] = [str(game_id) for game_id in frame["game_id"].tolist()] if "game_id" in frame else []
    for field in COUNT_FIELDS:
        if field not in frame:
            continue
        numeric = pd.to_numeric(frame[field], errors="coerce")
        total = numeric.sum(skipna=False)
        total = _none_if_na(total)
        if total is None:
            continue
        base[field] = float(total)
        base[f"{field}_per_game"] = float(total) / len(frame)
    if "minutes" in frame:
        minutes = pd.to_numeric(frame["minutes"], errors="coerce")
        if not minutes.isna().any():
            base["qualifying_games"] = int((minutes > 0).sum())
    return base


def aggregate_team_games(games: list[dict]) -> dict:
    if not games:
        return {"team_games": 0, "wins": None, "losses": None, "win_pct": None, "point_diff": None}
    frame = pd.DataFrame(games)
    wins = pd.to_numeric(frame["win"], errors="coerce") if "win" in frame else pd.Series(dtype=float)
    points_for = pd.to_numeric(frame["points_for"], errors="coerce") if "points_for" in frame else pd.Series(dtype=float)
    points_against = (
        pd.to_numeric(frame["points_against"], errors="coerce") if "points_against" in frame else pd.Series(dtype=float)
    )
    win_total = _none_if_na(wins.sum(skipna=False)) if len(wins) else None
    result = {
        "team_games": int(len(frame)),
        "wins": None if win_total is None else float(win_total),
        "losses": None if win_total is None else float(len(frame) - win_total),
        "win_pct": None if win_total is None else float(win_total) / len(frame),
        "point_diff": None,
    }
    if len(points_for) and len(points_against) and not points_for.isna().any() and not points_against.isna().any():
        result["point_diff"] = float(points_for.sum() - points_against.sum())
    return result


def _team_dates(team_games: list[dict], team_id: str) -> list[str]:
    return sorted(game["game_date"] for game in team_games if str(game["team_id"]) == str(team_id))


def cutoff_date(team_games: list[dict], team_id: str, snapshot_key: str, season_start: str | None) -> str | None:
    if snapshot_key == "preseason":
        return season_start
    dates = _team_dates(team_games, team_id)
    if snapshot_key == "end":
        return dates[-1] if dates else None
    if not snapshot_key.startswith("tg_"):
        raise ValueError(f"未知快照: {snapshot_key}")
    needed = int(snapshot_key.split("_", 1)[1])
    if len(dates) < needed:
        return None
    return dates[needed - 1]


def _filter_games(games: list[dict], player_id: str, as_of: str | None) -> list[dict]:
    if as_of is None:
        return []
    kept = []
    for game in games:
        if str(game["player_id"]) != str(player_id):
            continue
        if game["game_date"] <= as_of:
            kept.append(game)
    return kept


def _prior_team_win_pct(prior_player_games: list[dict], prior_team_games: list[dict], snapshot_date: str | None):
    """Previous season's final win rate for the team this player finished on.

    Missing history stays missing. It is not written as 0.
    """
    team_id, _abbr = _latest_team(prior_player_games)
    if not team_id or not snapshot_date:
        return None
    visible = [
        game
        for game in prior_team_games
        if str(game.get("team_id")) == str(team_id) and game.get("game_date") and game["game_date"] <= snapshot_date
    ]
    summary = aggregate_team_games(visible)
    if not summary.get("team_games"):
        return None
    return summary.get("win_pct")


def _latest_team(games: list[dict]) -> tuple[str | None, str | None]:
    if not games:
        return None, None
    ordered = sorted(games, key=lambda game: (game["game_date"], str(game.get("game_id"))))
    return ordered[-1].get("team_id"), ordered[-1].get("team_abbr")


def player_cutoff(player_games: list[dict], team_games: list[dict], snapshot_key: str, season_start: str | None):
    """Cutoff date and team using only games knowable by that date.

    A later trade cannot move an earlier snapshot onto the destination team.
    """
    if snapshot_key == "preseason":
        return season_start, None, None
    ordered = sorted(player_games, key=lambda game: (game["game_date"], str(game.get("game_id"))))
    teams: list[str] = []
    for game in ordered:
        team_id = game.get("team_id")
        if team_id and str(team_id) not in teams:
            teams.append(str(team_id))
    valid = []
    for team_id in teams:
        date = cutoff_date(team_games, team_id, snapshot_key, season_start)
        if not date:
            continue
        visible = [game for game in ordered if game.get("team_id") and game["game_date"] <= date]
        if not visible or str(visible[-1].get("team_id")) != team_id:
            continue
        valid.append((date, team_id, visible[-1].get("team_abbr")))
    if not valid:
        return None, None, None
    return max(valid)


def _labels(candidate_ids: set[str], votes: list[dict]) -> dict[str, int] | None:
    winners = [str(vote["player_id"]) for vote in votes if vote.get("is_winner")]
    if len(winners) != 1:
        return None
    winner = winners[0]
    labels = {player_id: 0 for player_id in candidate_ids}
    if winner in labels and winner != OTHER_ID:
        labels[winner] = 1
    else:
        labels[OTHER_ID] = 1
    return labels


def build_snapshot_rows(
    *,
    season: str,
    snapshot_key: str,
    games: list[dict],
    team_games: list[dict],
    prior_games: list[dict],
    votes: list[dict],
    players: dict[str, dict],
    min_minutes: float,
    rule: dict | None,
    season_start: str | None,
    official_eligible: dict | None = None,
    injury_status: dict | None = None,
    extra_player_ids: list[str] | None = None,
    source_timestamp: str | None = None,
    prior_team_games: list[dict] | None = None,
) -> list[dict]:
    if snapshot_key == "preseason" and not season_start:
        raise ValueError("赛前快照需要 season_start")
    official_eligible = official_eligible or {}
    injury_status = injury_status or {}
    prior_team_games = prior_team_games or []
    games_by_player: dict[str, list[dict]] = {}
    for game in games:
        games_by_player.setdefault(str(game["player_id"]), []).append(game)
    prior_by_player: dict[str, list[dict]] = {}
    for game in prior_games:
        prior_by_player.setdefault(str(game["player_id"]), []).append(game)
    known_players = set(players) | set(games_by_player) | set(prior_by_player)
    known_players.discard(OTHER_ID)

    candidates = set(extra_player_ids or [])
    for vote in votes:
        if vote.get("player_id"):
            candidates.add(str(vote["player_id"]))

    def as_of_current(player_id: str):
        date, _team_id, _abbr = player_cutoff(
            games_by_player.get(player_id, []), team_games, snapshot_key, season_start
        )
        return date

    def absorb_minutes(source_by_player: dict[str, list[dict]], as_of_for_player) -> None:
        for player_id in known_players:
            as_of = as_of_for_player(player_id)
            visible = _filter_games(source_by_player.get(player_id, []), player_id, as_of)
            total = 0.0
            complete = True
            for game in visible:
                if game.get("minutes") is None:
                    complete = False
                    break
                total += float(game["minutes"])
            if complete and total >= min_minutes:
                candidates.add(player_id)

    if snapshot_key == "preseason":
        absorb_minutes(prior_by_player, lambda _player_id: season_start)
    else:
        absorb_minutes(games_by_player, as_of_current)
        absorb_minutes(prior_by_player, as_of_current)
    candidates.add(OTHER_ID)

    labels = _labels(candidates, votes)
    rows = []
    for player_id in sorted(candidates, key=lambda value: (value == OTHER_ID, value)):
        profile = players.get(player_id, {})
        row_stamp = source_timestamp
        if player_id == OTHER_ID:
            snapshot_date = season_start if snapshot_key == "preseason" else None
            dated = [row["snapshot_date"] for row in rows if row.get("snapshot_date")]
            if dated:
                snapshot_date = max(dated)
            snapshot_date = snapshot_date or season_start or "end"
            player_features = aggregate_player_games([])
            player_features["prior_pts_per_game"] = None
            player_features["prior_games"] = None
            team_features = aggregate_team_games([])
            team_features["prior_win_pct"] = None
            team_id = None
            team_abbr = None
            games_played = 0
        else:
            snapshot_date, cutoff_team, _cutoff_abbr = player_cutoff(
                games_by_player.get(player_id, []), team_games, snapshot_key, season_start
            )
            if snapshot_key != "preseason" and snapshot_date is None:
                continue
            if snapshot_key == "preseason":
                snapshot_date = season_start
                visible = []
            else:
                visible = _filter_games(games_by_player.get(player_id, []), player_id, snapshot_date)
            prior_visible = _filter_games(prior_by_player.get(player_id, []), player_id, snapshot_date)
            if snapshot_key == "preseason":
                team_id, _prior_abbr = _latest_team(prior_visible)
            else:
                team_id = cutoff_team
            player_features = aggregate_player_games(visible)
            prior_features = aggregate_player_games(prior_visible)
            if prior_visible:
                player_features["prior_pts_per_game"] = prior_features.get("pts_per_game")
                player_features["prior_games"] = prior_features.get("games_played")
            else:
                player_features["prior_pts_per_game"] = None
                player_features["prior_games"] = None
            team_visible = [
                game
                for game in team_games
                if str(game["team_id"]) == str(team_id) and snapshot_date and game["game_date"] <= snapshot_date
            ]
            if snapshot_key == "preseason":
                team_visible = []
            team_features = aggregate_team_games(team_visible)
            team_features["prior_win_pct"] = _prior_team_win_pct(prior_visible, prior_team_games, snapshot_date)
            _team_id, team_abbr = _latest_team(visible or prior_visible)
            team_abbr = team_abbr or profile.get("team_abbr")
            games_played = player_features["games_played"]
            for game_id in player_features["game_ids"]:
                match = next(game for game in visible if str(game["game_id"]) == str(game_id))
                if match["game_date"] > snapshot_date:
                    raise AssertionError("快照泄漏了未来比赛")
            contributed = list(visible) + list(prior_visible) + list(team_visible)
            stamps = [game.get("source_timestamp") for game in contributed if game.get("source_timestamp")]
            if stamps:
                row_stamp = max(stamps)
        official = official_eligible.get(player_id)
        if official is not None:
            official = int(official)
        eligibility = eligibility_for(rule, games_played if player_id != OTHER_ID else None, official)
        rows.append(
            {
                "season": season,
                "snapshot_key": snapshot_key,
                "snapshot_date": snapshot_date,
                "player_id": player_id,
                "player_name": OTHER_NAME if player_id == OTHER_ID else profile.get("player_name") or player_id,
                "team_id": None if player_id == OTHER_ID else team_id,
                "team_abbr": None if player_id == OTHER_ID else team_abbr,
                "games_played": 0 if player_id == OTHER_ID else games_played,
                "qualifying_games": None if player_id == OTHER_ID else player_features.get("qualifying_games"),
                "team_games": 0 if player_id == OTHER_ID else team_features.get("team_games"),
                "player_features": player_features,
                "team_features": team_features,
                "injury_status": injury_status.get(player_id, "unknown"),
                "official_eligible": official,
                "eligibility_status": eligibility["eligibility_status"],
                "award_winner": None if labels is None else labels.get(player_id, 0),
                "source_timestamp": row_stamp,
                "eligibility_reason": eligibility["reason"],
                "p_eligibility": eligibility["p_eligibility"],
                "force_award_zero": eligibility["force_award_zero"],
            }
        )
    present = {row["player_id"] for row in rows}
    final_labels = _labels(present, votes)
    for row in rows:
        row["award_winner"] = None if final_labels is None else final_labels.get(row["player_id"], 0)
    return rows


def save_snapshots(conn, rows: list[dict]) -> None:
    if not rows:
        return
    season = rows[0]["season"]
    snapshot_key = rows[0]["snapshot_key"]
    with transaction(conn):
        conn.execute(
            "DELETE FROM feature_snapshots WHERE season = ? AND snapshot_key = ?",
            (season, snapshot_key),
        )
        for row in rows:
            conn.execute(
                """
                INSERT INTO feature_snapshots (
                  season, snapshot_key, snapshot_date, player_id, player_name, team_id, team_abbr,
                  games_played, qualifying_games, team_games, player_features, team_features,
                  injury_status, official_eligible, eligibility_status, award_winner, source_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["season"],
                    row["snapshot_key"],
                    row["snapshot_date"],
                    row["player_id"],
                    row["player_name"],
                    row["team_id"],
                    row["team_abbr"],
                    row["games_played"],
                    row["qualifying_games"],
                    row["team_games"],
                    dumps(row["player_features"]),
                    dumps(row["team_features"]),
                    row["injury_status"],
                    row["official_eligible"],
                    row["eligibility_status"],
                    row["award_winner"],
                    row["source_timestamp"],
                ),
            )


def _load_games(conn, season: str) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM game_logs WHERE season = ?", (season,))]


def _load_team_games(conn, season: str) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM team_results WHERE season = ?", (season,))]


def _load_votes(conn, season: str) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM vote_totals WHERE season = ?", (season,))]


def _load_players(conn) -> dict[str, dict]:
    return {row["player_id"]: dict(row) for row in conn.execute("SELECT * FROM players")}


def previous_season_label(season: str) -> str | None:
    try:
        start = int(season[:4])
    except ValueError:
        return None
    end = season[-2:]
    try:
        end_num = int(end)
    except ValueError:
        return None
    return f"{start - 1}-{(end_num - 1) % 100:02d}"


def build_season_snapshots(conn, cfg: dict, season: str, *, season_start: str | None = None) -> dict:
    games = _load_games(conn, season)
    if not games and not _load_team_games(conn, season):
        return {"status": "pending_collection", "season": season, "snapshots": 0}
    start = season_start or (cfg.get("season_start") if season == cfg.get("target_season") else None)
    if start is None and games:
        start = min(game["game_date"] for game in games)
    prior_label = previous_season_label(season)
    prior_games = _load_games(conn, prior_label) if prior_label else []
    prior_team_games = _load_team_games(conn, prior_label) if prior_label else []
    from nba_mvp.eligibility import load_rules
    from nba_mvp.config import project_path

    rules = load_rules(project_path(cfg, "rules"))
    rule = rules.get(season)
    players = _load_players(conn)
    votes = _load_votes(conn, season)
    written = 0
    for snapshot_key in snapshot_keys_from_config(cfg):
        rows = build_snapshot_rows(
            season=season,
            snapshot_key=snapshot_key,
            games=games,
            team_games=_load_team_games(conn, season),
            prior_games=prior_games,
            prior_team_games=prior_team_games,
            votes=votes,
            players=players,
            min_minutes=float(cfg["candidate_min_minutes"]),
            rule=rule,
            season_start=start,
        )
        save_snapshots(conn, rows)
        written += 1
    return {"status": "ok", "season": season, "snapshots": written}


def load_snapshot_frame(conn, season: str, snapshot_key: str) -> list[dict]:
    rows = []
    query = "SELECT * FROM feature_snapshots WHERE season = ? AND snapshot_key = ?"
    for row in conn.execute(query, (season, snapshot_key)):
        item = dict(row)
        item["player_features"] = json.loads(item["player_features"] or "{}")
        item["team_features"] = json.loads(item["team_features"] or "{}")
        rows.append(item)
    return rows
