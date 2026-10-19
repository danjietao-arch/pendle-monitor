from __future__ import annotations

import math

import numpy as np

FEATURE_NAMES = [
    "pts_per_game",
    "reb_per_game",
    "ast_per_game",
    "stl_per_game",
    "blk_per_game",
    "tov_per_game",
    "win_pct",
    "games_played",
    "prior_pts_per_game",
    "prior_games",
    "prior_win_pct",
]

# Knowable before the season starts. Baseline A never sees in-season rates.
PRESEASON_FEATURE_NAMES = (
    "prior_pts_per_game",
    "prior_games",
    "prior_win_pct",
)


def restrict_features(rows: list[dict], allowed: tuple[str, ...] | list[str]) -> list[dict]:
    """Copy rows so disallowed feature slots are missing, not zero."""
    allowed_names = set(allowed)
    restricted = []
    for row in rows:
        copied = dict(row)
        player = dict(row.get("player_features") or {})
        team = dict(row.get("team_features") or {})
        for name in FEATURE_NAMES:
            if name in allowed_names:
                continue
            player[name] = None
            team[name] = None
        if "games_played" not in allowed_names:
            copied["games_played"] = None
        copied["player_features"] = player
        copied["team_features"] = team
        restricted.append(copied)
    return restricted


def feature_vector(row: dict) -> list[float]:
    merged = {}
    merged.update(row.get("player_features") or {})
    merged.update(row.get("team_features") or {})
    if row.get("games_played") is not None:
        merged["games_played"] = row["games_played"]
    vector = []
    for name in FEATURE_NAMES:
        value = merged.get(name)
        vector.append(np.nan if value is None else float(value))
    return vector


def softmax(scores) -> np.ndarray | None:
    values = np.asarray(scores, dtype=float).reshape(-1)
    if values.size == 0 or not np.all(np.isfinite(values)):
        return None
    shifted = values - np.max(values)
    weights = np.exp(shifted)
    total = float(weights.sum())
    if total <= 0 or not math.isfinite(total):
        return None
    return weights / total


def apply_ineligible(player_ids: list[str], probs: np.ndarray, ineligible_ids: set[str]) -> np.ndarray | None:
    adjusted = np.array(probs, dtype=float, copy=True)
    for index, player_id in enumerate(player_ids):
        if player_id in ineligible_ids:
            adjusted[index] = 0.0
    total = float(adjusted.sum())
    if total <= 0 or not math.isfinite(total):
        return None
    return adjusted / total


def multiclass_scores(player_ids: list[str], probs: np.ndarray, winner_id: str | None) -> dict | None:
    if winner_id not in player_ids:
        return None
    mapping = {player_id: float(prob) for player_id, prob in zip(player_ids, probs)}
    ordered = sorted(player_ids, key=lambda player_id: mapping[player_id], reverse=True)
    brier = sum((mapping[player_id] - (1.0 if player_id == winner_id else 0.0)) ** 2 for player_id in player_ids) / len(
        player_ids
    )
    return {
        "log_loss": -math.log(max(mapping[winner_id], 1e-12)),
        "brier": brier,
        "top1": ordered[0] == winner_id,
        "top3": winner_id in ordered[:3],
    }
