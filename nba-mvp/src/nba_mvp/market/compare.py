from __future__ import annotations

from nba_mvp.market.alerts import alert_message, update_streak
from nba_mvp.snapshot import OTHER_ID, OTHER_NAME
from nba_mvp.util import previous_hour


def _prediction_status(prediction: dict) -> str:
    return prediction.get("status") or "pending_training"


def _pp(value):
    if value is None:
        return None
    return round(float(value), 6)


def build_comparison(
    *,
    as_of_hour: str,
    comparison_as_of: str,
    model_version: str,
    model_as_of: str | None,
    predictions: list[dict],
    quotes_by_player: dict[str, dict],
    registry_by_player: dict[str, dict],
    previous_by_player: dict[str, dict],
    existing_alert_keys: set[str],
    unprocessed_games: bool,
    exhaustive: bool,
    alert_abs_delta_pp: float,
    alert_max_spread_pp: float,
    alert_consecutive: int,
) -> dict:
    rows = []
    for prediction in predictions:
        player_id = str(prediction["player_id"])
        market = registry_by_player.get(player_id)
        quote = quotes_by_player.get(player_id) if market else None
        p_model = prediction.get("p_award")
        row = {
            "as_of_hour": as_of_hour,
            "model_version": model_version,
            "player_id": player_id,
            "player_name": prediction.get("player_name") or (OTHER_NAME if player_id == OTHER_ID else player_id),
            "team_abbr": prediction.get("team_abbr"),
            "market_id": None if market is None else market.get("market_id"),
            "model_as_of": model_as_of,
            "market_as_of": None if quote is None else quote.get("quote_timestamp"),
            "comparison_as_of": comparison_as_of,
            "p_model": p_model,
            "p_market_mid": None,
            "p_market_normalized": None,
            "bid": None if quote is None else quote.get("bid"),
            "ask": None if quote is None else quote.get("ask"),
            "spread_pp": None if quote is None else quote.get("spread_pp"),
            "last_trade": None if quote is None else quote.get("last_trade"),
            "gamma_price": None if quote is None else quote.get("gamma_price"),
            "delta_pp": None,
            "delta_rel": None,
            "vs_bid_pp": None,
            "vs_ask_pp": None,
            "delta_vs_prev_hour_pp": None,
            "p_eligibility": prediction.get("p_eligibility"),
            "row_status": _prediction_status(prediction),
            "reason": prediction.get("reason"),
            "condition_met": 0,
            "streak_start": None,
            "streak_length": 0,
        }
        previous = previous_by_player.get(player_id)
        if previous and previous.get("as_of_hour") not in (None, previous_hour(as_of_hour)):
            previous = None
        if previous and previous.get("p_model") is not None and p_model is not None:
            row["delta_vs_prev_hour_pp"] = _pp(100 * (p_model - previous["p_model"]))
        if p_model is None:
            row["row_status"] = _prediction_status(prediction)
        elif market is None:
            row["row_status"] = "unmapped"
            row["reason"] = "没有已审核的主市场"
        elif quote is None or quote.get("quote_status") != "ok" or unprocessed_games:
            if unprocessed_games or (quote and quote.get("quote_status") == "STALE"):
                row["row_status"] = "STALE"
                row["reason"] = "报价过期或应处理的完场比赛尚未进入模型"
            else:
                row["row_status"] = "N/A"
                row["reason"] = "没有有效双边报价"
            if quote and quote.get("quote_status") == "ok":
                row["p_market_mid"] = None
        else:
            mid = quote.get("market_mid")
            row["p_market_mid"] = mid
            row["market_as_of"] = quote.get("quote_timestamp")
            row["row_status"] = "ok"
            row["reason"] = "模型与订单簿中间价"
            if mid is not None:
                row["delta_pp"] = _pp(100 * (p_model - mid))
                if mid > 0:
                    row["delta_rel"] = p_model / mid - 1
            if quote.get("bid") is not None:
                row["vs_bid_pp"] = _pp(100 * (p_model - quote["bid"]))
            if quote.get("ask") is not None:
                row["vs_ask_pp"] = _pp(100 * (p_model - quote["ask"]))
        met = (
            row["row_status"] == "ok"
            and row["delta_pp"] is not None
            and abs(row["delta_pp"]) >= alert_abs_delta_pp
            and row["spread_pp"] is not None
            and row["spread_pp"] <= alert_max_spread_pp
        )
        streak = update_streak(previous, met, as_of_hour, player_id, alert_consecutive)
        row.update(
            {
                "condition_met": streak["condition_met"],
                "streak_start": streak["streak_start"],
                "streak_length": streak["streak_length"],
            }
        )
        row["_fire"] = streak["fire"]
        row["_ref_key"] = streak["ref_key"]
        rows.append(row)

    known_probs = [row["p_model"] for row in rows if row["p_model"] is not None]
    if not known_probs:
        coverage = None
        coverage_status = "pending_training"
    else:
        matched = sum(row["p_model"] for row in rows if row["row_status"] == "ok" and row["p_model"] is not None)
        coverage = matched / sum(known_probs)
        coverage_status = "ok"
    registry_markets = list(registry_by_player.values())
    mids_complete = bool(registry_markets) and all(
        (quotes_by_player.get(str(market["player_id"])) or {}).get("quote_status") == "ok"
        and (quotes_by_player.get(str(market["player_id"])) or {}).get("market_mid") is not None
        for market in registry_markets
    )
    if unprocessed_games or not exhaustive or not mids_complete:
        if coverage_status == "ok":
            coverage_status = "coverage_incomplete"
    else:
        total_mid = sum(quotes_by_player[str(market["player_id"])]["market_mid"] for market in registry_markets)
        if total_mid > 0:
            for row in rows:
                market = registry_by_player.get(row["player_id"])
                if market and row["p_market_mid"] is not None:
                    row["p_market_normalized"] = row["p_market_mid"] / total_mid
        else:
            coverage_status = "coverage_incomplete"

    alerts = []
    for row in rows:
        row["coverage"] = coverage
        row["coverage_status"] = coverage_status
        if row.pop("_fire") and row["_ref_key"] not in existing_alert_keys:
            alerts.append(
                {
                    "ref_key": row["_ref_key"],
                    "as_of_hour": as_of_hour,
                    "player_id": row["player_id"],
                    "message": alert_message(row["player_name"], row["delta_pp"]),
                }
            )
        row.pop("_ref_key", None)
    return {
        "rows": rows,
        "alerts": alerts,
        "coverage": coverage,
        "coverage_status": coverage_status,
        "data_status": "STALE" if unprocessed_games else "ok",
    }
