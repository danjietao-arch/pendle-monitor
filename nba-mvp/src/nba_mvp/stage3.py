"""Stage 3 daily incremental update.

The run consumes final games knowable on `as_of`, keeps the previous rows when
a fetch fails, and writes a daily log plus a weekly snapshot. Market prices are
not part of this path.
"""

from __future__ import annotations

import csv
import html
import json
from datetime import date, timedelta
from pathlib import Path

from nba_mvp.collect.nba_stats import _upsert_games, _upsert_players, _upsert_teams
from nba_mvp.collect.official import merge_final_score, parse_boxscore_players, parse_schedule, schedule_team_rows, schedule_url
from nba_mvp.config import project_path
from nba_mvp.db import audit, transaction
from nba_mvp.jobs.hourly import begin_job, finish_job
from nba_mvp.model.baseline import artifact_allowed, infer, latest_model, train_baselines
from nba_mvp.report.dashboard import DISCLAIMER
from nba_mvp.report.export import reports_dir, write_json, write_text
from nba_mvp.snapshot import build_season_snapshots, load_snapshot_frame
from nba_mvp.util import DataError, isoformat, utcnow

OFFICIAL_INJURY_STATUS = {"out", "doubtful", "questionable", "probable", "available"}
AVAILABILITY = {"out": 0.2, "doubtful": 0.4, "questionable": 0.6, "probable": 0.85, "available": 1.0}
WEEKLY_FIELDS = (
    "player_id",
    "player_name",
    "team_abbr",
    "p_award",
    "delta_pp_vs_last_week",
    "p_eligibility",
    "projected_wins_low",
    "projected_wins_high",
    "as_of",
    "model_version",
    "reasons",
)


def injury_report_urls(as_of: str) -> list[str]:
    return [
        f"https://ak-static.cms.nba.com/referee/injury/Injury-Report_{as_of}_05PM.pdf",
        "https://www.nba.com/injury-report",
    ]


def probe_injury_source(as_of: str) -> dict:
    """One official injury check. The response body is not stored or parsed into rows."""
    from nba_mvp.stage1 import _fetch_bytes

    import requests

    session = requests.Session()
    errors = []
    fetched_at = None
    for url in injury_report_urls(as_of):
        try:
            status, body, fetched_at = _fetch_bytes(session, url, retries=1)
        except DataError as exc:
            errors.append(str(exc)[:180])
            continue
        if status == 200 and body:
            return {
                "status": "未解析",
                "error": "已取得官方伤病文件，未解析到可入库的球员状态，不置 0",
                "rows": [],
                "url": url,
                "fetched_at": fetched_at,
            }
        errors.append(f"HTTP {status} {url}")
    return {
        "status": "待采集",
        "error": "; ".join(errors) or "伤病来源不可用",
        "rows": [],
        "url": injury_report_urls(as_of)[0],
        "fetched_at": fetched_at,
    }


def accept_injury_rows(rows: list[dict] | None) -> tuple[list[dict], list[dict]]:
    """Keep official report rows. Rumors are rejected and do not zero anyone."""
    accepted = []
    rejected = []
    for row in rows or []:
        status = str(row.get("status") or "").strip().lower()
        source = str(row.get("source_url") or "")
        official = bool(row.get("official")) or "nba.com" in source.lower()
        player_id = str(row.get("player_id") or "")
        if status not in OFFICIAL_INJURY_STATUS or not official or not player_id:
            rejected.append({"player_id": player_id or None, "status": status or None, "reason": "未证实伤病，不入库，不置 0"})
            continue
        accepted.append(
            {
                "player_id": player_id,
                "status": status,
                "confidence": "official",
                "source_url": source or None,
                "availability": AVAILABILITY[status],
                "note": "官方伤病报告。单日状态不把获奖概率置 0。",
            }
        )
    return accepted, rejected


def projected_record_band(
    wins: float | None,
    games_played: int | None,
    remaining: int | None,
    prior_win_pct: float | None,
    shrinkage_w: float | None,
) -> dict | None:
    """Binomial band around the shrunk win rate. Missing inputs stay missing."""
    if wins is None or games_played is None or games_played <= 0 or remaining is None or remaining < 0:
        return None
    current = float(wins) / float(games_played)
    if shrinkage_w is not None and prior_win_pct is not None:
        rate = float(shrinkage_w) * current + (1.0 - float(shrinkage_w)) * float(prior_win_pct)
    else:
        rate = current
    rate = min(1.0, max(0.0, rate))
    mean_future = rate * remaining
    sd = (remaining * rate * (1.0 - rate)) ** 0.5
    low = max(float(wins), float(wins) + mean_future - 1.96 * sd)
    high = min(float(wins) + remaining, float(wins) + mean_future + 1.96 * sd)
    return {"low": round(low, 2), "high": round(high, 2), "rate": rate}


def _job_id(season: str, as_of: str) -> str:
    return f"daily:{season}:{as_of}"


def _known_game_ids(conn, season: str) -> set[str]:
    return {row["game_id"] for row in conn.execute("SELECT DISTINCT game_id FROM team_results WHERE season = ?", (season,))}


def _team_record(conn, season: str, team_id: str, as_of: str) -> tuple[float | None, int | None]:
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT win FROM team_results WHERE season = ? AND team_id = ? AND game_date <= ?",
            (season, str(team_id), as_of),
        )
    ]
    if not rows:
        return None, 0
    if any(row["win"] is None for row in rows):
        return None, len(rows)
    return float(sum(int(row["win"]) for row in rows)), len(rows)


def _remaining_games(schedule: list[dict], team_id: str, as_of: str) -> int:
    count = 0
    for game in schedule:
        if game["game_date"] <= as_of:
            continue
        if str(game["home"]["team_id"]) == str(team_id) or str(game["away"]["team_id"]) == str(team_id):
            count += 1
    return count


def ingest_knowable_games(conn, season: str, schedule: list[dict], boxes: dict, as_of: str, fetched_at: str) -> dict:
    """Insert final games on or before as_of that are not already stored.

    A box score without player lines keeps the schedule score and writes no
    zeroed player row.
    """
    known = _known_game_ids(conn, season)
    due = [game for game in schedule if game["game_date"] <= as_of and game["game_id"] not in known]
    inserted_players = 0
    inserted_teams = 0
    new_ids = []
    missing = []
    for game in due:
        record = boxes.get(game["game_id"]) or {}
        body = record.get("body")
        merged = merge_final_score(game, body if isinstance(body, dict) else None)
        if merged["home"].get("win") is None:
            missing.append({"game_id": game["game_id"], "error": record.get("error") or "no_final_score", "player_rows": 0})
            continue
        meta = {"url": record.get("url") or "archive", "fetched_at": record.get("fetched_at") or fetched_at}
        team_rows = schedule_team_rows([merged])
        played = []
        if isinstance(body, dict):
            try:
                played, unparsed = parse_boxscore_players(body, merged)
            except DataError as exc:
                unparsed = [{"error": str(exc)}]
                played = []
            if unparsed and not played:
                missing.append({"game_id": game["game_id"], "error": "player_lines_unparsed", "player_rows": 0})
        else:
            missing.append({"game_id": game["game_id"], "error": record.get("error") or "player_lines_missing", "player_rows": 0})
        with transaction(conn):
            _upsert_teams(conn, season, team_rows, meta)
            if played:
                _upsert_players(conn, played, meta["fetched_at"])
                _upsert_games(conn, season, played, meta)
        inserted_teams += len(team_rows)
        inserted_players += len(played)
        new_ids.append(game["game_id"])
    return {
        "new_game_ids": new_ids,
        "skipped_known_games": len(known),
        "player_rows_inserted": inserted_players,
        "team_rows_inserted": inserted_teams,
        "missing_games": missing,
    }


def load_archive_bundle(cfg: dict, season: str) -> dict:
    from nba_mvp.stage1 import archive_root, iter_boxscore_records

    root = archive_root(cfg)
    schedule_path = root / "schedules" / f"{season}.json"
    box_path = root / "boxscores" / f"{season}.jsonl.gz"
    if not schedule_path.exists():
        return {"status": "待采集", "error": f"缺少赛程存档: {season}", "schedule": None, "boxes": {}}
    payload = json.loads(schedule_path.read_text(encoding="utf-8"))
    if payload.get("status") != 200 or not payload.get("body"):
        return {"status": "STALE", "error": payload.get("error") or "赛程存档不可用", "schedule": None, "boxes": {}}
    try:
        schedule = parse_schedule(payload["body"])
    except DataError as exc:
        return {"status": "STALE", "error": str(exc), "schedule": None, "boxes": {}}
    boxes = {}
    if box_path.exists():
        for record in iter_boxscore_records(box_path):
            game_id = str(record.get("game_id") or "")
            if game_id:
                boxes[game_id] = record
    return {"status": "ok", "error": None, "schedule": schedule, "boxes": boxes, "source": "archive"}


def fetch_live_schedule(season: str) -> dict:
    """One schedule request. Failure returns 待采集 and does not invent games."""
    from nba_mvp.stage1 import _fetch_bytes

    import requests

    url = schedule_url(season)
    session = requests.Session()
    try:
        status, body, fetched_at = _fetch_bytes(session, url, retries=2)
    except DataError as exc:
        return {"status": "待采集", "error": str(exc), "schedule": None, "boxes": {}, "url": url, "fetched_at": None}
    if status != 200:
        return {
            "status": "待采集",
            "error": f"HTTP {status}",
            "schedule": None,
            "boxes": {},
            "url": url,
            "fetched_at": fetched_at,
        }
    try:
        payload = json.loads(body.decode("utf-8"))
        schedule = parse_schedule(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, DataError) as exc:
        return {"status": "STALE", "error": str(exc), "schedule": None, "boxes": {}, "url": url, "fetched_at": fetched_at}
    return {"status": "ok", "error": None, "schedule": schedule, "boxes": {}, "url": url, "fetched_at": fetched_at, "source": "live"}


def _season_start(schedule: list[dict]) -> str | None:
    dates = [game["game_date"] for game in schedule if game.get("game_date")]
    if not dates:
        return None
    return (date.fromisoformat(min(dates)) - timedelta(days=1)).isoformat()


def _shrinkage(conn) -> float | None:
    row = conn.execute(
        "SELECT shrinkage_w FROM model_runs WHERE status = 'ok' AND shrinkage_w IS NOT NULL ORDER BY trained_at DESC LIMIT 1"
    ).fetchone()
    if row is None or row["shrinkage_w"] is None:
        return None
    return float(row["shrinkage_w"])


def _previous_weekly(conn, season: str, as_of: str) -> dict[str, float]:
    row = conn.execute(
        "SELECT MAX(as_of) AS as_of FROM weekly_snapshots WHERE season = ? AND as_of < ?",
        (season, as_of),
    ).fetchone()
    if row is None or not row["as_of"]:
        return {}
    return {
        item["player_id"]: item["p_award"]
        for item in conn.execute(
            "SELECT player_id, p_award FROM weekly_snapshots WHERE season = ? AND as_of = ?",
            (season, row["as_of"]),
        )
    }


def build_weekly_rows(conn, cfg: dict, season: str, as_of: str, schedule: list[dict], model_version: str) -> list[dict]:
    predictions = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM predictions
            WHERE season = ? AND model_version = ? AND p_award IS NOT NULL
            ORDER BY snapshot_key, player_id
            """,
            (season, model_version),
        )
    ]
    if not predictions:
        return []
    # Prefer the latest snapshot key that infer wrote.
    order = {"end": 0, "tg_60": 1, "tg_40": 2, "tg_20": 3, "tg_10": 4, "preseason": 5}
    snapshot_key = sorted({row["snapshot_key"] for row in predictions}, key=lambda key: order.get(key, 9))[0]
    chosen = [row for row in predictions if row["snapshot_key"] == snapshot_key]
    frames = {row["player_id"]: row for row in load_snapshot_frame(conn, season, snapshot_key)}
    previous = _previous_weekly(conn, season, as_of)
    weight = _shrinkage(conn)
    injury = {
        row["player_id"]: row["status"]
        for row in conn.execute(
            "SELECT player_id, status FROM injury_reports WHERE season = ? AND as_of = ?",
            (season, as_of),
        )
    }
    weekly = []
    for row in chosen:
        frame = frames.get(row["player_id"], {})
        team_id = frame.get("team_id")
        wins, played = (None, None)
        remaining = None
        prior = None
        if team_id:
            wins, played = _team_record(conn, season, team_id, as_of)
            remaining = _remaining_games(schedule, team_id, as_of)
            prior = (frame.get("team_features") or {}).get("prior_win_pct")
        band = projected_record_band(wins, played, remaining, prior, weight)
        previous_p = previous.get(row["player_id"])
        current_p = row["p_award"]
        delta = None
        if previous_p is not None and current_p is not None:
            delta = round(100.0 * (float(current_p) - float(previous_p)), 6)
        reasons = ["数据", "模型版本"]
        if band is not None:
            reasons.append("球队")
        if row["player_id"] in injury:
            reasons.append(f"健康：官方 {injury[row['player_id']]}，获奖概率不因此置 0")
        else:
            reasons.append("健康：未证实，不置 0")
        weekly.append(
            {
                "player_id": row["player_id"],
                "player_name": row.get("player_name"),
                "team_abbr": row.get("team_abbr"),
                "p_award": current_p,
                "delta_pp_vs_last_week": delta,
                "p_eligibility": row.get("p_eligibility"),
                "projected_wins_low": None if band is None else band["low"],
                "projected_wins_high": None if band is None else band["high"],
                "as_of": as_of,
                "model_version": model_version,
                "reasons": reasons,
            }
        )
    return weekly


def _store_weekly(conn, season: str, as_of: str, rows: list[dict], created_at: str) -> None:
    with transaction(conn):
        for row in rows:
            conn.execute(
                """
                INSERT INTO weekly_snapshots (
                  season, as_of, player_id, player_name, team_abbr, p_award, delta_pp_vs_last_week,
                  p_eligibility, projected_wins_low, projected_wins_high, model_version, reasons, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(season, as_of, player_id) DO UPDATE SET
                  p_award = excluded.p_award,
                  delta_pp_vs_last_week = excluded.delta_pp_vs_last_week,
                  p_eligibility = excluded.p_eligibility,
                  projected_wins_low = excluded.projected_wins_low,
                  projected_wins_high = excluded.projected_wins_high,
                  model_version = excluded.model_version,
                  reasons = excluded.reasons,
                  created_at = excluded.created_at
                """,
                (
                    season,
                    as_of,
                    row["player_id"],
                    row.get("player_name"),
                    row.get("team_abbr"),
                    row.get("p_award"),
                    row.get("delta_pp_vs_last_week"),
                    row.get("p_eligibility"),
                    row.get("projected_wins_low"),
                    row.get("projected_wins_high"),
                    row.get("model_version"),
                    json.dumps(row.get("reasons") or [], ensure_ascii=False),
                    created_at,
                ),
            )


def _write_weekly_files(cfg: dict, season: str, as_of: str, rows: list[dict]) -> dict[str, str]:
    folder = reports_dir(cfg) / "weekly" / season
    folder.mkdir(parents=True, exist_ok=True)
    json_path = folder / f"{as_of}.json"
    csv_path = folder / f"{as_of}.csv"
    write_json(json_path, {"season": season, "as_of": as_of, "rows": rows, "disclaimer": DISCLAIMER})
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(WEEKLY_FIELDS))
        writer.writeheader()
        for row in rows:
            exported = dict(row)
            exported["reasons"] = " | ".join(row.get("reasons") or [])
            writer.writerow({key: exported.get(key) for key in WEEKLY_FIELDS})
    return {"json": str(json_path), "csv": str(csv_path)}


def _append_run_log(cfg: dict, entry: dict) -> None:
    path = reports_dir(cfg) / "daily" / "run_log.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    key = (entry.get("season"), entry.get("as_of"), entry.get("data_status"), entry.get("error"))
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            previous = json.loads(line)
            previous_key = (previous.get("season"), previous.get("as_of"), previous.get("data_status"), previous.get("error"))
            if previous_key == key:
                return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _latest_weekly_rows(conn, season: str) -> list[dict]:
    row = conn.execute("SELECT MAX(as_of) AS as_of FROM weekly_snapshots WHERE season = ?", (season,)).fetchone()
    if row is None or not row["as_of"]:
        return []
    items = []
    for item in conn.execute(
        """
        SELECT player_id, player_name, team_abbr, p_award, delta_pp_vs_last_week, p_eligibility,
               projected_wins_low, projected_wins_high, as_of, model_version, reasons
        FROM weekly_snapshots WHERE season = ? AND as_of = ?
        ORDER BY p_award IS NULL, p_award DESC
        """,
        (season, row["as_of"]),
    ):
        parsed = dict(item)
        reasons = parsed.get("reasons")
        if isinstance(reasons, str):
            try:
                parsed["reasons"] = json.loads(reasons)
            except json.JSONDecodeError:
                parsed["reasons"] = [reasons]
        items.append(parsed)
    return items


def _board_table(rows: list[dict]) -> list[str]:
    lines = ["<table><tr><th>球员</th><th>概率</th><th>较上周 Δpp</th><th>资格概率</th><th>预计胜场</th><th>理由</th></tr>"]
    for row in rows:
        low = row.get("projected_wins_low")
        high = row.get("projected_wins_high")
        band = "" if low is None else f"{low}–{high}"
        delta = "" if row.get("delta_pp_vs_last_week") is None else row["delta_pp_vs_last_week"]
        eligibility = "" if row.get("p_eligibility") is None else row["p_eligibility"]
        probability = "" if row.get("p_award") is None else f"{float(row['p_award']) * 100:.1f}%"
        reasons = "；".join(str(part) for part in (row.get("reasons") or []))
        lines.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('player_name') or row['player_id']))}</td>"
            f"<td>{html.escape(str(probability))}</td><td>{html.escape(str(delta))}</td>"
            f"<td>{html.escape(str(eligibility))}</td><td>{html.escape(str(band))}</td>"
            f"<td>{html.escape(reasons)}</td></tr>"
        )
    lines.append("</table>")
    return lines


def _write_board(cfg: dict, summary: dict, prior_rows: list[dict] | None = None) -> str:
    current = summary.get("weekly_rows") or summary.get("curve") or []
    lines = [
        "<!DOCTYPE html>",
        "<meta charset=\"utf-8\">",
        "<title>NBA MVP 日更</title>",
        f"<p>{html.escape(DISCLAIMER)}</p>",
        f"<p>as_of {html.escape(str(summary.get('as_of')))} · {html.escape(str(summary.get('model_version')))} · {html.escape(str(summary.get('data_status')))}</p>",
    ]
    if summary.get("data_status") == "STALE":
        lines.append("<p>STALE · 待采集</p>")
        if prior_rows:
            lines.append(f"<p>以下为上次周快照 {html.escape(str(prior_rows[0].get('as_of')))}，本次抓取未覆盖。</p>")
            lines.extend(_board_table(prior_rows))
        else:
            lines.append("<p>没有可展示的上一份概率。</p>")
    elif summary.get("data_status") == "partial":
        lines.append("<p>部分比赛缺失，未把缺席球员填 0。</p>")
        if current and summary.get("infer_status") == "ok":
            lines.extend(_board_table(current))
        else:
            lines.append("<p>待采集</p>")
    elif not current or summary.get("infer_status") != "ok":
        lines.append("<p>待采集</p>")
    else:
        lines.extend(_board_table(current))
    path = reports_dir(cfg) / "stage3_board.html"
    write_text(path, "\n".join(lines))
    return str(path)


def _resolve_bundle(cfg: dict, season: str, source: str, bundle: dict | None, live_fetch) -> dict:
    if bundle is not None:
        return bundle
    if source in {"auto", "archive"}:
        archived = load_archive_bundle(cfg, season)
        if archived.get("schedule") or source == "archive":
            return archived
    fetcher = live_fetch or fetch_live_schedule
    return fetcher(season)


def run_daily(
    cfg: dict,
    conn,
    *,
    as_of: str,
    season: str | None = None,
    source: str = "auto",
    bundle: dict | None = None,
    injury_rows: list[dict] | None = None,
    live_fetch=None,
    probe_injuries: bool = False,
    now=None,
) -> dict:
    season = season or cfg["target_season"]
    moment = now or utcnow()
    now_iso = isoformat(moment)
    job_id = _job_id(season, as_of)
    lock = begin_job(conn, job_id, now_iso)
    if lock != "acquired":
        return {"status": lock, "season": season, "as_of": as_of, "duplicated": False, "job_id": job_id}
    try:
        injury_meta = _injury_meta(as_of, injury_rows, probe_injuries)
        if injury_rows is None:
            injury_rows = injury_meta.pop("rows")
        resolved = _resolve_bundle(cfg, season, source, bundle, live_fetch)
        schedule = resolved.get("schedule")
        if not schedule:
            summary = _closed_summary(
                season=season,
                as_of=as_of,
                data_status="STALE",
                collection_status="待采集",
                error=resolved.get("error") or "赛程不可用",
                infer_status="待采集",
                model_version="untrained",
                injury_source_status=injury_meta.get("injury_source_status"),
                injury_source_error=injury_meta.get("injury_source_error"),
            )
            _persist_outputs(cfg, conn, summary, now_iso, weekly_rows=[], prior_rows=_latest_weekly_rows(conn, season))
            finish_job(conn, job_id, "success", summary["data_status"], now_iso)
            audit(conn, "stage3_source_failed", season, summary["error"], now_iso)
            return summary
        accepted, rejected = accept_injury_rows(injury_rows)
        with transaction(conn):
            conn.execute("DELETE FROM injury_reports WHERE season = ? AND as_of = ?", (season, as_of))
            for row in accepted:
                conn.execute(
                    """
                    INSERT INTO injury_reports (
                      season, as_of, player_id, status, confidence, source_url, note, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (season, as_of, row["player_id"], row["status"], row["confidence"], row.get("source_url"), row.get("note"), now_iso),
                )
        ingest = ingest_knowable_games(conn, season, schedule, resolved.get("boxes") or {}, as_of, now_iso)
        season_start = _season_start(schedule)
        games_present = conn.execute("SELECT COUNT(*) AS n FROM team_results WHERE season = ?", (season,)).fetchone()["n"]
        if games_present:
            build_season_snapshots(conn, cfg, season, season_start=season_start)
            injury_map = _latest_injury_map(conn, season, as_of)
            if injury_map:
                with transaction(conn):
                    for player_id, status in injury_map.items():
                        conn.execute(
                            """
                            UPDATE feature_snapshots
                            SET injury_status = ?
                            WHERE season = ? AND player_id = ?
                            """,
                            (status, season, player_id),
                        )
        model_version = "untrained"
        infer_status = "待采集"
        if season == cfg.get("target_season") and games_present:
            run = latest_model(conn)
            meta = None
            if run and run.get("artifact_path"):
                try:
                    from nba_mvp.model.baseline import load_artifact

                    meta = load_artifact(run["artifact_path"]).get("meta")
                except Exception:
                    meta = None
            if not artifact_allowed(meta, cfg):
                train_baselines(conn, cfg)
            inferred = infer(conn, cfg, season)
            model_version = inferred.get("model_version") or "untrained"
            infer_status = inferred.get("status") or "待采集"
        elif not games_present:
            infer_status = "待采集"
        else:
            infer_status = "skipped"
        curve_rows = []
        weekly_rows = []
        weekly_paths = None
        shown_infer = "待采集" if infer_status == "pending_collection" else ("待训练" if infer_status == "pending_training" else infer_status)
        should_week = date.fromisoformat(as_of).weekday() == 0 or not _previous_weekly(conn, season, as_of)
        if shown_infer == "ok":
            curve_rows = build_weekly_rows(conn, cfg, season, as_of, schedule, model_version)
            if should_week and curve_rows:
                weekly_rows = curve_rows
                _store_weekly(conn, season, as_of, weekly_rows, now_iso)
                weekly_paths = _write_weekly_files(cfg, season, as_of, weekly_rows)
        data_status = "partial" if ingest["missing_games"] else "ok"
        summary = {
            "stage": 3,
            "status": "success",
            "season": season,
            "as_of": as_of,
            "data_status": data_status,
            "collection_status": "ok",
            "error": None,
            "new_game_ids": ingest["new_game_ids"],
            "skipped_known_games": ingest["skipped_known_games"],
            "player_rows_inserted": ingest["player_rows_inserted"],
            "missing_games": ingest["missing_games"],
            "zero_filled": False,
            "injury_accepted": [row["player_id"] for row in accepted],
            "injury_rejected": rejected,
            "injury_source_status": injury_meta.get("injury_source_status"),
            "injury_source_error": injury_meta.get("injury_source_error"),
            "model_version": model_version,
            "infer_status": shown_infer,
            "curve": curve_rows,
            "weekly_rows": weekly_rows,
            "weekly_paths": weekly_paths,
            "duplicated": False,
        }
        _persist_outputs(cfg, conn, summary, now_iso, weekly_rows=weekly_rows)
        finish_job(conn, job_id, "success", data_status, now_iso)
        audit(conn, "stage3_daily", season, f"as_of={as_of} new={len(ingest['new_game_ids'])} missing={len(ingest['missing_games'])}", now_iso)
        return summary
    except Exception as exc:
        finished = isoformat(utcnow())
        finish_job(conn, job_id, "failed", str(exc), finished)
        audit(conn, "stage3_failed", season, f"{type(exc).__name__}: {exc}", finished)
        return {"status": "failed", "season": season, "as_of": as_of, "error": f"{type(exc).__name__}: {exc}", "duplicated": False}


def _closed_summary(**fields) -> dict:
    summary = {
        "stage": 3,
        "status": "success",
        "new_game_ids": [],
        "skipped_known_games": 0,
        "player_rows_inserted": 0,
        "missing_games": [],
        "zero_filled": False,
        "injury_accepted": [],
        "injury_rejected": [],
        "weekly_rows": [],
        "weekly_paths": None,
        "duplicated": False,
    }
    summary.update(fields)
    return summary


def _latest_injury_map(conn, season: str, as_of: str) -> dict[str, str]:
    latest = {}
    for row in conn.execute(
        """
        SELECT player_id, status FROM injury_reports
        WHERE season = ? AND as_of <= ?
        ORDER BY as_of
        """,
        (season, as_of),
    ):
        latest[row["player_id"]] = row["status"]
    return latest


def _injury_meta(as_of: str, injury_rows: list[dict] | None, probe_injuries: bool) -> dict:
    if injury_rows is not None:
        return {"injury_source_status": "provided", "injury_source_error": None, "rows": injury_rows}
    if not probe_injuries:
        return {"injury_source_status": "未检查", "injury_source_error": None, "rows": []}
    probed = probe_injury_source(as_of)
    return {
        "injury_source_status": probed.get("status") or "待采集",
        "injury_source_error": probed.get("error"),
        "rows": [],
    }


def _persist_outputs(cfg: dict, conn, summary: dict, now_iso: str, weekly_rows: list[dict], prior_rows: list[dict] | None = None) -> None:
    del conn, weekly_rows
    folder = reports_dir(cfg) / "daily" / summary["season"]
    folder.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in summary.items() if key != "weekly_rows"}
    payload["weekly_row_count"] = len(summary.get("weekly_rows") or [])
    payload["logged_at"] = now_iso
    write_json(folder / f"{summary['as_of']}.json", payload)
    write_json(reports_dir(cfg) / "stage3_latest.json", payload)
    _append_run_log(
        cfg,
        {
            "season": summary["season"],
            "as_of": summary["as_of"],
            "data_status": summary["data_status"],
            "collection_status": summary.get("collection_status"),
            "error": summary.get("error"),
            "new_games": len(summary.get("new_game_ids") or []),
            "missing_games": len(summary.get("missing_games") or []),
            "infer_status": summary.get("infer_status"),
            "model_version": summary.get("model_version"),
            "logged_at": now_iso,
        },
    )
    summary["board"] = _write_board(cfg, summary, prior_rows=prior_rows)
    summary["log_path"] = str(folder / f"{summary['as_of']}.json")
