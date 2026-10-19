from __future__ import annotations

import json

from nba_mvp.db import audit, rows_as_dicts, transaction
from nba_mvp.market.compare import build_comparison
from nba_mvp.market.discover import discover_markets, load_registry, queue_discovered, sync_registry
from nba_mvp.market.quotes import classify_quote, fetch_registry_books, persist_quote
from nba_mvp.model.baseline import current_snapshot_key, infer, train_baselines
from nba_mvp.report.dashboard import render_dashboard
from nba_mvp.report.export import (
    append_alerts,
    export_hour,
    import_reports,
    load_model_state,
    persist_hourly_rows,
    read_alert_keys,
    save_model_state,
    write_dashboard,
)
from nba_mvp.snapshot import build_season_snapshots
from nba_mvp.util import hour_stamp, isoformat, parse_time, previous_hour, utcnow


def game_fingerprint(conn, season: str) -> str:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(game_date) AS d, MAX(source_timestamp) AS s FROM game_logs WHERE season = ?",
        (season,),
    ).fetchone()
    return f"{row['n']}|{row['d']}|{row['s']}"


def begin_job(conn, as_of_hour: str, now_iso: str) -> str:
    with transaction(conn):
        row = conn.execute("SELECT status, started_at FROM job_status WHERE job_id = ?", (as_of_hour,)).fetchone()
        if row and row["status"] == "success":
            return "already_done"
        if row and row["status"] == "running":
            started = parse_time(row["started_at"])
            if started and (parse_time(now_iso) - started).total_seconds() < 1800:
                return "busy"
        conn.execute(
            """
            INSERT INTO job_status (job_id, as_of_hour, started_at, finished_at, status, detail)
            VALUES (?, ?, ?, NULL, 'running', NULL)
            ON CONFLICT(job_id) DO UPDATE SET
              started_at = excluded.started_at,
              status = 'running',
              finished_at = NULL,
              detail = NULL
            """,
            (as_of_hour, as_of_hour, now_iso),
        )
    return "acquired"


def finish_job(conn, as_of_hour: str, status: str, detail: str, finished_at: str) -> None:
    conn.execute(
        "UPDATE job_status SET status = ?, detail = ?, finished_at = ? WHERE job_id = ?",
        (status, detail, finished_at, as_of_hour),
    )


def _predictions(conn, season: str, snapshot_key: str | None) -> list[dict]:
    if not snapshot_key:
        return []
    return rows_as_dicts(
        conn.execute(
            "SELECT * FROM predictions WHERE season = ? AND snapshot_key = ?",
            (season, snapshot_key),
        )
    )


def _quotes(conn, as_of_hour: str, model_version: str) -> dict[str, dict]:
    found = {}
    for row in conn.execute(
        "SELECT * FROM market_quotes WHERE as_of_hour = ? AND model_version = ?",
        (as_of_hour, model_version),
    ):
        if row["player_id"]:
            found[row["player_id"]] = dict(row)
    return found


def _previous(conn, as_of_hour: str) -> dict[str, dict]:
    prev = previous_hour(as_of_hour)
    return {
        row["player_id"]: dict(row)
        for row in conn.execute("SELECT * FROM hourly_snapshots WHERE as_of_hour = ?", (prev,))
    }


def _registry(conn) -> dict[str, dict]:
    return {
        row["player_id"]: dict(row)
        for row in conn.execute("SELECT * FROM market_registry WHERE status = 'active'")
    }


def _history(conn) -> list[dict]:
    return rows_as_dicts(conn.execute("SELECT * FROM hourly_snapshots ORDER BY as_of_hour, player_id"))


def _queue(conn) -> list[dict]:
    return rows_as_dicts(conn.execute("SELECT * FROM review_queue ORDER BY status, question"))


def _refresh_model(cfg, conn, season: str, fingerprint: str, nba_status: str) -> tuple[dict, bool]:
    state = load_model_state(cfg)
    changed = state.get("fingerprint") != fingerprint
    source_stale = nba_status == "stale"
    if changed and nba_status != "stale":
        built = build_season_snapshots(conn, cfg, season)
        if built["status"] == "ok":
            train_baselines(conn, cfg)
            inferred = infer(conn, cfg, season)
            state = {
                "fingerprint": fingerprint,
                "model_as_of": inferred.get("model_as_of"),
                "model_version": inferred.get("model_version"),
                "status": inferred.get("status"),
                "season": season,
                "snapshot_key": current_snapshot_key(conn, season),
                "predictions": _predictions(conn, season, current_snapshot_key(conn, season)),
            }
        else:
            state = {
                "fingerprint": fingerprint,
                "model_as_of": None,
                "model_version": "untrained",
                "status": "pending_collection",
                "season": season,
                "snapshot_key": None,
                "predictions": [],
            }
        save_model_state(cfg, state)
        return state, source_stale
    if changed and source_stale:
        return state, True
    return state, source_stale


def _publish(cfg, conn, *, as_of_hour: str, comparison: dict, state: dict, now_iso: str, data_status: str) -> None:
    payload = {
        "as_of_hour": as_of_hour,
        "model_version": state.get("model_version") or "untrained",
        "model_as_of": state.get("model_as_of"),
        "comparison_as_of": now_iso,
        "coverage": comparison["coverage"],
        "coverage_status": comparison["coverage_status"],
        "data_status": data_status,
        "rows": comparison["rows"],
    }
    market_times = [row.get("market_as_of") for row in comparison["rows"] if row.get("market_as_of")]
    payload["market_as_of"] = max(market_times) if market_times else None
    persist_hourly_rows(conn, comparison["rows"])
    append_alerts(cfg, comparison["alerts"], now_iso)
    for alert in comparison["alerts"]:
        audit(conn, "divergence_alert", alert["ref_key"], alert["message"], now_iso)
    export_hour(cfg, payload)
    history = _history(conn)
    job = conn.execute("SELECT * FROM job_status WHERE job_id = ?", (as_of_hour,)).fetchone()
    last_success = conn.execute(
        "SELECT finished_at FROM job_status WHERE status = 'success' ORDER BY finished_at DESC LIMIT 1"
    ).fetchone()
    html = render_dashboard(
        rows=comparison["rows"],
        queue=_queue(conn),
        job=dict(job) if job else None,
        history=history,
        coverage=comparison["coverage"],
        coverage_status=comparison["coverage_status"],
        data_status=data_status,
        model_version=payload["model_version"],
        model_as_of=payload["model_as_of"],
        market_as_of=payload["market_as_of"],
        comparison_as_of=now_iso,
        last_success=None if last_success is None else last_success["finished_at"],
    )
    write_dashboard(cfg, html)


def run_hourly(
    cfg: dict,
    conn,
    *,
    now=None,
    refresh_nba: bool = True,
    search_fn=None,
    nba_fn=None,
    book_fn=None,
    session=None,
) -> dict:
    moment = now or utcnow()
    as_of_hour = hour_stamp(moment)
    now_iso = isoformat(moment)
    import_reports(conn, cfg)
    lock = begin_job(conn, as_of_hour, now_iso)
    if lock != "acquired":
        return {"status": lock, "as_of_hour": as_of_hour}
    season = cfg["target_season"]
    try:
        registry = load_registry(cfg)
        sync_registry(conn, registry, now_iso)
        if search_fn is not None:
            queue_discovered(conn, search_fn(), season, now_iso)
        elif cfg.get("polymarket", {}).get("enabled", True):
            if session is None:
                import requests

                session = requests.Session()
            discover_markets(session, cfg, conn)
        if not refresh_nba:
            nba_result = {"status": "skipped"}
        elif nba_fn is not None:
            nba_result = nba_fn(conn)
        else:
            from nba_mvp.collect.nba_stats import collect_season_stats, nba_date_param

            if session is None:
                import requests

                session = requests.Session()
            nba_result = collect_season_stats(
                session,
                cfg,
                conn,
                season,
                date_to=nba_date_param(moment),
                fetched_on=moment.date().isoformat(),
            )
        fingerprint = game_fingerprint(conn, season)
        state, source_stale = _refresh_model(cfg, conn, season, fingerprint, nba_result.get("status"))
        model_version = state.get("model_version") or "untrained"
        markets = registry["markets"]
        if book_fn is not None:
            for market in markets:
                try:
                    book, meta = book_fn(market["yes_token_id"])
                except Exception as exc:
                    audit(conn, "book_fetch_failed", market["market_id"], str(exc), now_iso)
                    continue
                classified = classify_quote(book, now=moment, stale_minutes=int(cfg["polymarket"]["stale_minutes"]))
                persist_quote(
                    conn,
                    as_of_hour,
                    model_version,
                    market["market_id"],
                    str(market["player_id"]),
                    classified,
                    source_url=meta.get("url"),
                    fetched_at=meta.get("fetched_at") or now_iso,
                )
        elif markets:
            if session is None:
                import requests

                session = requests.Session()
            fetch_registry_books(
                session,
                cfg,
                conn,
                markets,
                as_of_hour=as_of_hour,
                model_version=model_version,
                now=moment,
            )
        snapshot_key = state.get("snapshot_key") or current_snapshot_key(conn, season)
        comparison = build_comparison(
            as_of_hour=as_of_hour,
            comparison_as_of=now_iso,
            model_version=model_version,
            model_as_of=state.get("model_as_of"),
            predictions=_predictions(conn, season, snapshot_key),
            quotes_by_player=_quotes(conn, as_of_hour, model_version),
            registry_by_player=_registry(conn),
            previous_by_player=_previous(conn, as_of_hour),
            existing_alert_keys=read_alert_keys(cfg),
            unprocessed_games=source_stale or state.get("fingerprint") != fingerprint,
            exhaustive=bool(registry.get("exhaustive")),
            alert_abs_delta_pp=float(cfg["polymarket"]["alert_abs_delta_pp"]),
            alert_max_spread_pp=float(cfg["polymarket"]["alert_max_spread_pp"]),
            alert_consecutive=int(cfg["polymarket"]["alert_consecutive"]),
        )
        data_status = "STALE" if source_stale or comparison["data_status"] == "STALE" else nba_result.get("status") or "ok"
        detail = json.dumps({"nba": nba_result.get("status"), "data_status": data_status, "model": state.get("status")}, ensure_ascii=False)
        finish_job(conn, as_of_hour, "success", detail, now_iso)
        _publish(cfg, conn, as_of_hour=as_of_hour, comparison=comparison, state=state, now_iso=now_iso, data_status=data_status)
        return {
            "status": "success",
            "as_of_hour": as_of_hour,
            "model_version": model_version,
            "data_status": data_status,
            "rows": len(comparison["rows"]),
            "alerts": len(comparison["alerts"]),
        }
    except Exception as exc:
        finished = isoformat(utcnow())
        finish_job(conn, as_of_hour, "failed", str(exc), finished)
        audit(conn, "hourly_failed", as_of_hour, f"{type(exc).__name__}: {exc}", finished)
        return {"status": "failed", "as_of_hour": as_of_hour, "error": f"{type(exc).__name__}: {exc}"}
