"""Stage 2 walk-forward backtest.

Replays the archived seasons, fits baseline A/B on earlier seasons only, and
writes metrics plus a reliability plot. It does not infer the target season.
"""

from __future__ import annotations

import json

from nba_mvp.config import project_path
from nba_mvp.model.backtest import reliability_svg, walk_forward
from nba_mvp.model.baseline import train_baselines
from nba_mvp.snapshot import load_snapshot_frame
from nba_mvp.stage1 import DOCUMENTED_BLOCKERS, HISTORICAL_SEASONS, _frame_brief, archive_root, replay_season, season_paths
from nba_mvp.util import DataError

SNAPSHOT_KEYS = ("preseason", "tg_10", "tg_20", "tg_40", "tg_60", "end")


def _snapshot_shortfalls(report: dict) -> list[str]:
    notes = []
    for key, item in report.get("by_snapshot", {}).items():
        for metric in ("log_loss", "brier"):
            baseline_b = item.get(f"{metric}_b")
            baseline_a = item.get(f"{metric}_a")
            if baseline_b is None or baseline_a is None:
                continue
            if baseline_b > baseline_a:
                notes.append(f"{key} {metric}: B {baseline_b:.4f} > A {baseline_a:.4f}")
    return notes


def run_stage2_backtest(cfg: dict, conn) -> dict:
    root = archive_root(cfg)
    available = []
    missing = []
    for season in HISTORICAL_SEASONS:
        paths = season_paths(root, season)
        ready = all(paths[name].exists() for name in ("schedule", "boxscores", "votes", "vote_meta"))
        if ready:
            available.append(season)
        else:
            missing.append(season)
    if not available:
        raise DataError("没有可重放的历史存档")
    details = []
    for season in available:
        result = replay_season(conn, cfg, season)
        frames = result.pop("frames")
        result["snapshots_brief"] = {key: _frame_brief(rows) for key, rows in frames.items()}
        fabricated = []
        for game in result["failed_games"]:
            game_id = str(game.get("game_id") or "")
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM game_logs WHERE season = ? AND game_id = ?",
                (season, game_id),
            ).fetchone()["n"]
            if count:
                fabricated.append(game_id)
        result["fabricated_player_rows"] = fabricated
        details.append(result)
    labeled = [item["season"] for item in details if item["votes"] == "ok" and item["winner"]]
    rows = []
    for season in labeled:
        for key in SNAPSHOT_KEYS:
            rows.extend(load_snapshot_frame(conn, season, key))
    report = walk_forward(rows)
    duplicate = walk_forward(rows)
    reproducible = report == duplicate
    snapshot_shortfalls = _snapshot_shortfalls(report)
    unpaired = sorted(
        {
            fold["season"]
            for fold in report["folds"]
            if "baseline_a" not in fold or "baseline_b" not in fold
        }
    )
    required = int(cfg["min_historical_seasons"])
    training = train_baselines(conn, cfg)
    trained = training.get("status") == "ok" and len(labeled) >= required
    reports = project_path(cfg, "reports")
    reports.mkdir(parents=True, exist_ok=True)
    svg_path = reports / "stage2_reliability.svg"
    svg_path.write_text(reliability_svg(report["calibration"]["bins"]), encoding="utf-8")
    schedule_games = {item["season"]: item["schedule_games"] for item in details}
    summary = {
        "stage": 2,
        "labeled_seasons": labeled,
        "labeled_season_count": len(labeled),
        "min_historical_seasons": required,
        "missing_archives": missing,
        "unlabeled": [
            {"season": item["season"], "votes": item["votes"], "error": item["vote_error"]}
            for item in details
            if item["season"] not in labeled
        ],
        "schedule_games": schedule_games,
        "failed_games": {item["season"]: [game["game_id"] for game in item["failed_games"]] for item in details},
        "fabricated_player_rows": {item["season"]: item["fabricated_player_rows"] for item in details},
        "winners": {item["season"]: item["winner"] for item in details},
        "leakage_ok": all(not item["leakage_problems"] for item in details),
        "leakage_problems": {item["season"]: item["leakage_problems"] for item in details if item["leakage_problems"]},
        "blockers": list(DOCUMENTED_BLOCKERS),
        "walk_forward": report,
        "walk_forward_reproducible": reproducible,
        "not_worse_than_a": report["not_worse_than_a"],
        "shortfall": report["shortfall"],
        "snapshot_shortfalls": snapshot_shortfalls,
        "unpaired_seasons": unpaired,
        "model_status": "已训练" if trained else "待训练",
        "training": {
            "status": training.get("status"),
            "reason": training.get("reason"),
            "model_version": training.get("model_version"),
            "shrinkage_w": training.get("shrinkage_w"),
        },
        "target_season": cfg.get("target_season"),
        "target_season_probabilities": "未推断" if trained else "待训练",
        "calibration_plot": str(svg_path),
        "seasons": [
            {
                "season": item["season"],
                "winner": item["winner"],
                "votes": item["votes"],
                "vote_error": item["vote_error"],
                "schedule_games": item["schedule_games"],
                "failed_games": len(item["failed_games"]),
                "leakage_problems": len(item["leakage_problems"]),
            }
            for item in details
        ],
        "note": (
            "回测只读 data/archive/stage1，不访问网络，也不对目标赛季调用 infer。"
            "有标签赛季少于 min_historical_seasons 时模型状态保持待训练。"
            "2014-15 及更早赛程在此环境为 HTTP 403，没有补造比赛或选票。"
        ),
    }
    path = reports / "stage2_backtest.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["report_path"] = str(path)
    return summary
