from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
  player_id TEXT PRIMARY KEY,
  player_name TEXT,
  team_id TEXT,
  team_abbr TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS game_logs (
  season TEXT NOT NULL,
  player_id TEXT NOT NULL,
  player_name TEXT,
  game_id TEXT NOT NULL,
  game_date TEXT NOT NULL,
  team_id TEXT,
  team_abbr TEXT,
  opponent TEXT,
  minutes REAL,
  pts REAL,
  reb REAL,
  ast REAL,
  stl REAL,
  blk REAL,
  tov REAL,
  fgm REAL,
  fga REAL,
  ftm REAL,
  fta REAL,
  started INTEGER,
  source_url TEXT,
  source_timestamp TEXT,
  PRIMARY KEY (season, player_id, game_id)
);

CREATE TABLE IF NOT EXISTS team_results (
  season TEXT NOT NULL,
  team_id TEXT NOT NULL,
  team_abbr TEXT,
  game_id TEXT NOT NULL,
  game_date TEXT NOT NULL,
  win INTEGER,
  points_for REAL,
  points_against REAL,
  source_url TEXT,
  source_timestamp TEXT,
  PRIMARY KEY (season, team_id, game_id)
);

CREATE TABLE IF NOT EXISTS vote_totals (
  season TEXT NOT NULL,
  player_id TEXT NOT NULL,
  player_name TEXT,
  vote_points REAL,
  first_place_votes REAL,
  vote_share REAL,
  is_winner INTEGER,
  source_url TEXT,
  source_timestamp TEXT,
  parse_status TEXT,
  PRIMARY KEY (season, player_id)
);

CREATE TABLE IF NOT EXISTS feature_snapshots (
  season TEXT NOT NULL,
  snapshot_key TEXT NOT NULL,
  snapshot_date TEXT NOT NULL,
  player_id TEXT NOT NULL,
  player_name TEXT,
  team_id TEXT,
  team_abbr TEXT,
  games_played INTEGER,
  qualifying_games INTEGER,
  team_games INTEGER,
  player_features TEXT,
  team_features TEXT,
  injury_status TEXT,
  official_eligible INTEGER,
  eligibility_status TEXT,
  award_winner INTEGER,
  source_timestamp TEXT,
  PRIMARY KEY (season, snapshot_key, player_id)
);

CREATE TABLE IF NOT EXISTS model_runs (
  model_version TEXT PRIMARY KEY,
  kind TEXT,
  trained_at TEXT,
  train_seasons TEXT,
  metrics_json TEXT,
  shrinkage_w REAL,
  status TEXT,
  beats_baseline INTEGER,
  synthetic INTEGER,
  artifact_path TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
  season TEXT NOT NULL,
  snapshot_key TEXT NOT NULL,
  model_version TEXT NOT NULL,
  player_id TEXT NOT NULL,
  player_name TEXT,
  team_abbr TEXT,
  as_of TEXT,
  p_award REAL,
  p_eligibility REAL,
  status TEXT,
  reason TEXT,
  created_at TEXT,
  PRIMARY KEY (season, snapshot_key, model_version, player_id),
  CHECK (p_award IS NULL OR (p_award >= 0 AND p_award <= 1)),
  CHECK (p_eligibility IS NULL OR (p_eligibility >= 0 AND p_eligibility <= 1))
);

CREATE TABLE IF NOT EXISTS market_registry (
  market_id TEXT PRIMARY KEY,
  event_id TEXT,
  yes_token_id TEXT,
  player_id TEXT,
  player_name TEXT,
  team_abbr TEXT,
  question TEXT,
  resolution_text TEXT,
  status TEXT,
  reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS review_queue (
  market_id TEXT PRIMARY KEY,
  event_id TEXT,
  question TEXT,
  resolution_text TEXT,
  yes_token_id TEXT,
  status TEXT,
  reason TEXT,
  discovered_at TEXT,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS market_quotes (
  as_of_hour TEXT NOT NULL,
  model_version TEXT NOT NULL,
  market_id TEXT NOT NULL,
  player_id TEXT,
  bid REAL,
  ask REAL,
  market_mid REAL,
  spread_pp REAL,
  last_trade REAL,
  gamma_price REAL,
  quote_timestamp TEXT,
  fetched_at TEXT,
  quote_status TEXT,
  source_url TEXT,
  PRIMARY KEY (as_of_hour, model_version, market_id),
  CHECK (market_mid IS NULL OR (market_mid >= 0 AND market_mid <= 1))
);

CREATE TABLE IF NOT EXISTS hourly_snapshots (
  as_of_hour TEXT NOT NULL,
  model_version TEXT NOT NULL,
  player_id TEXT NOT NULL,
  player_name TEXT,
  team_abbr TEXT,
  market_id TEXT,
  model_as_of TEXT,
  market_as_of TEXT,
  comparison_as_of TEXT,
  p_model REAL,
  p_market_mid REAL,
  p_market_normalized REAL,
  bid REAL,
  ask REAL,
  spread_pp REAL,
  last_trade REAL,
  gamma_price REAL,
  delta_pp REAL,
  delta_rel REAL,
  vs_bid_pp REAL,
  vs_ask_pp REAL,
  delta_vs_prev_hour_pp REAL,
  p_eligibility REAL,
  coverage REAL,
  coverage_status TEXT,
  row_status TEXT,
  reason TEXT,
  condition_met INTEGER,
  streak_start TEXT,
  streak_length INTEGER,
  PRIMARY KEY (as_of_hour, model_version, player_id),
  CHECK (p_model IS NULL OR (p_model >= 0 AND p_model <= 1)),
  CHECK (p_market_mid IS NULL OR (p_market_mid >= 0 AND p_market_mid <= 1))
);

CREATE TABLE IF NOT EXISTS injury_reports (
  season TEXT NOT NULL,
  as_of TEXT NOT NULL,
  player_id TEXT NOT NULL,
  status TEXT NOT NULL,
  confidence TEXT NOT NULL,
  source_url TEXT,
  note TEXT,
  fetched_at TEXT,
  PRIMARY KEY (season, as_of, player_id)
);

CREATE TABLE IF NOT EXISTS weekly_snapshots (
  season TEXT NOT NULL,
  as_of TEXT NOT NULL,
  player_id TEXT NOT NULL,
  player_name TEXT,
  team_abbr TEXT,
  p_award REAL,
  delta_pp_vs_last_week REAL,
  p_eligibility REAL,
  projected_wins_low REAL,
  projected_wins_high REAL,
  model_version TEXT,
  reasons TEXT,
  created_at TEXT,
  PRIMARY KEY (season, as_of, player_id),
  CHECK (p_award IS NULL OR (p_award >= 0 AND p_award <= 1)),
  CHECK (p_eligibility IS NULL OR (p_eligibility >= 0 AND p_eligibility <= 1))
);

CREATE TABLE IF NOT EXISTS job_status (
  job_id TEXT PRIMARY KEY,
  as_of_hour TEXT,
  started_at TEXT,
  finished_at TEXT,
  status TEXT,
  detail TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  ref_key TEXT,
  detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_game_logs_season_date ON game_logs (season, game_date);
CREATE INDEX IF NOT EXISTS idx_team_results_season_date ON team_results (season, game_date);
CREATE INDEX IF NOT EXISTS idx_hourly_hour ON hourly_snapshots (as_of_hour);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def audit(conn: sqlite3.Connection, event_type: str, ref_key: str | None, detail: str, created_at: str) -> None:
    conn.execute(
        "INSERT INTO audit_log (created_at, event_type, ref_key, detail) VALUES (?, ?, ?, ?)",
        (created_at, event_type, ref_key, detail),
    )


def rows_as_dicts(rows) -> list[dict]:
    return [dict(row) for row in rows]
