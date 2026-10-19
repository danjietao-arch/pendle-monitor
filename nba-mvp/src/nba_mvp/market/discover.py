from __future__ import annotations

import json

from nba_mvp.config import project_path
from nba_mvp.db import audit, transaction
from nba_mvp.http_client import HttpError, get_json
from nba_mvp.util import DataError, isoformat, normalize_season, utcnow

REQUIRED_FIELDS = (
    "event_id",
    "market_id",
    "yes_token_id",
    "player_id",
    "question",
    "resolution_text",
    "status",
    "reviewed_at",
)


def is_wrong_award(question: str, resolution: str = "") -> bool:
    text = f"{question} {resolution}".casefold()
    return "finals" in text


def yes_token_id(market: dict) -> str | None:
    outcomes = market.get("outcomes")
    tokens = market.get("clobTokenIds")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            return None
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except json.JSONDecodeError:
            return None
    if not isinstance(outcomes, list) or not isinstance(tokens, list):
        return None
    for label, token in zip(outcomes, tokens):
        if str(label).casefold() == "yes" and token:
            return str(token)
    return None


def extract_markets(payload) -> list[dict]:
    markets: list[dict] = []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("markets", "data"):
        values = payload.get(key)
        if isinstance(values, list):
            markets.extend(item for item in values if isinstance(item, dict))
    events = payload.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            nested = event.get("markets") or []
            if not isinstance(nested, list):
                continue
            for market in nested:
                if not isinstance(market, dict):
                    continue
                copied = dict(market)
                copied.setdefault("event_id", event.get("id"))
                copied.setdefault("description", event.get("description"))
                markets.append(copied)
    return markets


def classify_discovered(market: dict, target_season: str) -> tuple[str, str]:
    question = str(market.get("question") or "")
    resolution = str(market.get("description") or market.get("resolution_text") or "")
    if is_wrong_award(question, resolution):
        return "rejected", "wrong_award"
    season = normalize_season(f"{question} {resolution}")
    if season and season != target_season:
        return "rejected", "wrong_season"
    return "unmapped", "pending_review"


def validate_registry(document: dict, target_season: str) -> tuple[list[dict], list[dict]]:
    accepted: list[dict] = []
    errors: list[dict] = []
    grouped: dict[str, list[dict]] = {}
    for market in document.get("markets") or []:
        question = str(market.get("question") or "")
        resolution = str(market.get("resolution_text") or "")
        market_id = market.get("market_id")
        if is_wrong_award(question, resolution):
            errors.append({"market_id": market_id, "reason": "wrong_award"})
            continue
        season = normalize_season(f"{question} {resolution}")
        if season and season != target_season:
            errors.append({"market_id": market_id, "reason": "wrong_season"})
            continue
        if any(not market.get(field) for field in REQUIRED_FIELDS):
            errors.append({"market_id": market_id, "reason": "incomplete"})
            continue
        grouped.setdefault(str(market["player_id"]), []).append(market)
    for player_id, items in grouped.items():
        if len(items) > 1:
            errors.append({"player_id": player_id, "reason": "duplicate_player"})
            continue
        accepted.append(items[0])
    return accepted, errors


def load_registry(cfg: dict) -> dict:
    path = project_path(cfg, "registry")
    document = json.loads(path.read_text(encoding="utf-8"))
    accepted, errors = validate_registry(document, cfg["target_season"])
    return {
        "exhaustive": bool(document.get("exhaustive")),
        "event_id": document.get("event_id"),
        "markets": accepted,
        "errors": errors,
    }


def sync_registry(conn, registry: dict, now: str | None = None) -> None:
    stamp = now or isoformat(utcnow())
    with transaction(conn):
        conn.execute("DELETE FROM market_registry")
        for market in registry["markets"]:
            conn.execute(
                """
                INSERT INTO market_registry (
                  market_id, event_id, yes_token_id, player_id, player_name, team_abbr,
                  question, resolution_text, status, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    market["market_id"],
                    market["event_id"],
                    market["yes_token_id"],
                    str(market["player_id"]),
                    market.get("player_name"),
                    market.get("team_abbr"),
                    market["question"],
                    market["resolution_text"],
                    market["status"],
                    market["reviewed_at"],
                ),
            )
        for error in registry.get("errors") or []:
            audit(conn, "registry_rejected", str(error.get("market_id") or error.get("player_id")), json.dumps(error), stamp)


def queue_discovered(conn, markets: list[dict], target_season: str, now: str | None = None) -> dict:
    stamp = now or isoformat(utcnow())
    counts = {"unmapped": 0, "rejected": 0, "exists": 0}
    with transaction(conn):
        for market in markets:
            status, reason = classify_discovered(market, target_season)
            market_id = str(market.get("id") or market.get("conditionId") or market.get("market_id") or "")
            if not market_id:
                audit(conn, "market_missing_id", None, json.dumps(market)[:500], stamp)
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO review_queue (
                  market_id, event_id, question, resolution_text, yes_token_id, status, reason, discovered_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    market_id,
                    None if market.get("event_id") is None else str(market.get("event_id")),
                    market.get("question"),
                    market.get("description") or market.get("resolution_text"),
                    yes_token_id(market),
                    status,
                    reason,
                    stamp,
                    json.dumps(market, ensure_ascii=False),
                ),
            )
            if cursor.rowcount == 0:
                counts["exists"] += 1
            else:
                counts[status] += 1
    return counts


def search_gamma(session, cfg: dict) -> list[dict]:
    raw_dir = project_path(cfg, "raw")
    found: list[dict] = []
    base = cfg["polymarket"]["gamma_base"].rstrip("/")
    for query in cfg["polymarket"]["search_queries"]:
        payload, _meta = get_json(session, f"{base}/public-search", raw_dir=raw_dir, params={"q": query})
        found.extend(extract_markets(payload))
    return found


def discover_markets(session, cfg: dict, conn) -> dict:
    try:
        found = search_gamma(session, cfg)
    except (HttpError, DataError) as exc:
        audit(conn, "gamma_search_failed", None, str(exc), isoformat(utcnow()))
        return {"status": "stale", "error": str(exc), "unmapped": 0, "rejected": 0}
    counts = queue_discovered(conn, found, cfg["target_season"])
    return {"status": "ok", **counts}
