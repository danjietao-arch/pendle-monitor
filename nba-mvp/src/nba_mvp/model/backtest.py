from __future__ import annotations

import numpy as np

from nba_mvp.model.common import FEATURE_NAMES, apply_ineligible, multiclass_scores, softmax
from nba_mvp.snapshot import OTHER_ID
from nba_mvp.util import is_synthetic_season, season_start_year

# Imported lazily inside the scorer so this module can be imported by baseline
# without a circular import of the sklearn pipeline builder.


def _scores_for(train_rows: list[dict], test_rows: list[dict]) -> dict[str, float] | None:
    from nba_mvp.model.baseline import _fit_pipeline, _score

    artifact = _fit_pipeline(train_rows)
    if artifact is None:
        return None
    scores = _score(artifact, test_rows)
    if scores is None:
        return None
    probs = softmax(scores)
    if probs is None:
        return None
    player_ids = [row["player_id"] for row in test_rows]
    ineligible = {row["player_id"] for row in test_rows if row.get("eligibility_status") == "ineligible"}
    adjusted = apply_ineligible(player_ids, probs, ineligible)
    if adjusted is None:
        return None
    return {player_id: float(prob) for player_id, prob in zip(player_ids, adjusted)}


def _winner(rows: list[dict]) -> str | None:
    winners = [row["player_id"] for row in rows if row.get("award_winner") == 1]
    if len(winners) != 1:
        return None
    return winners[0]


def walk_forward(rows: list[dict]) -> dict:
    """Train on earlier seasons only. Metrics are not a claim of beating the market."""
    seasons = sorted({row["season"] for row in rows}, key=season_start_year)
    by_season: dict[str, list[dict]] = {}
    for row in rows:
        by_season.setdefault(row["season"], []).append(row)
    per_key: dict[str, list[dict]] = {}
    evaluated_seasons = []
    for test_season in seasons:
        train_rows = [row for season, items in by_season.items() if season_start_year(season) < season_start_year(test_season) for row in items]
        if not train_rows:
            continue
        preseason_train = [row for row in train_rows if row["snapshot_key"] == "preseason"]
        test_rows = by_season[test_season]
        keys = sorted({row["snapshot_key"] for row in test_rows})
        evaluated_seasons.append(test_season)
        for key in keys:
            group = [row for row in test_rows if row["snapshot_key"] == key]
            winner = _winner(group)
            if winner is None:
                continue
            probs_b = _scores_for(train_rows, group)
            probs_a = _scores_for(preseason_train, group) if preseason_train else None
            entry = {"season": test_season, "snapshot_key": key}
            if probs_b is not None:
                scored = multiclass_scores(list(probs_b), np.asarray(list(probs_b.values())), winner)
                if scored:
                    entry["baseline_b"] = scored
            if probs_a is not None:
                scored = multiclass_scores(list(probs_a), np.asarray(list(probs_a.values())), winner)
                if scored:
                    entry["baseline_a"] = scored
            per_key.setdefault(key, []).append(entry)
    summary = {}
    comparable_b = []
    comparable_a = []
    for key, entries in per_key.items():
        b_losses = [item["baseline_b"]["log_loss"] for item in entries if "baseline_b" in item]
        a_losses = [item["baseline_a"]["log_loss"] for item in entries if "baseline_a" in item]
        b_brier = [item["baseline_b"]["brier"] for item in entries if "baseline_b" in item]
        top1 = [item["baseline_b"]["top1"] for item in entries if "baseline_b" in item]
        top3 = [item["baseline_b"]["top3"] for item in entries if "baseline_b" in item]
        summary[key] = {
            "folds": len(entries),
            "log_loss_b": _mean(b_losses),
            "log_loss_a": _mean(a_losses),
            "brier_b": _mean(b_brier),
            "top1_b": _mean([1.0 if value else 0.0 for value in top1]),
            "top3_b": _mean([1.0 if value else 0.0 for value in top3]),
        }
        if b_losses and a_losses and len(b_losses) == len(a_losses):
            comparable_b.extend(b_losses)
            comparable_a.extend(a_losses)
    synthetic = any(is_synthetic_season(season) for season in seasons)
    beats = (
        not synthetic
        and len(set(evaluated_seasons)) >= 3
        and bool(comparable_b)
        and _mean(comparable_b) < _mean(comparable_a)
    )
    return {
        "by_snapshot": summary,
        "test_seasons": evaluated_seasons,
        "beats_baseline": bool(beats),
        "feature_names": list(FEATURE_NAMES),
        "note": "这是赛季留出的基线对照，不与市场比较，也不声称优于市场或代表真实实力。",
        "other_player_id": OTHER_ID,
    }


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))
