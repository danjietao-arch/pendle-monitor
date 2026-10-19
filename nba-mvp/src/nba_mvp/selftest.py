from __future__ import annotations

import json
import shutil
import traceback
from datetime import timedelta
from pathlib import Path

from nba_mvp.collect.nba_stats import _upsert_games, parse_player_gamelogs, parse_team_gamelogs
from nba_mvp.collect.official import (
    parse_boxscore_players,
    parse_schedule,
    parse_voter_ballot_text,
    parse_voting_results_text,
)
from nba_mvp.collect.votes import parse_vote_html
from nba_mvp.config import load_config, project_path
from nba_mvp.db import connect
from nba_mvp.eligibility import eligibility_for, load_rules
from nba_mvp.http_client import request_with_retry
from nba_mvp.jobs.hourly import run_hourly
from nba_mvp.market.compare import build_comparison
from nba_mvp.market.discover import classify_discovered, load_registry, validate_registry
from nba_mvp.market.quotes import classify_quote, persist_quote
from nba_mvp.model.baseline import ShrinkageNotFitted, fit_shrinkage_w, infer, shrink_rate, train_baselines
from nba_mvp.model.common import FEATURE_NAMES
from nba_mvp.model.montecarlo import run_monte_carlo
from nba_mvp.report.dashboard import DISCLAIMER, FICTIONAL_EXAMPLE, chart_segments, render_dashboard
from nba_mvp.report.export import persist_hourly_rows, read_alert_keys
from nba_mvp.snapshot import OTHER_ID, build_snapshot_rows, save_snapshots
from nba_mvp.util import DataError, filename_stamp, hour_stamp, isoformat, parse_time


def _game(player_id, team_id, game_id, game_date, pts, minutes=36):
    return {
        "player_id": player_id,
        "player_name": player_id,
        "team_id": team_id,
        "team_abbr": team_id,
        "game_id": game_id,
        "game_date": game_date,
        "minutes": minutes,
        "pts": pts,
        "reb": 5,
        "ast": 5,
        "stl": 1,
        "blk": 1,
        "tov": 2,
        "fgm": 10,
        "fga": 20,
        "ftm": 4,
        "fta": 5,
    }


def _team(team_id, game_id, game_date, win=1):
    return {"team_id": team_id, "team_abbr": team_id, "game_id": game_id, "game_date": game_date, "win": win, "points_for": 110, "points_against": 100}


def test_leakage():
    games = [
        _game("A", "T", "g1", "2098-11-01", 10),
        _game("A", "T", "g2", "2098-11-05", 100),
    ]
    teams = [_team("T", "g1", "2098-11-01"), _team("T", "g2", "2098-11-05")]
    prior = [_game("A", "T", "old", "2098-04-01", 7), _game("A", "T", "future", "2099-01-01", 99)]
    rows = build_snapshot_rows(
        season="2098-99",
        snapshot_key="tg_1",
        games=games,
        team_games=teams,
        prior_games=prior,
        votes=[{"player_id": "A", "is_winner": 1}],
        players={"A": {"player_id": "A", "player_name": "Alpha", "team_id": "T"}},
        min_minutes=0,
        rule={"season": "2098-99", "status": "unverified"},
        season_start="2098-10-20",
    )
    alpha = next(row for row in rows if row["player_id"] == "A")
    assert alpha["player_features"]["pts"] == 10
    assert alpha["player_features"]["game_ids"] == ["g1"]
    assert alpha["player_features"]["prior_pts_per_game"] == 7
    assert "g2" not in alpha["player_features"]["game_ids"]
    pre = build_snapshot_rows(
        season="2098-99",
        snapshot_key="preseason",
        games=games,
        team_games=teams,
        prior_games=prior,
        votes=[{"player_id": "A", "is_winner": 1}],
        players={"A": {"player_id": "A", "player_name": "Alpha", "team_id": "T"}},
        min_minutes=0,
        rule={"season": "2098-99", "status": "unverified"},
        season_start="2098-10-20",
    )
    alpha_pre = next(row for row in pre if row["player_id"] == "A")
    assert alpha_pre["player_features"]["game_ids"] == []
    assert alpha_pre["player_features"]["pts"] is None
    assert alpha_pre["games_played"] == 0
    assert alpha["team_features"]["prior_win_pct"] is None
    assert alpha_pre["team_features"]["prior_win_pct"] is None
    with_prior = build_snapshot_rows(
        season="2098-99",
        snapshot_key="tg_1",
        games=games,
        team_games=teams,
        prior_games=prior,
        prior_team_games=[
            _team("T", "old", "2098-04-01", win=1),
            _team("T", "old2", "2098-04-02", win=0),
            _team("T", "future-team", "2099-01-01", win=1),
        ],
        votes=[{"player_id": "A", "is_winner": 1}],
        players={"A": {"player_id": "A", "player_name": "Alpha", "team_id": "T"}},
        min_minutes=0,
        rule={"season": "2098-99", "status": "unverified"},
        season_start="2098-10-20",
    )
    alpha_prior = next(row for row in with_prior if row["player_id"] == "A")
    assert alpha_prior["team_features"]["prior_win_pct"] == 0.5
    no_history = build_snapshot_rows(
        season="2098-99",
        snapshot_key="preseason",
        games=games,
        team_games=teams,
        prior_games=[],
        votes=[{"player_id": "A", "is_winner": 1}],
        players={"A": {"player_id": "A", "player_name": "Alpha", "team_id": "T"}},
        min_minutes=0,
        rule={"season": "2098-99", "status": "unverified"},
        season_start="2098-10-20",
    )
    alpha_new = next(row for row in no_history if row["player_id"] == "A")
    assert alpha_new["player_features"]["prior_games"] is None
    assert alpha_new["player_features"]["prior_pts_per_game"] is None
    assert alpha_new["team_features"]["prior_win_pct"] is None


def test_trade_cutoff_does_not_use_future_team():
    games = [
        {**_game("A", "T1", "g1", "2098-11-01", 10), "source_timestamp": "2098-11-02T00:00:00Z"},
        {**_game("A", "T2", "g2", "2098-12-01", 100), "source_timestamp": "2098-12-02T00:00:00Z"},
    ]
    teams = [
        {**_team("T1", "g1", "2098-11-01"), "source_timestamp": "2098-11-02T00:00:00Z"},
        {**_team("T1", "g1b", "2098-11-03"), "source_timestamp": "2098-11-04T00:00:00Z"},
        {**_team("T2", "g2a", "2098-11-05"), "source_timestamp": "2098-11-06T00:00:00Z"},
        {**_team("T2", "g2", "2098-12-01"), "source_timestamp": "2098-12-02T00:00:00Z"},
    ]
    rows = build_snapshot_rows(
        season="2098-99",
        snapshot_key="tg_1",
        games=games,
        team_games=teams,
        prior_games=[],
        votes=[{"player_id": "A", "is_winner": 1}],
        players={"A": {"player_id": "A", "player_name": "Alpha", "team_id": "T2"}},
        min_minutes=0,
        rule={"season": "2098-99", "status": "unverified"},
        season_start="2098-10-20",
    )
    alpha = next(row for row in rows if row["player_id"] == "A")
    assert alpha["team_id"] == "T1"
    assert alpha["snapshot_date"] == "2098-11-01"
    assert alpha["player_features"]["pts"] == 10
    assert alpha["player_features"]["game_ids"] == ["g1"]
    assert alpha["team_games"] == 1
    assert alpha["source_timestamp"] == "2098-11-02T00:00:00Z"
    assert alpha["source_timestamp"] != "2098-12-02T00:00:00Z"


def test_unverified_eligibility_not_zero():
    rules = load_rules(project_path(load_config(), "rules"))
    result = eligibility_for(rules["2026-27"], games_played=1, official_eligible=None)
    assert result["force_award_zero"] is False
    assert result["p_eligibility"] is None
    verified = eligibility_for(rules["2025-26"], games_played=10, official_eligible=0)
    assert verified["force_award_zero"] is True
    assert verified["p_eligibility"] == 0


def test_official_vote_and_boxscore_parsers():
    results = parse_voting_results_text(
        "\n".join(
            [
                "Player, Team 1st Place Votes (10 points) 2nd Place Votes (7 points) Total Points",
                "Joel Embiid, Philadelphia 73 25 2 0 0 915",
                "Nikola Jokić, Denver 15 52 32 0 0 674",
            ]
        )
    )
    assert results[0]["player_name"] == "Joel Embiid"
    assert results[0]["is_winner"] == 1
    assert results[0]["vote_points"] == 915
    assert results[1]["vote_points"] == 674
    try:
        parse_voting_results_text("Joel Embiid, Philadelphia 73 25 2 0 0 1")
        raise AssertionError("票数与总分不一致时不应接受")
    except DataError:
        pass
    ballots = parse_voter_ballot_text(
        "David Aldridge, The Athletic"
        "    Shai Gilgeous-Alexander, Oklahoma City"
        "    Nikola Jokić, Denver"
        "    Giannis Antetokounmpo, Milwaukee"
        "    Jayson Tatum, Boston"
        "    Donovan Mitchell, Cleveland\n"
    )
    shai = next(row for row in ballots if row["player_name"] == "Shai Gilgeous-Alexander")
    jokic = next(row for row in ballots if row["player_name"] == "Nikola Jokić")
    assert shai["is_winner"] == 1
    assert shai["vote_points"] == 10
    assert shai["first_place_votes"] == 1
    assert jokic["vote_points"] == 7
    assert jokic["is_winner"] == 0
    westbrook = parse_voting_results_text("Westbrook, Russell (OKC.) 69 19 13 0 0 888\n")
    assert westbrook[0]["player_name"] == "Russell Westbrook"
    assert westbrook[0]["vote_points"] == 888
    jokic_line = parse_voting_results_text("Nikola Jokić (Denver) 91 8 1 0 0 971\n")
    assert jokic_line[0]["player_name"] == "Nikola Jokić"
    html = """
    <table>
      <tr><th>Player</th><th>1st</th><th>2nd</th><th>3rd</th><th>4th</th><th>5th</th><th>Points</th></tr>
      <tr><td>Stephen Curry</td><td>131</td><td>0</td><td>0</td><td>0</td><td>0</td><td>1,310</td></tr>
      <tr><td>Kawhi Leonard</td><td></td><td>1</td><td>0</td><td>0</td><td>0</td><td>7</td></tr>
      <tr><td>LeBron James</td><td>&#8212;</td><td>&#8212;</td><td>&#8212;</td><td>&#8212;</td><td>1</td><td>1</td></tr>
    </table>
    """
    from nba_mvp.collect.official import parse_mvp_results_html

    table = parse_mvp_results_html(html)
    names = [row["player_name"] for row in table]
    assert names == ["Stephen Curry", "LeBron James"]
    assert table[0]["vote_points"] == 1310
    assert next(row for row in table if row["player_name"] == "LeBron James")["first_place_votes"] == 0
    ballot = (
        "Albert, Marv         Turner                        Antetokounmpo, Giannis (MIL)"
        "        James, LeBron (LAL)       Harden, James (HOU) Leonard, Kawhi (LAC) Doncic, Luka (DAL)\n"
    )
    giannis_ballot = parse_voter_ballot_text(ballot)
    assert giannis_ballot[0]["player_name"] == "Giannis Antetokounmpo"
    assert all("Turner" not in row["player_name"] and "Albert" not in row["player_name"] for row in giannis_ballot)
    try:
        parse_voting_results_text("Alpha, Team 1 0 0 0 0 10\nBeta, Team 1 0 0 0 0 10\n")
        raise AssertionError("并列最高票不能猜获奖者")
    except DataError:
        pass
    schedule = parse_schedule(
        {
            "lscd": [
                {
                    "mscd": {
                        "g": [
                            {
                                "gid": "0022400001",
                                "gdte": "2024-10-22",
                                "stt": "Final",
                                "h": {"tid": 1610612738, "ta": "BOS", "s": "116"},
                                "v": {"tid": 1610612737, "ta": "ATL", "s": "100"},
                            },
                            {"gid": "0012400001", "gdte": "2024-10-10", "stt": "Final", "h": {}, "v": {}},
                        ]
                    }
                }
            ]
        }
    )
    assert len(schedule) == 1
    assert schedule[0]["home"]["win"] == 1
    stuck = parse_schedule(
        {
            "lscd": [
                {
                    "mscd": {
                        "g": [
                            {
                                "gid": "0021700211",
                                "gdte": "2017-11-15",
                                "stt": "1st Qtr",
                                "h": {"tid": 1610612740, "ta": "NOP", "s": "20"},
                                "v": {"tid": 1610612761, "ta": "TOR", "s": "18"},
                            }
                        ]
                    }
                }
            ]
        }
    )
    assert stuck[0]["schedule_final"] is False
    assert stuck[0]["home"]["win"] is None
    assert stuck[0]["home"]["points_for"] is None
    from nba_mvp.collect.official import merge_final_score

    merged = merge_final_score(
        stuck[0],
        {"gameStatus": 3, "homeTeam": {"teamId": 1610612740, "score": 116}, "awayTeam": {"teamId": 1610612761, "score": 125}},
    )
    assert merged["home"]["points_for"] == 116
    assert merged["away"]["win"] == 1
    assert merged["score_source"] == "boxscore"
    played, unparsed = parse_boxscore_players(
        {
            "gameId": "0022400001",
            "gameStatus": 3,
            "gameStatusText": "Final",
            "homeTeam": {
                "teamId": 1610612738,
                "teamTricode": "BOS",
                "players": [
                    {
                        "personId": 1627759,
                        "firstName": "Jaylen",
                        "familyName": "Brown",
                        "position": "F",
                        "comment": "",
                        "statistics": {
                            "minutes": "35:56",
                            "points": 37,
                            "reboundsTotal": 5,
                            "assists": 2,
                            "steals": 1,
                            "blocks": 0,
                            "turnovers": 6,
                            "fieldGoalsMade": 14,
                            "fieldGoalsAttempted": 22,
                            "freeThrowsMade": 6,
                            "freeThrowsAttempted": 10,
                        },
                    },
                    {
                        "personId": 204001,
                        "firstName": "Kristaps",
                        "familyName": "Porzingis",
                        "position": "",
                        "comment": "DNP - Injury",
                        "statistics": {"minutes": None},
                    },
                ],
            },
            "awayTeam": {"teamId": 1610612737, "teamTricode": "ATL", "players": []},
        },
        schedule[0],
    )
    assert unparsed == []
    assert len(played) == 1
    assert played[0]["player_id"] == "1627759"
    assert played[0]["game_date"] == "2024-10-22"
    assert played[0]["minutes"] == 35 + 56 / 60
    assert played[0]["opponent"] == "ATL"
    assert played[0]["started"] == 1
    from nba_mvp.stage1 import resolve_vote_player_id

    names = {
        "jimmy butler iii": ["202710"],
        "john butler jr.": ["1631219"],
        "jared butler": ["1630215"],
    }
    assert resolve_vote_player_id("Jimmy Butler", names) == "202710"
    try:
        resolve_vote_player_id("Chris Paul", names)
        raise AssertionError("没有对应球员时不应猜测")
    except DataError:
        pass


def test_stage1_archive_replay(tmp_path):
    from nba_mvp.stage1 import STAGE1_SEASONS, replay_stage1

    cfg = load_config()
    archive = project_path(cfg, "archive")
    assert (archive / "manifest.json").exists(), "缺少 Stage 1 原始存档"
    cfg = dict(cfg)
    cfg["paths"] = dict(cfg["paths"])
    cfg["paths"]["reports"] = str(tmp_path / "reports")
    conn = connect(tmp_path / "mvp.sqlite")
    try:
        summary = replay_stage1(cfg, conn, list(STAGE1_SEASONS))
        _assert_stage1_summary(conn, summary)
    finally:
        conn.close()


def _assert_stage1_summary(conn, summary):
    assert summary["model_status"] == "待训练"
    assert summary["target_season_probabilities"] == "待采集 / 待训练"
    assert summary["leakage_ok"] is True
    winners = {}
    for item in summary["seasons"]:
        assert item["schedule_games"] == 1230
        failed_ids = [game["game_id"] for game in item["failed_games"]]
        if item["season"] == "2024-25":
            assert failed_ids == ["0022401084"]
            team_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM team_results WHERE season = ? AND game_id = ?",
                ("2024-25", "0022401084"),
            ).fetchone()["n"]
            player_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM game_logs WHERE season = ? AND game_id = ?",
                ("2024-25", "0022401084"),
            ).fetchone()["n"]
            assert team_rows == 2
            assert player_rows == 0
        else:
            assert failed_ids == []
        assert item["unparsed_players"] == []
        assert item["votes"] == "ok"
        assert item["leakage_problems"] == []
        winners[item["season"]] = item["winner"]
        brief = item["snapshots_brief"]
        assert brief["preseason"]["award_winner"]["games_played"] == 0
        assert brief["preseason"]["award_winner"]["team_games"] == 0
        mid = brief["tg_10"]["award_winner"]
        end = brief["end"]["award_winner"]
        assert mid["team_games"] >= 10
        assert mid["team_games"] < end["team_games"]
        assert mid["source_timestamp"]
        assert end["source_timestamp"]
        assert mid["games_played"] < end["games_played"]
    assert winners == {
        "2022-23": "Joel Embiid",
        "2023-24": "Nikola Jokić",
        "2024-25": "Shai Gilgeous-Alexander",
    }


def test_vote_and_gamelog_parsers():
    root = project_path(load_config(), "fixtures")
    rows = parse_vote_html((root / "vote_sample.html").read_text(encoding="utf-8"))
    assert [row["player_name"] for row in rows] == ["Alpha Player", "Beta Player"]
    assert rows[0]["is_winner"] == 1
    try:
        parse_vote_html((root / "vote_garbage.html").read_text(encoding="utf-8"))
        raise AssertionError("垃圾页面不应解析成功")
    except DataError:
        pass
    try:
        parse_vote_html("<h2>Finals MVP</h2><table><tr><td>A</td><td>1</td></tr></table>")
        raise AssertionError("总决赛 MVP 不应解析")
    except DataError:
        pass
    payload = json.loads((root / "player_gamelogs_sample.json").read_text(encoding="utf-8"))
    parsed = parse_player_gamelogs(payload, fetched_on="2098-11-02")
    assert len(parsed) == 1
    assert parsed[0]["game_date"] == "2098-11-01"
    assert parsed[0]["minutes"] == 36
    assert parsed[0]["pts"] == 28
    empty = parse_player_gamelogs(
        {"resultSets": [{"name": "PlayerGameLogs", "headers": ["PLAYER_ID", "GAME_ID", "GAME_DATE"], "rowSet": []}]}
    )
    assert empty == []
    teams = parse_team_gamelogs(
        {
            "resultSets": [
                {
                    "name": "TeamGameLogs",
                    "headers": ["TEAM_ID", "TEAM_ABBREVIATION", "GAME_ID", "GAME_DATE", "WL", "PTS", "PLUS_MINUS"],
                    "rowSet": [["10", "ALP", "g1", "2098-11-01", "W", 110, None]],
                }
            ]
        }
    )
    assert teams[0]["win"] == 1
    assert teams[0]["points_against"] is None


def test_upsert_does_not_replace_with_null(tmp_path):
    conn = connect(tmp_path / "mvp.sqlite")
    meta = {"url": "https://stats.nba.com/example", "fetched_at": "2098-11-02T00:00:00Z"}
    _upsert_games(conn, "2098-99", [_game("A", "T", "g1", "2098-11-01", 10)], meta)
    blank = _game("A", "T", "g1", "2098-11-01", None)
    blank["pts"] = None
    _upsert_games(conn, "2098-99", [blank], meta)
    stored = conn.execute("SELECT pts FROM game_logs").fetchone()["pts"]
    assert stored == 10
    conn.close()


def test_quote_book_and_future_backfill(tmp_path):
    now = parse_time("2026-10-19T17:17:00Z")
    fresh = classify_quote(
        {
            "bids": [{"price": "0.16"}],
            "asks": [{"price": "0.18"}],
            "timestamp": str(int(now.timestamp() * 1000)),
            "last_trade_price": "0.10",
            "gamma_price": "0.99",
        },
        now=now,
    )
    assert abs(fresh["market_mid"] - 0.17) < 1e-9
    assert fresh["gamma_price"] == 0.99
    assert fresh["market_mid"] != fresh["gamma_price"]
    one_sided = classify_quote(
        {"bids": [{"price": "0.20"}], "asks": [], "gamma_price": "0.40", "last_trade_price": "0.40", "timestamp": isoformat(now)},
        now=now,
    )
    assert one_sided["market_mid"] is None
    assert one_sided["quote_status"] == "N/A"
    stale = classify_quote(
        {"bids": [{"price": "0.16"}], "asks": [{"price": "0.18"}], "timestamp": isoformat(now - timedelta(minutes=91))},
        now=now,
    )
    assert stale["quote_status"] == "STALE"
    assert stale["market_mid"] is None
    conn = connect(tmp_path / "mvp.sqlite")
    rejected = persist_quote(
        conn,
        "2026-10-19T15:00:00Z",
        "untrained",
        "m1",
        "A",
        {**fresh, "quote_timestamp": "2026-10-19T17:17:00Z"},
        source_url="https://clob.polymarket.com/book",
        fetched_at="2026-10-19T17:17:00Z",
    )
    assert rejected == "rejected_future"
    assert conn.execute("SELECT COUNT(*) AS n FROM market_quotes").fetchone()["n"] == 0
    first = persist_quote(
        conn,
        "2026-10-19T17:00:00Z",
        "untrained",
        "m1",
        "A",
        fresh,
        source_url="https://clob.polymarket.com/book",
        fetched_at=isoformat(now),
    )
    second = persist_quote(
        conn,
        "2026-10-19T17:00:00Z",
        "untrained",
        "m1",
        "A",
        fresh,
        source_url="https://clob.polymarket.com/book",
        fetched_at=isoformat(now),
    )
    assert first == "inserted"
    assert second == "exists"
    assert conn.execute("SELECT COUNT(*) AS n FROM market_quotes").fetchone()["n"] == 1
    conn.close()


def _comparison(hour, p_model, previous, keys, *, unprocessed=False, exhaustive=False, quote_status="ok", mid=0.17):
    quote = {
        "bid": 0.16,
        "ask": 0.18,
        "spread_pp": 2,
        "market_mid": mid,
        "quote_status": quote_status,
        "quote_timestamp": hour,
        "last_trade": 0.1,
        "gamma_price": 0.99,
    }
    if quote_status != "ok":
        quote["market_mid"] = None
    return build_comparison(
        as_of_hour=hour,
        comparison_as_of=hour,
        model_version="baseline-b-test",
        model_as_of="2099-04-01T00:00:00Z",
        predictions=[{"player_id": "A", "player_name": "Alpha", "team_abbr": "ALP", "p_award": p_model, "p_eligibility": None, "status": "ok"}],
        quotes_by_player={"A": quote},
        registry_by_player={"A": {"player_id": "A", "market_id": "m1"}},
        previous_by_player=previous,
        existing_alert_keys=keys,
        unprocessed_games=unprocessed,
        exhaustive=exhaustive,
        alert_abs_delta_pp=5,
        alert_max_spread_pp=3,
        alert_consecutive=2,
    )


def test_delta_alerts_and_normalization():
    hour1 = "2026-10-19T17:00:00Z"
    first = _comparison(hour1, 0.22, {}, set())
    assert first["alerts"] == []
    assert abs(first["rows"][0]["delta_pp"] - 5.0) < 1e-9
    assert first["rows"][0]["p_market_normalized"] is None
    assert first["coverage_status"] == "coverage_incomplete"
    hour2 = "2026-10-19T18:00:00Z"
    second = _comparison(hour2, 0.22, {"A": first["rows"][0]}, set())
    assert len(second["alerts"]) == 1
    assert second["alerts"][0]["message"].startswith("模型与市场分歧")
    assert "套利" not in second["alerts"][0]["message"]
    again = _comparison(hour2, 0.22, {"A": first["rows"][0]}, {second["alerts"][0]["ref_key"]})
    assert again["alerts"] == []
    hour3 = "2026-10-19T19:00:00Z"
    third = _comparison(hour3, 0.22, {"A": second["rows"][0]}, {second["alerts"][0]["ref_key"]})
    assert third["alerts"] == []
    hour4 = "2026-10-19T20:00:00Z"
    fourth = _comparison(hour4, 0.17, {"A": third["rows"][0]}, {second["alerts"][0]["ref_key"]})
    assert fourth["rows"][0]["condition_met"] == 0
    hour5 = "2026-10-19T21:00:00Z"
    fifth = _comparison(hour5, 0.22, {"A": fourth["rows"][0]}, {second["alerts"][0]["ref_key"]})
    assert fifth["alerts"] == []
    hour6 = "2026-10-19T22:00:00Z"
    sixth = _comparison(hour6, 0.22, {"A": fifth["rows"][0]}, {second["alerts"][0]["ref_key"]})
    assert len(sixth["alerts"]) == 1
    assert sixth["alerts"][0]["ref_key"] != second["alerts"][0]["ref_key"]
    stale_rows = _comparison(hour1, 0.22, {}, set(), unprocessed=True)
    assert stale_rows["rows"][0]["row_status"] == "STALE"
    assert stale_rows["rows"][0]["delta_pp"] is None
    assert stale_rows["data_status"] == "STALE"
    missing = _comparison(hour1, 0.22, {}, set(), quote_status="N/A")
    assert missing["rows"][0]["row_status"] == "N/A"
    assert missing["rows"][0]["p_market_mid"] is None
    both = build_comparison(
        as_of_hour=hour1,
        comparison_as_of=hour1,
        model_version="baseline-b-test",
        model_as_of=hour1,
        predictions=[
            {"player_id": "A", "player_name": "Alpha", "p_award": 0.4, "status": "ok"},
            {"player_id": "B", "player_name": "Beta", "p_award": 0.6, "status": "ok"},
        ],
        quotes_by_player={
            "A": {"bid": 0.2, "ask": 0.2, "spread_pp": 0, "market_mid": 0.2, "quote_status": "ok", "quote_timestamp": hour1},
            "B": {"bid": 0.3, "ask": 0.3, "spread_pp": 0, "market_mid": 0.3, "quote_status": "ok", "quote_timestamp": hour1},
        },
        registry_by_player={"A": {"player_id": "A", "market_id": "mA"}, "B": {"player_id": "B", "market_id": "mB"}},
        previous_by_player={},
        existing_alert_keys=set(),
        unprocessed_games=False,
        exhaustive=True,
        alert_abs_delta_pp=5,
        alert_max_spread_pp=3,
        alert_consecutive=2,
    )
    normalized = [row["p_market_normalized"] for row in both["rows"]]
    assert abs(sum(normalized) - 1) < 1e-9
    incomplete = build_comparison(
        as_of_hour=hour1,
        comparison_as_of=hour1,
        model_version="baseline-b-test",
        model_as_of=hour1,
        predictions=[{"player_id": "A", "player_name": "Alpha", "p_award": 0.4, "status": "ok"}],
        quotes_by_player={"A": {"quote_status": "N/A", "market_mid": None, "bid": 0.2, "ask": None}},
        registry_by_player={
            "A": {"player_id": "A", "market_id": "mA"},
            "B": {"player_id": "B", "market_id": "mB"},
        },
        previous_by_player={},
        existing_alert_keys=set(),
        unprocessed_games=False,
        exhaustive=True,
        alert_abs_delta_pp=5,
        alert_max_spread_pp=3,
        alert_consecutive=2,
    )
    assert incomplete["rows"][0]["p_market_normalized"] is None
    assert incomplete["coverage_status"] == "coverage_incomplete"


def test_markets_reject_finals(tmp_path):
    document = {
        "exhaustive": False,
        "markets": [
            {
                "event_id": "e1",
                "market_id": "bad",
                "yes_token_id": "t",
                "player_id": "A",
                "question": "Will A win the 2027 Finals MVP?",
                "resolution_text": "Finals MVP",
                "status": "active",
                "reviewed_at": "2026-10-19T00:00:00Z",
            }
        ],
    }
    accepted, errors = validate_registry(document, "2026-27")
    assert accepted == []
    assert errors[0]["reason"] == "wrong_award"
    status, reason = classify_discovered({"question": "Will A win the 2027 Finals MVP?"}, "2026-27")
    assert status == "rejected" and reason == "wrong_award"
    other = classify_discovered({"question": "Will A win the 2025-26 NBA MVP?"}, "2026-27")
    assert other == ("rejected", "wrong_season")
    pending = classify_discovered({"question": "Will A win the 2026-27 NBA Regular Season MVP?"}, "2026-27")
    assert pending == ("unmapped", "pending_review")
    cfg = _temp_config(tmp_path)
    registry = load_registry(cfg)
    assert registry["markets"] == []
    assert registry["exhaustive"] is False


def _temp_config(root: Path, **overrides):
    cfg = load_config()
    cfg["_root"] = str(root)
    cfg.update(overrides)
    for relative in ("data/db", "data/models", "data/reports/hourly", "data/raw", "data/registry", "data/rules"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    real = load_config()
    shutil.copy(project_path(real, "rules"), root / "data/rules/eligibility_rules.json")
    shutil.copy(project_path(real, "registry"), root / "data/registry/market_registry.json")
    return cfg


def _save_season(conn, season: str, pts_a: float, pts_b: float):
    start = f"{season[:4]}-11-01"
    later = f"{season[:4]}-11-05"
    end = f"{int(season[:4]) + 1}-04-10"
    games = [
        _game("A", "T1", f"{season}-a1", start, pts_a),
        _game("A", "T1", f"{season}-a2", end, pts_a + 2),
        _game("B", "T2", f"{season}-b1", later, pts_b),
        _game("B", "T2", f"{season}-b2", end, pts_b + 1),
    ]
    teams = [
        _team("T1", f"{season}-t1", start),
        _team("T1", f"{season}-t2", end),
        _team("T2", f"{season}-u1", later, win=0),
        _team("T2", f"{season}-u2", end, win=0),
    ]
    players = {
        "A": {"player_id": "A", "player_name": "Alpha", "team_id": "T1", "team_abbr": "T1"},
        "B": {"player_id": "B", "player_name": "Beta", "team_id": "T2", "team_abbr": "T2"},
    }
    votes = [{"player_id": "A", "is_winner": 1}, {"player_id": "B", "is_winner": 0}]
    for key in ("end", "tg_1"):
        rows = build_snapshot_rows(
            season=season,
            snapshot_key=key,
            games=games,
            team_games=teams,
            prior_games=[],
            votes=votes,
            players=players,
            min_minutes=0,
            rule={"season": season, "status": "unverified"},
            season_start=f"{season[:4]}-10-20",
        )
        assert any(row["player_id"] == OTHER_ID for row in rows)
        save_snapshots(conn, rows)


def test_probabilities_gate_and_sum(tmp_path):
    cfg = _temp_config(tmp_path, target_season="2099-00", min_historical_seasons=10)
    conn = connect(project_path(cfg, "db"))
    _save_season(conn, "2098-99", 30, 12)
    _save_season(conn, "2099-00", 28, 14)
    pending = train_baselines(conn, cfg)
    assert pending["status"] == "pending_training"
    hidden = infer(conn, cfg, "2099-00", snapshot_key="end")
    assert hidden["status"] == "pending_training"
    stored = conn.execute("SELECT p_award FROM predictions").fetchall()
    assert stored and all(row["p_award"] is None for row in stored)
    cfg["min_historical_seasons"] = 2
    trained = train_baselines(conn, cfg)
    assert trained["status"] == "ok"
    emitted = infer(conn, cfg, "2099-00", snapshot_key="end")
    assert emitted["status"] == "ok"
    probs = [row["p_award"] for row in conn.execute("SELECT p_award FROM predictions WHERE season='2099-00' AND snapshot_key='end'")]
    assert all(prob is not None for prob in probs)
    assert abs(sum(probs) - 1) < 0.001
    conn.execute(
        "UPDATE feature_snapshots SET eligibility_status='ineligible', official_eligible=0 WHERE player_id='B'"
    )
    infer(conn, cfg, "2099-00", snapshot_key="end")
    rows = {row["player_id"]: row["p_award"] for row in conn.execute("SELECT player_id, p_award FROM predictions WHERE snapshot_key='end' AND season='2099-00'")}
    assert rows["B"] == 0
    assert abs(sum(rows.values()) - 1) < 0.001
    cfg["polymarket"]["use_in_training"] = True
    refused = train_baselines(conn, cfg)
    assert refused["status"] == "refused"
    conn.close()


def test_shrinkage_and_monte_carlo():
    assert fit_shrinkage_w([{"season_avg": 1, "prior": 1, "final_avg": 1}]) is None
    try:
        shrink_rate(20, 10, None)
        raise AssertionError("未拟合的收缩权重不能使用")
    except ShrinkageNotFitted:
        pass
    assert shrink_rate(20, 10, 0.25) == 12.5
    cfg = load_config()
    assert cfg["monte_carlo"]["enabled"] is False
    disabled = run_monte_carlo(cfg, {"beats_baseline": True}, [{"player_id": "A"}], 0.5)
    assert disabled["status"] == "disabled"
    assert disabled["probabilities"] is None
    enabled = json.loads(json.dumps(cfg))
    enabled["monte_carlo"]["enabled"] = True
    still = run_monte_carlo(enabled, {"beats_baseline": False}, [{"player_id": "A", "season_avg": 20, "prior": 10, "remaining_games": 2, "attend_p": 1}], 0.5)
    assert still["status"] == "disabled"
    assert "market" not in ",".join(FEATURE_NAMES)
    assert "polymarket" not in ",".join(FEATURE_NAMES)


def test_dashboard_and_segments():
    assert chart_segments([0.2, None, 0.4]) == [[(0, 0.2)], [(2, 0.4)]]
    html = render_dashboard(
        rows=[{"player_id": "A", "player_name": "Alpha", "team_abbr": "ALP", "p_model": None, "p_market_mid": None, "bid": None, "ask": None, "delta_pp": None, "delta_vs_prev_hour_pp": None, "p_eligibility": None, "market_as_of": None, "model_as_of": None, "row_status": "pending_training"}],
        queue=[{"status": "unmapped", "question": "Will Alpha win MVP?"}, {"status": "rejected", "question": "Finals MVP", "reason": "wrong_award"}],
        job={"status": "success", "finished_at": "2026-10-19T17:20:00Z"},
        history=[],
        coverage=None,
        coverage_status="pending_training",
        data_status="ok",
        model_version="untrained",
        model_as_of=None,
        market_as_of=None,
        comparison_as_of="2026-10-19T17:17:00Z",
        last_success="2026-10-19T17:20:00Z",
    )
    assert FICTIONAL_EXAMPLE in html
    assert DISCLAIMER in html
    assert "未启用" in html
    assert "待训练" in html
    assert "套利" not in html
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    assert "虚构示例" not in body
    assert "0.0%" not in body
    assert ":" not in filename_stamp("2026-10-19T17:00:00Z")


def test_retry_and_hourly_idempotency(tmp_path):
    class Response:
        def __init__(self, status, payload, url):
            self.status_code = status
            self._payload = payload
            self.url = url
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    class Session:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, timeout=None, headers=None, params=None):
            self.calls += 1
            if self.calls < 3:
                return Response(503, {"error": True}, url)
            return Response(200, {"ok": True}, url)

    session = Session()
    response = request_with_retry(session, "GET", "https://example.test/book", retries=4, backoff=0)
    assert response.status_code == 200
    assert session.calls == 3
    cfg = _temp_config(tmp_path)
    before = (tmp_path / "data/registry/market_registry.json").read_text(encoding="utf-8")
    conn = connect(project_path(cfg, "db"))
    moment = parse_time("2026-10-19T17:17:00Z")
    assert hour_stamp(moment) == "2026-10-19T17:00:00Z"

    def search():
        return [
            {"id": "finals-1", "question": "Will X win the 2027 Finals MVP?", "description": "Finals MVP"},
            {"id": "rs-1", "question": "Will Alpha win the 2026-27 NBA Regular Season MVP?", "description": "regular season"},
        ]

    def nba(_conn):
        return {"status": "empty", "season": cfg["target_season"], "games": 0}

    first = run_hourly(cfg, conn, now=moment, search_fn=search, nba_fn=nba, book_fn=None)
    assert first["status"] == "success", first
    queued = {row["market_id"]: row["status"] for row in conn.execute("SELECT market_id, status FROM review_queue")}
    assert queued["finals-1"] == "rejected"
    assert queued["rs-1"] == "unmapped"
    assert conn.execute("SELECT COUNT(*) AS n FROM market_registry").fetchone()["n"] == 0
    assert (tmp_path / "data/registry/market_registry.json").read_text(encoding="utf-8") == before
    second = run_hourly(cfg, conn, now=moment, search_fn=search, nba_fn=nba)
    assert second["status"] == "already_done"
    assert read_alert_keys(cfg) == set()
    dashboard = (tmp_path / "data/reports/dashboard.html").read_text(encoding="utf-8")
    assert "待采集" in dashboard or "待训练" in dashboard
    assert DISCLAIMER in dashboard
    conn.close()


def test_hourly_rows_idempotent(tmp_path):
    conn = connect(tmp_path / "mvp.sqlite")
    row = {name: None for name in (
        "as_of_hour", "model_version", "player_id", "player_name", "team_abbr", "market_id",
        "model_as_of", "market_as_of", "comparison_as_of", "p_model", "p_market_mid", "p_market_normalized",
        "bid", "ask", "spread_pp", "last_trade", "gamma_price", "delta_pp", "delta_rel", "vs_bid_pp",
        "vs_ask_pp", "delta_vs_prev_hour_pp", "p_eligibility", "coverage", "coverage_status", "row_status",
        "reason", "condition_met", "streak_start", "streak_length",
    )}
    row.update({"as_of_hour": "2026-10-19T17:00:00Z", "model_version": "untrained", "player_id": "A", "row_status": "pending_training", "condition_met": 0, "streak_length": 0})
    assert persist_hourly_rows(conn, [row]) == 1
    assert persist_hourly_rows(conn, [row]) == 0
    conn.close()


def test_walk_forward_reproducible_and_not_worse():
    import math

    from nba_mvp.model.backtest import walk_forward
    from nba_mvp.model.common import FEATURE_NAMES, PRESEASON_FEATURE_NAMES, feature_vector, restrict_features

    def row(season, key, player, winner, pts, prior=20):
        preseason = key == "preseason"
        return {
            "season": season,
            "snapshot_key": key,
            "player_id": player,
            "award_winner": 1 if player == winner else 0,
            "eligibility_status": "unverified",
            "games_played": 0 if preseason else 40,
            "player_features": {
                "pts_per_game": None if preseason else pts,
                "reb_per_game": None if preseason else 8,
                "ast_per_game": None if preseason else 6,
                "stl_per_game": None if preseason else 1,
                "blk_per_game": None if preseason else 1,
                "tov_per_game": None if preseason else 3,
                "prior_pts_per_game": prior,
                "prior_games": 70,
            },
            "team_features": {
                "win_pct": None if preseason else 0.6,
                "prior_win_pct": 0.55,
            },
        }

    rows = []
    seasons = ["2010-11", "2011-12", "2012-13", "2013-14"]
    for index, season in enumerate(seasons):
        winner = "A" if index % 2 == 0 else "B"
        for key in ("preseason", "end"):
            for player in ("A", "B"):
                points = 32 if player == winner else 14
                rows.append(row(season, key, player, winner, points))
    masked = restrict_features([row("2013-14", "end", "A", "A", 32)], PRESEASON_FEATURE_NAMES)[0]
    vector = feature_vector(masked)
    assert math.isnan(vector[FEATURE_NAMES.index("pts_per_game")])
    assert math.isnan(vector[FEATURE_NAMES.index("win_pct")])
    assert vector[FEATURE_NAMES.index("prior_pts_per_game")] == 20
    assert vector[FEATURE_NAMES.index("games_played")] != vector[FEATURE_NAMES.index("games_played")]
    first = walk_forward(rows)
    second = walk_forward(rows)
    assert first == second
    assert first["baseline_a_features"] == list(PRESEASON_FEATURE_NAMES)
    assert first["not_worse_than_a"] is True, first["shortfall"]
    assert first["by_snapshot"]["end"]["log_loss_b"] < first["by_snapshot"]["end"]["log_loss_a"]
    assert "market" not in ",".join(first["feature_names"])


def test_stage2_archive_backtest(tmp_path):
    from nba_mvp.stage1 import DOCUMENTED_BLOCKERS, HISTORICAL_SEASONS
    from nba_mvp.stage2 import run_stage2_backtest

    real = load_config()
    archive = project_path(real, "archive")
    present = [season for season in HISTORICAL_SEASONS if (archive / "schedules" / f"{season}.json").exists()]
    assert "2014-15" not in present
    assert any(item["season"] == "2014-15" and item["error"] == "HTTP 403" for item in DOCUMENTED_BLOCKERS)
    if len(present) < len(HISTORICAL_SEASONS):
        missing = [season for season in HISTORICAL_SEASONS if season not in present]
        raise AssertionError(f"历史存档未齐: {missing}")
    cfg = _temp_config(tmp_path)
    cfg["paths"]["archive"] = str(archive)
    cfg["paths"]["reports"] = str(tmp_path / "reports")
    cfg["paths"]["models"] = str(tmp_path / "models")
    conn = connect(tmp_path / "stage2.sqlite")
    try:
        summary = run_stage2_backtest(cfg, conn)
    finally:
        conn.close()
    assert summary["leakage_ok"] is True
    assert summary["walk_forward_reproducible"] is True
    assert summary["not_worse_than_a"] is True or bool(summary["shortfall"])
    assert not any(summary["fabricated_player_rows"].values())
    assert summary["labeled_season_count"] <= len(HISTORICAL_SEASONS)
    if summary["labeled_season_count"] < summary["min_historical_seasons"]:
        assert summary["model_status"] == "待训练"
        assert summary["target_season_probabilities"] == "待训练"
    else:
        assert summary["target_season_probabilities"] == "未推断"
    conn = connect(tmp_path / "stage2.sqlite")
    try:
        stored = conn.execute(
            "SELECT COUNT(*) AS n FROM predictions WHERE season = ?",
            (cfg["target_season"],),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert stored == 0
    for season in ("2019-20", "2020-21"):
        if season in summary["schedule_games"]:
            assert summary["schedule_games"][season] < 1230
            assert summary["schedule_games"][season] > 0
    for season, count in summary["schedule_games"].items():
        if season not in {"2019-20", "2020-21"}:
            assert count == 1230
    plot = Path(summary["calibration_plot"])
    assert plot.exists()
    assert plot.read_text(encoding="utf-8").startswith("<svg")


def _box(game_id, team_id, player_id, points):
    return {
        "gameId": game_id,
        "gameStatus": 3,
        "gameStatusText": "Final",
        "homeTeam": {
            "teamId": team_id,
            "teamTricode": team_id,
            "score": 110,
            "players": [
                {
                    "personId": player_id,
                    "firstName": "Alpha",
                    "familyName": "Player",
                    "position": "F",
                    "comment": "",
                    "statistics": {
                        "minutes": "30:00",
                        "points": points,
                        "reboundsTotal": 5,
                        "assists": 5,
                        "steals": 1,
                        "blocks": 1,
                        "turnovers": 2,
                        "fieldGoalsMade": 10,
                        "fieldGoalsAttempted": 20,
                        "freeThrowsMade": 4,
                        "freeThrowsAttempted": 5,
                    },
                }
            ],
        },
        "awayTeam": {"teamId": "T2", "teamTricode": "T2", "score": 100, "players": []},
    }


def _scheduled(game_id, game_date):
    return {
        "game_id": game_id,
        "game_date": game_date,
        "schedule_final": True,
        "schedule_status": "Final",
        "home": {"team_id": "T1", "team_abbr": "T1", "points_for": 110, "points_against": 100, "win": 1},
        "away": {"team_id": "T2", "team_abbr": "T2", "points_for": 100, "points_against": 110, "win": 0},
    }


def test_stage4_monte_carlo_and_board():
    from nba_mvp.model.baseline import _fit_pipeline
    from nba_mvp.model.montecarlo import (
        earlier_season_rows,
        fit_shrinkage_schedule,
        games_still_needed,
        promotion_decision,
        shrinkage_weight,
        simulate_remainder,
        totals_as_of,
        with_extra_absences,
    )
    from nba_mvp.report.dashboard import render_research_board
    from nba_mvp.snapshot import OTHER_ID

    played = {"game_id": "past", "game_date": "2024-01-01", "pts": 10, "reb": 1, "ast": 1, "stl": 0, "blk": 0, "tov": 0, "fgm": 4, "fga": 8, "ftm": 2, "fta": 2, "minutes": 30}
    future = {"game_id": "future", "game_date": "2024-02-01", "pts": 80, "reb": 1, "ast": 1, "stl": 0, "blk": 0, "tov": 0, "fgm": 20, "fga": 30, "ftm": 10, "fta": 12, "minutes": 40}
    visible = totals_as_of([played, future], "2024-01-15")
    assert visible["games_played"] == 1
    assert visible["pts"] == 10
    assert earlier_season_rows(
        [{"season": "2098-99", "player_id": "A"}, {"season": "2099-00", "player_id": "A"}, {"season": "2100-01", "player_id": "A"}],
        "2099-00",
    ) == [{"season": "2098-99", "player_id": "A"}]
    samples = []
    for _ in range(10):
        samples.append({"games_played": 5, "season_avg": 30, "prior": 10, "final_avg": 12})
        samples.append({"games_played": 60, "season_avg": 30, "prior": 10, "final_avg": 28})
    schedule = fit_shrinkage_schedule(samples)
    assert schedule is not None
    assert shrinkage_weight(schedule, 5) < shrinkage_weight(schedule, 60)
    assert shrinkage_weight(schedule, 20) < 1
    rule = {"status": "verified", "min_games": 65, "stat_leaderboard_minimum_games": 58}
    assert games_still_needed(rule, 58) == 7
    assert games_still_needed({"status": "unverified", "min_games": None, "stat_leaderboard_minimum_games": 58}, 10) is None

    def labeled(player_id, winner, pts, season):
        return {
            "season": season,
            "snapshot_key": "end",
            "player_id": player_id,
            "award_winner": winner,
            "eligibility_status": "unknown",
            "player_features": {
                "pts_per_game": pts,
                "reb_per_game": 5,
                "ast_per_game": 5,
                "stl_per_game": 1,
                "blk_per_game": 1,
                "tov_per_game": 2,
                "prior_pts_per_game": pts,
                "prior_games": 70,
                "prior_win_pct": 0.6,
            },
            "team_features": {"win_pct": 0.7 if winner else 0.4, "prior_win_pct": 0.6},
            "games_played": 70,
        }

    train = []
    for season in ("2096-97", "2097-98"):
        train.append(labeled("A", 1, 30, season))
        train.append(labeled("B", 0, 12, season))
        train.append(labeled(OTHER_ID, 0, 8, season))
    model = _fit_pipeline(train)
    assert model is not None

    def player(player_id, pts, games, remaining, official=None):
        return {
            "player_id": player_id,
            "player_name": "Alpha" if player_id == "A" else player_id,
            "team_abbr": "T1" if player_id == "A" else "T2",
            "team_id": "T1" if player_id == "A" else "T2",
            "games_played": games,
            "pts_per_game": pts,
            "reb_per_game": 5,
            "ast_per_game": 5,
            "stl_per_game": 1,
            "blk_per_game": 1,
            "tov_per_game": 2,
            "prior_pts_per_game": 18,
            "prior_games": 60,
            "prior_win_pct": 0.55,
            "wins": 20,
            "team_games": max(games, 1),
            "win_pct": 0.6,
            "remaining_games": remaining,
            "remaining_team_games": remaining,
            "attend_p": 0.9,
            "official_eligible": official,
        }

    other = {"player_id": OTHER_ID, "player_name": "其他球员", "games_played": 0, "remaining_games": 0, "remaining_team_games": 0}
    roster = [player("A", 28, 30, 40), player("B", 14, 30, 40), other]
    first = simulate_remainder(roster, schedule, [model], {"status": "unverified", "min_games": None}, draws=30, seed=7)
    second = simulate_remainder(roster, schedule, [model], {"status": "unverified", "min_games": None}, draws=30, seed=7)
    assert first["status"] == "ok", first
    assert first["probabilities"] == second["probabilities"]
    assert first["zero_filled"] is False
    assert abs(sum(first["probabilities"].values()) - 1) < 1e-9
    assert OTHER_ID in first["probabilities"]
    assert first["p_eligibility"]["A"] is None
    assert first["p_games_threshold"]["A"] is None
    assert first["probabilities"]["A"] > 0
    verified = simulate_remainder(
        [player("A", 28, 60, 0), player("B", 14, 70, 0, official=0), other],
        schedule,
        [model],
        rule,
        draws=20,
        seed=3,
    )
    assert verified["status"] == "ok", verified
    assert verified["p_games_threshold"]["A"] == 0
    assert verified["p_eligibility"]["A"] is None
    assert verified["probabilities"]["A"] > 0
    assert verified["p_eligibility"]["B"] == 0
    assert verified["probabilities"]["B"] == 0
    missing = simulate_remainder(
        [{**player("A", 28, 30, 40), "remaining_games": None}, other],
        schedule,
        [model],
        None,
        draws=5,
        seed=1,
    )
    assert missing["status"] == "待采集"
    assert missing["probabilities"] is None
    assert with_extra_absences(roster, "A", 10)[0]["remaining_games"] == 30
    assert with_extra_absences([{**player("A", 28, 30, 40), "remaining_games": None}], "A", 10) is None
    held_back = promotion_decision([1.2, 1.1, 1.0], [0.4, 0.5, 0.45], [0.2, 0.2, 0.2], [0.05, 0.05, 0.05], synthetic=False)
    assert held_back["promoted"] is False
    assert held_back["primary"] == "baseline_b"
    promoted = promotion_decision([0.2, 0.2, 0.2], [0.8, 0.9, 1.0], [0.02, 0.02, 0.02], [0.2, 0.2, 0.2], synthetic=False)
    assert promoted["promoted"] is True
    assert promoted["primary"] == "monte_carlo"
    assert promotion_decision([0.1, 0.1, 0.1], [0.8, 0.8, 0.8], [0.01, 0.01, 0.01], [0.2, 0.2, 0.2], synthetic=True)["promoted"] is False
    disabled = run_monte_carlo(load_config(), {"beats_baseline": True, "mc_promoted": True}, [{"player_id": "A"}], 0.5)
    assert disabled["status"] == "disabled"
    assert disabled["probabilities"] is None
    board = render_research_board(
        live_rows=[{"player_id": "A", "player_name": "Alpha", "team_abbr": "T", "p_award": 0.5, "p_eligibility": None, "reasons": ["真实预测"]}],
        scenario_rows=[{"player_id": "A", "player_name": "Alpha", "team_abbr": "T", "p_award": 0.1, "p_eligibility": None, "reasons": ["再缺席 10 场", "这不是真实预测"]}],
        curve=[{"player_id": "A", "player_name": "Alpha", "values": [0.2, None, 0.4]}],
        data_status="ok",
        model_version="baseline_b",
        as_of="2025-01-15",
        season="2024-25 历史重放",
        gate={"primary": "baseline_b", "promoted": False, "reason": "默认仍是基线 B"},
    )
    live, scenario = board.split("data-kind='scenario'", 1)
    assert "真实预测" in live
    assert "再缺席 10 场" not in live
    assert "50.0%" in live
    assert "这不是真实预测" in scenario
    assert "10.0%" in scenario
    assert "50.0%" not in scenario
    stale = render_research_board(
        live_rows=[],
        scenario_rows=[],
        curve=[],
        data_status="STALE",
        model_version="baseline_b",
        as_of="2026-10-19",
        season="2026-27",
        gate={"primary": "baseline_b", "promoted": False, "reason": "待采集"},
        collection_note="HTTP 403",
    )
    assert "STALE" in stale and "待采集" in stale


def test_stage3_daily_incremental(tmp_path):
    from datetime import date, timedelta

    from nba_mvp.stage3 import WEEKLY_FIELDS, accept_injury_rows, projected_record_band, run_daily

    assert projected_record_band(None, 10, 5, 0.5, 0.5) is None
    band = projected_record_band(8, 10, 4, 0.6, 0.5)
    assert band["low"] >= 8
    assert band["high"] <= 12
    accepted, rejected = accept_injury_rows(
        [
            {"player_id": "A", "status": "out", "source_url": "https://official.nba.com/injury-report"},
            {"player_id": "B", "status": "out", "source_url": "https://rumor.example/mvp"},
        ]
    )
    assert [row["player_id"] for row in accepted] == ["A"]
    assert rejected[0]["reason"] == "未证实伤病，不入库，不置 0"
    monday = date(2099, 11, 1)
    while monday.weekday() != 0:
        monday += timedelta(days=1)
    second = monday + timedelta(days=7)
    later = second + timedelta(days=7)
    cfg = _temp_config(tmp_path, target_season="2099-00", min_historical_seasons=2, candidate_min_minutes=0)
    conn = connect(tmp_path / "stage3.sqlite")
    try:
        _save_season(conn, "2097-98", 30, 12)
        _save_season(conn, "2098-99", 28, 14)
        trained = train_baselines(conn, cfg)
        assert trained["status"] == "ok"
        first_game = monday.isoformat()
        second_game = second.isoformat()
        future = (later + timedelta(days=3)).isoformat()
        bundle = {
            "status": "ok",
            "schedule": [
                _scheduled("g-mon", first_game),
                _scheduled("g-next", second_game),
                _scheduled("g-future", future),
                _scheduled("g-missing", later.isoformat()),
            ],
            "boxes": {
                "g-mon": {"body": _box("g-mon", "T1", "A", 24), "url": "archive://g-mon", "fetched_at": "2099-11-02T00:00:00Z"},
                "g-next": {"body": _box("g-next", "T1", "A", 30), "url": "archive://g-next", "fetched_at": "2099-11-09T00:00:00Z"},
                "g-missing": {"body": None, "error": "HTTP 500", "url": "archive://g-missing"},
            },
        }
        first = run_daily(
            cfg,
            conn,
            as_of=first_game,
            season="2099-00",
            bundle=bundle,
            injury_rows=[{"player_id": "A", "status": "questionable", "source_url": "https://www.nba.com/injury"}],
        )
        assert first["status"] == "success", first
        assert first["new_game_ids"] == ["g-mon"]
        assert first["infer_status"] == "ok"
        assert first["weekly_rows"]
        assert first["zero_filled"] is False
        for row in first["weekly_rows"]:
            assert set(WEEKLY_FIELDS).issubset(row)
            assert row["delta_pp_vs_last_week"] is None
        daily_payload = json.loads((tmp_path / "data/reports/daily/2099-00" / f"{first_game}.json").read_text(encoding="utf-8"))
        assert daily_payload["curve"]
        assert set(WEEKLY_FIELDS).issubset(daily_payload["curve"][0])
        assert conn.execute("SELECT COUNT(*) AS n FROM game_logs WHERE game_id = 'g-future'").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM game_logs WHERE game_id = 'g-next'").fetchone()["n"] == 0
        logs_before = (tmp_path / "data/reports/daily/run_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
        games_before = conn.execute("SELECT COUNT(*) AS n FROM game_logs WHERE season = '2099-00'").fetchone()["n"]
        again = run_daily(cfg, conn, as_of=first_game, season="2099-00", bundle=bundle)
        assert again["status"] == "already_done"
        assert conn.execute("SELECT COUNT(*) AS n FROM game_logs WHERE season = '2099-00'").fetchone()["n"] == games_before
        logs_after = (tmp_path / "data/reports/daily/run_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert logs_after == logs_before
        assert conn.execute("SELECT COUNT(*) AS n FROM market_quotes").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM hourly_snapshots").fetchone()["n"] == 0
        second_run = run_daily(cfg, conn, as_of=second_game, season="2099-00", bundle=bundle)
        assert second_run["status"] == "success"
        assert second_run["new_game_ids"] == ["g-next"]
        deltas = [row["delta_pp_vs_last_week"] for row in second_run["weekly_rows"] if row["player_id"] == "A"]
        assert deltas and deltas[0] is not None
        award = conn.execute(
            "SELECT p_award, p_eligibility FROM predictions WHERE season = '2099-00' AND player_id = 'A' AND snapshot_key = 'end'"
        ).fetchone()
        assert award["p_award"] is not None
        assert award["p_eligibility"] is None
        injury = conn.execute("SELECT status FROM injury_reports WHERE player_id = 'B'").fetchone()
        assert injury is None
        missing = run_daily(cfg, conn, as_of=later.isoformat(), season="2099-00", bundle=bundle)
        assert missing["status"] == "success"
        assert missing["data_status"] == "partial"
        assert conn.execute("SELECT COUNT(*) AS n FROM game_logs WHERE game_id = 'g-missing'").fetchone()["n"] == 0
        team_rows = conn.execute("SELECT win, points_for FROM team_results WHERE game_id = 'g-missing'").fetchall()
        assert len(team_rows) == 2
        assert all(row["win"] is not None and row["points_for"] != 0 for row in team_rows)
        kept = conn.execute("SELECT COUNT(*) AS n FROM game_logs WHERE season = '2099-00'").fetchone()["n"]
        stale = run_daily(
            cfg,
            conn,
            as_of=(later + timedelta(days=1)).isoformat(),
            season="2099-00",
            source="live",
            live_fetch=lambda _season: {"status": "待采集", "error": "HTTP 403", "schedule": None, "boxes": {}},
        )
        assert stale["data_status"] == "STALE"
        assert stale["collection_status"] == "待采集"
        assert conn.execute("SELECT COUNT(*) AS n FROM game_logs WHERE season = '2099-00'").fetchone()["n"] == kept
        board = (tmp_path / "data/reports/stage3_board.html").read_text(encoding="utf-8")
        assert "待采集" in board
        assert "STALE" in board
        assert "Alpha Player" in board
        stale_again = run_daily(
            cfg,
            conn,
            as_of=(later + timedelta(days=1)).isoformat(),
            season="2099-00",
            source="live",
            live_fetch=lambda _season: {"status": "待采集", "error": "HTTP 403", "schedule": None, "boxes": {}},
        )
        assert stale_again["status"] == "already_done"
        assert conn.execute("SELECT COUNT(*) AS n FROM game_logs WHERE season = '2099-00'").fetchone()["n"] == kept
    finally:
        conn.close()


def main() -> int:
    import tempfile

    tests = [
        test_leakage,
        test_trade_cutoff_does_not_use_future_team,
        test_unverified_eligibility_not_zero,
        test_official_vote_and_boxscore_parsers,
        test_stage1_archive_replay,
        test_walk_forward_reproducible_and_not_worse,
        test_stage2_archive_backtest,
        test_stage3_daily_incremental,
        test_stage4_monte_carlo_and_board,
        test_vote_and_gamelog_parsers,
        test_quote_book_and_future_backfill,
        test_delta_alerts_and_normalization,
        test_markets_reject_finals,
        test_probabilities_gate_and_sum,
        test_shrinkage_and_monte_carlo,
        test_dashboard_and_segments,
        test_retry_and_hourly_idempotency,
        test_hourly_rows_idempotent,
        test_upsert_does_not_replace_with_null,
    ]
    failed = 0
    for test in tests:
        try:
            if test in {
                test_quote_book_and_future_backfill,
                test_markets_reject_finals,
                test_probabilities_gate_and_sum,
                test_retry_and_hourly_idempotency,
                test_hourly_rows_idempotent,
                test_upsert_does_not_replace_with_null,
                test_stage1_archive_replay,
                test_stage2_archive_backtest,
                test_stage3_daily_incremental,
            }:
                with tempfile.TemporaryDirectory() as folder:
                    test(Path(folder))
            else:
                test()
            print(f"OK  {test.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0
