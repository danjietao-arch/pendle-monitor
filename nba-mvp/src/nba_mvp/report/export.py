from __future__ import annotations

import csv
import json
from pathlib import Path

from nba_mvp.config import project_path

HOURLY_COLUMNS = [
    "as_of_hour",
    "model_version",
    "player_id",
    "player_name",
    "team_abbr",
    "market_id",
    "model_as_of",
    "market_as_of",
    "comparison_as_of",
    "p_model",
    "p_market_mid",
    "p_market_normalized",
    "bid",
    "ask",
    "spread_pp",
    "last_trade",
    "gamma_price",
    "delta_pp",
    "delta_rel",
    "vs_bid_pp",
    "vs_ask_pp",
    "delta_vs_prev_hour_pp",
    "p_eligibility",
    "coverage",
    "coverage_status",
    "row_status",
    "reason",
    "condition_met",
    "streak_start",
    "streak_length",
]


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def write_json(path: Path, payload) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def reports_dir(cfg: dict) -> Path:
    return project_path(cfg, "reports")


def model_state_path(cfg: dict) -> Path:
    return reports_dir(cfg) / "model_state.json"


def alerts_path(cfg: dict) -> Path:
    return reports_dir(cfg) / "alerts.jsonl"


def load_model_state(cfg: dict) -> dict:
    path = model_state_path(cfg)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_model_state(cfg: dict, state: dict) -> None:
    write_json(model_state_path(cfg), state)


def read_alert_keys(cfg: dict) -> set[str]:
    path = alerts_path(cfg)
    if not path.exists():
        return set()
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            keys.add(json.loads(line)["ref_key"])
    return keys


def append_alerts(cfg: dict, alerts: list[dict], created_at: str) -> int:
    if not alerts:
        return 0
    path = alerts_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_alert_keys(cfg)
    written = 0
    with path.open("a", encoding="utf-8") as handle:
        for alert in alerts:
            if alert["ref_key"] in existing:
                continue
            payload = {**alert, "created_at": created_at}
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            existing.add(alert["ref_key"])
            written += 1
    return written


def persist_hourly_rows(conn, rows: list[dict]) -> int:
    inserted = 0
    columns = ",".join(HOURLY_COLUMNS)
    placeholders = ",".join("?" * len(HOURLY_COLUMNS))
    for row in rows:
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO hourly_snapshots ({columns}) VALUES ({placeholders})",
            [row.get(column) for column in HOURLY_COLUMNS],
        )
        inserted += cursor.rowcount
    return inserted


def import_reports(conn, cfg: dict) -> int:
    folder = reports_dir(cfg) / "hourly"
    if not folder.exists():
        return 0
    imported = 0
    for path in sorted(folder.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        imported += persist_hourly_rows(conn, payload.get("rows") or [])
    state = load_model_state(cfg)
    for prediction in state.get("predictions") or []:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO predictions (
              season, snapshot_key, model_version, player_id, player_name, team_abbr,
              as_of, p_award, p_eligibility, status, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction.get("season"),
                prediction.get("snapshot_key"),
                prediction.get("model_version"),
                prediction.get("player_id"),
                prediction.get("player_name"),
                prediction.get("team_abbr"),
                prediction.get("as_of"),
                prediction.get("p_award"),
                prediction.get("p_eligibility"),
                prediction.get("status"),
                prediction.get("reason"),
                prediction.get("created_at"),
            ),
        )
        imported += cursor.rowcount
    return imported


def export_hour(cfg: dict, payload: dict) -> dict:
    from nba_mvp.util import filename_stamp

    folder = reports_dir(cfg) / "hourly"
    stamp = filename_stamp(payload["as_of_hour"])
    json_path = folder / f"{stamp}.json"
    csv_path = folder / f"{stamp}.csv"
    write_json(json_path, payload)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HOURLY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in payload.get("rows") or []:
            writer.writerow({key: "" if row.get(key) is None else row.get(key) for key in HOURLY_COLUMNS})
    write_json(reports_dir(cfg) / "latest.json", payload)
    return {"json": str(json_path), "csv": str(csv_path)}


def write_dashboard(cfg: dict, html_text: str) -> Path:
    path = reports_dir(cfg) / "dashboard.html"
    write_text(path, html_text)
    return path
