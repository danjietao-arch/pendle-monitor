"""Stage 4 remainder simulation and the research board.

Baseline B stays the default prediction unless the walk-forward gate promotes
Monte Carlo. JSON and CSV are the source of truth; the HTML page only displays them.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from nba_mvp.config import project_path
from nba_mvp.eligibility import load_rules
from nba_mvp.model.backtest import _scores_for, _winner
from nba_mvp.model.baseline import _fit_pipeline
from nba_mvp.model.montecarlo import (
    attend_probability,
    earlier_season_rows,
    fit_shrinkage_schedule,
    games_still_needed,
    promotion_decision,
    score_fold,
    simulate_remainder,
    with_extra_absences,
)
from nba_mvp.report.dashboard import DISCLAIMER, render_research_board
from nba_mvp.report.export import reports_dir, write_json, write_text
from nba_mvp.snapshot import OTHER_ID, load_snapshot_frame
from nba_mvp.util import is_synthetic_season, season_start_year

GATE_KEYS = ("tg_40",)
DEMO_KEYS = ("preseason", "tg_10", "tg_20", "tg_40", "tg_60", "end")
CANDIDATE_LIMIT = 25


def shrinkage_samples(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        if row["player_id"] == OTHER_ID:
            continue
        grouped.setdefault((row["season"], row["player_id"]), {})[row["snapshot_key"]] = row
    samples = []
    for snapshots in grouped.values():
        final = snapshots.get("end")
        if not final:
            continue
        final_avg = (final.get("player_features") or {}).get("pts_per_game")
        for key, early in snapshots.items():
            if key in {"end", "preseason"}:
                continue
            features = early.get("player_features") or {}
            samples.append(
                {
                    "games_played": early.get("games_played"),
                    "season_avg": features.get("pts_per_game"),
                    "prior": features.get("prior_pts_per_game"),
                    "final_avg": final_avg,
                }
            )
    return samples


def fit_award_models(rows: list[dict], *, n_models: int, seed: int) -> list[dict]:
    models = []
    base = _fit_pipeline(rows)
    if base:
        models.append(base)
    if len(rows) < 2 or n_models <= 1:
        return models
    rng = np.random.default_rng(seed)
    for _ in range(n_models - 1):
        index = rng.integers(0, len(rows), size=len(rows))
        artifact = _fit_pipeline([rows[int(item)] for item in index])
        if artifact:
            models.append(artifact)
    return models


def _team_known(schedule: list[dict]) -> set[str]:
    known = set()
    for game in schedule:
        known.add(str(game["home"]["team_id"]))
        known.add(str(game["away"]["team_id"]))
    return known


def remaining_on_schedule(schedule: list[dict] | None, team_id: str | None, as_of: str | None) -> int | None:
    if schedule is None or not team_id or not as_of:
        return None
    if str(team_id) not in _team_known(schedule):
        return None
    count = 0
    for game in schedule:
        if game["game_date"] <= as_of:
            continue
        if str(team_id) in {str(game["home"]["team_id"]), str(game["away"]["team_id"])}:
            count += 1
    return count


def state_from_row(row: dict, schedule: list[dict] | None) -> dict:
    features = row.get("player_features") or {}
    team = row.get("team_features") or {}
    player_id = row["player_id"]
    as_of = row.get("snapshot_date")
    team_id = row.get("team_id")
    if player_id == OTHER_ID:
        remaining = 0
        attend = None
    else:
        remaining = remaining_on_schedule(schedule, str(team_id) if team_id else None, as_of)
        attend = attend_probability(row.get("games_played"), team.get("team_games"), row.get("injury_status"))
    return {
        "player_id": player_id,
        "player_name": row.get("player_name"),
        "team_abbr": row.get("team_abbr"),
        "team_id": team_id,
        "games_played": row.get("games_played"),
        "pts_per_game": features.get("pts_per_game"),
        "reb_per_game": features.get("reb_per_game"),
        "ast_per_game": features.get("ast_per_game"),
        "stl_per_game": features.get("stl_per_game"),
        "blk_per_game": features.get("blk_per_game"),
        "tov_per_game": features.get("tov_per_game"),
        "prior_pts_per_game": features.get("prior_pts_per_game"),
        "prior_games": features.get("prior_games"),
        "prior_win_pct": team.get("prior_win_pct"),
        "wins": team.get("wins"),
        "team_games": team.get("team_games"),
        "win_pct": team.get("win_pct"),
        "remaining_games": remaining,
        "remaining_team_games": remaining,
        "attend_p": attend,
        "official_eligible": row.get("official_eligible"),
        "injury_status": row.get("injury_status"),
        "eligibility_status": row.get("eligibility_status"),
        "as_of": as_of,
    }


def _focus_rows(rows: list[dict]) -> list[dict]:
    winner = _winner(rows)
    other = [row for row in rows if row["player_id"] == OTHER_ID]
    named = [row for row in rows if row["player_id"] != OTHER_ID]
    named.sort(key=lambda row: float((row.get("player_features") or {}).get("pts_per_game") or -1), reverse=True)
    chosen = named[:CANDIDATE_LIMIT]
    if winner and winner != OTHER_ID and all(row["player_id"] != winner for row in chosen):
        match = next((row for row in named if row["player_id"] == winner), None)
        if match:
            chosen.append(match)
    return chosen + other


def _display_rows(rows: list[dict], probabilities: dict | None, simulation: dict | None, *, reasons: list[str]) -> list[dict]:
    simulation = simulation or {}
    bands = simulation.get("record_bands") or {}
    eligibility = simulation.get("p_eligibility") or {}
    reached = simulation.get("p_games_threshold") or {}
    needed = simulation.get("games_needed") or {}
    displayed = []
    for row in rows:
        player_id = row["player_id"]
        team_id = str(row.get("team_id") or "")
        band = bands.get(team_id)
        band_text = None if not band else f"{band['p10']:.0f}–{band['p90']:.0f}"
        probability = None if not probabilities else probabilities.get(player_id)
        displayed.append(
            {
                "player_id": player_id,
                "player_name": row.get("player_name"),
                "team_abbr": row.get("team_abbr"),
                "p_award": probability,
                "p_eligibility": eligibility.get(player_id) if simulation else row.get("p_eligibility"),
                "p_games_threshold": reached.get(player_id),
                "games_needed": needed.get(player_id, games_still_needed(None, None)),
                "record_band": band_text,
                "reasons": reasons,
            }
        )
    displayed.sort(key=lambda item: -1 if item["p_award"] is None else -float(item["p_award"]))
    return displayed


def _curve(train_by_key: dict[str, list[dict]], season_rows: dict[str, list[dict]], player_ids: list[str]) -> list[dict]:
    points = {player_id: [] for player_id in player_ids}
    labels = []
    names = {}
    for key in DEMO_KEYS:
        group = _focus_rows(season_rows.get(key) or [])
        for row in group:
            names.setdefault(row["player_id"], row.get("player_name"))
        labels.append(key)
        probs = _scores_for(train_by_key.get(key) or [], group) if group else None
        for player_id in player_ids:
            value = None if not probs or player_id not in probs else probs[player_id]
            points[player_id].append(value)
    return [
        {"player_id": player_id, "player_name": names.get(player_id) or player_id, "label": "时间线", "values": values, "labels": labels}
        for player_id, values in points.items()
        if any(value is not None for value in values)
    ]


def _gate_once(frames, schedules, rules, *, draws: int, seed: int, bootstrap: int) -> dict:
    seasons = sorted({season for season, _key in frames}, key=season_start_year)
    mc_loss, b_loss, mc_brier, b_brier = [], [], [], []
    folds = []
    synthetic = any(is_synthetic_season(season) for season in seasons)
    for season in seasons:
        test_rows = frames.get((season, "tg_40")) or []
        if not test_rows:
            continue
        train = []
        for (other_season, key), rows in frames.items():
            if season_start_year(other_season) < season_start_year(season):
                train.extend(rows if key == "end" or key == "tg_40" else [])
        end_train = [row for row in train if row["snapshot_key"] == "end"]
        same_key = [row for row in train if row["snapshot_key"] == "tg_40"]
        focused = _focus_rows(test_rows)
        shrinkage = fit_shrinkage_schedule(shrinkage_samples(earlier_season_rows(
            [row for (_season, key), rows in frames.items() if key != "preseason" for row in rows],
            season,
        )))
        models = fit_award_models(end_train, n_models=bootstrap, seed=seed)
        states = [state_from_row(row, schedules.get(season)) for row in focused]
        simulated = simulate_remainder(states, shrinkage, models, rules.get(season), draws=draws, seed=seed)
        baseline = _scores_for(same_key, focused) if same_key else None
        winner = _winner(focused)
        mc_scored = score_fold(simulated.get("probabilities"), winner)
        b_scored = score_fold(baseline, winner)
        fold = {
            "season": season,
            "snapshot_key": "tg_40",
            "monte_carlo_status": simulated.get("status"),
            "baseline_scored": b_scored is not None,
            "monte_carlo_scored": mc_scored is not None,
        }
        if mc_scored and b_scored:
            mc_loss.append(mc_scored["log_loss"])
            b_loss.append(b_scored["log_loss"])
            mc_brier.append(mc_scored["brier"])
            b_brier.append(b_scored["brier"])
            fold["monte_carlo"] = mc_scored
            fold["baseline_b"] = b_scored
        folds.append(fold)
    decision = promotion_decision(mc_loss, b_loss, mc_brier, b_brier, synthetic=synthetic)
    decision["seed"] = seed
    decision["draws"] = draws
    return {"folds": folds, "decision": decision, "shrinkage_form": "games/(games+k)"}


def evaluate_gate(
    frames: dict[tuple[str, str], list[dict]],
    schedules: dict[str, list[dict] | None],
    rules: dict[str, dict],
    *,
    draws: int,
    seed: int,
    bootstrap: int,
) -> dict:
    """Promote only when two seeds of at least 200 draws both beat Baseline B."""
    first = _gate_once(frames, schedules, rules, draws=draws, seed=seed, bootstrap=bootstrap)
    decision = first["decision"]
    if draws < 200:
        decision["point_estimate_promoted"] = decision["promoted"]
        decision["promoted"] = False
        decision["primary"] = "baseline_b"
        decision["reason"] = "模拟次数少于 200，胜者频率太粗，不能升格。默认仍是基线 B。"
        return first
    confirm = _gate_once(frames, schedules, rules, draws=draws, seed=seed + 1, bootstrap=bootstrap)
    decision["confirm_seed"] = seed + 1
    decision["confirm_promoted"] = confirm["decision"]["promoted"]
    decision["confirm_mc_log_loss"] = confirm["decision"]["mc_log_loss"]
    decision["confirm_baseline_log_loss"] = confirm["decision"]["baseline_log_loss"]
    decision["point_estimate_promoted"] = decision["promoted"]
    if not (decision["promoted"] and confirm["decision"]["promoted"]):
        decision["promoted"] = False
        decision["primary"] = "baseline_b"
        decision["reason"] = "两个随机种子没有同时在校准和稳定性上优于基线 B。默认仍是基线 B。"
    return first


def demo_season(
    frames: dict[tuple[str, str], list[dict]],
    schedules: dict[str, list[dict] | None],
    rules: dict[str, dict],
    season: str,
    *,
    draws: int,
    seed: int,
    bootstrap: int,
    gate: dict,
) -> dict | None:
    current = frames.get((season, "tg_40"))
    if not current:
        return None
    focused = _focus_rows(current)
    earlier = []
    train_by_key: dict[str, list[dict]] = {}
    season_rows = {key: frames.get((season, key)) or [] for key in DEMO_KEYS}
    for (other_season, key), rows in frames.items():
        if season_start_year(other_season) >= season_start_year(season):
            continue
        earlier.extend(rows)
        train_by_key.setdefault(key, []).extend(rows)
    shrinkage = fit_shrinkage_schedule(shrinkage_samples(earlier))
    models = fit_award_models([row for row in earlier if row["snapshot_key"] == "end"], n_models=bootstrap, seed=seed)
    states = [state_from_row(row, schedules.get(season)) for row in focused]
    simulated = simulate_remainder(states, shrinkage, models, rules.get(season), draws=draws, seed=seed)
    baseline = _scores_for(train_by_key.get("tg_40") or [], focused)
    as_of = max((row.get("snapshot_date") or "" for row in focused), default="")
    live_reasons = ["数据", "基线 B", "真实预测"]
    if not gate.get("promoted"):
        live_reasons.append("蒙特卡洛未升格")
    live_rows = _display_rows(focused, baseline, None, reasons=live_reasons)
    for row in live_rows:
        row["p_eligibility"] = next((item.get("p_eligibility") for item in focused if item["player_id"] == row["player_id"]), None)
        row["games_needed"] = games_still_needed(rules.get(season), next((item.get("games_played") for item in focused if item["player_id"] == row["player_id"]), None))
    mc_reasons = ["数据", "球队", "健康", "实验性蒙特卡洛", "不是默认预测"]
    scenario_source = simulated if simulated.get("status") == "ok" else None
    scenario_rows = _display_rows(focused, (scenario_source or {}).get("probabilities"), scenario_source, reasons=mc_reasons)
    target = next((row["player_id"] for row in live_rows if row["player_id"] != OTHER_ID and row.get("p_award") is not None), None)
    missed = with_extra_absences(states, target, 10) if target else None
    if missed:
        missed_sim = simulate_remainder(missed, shrinkage, models, rules.get(season), draws=draws, seed=seed + 1)
        if missed_sim.get("status") == "ok":
            for row in scenario_rows:
                if row["player_id"] == target:
                    row["reasons"] = mc_reasons + ["假设情景：再缺席 10 场，这不是真实预测"]
                    row["scenario_p_award"] = missed_sim["probabilities"].get(target)
    top_ids = [row["player_id"] for row in live_rows if row.get("p_award") is not None][:4]
    return {
        "season": season,
        "as_of": as_of,
        "snapshot_key": "tg_40",
        "shrinkage": shrinkage,
        "simulation_status": simulated.get("status"),
        "simulation_reason": simulated.get("reason"),
        "live_rows": live_rows,
        "scenario_rows": scenario_rows,
        "curve": _curve(train_by_key, season_rows, top_ids),
        "candidate_note": f"展示和门禁都只用得分最高的 {CANDIDATE_LIMIT} 人、当季赢家和其他球员。这不是「20 场后相信当季」。",
    }


def _load_frames(conn, seasons: list[str]) -> dict[tuple[str, str], list[dict]]:
    frames = {}
    for season in seasons:
        for key in DEMO_KEYS:
            rows = load_snapshot_frame(conn, season, key)
            if rows:
                frames[(season, key)] = rows
    return frames


def _load_schedule(cfg: dict, season: str) -> list[dict] | None:
    from nba_mvp.collect.official import parse_schedule
    from nba_mvp.stage1 import archive_root
    from nba_mvp.util import DataError

    path = archive_root(cfg) / "schedules" / f"{season}.json"
    if not path.exists():
        return None
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != 200 or not payload.get("body"):
        return None
    try:
        return parse_schedule(payload["body"])
    except DataError:
        return None


def _write_csv(path: Path, live_rows: list[dict], scenario_rows: list[dict], *, season: str, as_of: str, model_version: str, primary: str) -> None:
    scenario = {row["player_id"]: row for row in scenario_rows}
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "season",
        "as_of",
        "player_id",
        "player_name",
        "team_abbr",
        "p_baseline",
        "p_monte_carlo",
        "p_scenario_miss10",
        "p_eligibility",
        "p_games_threshold",
        "games_needed",
        "record_band",
        "model_version",
        "primary",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in live_rows:
            other = scenario.get(row["player_id"], {})
            writer.writerow(
                {
                    "season": season,
                    "as_of": as_of,
                    "player_id": row["player_id"],
                    "player_name": row.get("player_name"),
                    "team_abbr": row.get("team_abbr"),
                    "p_baseline": row.get("p_award"),
                    "p_monte_carlo": other.get("p_award"),
                    "p_scenario_miss10": other.get("scenario_p_award"),
                    "p_eligibility": row.get("p_eligibility"),
                    "p_games_threshold": other.get("p_games_threshold"),
                    "games_needed": row.get("games_needed"),
                    "record_band": other.get("record_band"),
                    "model_version": model_version,
                    "primary": primary,
                }
            )


def _live_note(cfg: dict) -> str:
    from nba_mvp.stage3 import fetch_live_schedule, probe_injury_source

    season = cfg["target_season"]
    schedule = fetch_live_schedule(season)
    injury = probe_injury_source(cfg.get("season_start") or "2026-10-20")
    return (
        f"{season} 赛程 {schedule.get('error') or schedule.get('status')}；"
        f"伤病 {injury.get('status')} {injury.get('error') or ''}"
    ).strip()


def run_stage4(cfg: dict, conn, *, draws: int = 40, seed: int = 0, bootstrap: int = 3, demo: str = "2024-25", probe_live: bool = False) -> dict:
    rules = load_rules(project_path(cfg, "rules"))
    seasons = sorted({row["season"] for row in conn.execute("SELECT DISTINCT season FROM feature_snapshots")})
    frames = _load_frames(conn, seasons)
    if not frames:
        summary = {
            "status": "待采集",
            "primary": "baseline_b",
            "promoted": False,
            "reason": "没有历史快照，不能模拟",
            "zero_filled": False,
        }
        _write_blocked(cfg, summary, probe_live)
        return summary
    schedules = {season: _load_schedule(cfg, season) for season in {item[0] for item in frames}}
    gate = evaluate_gate(frames, schedules, rules, draws=draws, seed=seed, bootstrap=bootstrap)
    demo_result = demo_season(frames, schedules, rules, demo, draws=draws, seed=seed, bootstrap=bootstrap, gate=gate["decision"])
    live_text = _live_note(cfg) if probe_live else f"{cfg['target_season']} 未在这次命令里抓取；没有赛程存档时日更会标待采集。"
    summary = {
        "stage": 4,
        "status": "ok" if demo_result else "待采集",
        "primary": gate["decision"]["primary"],
        "promoted": gate["decision"]["promoted"],
        "reason": gate["decision"]["reason"],
        "gate": gate["decision"],
        "draws": draws,
        "seed": seed,
        "folds": gate["folds"],
        "demo": None if not demo_result else {
            "season": demo_result["season"],
            "as_of": demo_result["as_of"],
            "snapshot_key": demo_result["snapshot_key"],
            "simulation_status": demo_result["simulation_status"],
            "simulation_reason": demo_result["simulation_reason"],
            "shrinkage": demo_result["shrinkage"],
            "candidate_note": demo_result["candidate_note"],
        },
        "live_target": live_text,
        "disclaimer": DISCLAIMER,
        "zero_filled": False,
        "config_monte_carlo_enabled": bool(cfg.get("monte_carlo", {}).get("enabled", False)),
    }
    folder = reports_dir(cfg)
    write_json(folder / "stage4_monte_carlo.json", summary)
    board_rows = [] if not demo_result else demo_result["live_rows"][:12]
    scenario_rows = [] if not demo_result else demo_result["scenario_rows"][:12]
    if demo_result:
        _write_csv(
            folder / "stage4_probabilities.csv",
            demo_result["live_rows"],
            demo_result["scenario_rows"],
            season=demo_result["season"],
            as_of=demo_result["as_of"],
            model_version="baseline_b" if not gate["decision"]["promoted"] else "monte_carlo",
            primary=gate["decision"]["primary"],
        )
        html = render_research_board(
            live_rows=board_rows,
            scenario_rows=scenario_rows,
            curve=demo_result["curve"],
            data_status="ok",
            model_version="baseline_b" if not gate["decision"]["promoted"] else "monte_carlo",
            as_of=str(demo_result["as_of"]),
            season=f"{demo_result['season']} 历史重放",
            gate=gate["decision"],
            collection_note=live_text,
        )
    else:
        html = render_research_board(
            live_rows=[],
            scenario_rows=[],
            curve=[],
            data_status="STALE",
            model_version="baseline_b",
            as_of="",
            season=cfg["target_season"],
            gate=gate["decision"],
            collection_note=live_text,
        )
    board = folder / "stage4_board.html"
    write_text(board, html)
    summary["board"] = str(board)
    summary["report"] = str(folder / "stage4_monte_carlo.json")
    return summary


def _write_blocked(cfg: dict, summary: dict, probe_live: bool) -> None:
    note = _live_note(cfg) if probe_live else summary["reason"]
    html = render_research_board(
        live_rows=[],
        scenario_rows=[],
        curve=[],
        data_status="STALE",
        model_version="baseline_b",
        as_of="",
        season=cfg["target_season"],
        gate={"primary": "baseline_b", "promoted": False, "reason": summary["reason"]},
        collection_note=note,
    )
    folder = reports_dir(cfg)
    write_text(folder / "stage4_board.html", html)
    write_json(folder / "stage4_monte_carlo.json", summary)
