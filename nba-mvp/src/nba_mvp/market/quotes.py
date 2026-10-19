from __future__ import annotations

from datetime import datetime

from nba_mvp.config import project_path
from nba_mvp.db import audit
from nba_mvp.http_client import HttpError, get_json
from nba_mvp.util import DataError, hour_end, isoformat, parse_time


def _price(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > 1:
        return None
    return number


def best_bid_ask(book: dict) -> tuple[float | None, float | None]:
    bids = [_price(level.get("price")) for level in (book.get("bids") or []) if isinstance(level, dict)]
    asks = [_price(level.get("price")) for level in (book.get("asks") or []) if isinstance(level, dict)]
    bids = [price for price in bids if price is not None]
    asks = [price for price in asks if price is not None]
    return (max(bids) if bids else None, min(asks) if asks else None)


def classify_quote(book: dict, *, now: datetime, stale_minutes: int = 90) -> dict:
    """Mid is the average of a fresh two-sided book. Last trade and gamma price never replace it."""
    bid, ask = best_bid_ask(book)
    timestamp = None
    if book.get("timestamp") not in (None, ""):
        timestamp = parse_time(book.get("timestamp"))
    spread_pp = None
    if bid is not None and ask is not None:
        spread_pp = (ask - bid) * 100
    quote = {
        "bid": bid,
        "ask": ask,
        "spread_pp": spread_pp,
        "last_trade": _price(book.get("last_trade_price")),
        "gamma_price": _price(book.get("gamma_price")),
        "quote_timestamp": None if timestamp is None else isoformat(timestamp),
        "market_mid": None,
        "quote_status": "N/A",
    }
    if bid is None or ask is None or ask < bid:
        return quote
    if timestamp is None or (now - timestamp).total_seconds() > stale_minutes * 60:
        quote["quote_status"] = "STALE"
        return quote
    quote["market_mid"] = (bid + ask) / 2
    quote["quote_status"] = "ok"
    return quote


def fetch_book(session, cfg: dict, token_id: str) -> tuple[dict, dict]:
    raw_dir = project_path(cfg, "raw")
    url = cfg["polymarket"]["clob_base"].rstrip("/") + "/book"
    return get_json(session, url, raw_dir=raw_dir, params={"token_id": token_id})


def persist_quote(conn, as_of_hour: str, model_version: str, market_id: str, player_id: str | None, quote: dict, *, source_url: str | None, fetched_at: str) -> str:
    timestamp = quote.get("quote_timestamp")
    if timestamp and parse_time(timestamp) >= hour_end(as_of_hour):
        audit(conn, "quote_rejected_future", market_id, timestamp, fetched_at)
        return "rejected_future"
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO market_quotes (
          as_of_hour, model_version, market_id, player_id, bid, ask, market_mid, spread_pp,
          last_trade, gamma_price, quote_timestamp, fetched_at, quote_status, source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            as_of_hour,
            model_version,
            market_id,
            player_id,
            quote.get("bid"),
            quote.get("ask"),
            quote.get("market_mid"),
            quote.get("spread_pp"),
            quote.get("last_trade"),
            quote.get("gamma_price"),
            quote.get("quote_timestamp"),
            fetched_at,
            quote.get("quote_status"),
            source_url,
        ),
    )
    return "inserted" if cursor.rowcount else "exists"


def fetch_registry_books(session, cfg: dict, conn, markets: list[dict], *, as_of_hour: str, model_version: str, now: datetime) -> dict:
    stale_minutes = int(cfg["polymarket"]["stale_minutes"])
    inserted = 0
    failed = []
    for market in markets:
        token_id = market["yes_token_id"]
        try:
            book, meta = fetch_book(session, cfg, token_id)
        except (HttpError, DataError) as exc:
            failed.append({"market_id": market["market_id"], "error": str(exc)})
            audit(conn, "book_fetch_failed", market["market_id"], str(exc), isoformat(now))
            continue
        if isinstance(book, dict):
            classified = classify_quote(book, now=now, stale_minutes=stale_minutes)
        else:
            classified = classify_quote({}, now=now, stale_minutes=stale_minutes)
        result = persist_quote(
            conn,
            as_of_hour,
            model_version,
            market["market_id"],
            str(market["player_id"]),
            classified,
            source_url=meta.get("url"),
            fetched_at=meta.get("fetched_at") or isoformat(now),
        )
        if result == "inserted":
            inserted += 1
    return {"inserted": inserted, "failed": failed}
