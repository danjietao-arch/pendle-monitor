from __future__ import annotations

import argparse

from nba_mvp.config import load_config, project_path
from nba_mvp.db import connect
from nba_mvp.jobs.hourly import run_hourly
from nba_mvp.report.dashboard import render_dashboard
from nba_mvp.report.export import import_reports, load_model_state, write_dashboard
from nba_mvp.util import utcnow


def _connection(cfg):
    return connect(project_path(cfg, "db"))


def _cmd_collect(cfg, args) -> int:
    import requests

    from nba_mvp.collect.nba_stats import collect_season_stats, nba_date_param
    from nba_mvp.collect.votes import collect_votes

    seasons = args.season or [cfg["target_season"]]
    session = requests.Session()
    conn = _connection(cfg)
    try:
        for season in seasons:
            date_to = args.date_to
            fetched_on = None
            if date_to is None and season == cfg["target_season"]:
                moment = utcnow()
                date_to = nba_date_param(moment)
                fetched_on = moment.date().isoformat()
            result = collect_season_stats(session, cfg, conn, season, date_to=date_to, fetched_on=fetched_on)
            print(result)
            if args.votes_url:
                print(collect_votes(session, cfg, conn, season, args.votes_url))
    finally:
        conn.close()
    return 0


def _cmd_snapshot(cfg, args) -> int:
    from nba_mvp.snapshot import build_season_snapshots

    conn = _connection(cfg)
    try:
        for season in args.season or [cfg["target_season"]]:
            print(build_season_snapshots(conn, cfg, season))
    finally:
        conn.close()
    return 0


def _cmd_train(cfg, _args) -> int:
    from nba_mvp.model.baseline import train_baselines

    conn = _connection(cfg)
    try:
        print(train_baselines(conn, cfg))
    finally:
        conn.close()
    return 0


def _cmd_infer(cfg, args) -> int:
    from nba_mvp.model.baseline import infer

    conn = _connection(cfg)
    try:
        print(infer(conn, cfg, args.season or cfg["target_season"]))
    finally:
        conn.close()
    return 0


def _cmd_hourly(cfg, _args, *, refresh_nba: bool) -> int:
    conn = _connection(cfg)
    try:
        result = run_hourly(cfg, conn, refresh_nba=refresh_nba)
        print(result)
        return 0 if result["status"] in {"success", "already_done", "busy"} else 1
    finally:
        conn.close()


def _cmd_dashboard(cfg, _args) -> int:
    conn = _connection(cfg)
    try:
        import_reports(conn, cfg)
        state = load_model_state(cfg)
        from nba_mvp.jobs.hourly import _history, _queue

        rows = []
        latest = project_latest(cfg)
        history = _history(conn)
        html = render_dashboard(
            rows=latest.get("rows") or rows,
            queue=_queue(conn),
            job=None,
            history=history,
            coverage=latest.get("coverage"),
            coverage_status=latest.get("coverage_status") or "pending_training",
            data_status=latest.get("data_status") or "pending_collection",
            model_version=latest.get("model_version") or state.get("model_version") or "untrained",
            model_as_of=latest.get("model_as_of"),
            market_as_of=latest.get("market_as_of"),
            comparison_as_of=latest.get("comparison_as_of"),
            last_success=None,
        )
        path = write_dashboard(cfg, html)
        print(path)
    finally:
        conn.close()
    return 0


def project_latest(cfg) -> dict:
    from nba_mvp.report.export import reports_dir
    import json

    path = reports_dir(cfg) / "latest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nba_mvp")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    collect = sub.add_parser("collect")
    collect.add_argument("--season", action="append")
    collect.add_argument("--votes-url")
    collect.add_argument("--date-to")

    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--season", action="append")

    sub.add_parser("train")
    infer = sub.add_parser("infer")
    infer.add_argument("--season")
    sub.add_parser("compare")
    sub.add_parser("dashboard")
    sub.add_parser("hourly")
    sub.add_parser("selftest")
    fetch = sub.add_parser("stage1-fetch")
    fetch.add_argument("--season", action="append")
    fetch.add_argument("--workers", type=int, default=8)
    replay = sub.add_parser("stage1-replay")
    replay.add_argument("--season", action="append")

    args = parser.parse_args(argv)
    if args.cmd == "selftest":
        from nba_mvp.selftest import main as run_selftest

        return run_selftest()
    cfg = load_config(None if args.config is None else __import__("pathlib").Path(args.config))
    if args.cmd == "collect":
        return _cmd_collect(cfg, args)
    if args.cmd == "snapshot":
        return _cmd_snapshot(cfg, args)
    if args.cmd == "train":
        return _cmd_train(cfg, args)
    if args.cmd == "infer":
        return _cmd_infer(cfg, args)
    if args.cmd == "compare":
        return _cmd_hourly(cfg, args, refresh_nba=False)
    if args.cmd == "dashboard":
        return _cmd_dashboard(cfg, args)
    if args.cmd == "hourly":
        return _cmd_hourly(cfg, args, refresh_nba=True)
    if args.cmd == "stage1-fetch":
        from nba_mvp.stage1 import fetch_stage1

        result = fetch_stage1(cfg, args.season, workers=args.workers)
        print({season: {"votes": item["votes"].get("winner"), "games": item["schedule"]["games"]} for season, item in result["seasons"].items()})
        return 0
    if args.cmd == "stage1-replay":
        from nba_mvp.stage1 import replay_stage1

        conn = _connection(cfg)
        try:
            result = replay_stage1(cfg, conn, args.season)
        finally:
            conn.close()
        print(
            {
                "model_status": result["model_status"],
                "leakage_ok": result["leakage_ok"],
                "seasons": [
                    {
                        "season": item["season"],
                        "winner": item["winner"],
                        "votes": item["votes"],
                        "failed_games": len(item["failed_games"]),
                        "leakage_problems": len(item["leakage_problems"]),
                    }
                    for item in result["seasons"]
                ],
            }
        )
        return 0 if result["leakage_ok"] else 1
    parser.error(f"未知命令: {args.cmd}")
    return 2
