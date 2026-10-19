from __future__ import annotations

import numpy as np

from nba_mvp.model.common import FEATURE_NAMES, PRESEASON_FEATURE_NAMES, apply_ineligible, multiclass_scores, restrict_features, softmax
from nba_mvp.snapshot import OTHER_ID
from nba_mvp.util import is_synthetic_season, season_start_year


def _scores_for(train_rows: list[dict], test_rows: list[dict], allowed: tuple[str, ...] | None = None) -> dict[str, float] | None:
    from nba_mvp.model.baseline import _fit_pipeline, _score

    if allowed is not None:
        train_rows = restrict_features(train_rows, allowed)
        test_rows = restrict_features(test_rows, allowed)
    train_rows = _sort_rows(train_rows)
    test_rows = _sort_rows(test_rows)
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


def _sort_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: (str(row.get("season")), str(row.get("snapshot_key")), str(row.get("player_id"))))


def _winner(rows: list[dict]) -> str | None:
    winners = [row["player_id"] for row in rows if row.get("award_winner") == 1]
    if len(winners) != 1:
        return None
    return winners[0]


def reliability_bins(pairs: list[tuple[float, int]], width: float = 0.1) -> list[dict]:
    """Bin predicted probabilities against observed outcomes. Empty bins stay empty."""
    bins = []
    steps = int(round(1 / width))
    for index in range(steps):
        low = index * width
        high = (index + 1) * width
        chosen = []
        for probability, outcome in pairs:
            if index == steps - 1:
                if low <= probability <= high:
                    chosen.append((probability, outcome))
            elif low <= probability < high:
                chosen.append((probability, outcome))
        count = len(chosen)
        bins.append(
            {
                "low": low,
                "high": high,
                "count": count,
                "mean_predicted": None if count == 0 else float(sum(item[0] for item in chosen) / count),
                "empirical_rate": None if count == 0 else float(sum(item[1] for item in chosen) / count),
            }
        )
    return bins


def reliability_svg(bins: list[dict]) -> str:
    width = 440
    height = 460
    left = 52
    bottom = 46
    plot = 360

    def point(probability: float, rate: float) -> tuple[float, float]:
        return left + probability * plot, bottom + plot - rate * plot

    x0, y0 = point(0, 0)
    x1, y1 = point(1, 1)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="220" y="24" text-anchor="middle" font-family="sans-serif" font-size="14">Baseline B reliability</text>',
        f'<line x1="{left}" y1="{bottom}" x2="{left}" y2="{bottom + plot}" stroke="#222"/>',
        f'<line x1="{left}" y1="{bottom + plot}" x2="{left + plot}" y2="{bottom + plot}" stroke="#222"/>',
        f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" stroke="#999" stroke-dasharray="4 4"/>',
        '<text x="232" y="450" text-anchor="middle" font-family="sans-serif" font-size="12">predicted probability</text>',
        f'<text x="16" y="230" font-family="sans-serif" font-size="12" transform="rotate(-90 16 230)">observed rate</text>',
    ]
    for item in bins:
        if not item["count"] or item["mean_predicted"] is None or item["empirical_rate"] is None:
            continue
        x, y = point(item["mean_predicted"], item["empirical_rate"])
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#0b57d0"/>')
        parts.append(
            f'<text x="{x + 6:.1f}" y="{y - 6:.1f}" font-family="sans-serif" font-size="10" fill="#333">{item["count"]}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts)


def _bootstrap_log_loss_diff(b_losses: list[float], a_losses: list[float], *, draws: int = 1000) -> dict | None:
    if len(b_losses) != len(a_losses) or len(b_losses) < 2:
        return None
    diffs = np.asarray(b_losses, dtype=float) - np.asarray(a_losses, dtype=float)
    rng = np.random.default_rng(0)
    means = []
    for _ in range(draws):
        sample = rng.choice(diffs, size=len(diffs), replace=True)
        means.append(float(sample.mean()))
    means.sort()
    low_index = int(0.025 * (draws - 1))
    high_index = int(0.975 * (draws - 1))
    return {
        "mean_log_loss_b_minus_a": float(diffs.mean()),
        "ci95": [means[low_index], means[high_index]],
        "resamples": draws,
        "folds": len(diffs),
    }


def walk_forward(rows: list[dict]) -> dict:
    """Train on earlier seasons only, and score each snapshot on its own terms.

    Baseline A is fit on earlier preseason rows and is shown only preseason
    features, including when the test snapshot is mid-season or end of season.
    Baseline B is fit on earlier rows of the same snapshot key and uses the
    features knowable at that snapshot. Neither sees the season being scored.
    """
    seasons = sorted({row["season"] for row in rows}, key=season_start_year)
    by_season: dict[str, list[dict]] = {}
    for row in rows:
        by_season.setdefault(row["season"], []).append(row)
    per_key: dict[str, list[dict]] = {}
    evaluated_seasons = []
    calibration_pairs: list[tuple[float, int]] = []
    for test_season in seasons:
        train_rows = [
            row
            for season, items in by_season.items()
            if season_start_year(season) < season_start_year(test_season)
            for row in items
        ]
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
            same_key_train = [row for row in train_rows if row["snapshot_key"] == key]
            probs_b = _scores_for(same_key_train, group) if same_key_train else None
            probs_a = _scores_for(preseason_train, group, PRESEASON_FEATURE_NAMES) if preseason_train else None
            entry = {"season": test_season, "snapshot_key": key}
            if probs_b is not None:
                scored = multiclass_scores(list(probs_b), np.asarray(list(probs_b.values())), winner)
                if scored:
                    entry["baseline_b"] = scored
                    for player_id, probability in probs_b.items():
                        calibration_pairs.append((probability, 1 if player_id == winner else 0))
            if probs_a is not None:
                scored = multiclass_scores(list(probs_a), np.asarray(list(probs_a.values())), winner)
                if scored:
                    entry["baseline_a"] = scored
            per_key.setdefault(key, []).append(entry)
    summary = {}
    comparable_b = []
    comparable_a = []
    comparable_brier_b = []
    comparable_brier_a = []
    fold_results = []
    for key, entries in per_key.items():
        paired = [item for item in entries if "baseline_b" in item and "baseline_a" in item]
        b_losses = [item["baseline_b"]["log_loss"] for item in paired]
        a_losses = [item["baseline_a"]["log_loss"] for item in paired]
        b_brier = [item["baseline_b"]["brier"] for item in paired]
        a_brier = [item["baseline_a"]["brier"] for item in paired]
        top1_b = [item["baseline_b"]["top1"] for item in entries if "baseline_b" in item]
        top3_b = [item["baseline_b"]["top3"] for item in entries if "baseline_b" in item]
        top1_a = [item["baseline_a"]["top1"] for item in entries if "baseline_a" in item]
        top3_a = [item["baseline_a"]["top3"] for item in entries if "baseline_a" in item]
        summary[key] = {
            "folds": len(entries),
            "paired_folds": len(paired),
            "log_loss_b": _mean(b_losses),
            "log_loss_a": _mean(a_losses),
            "brier_b": _mean(b_brier),
            "brier_a": _mean(a_brier),
            "top1_b": _mean([1.0 if value else 0.0 for value in top1_b]),
            "top3_b": _mean([1.0 if value else 0.0 for value in top3_b]),
            "top1_a": _mean([1.0 if value else 0.0 for value in top1_a]),
            "top3_a": _mean([1.0 if value else 0.0 for value in top3_a]),
        }
        comparable_b.extend(b_losses)
        comparable_a.extend(a_losses)
        comparable_brier_b.extend(b_brier)
        comparable_brier_a.extend(a_brier)
        fold_results.extend(entries)
    synthetic = any(is_synthetic_season(season) for season in seasons)
    beats = (
        not synthetic
        and len(set(evaluated_seasons)) >= 3
        and bool(comparable_b)
        and _mean(comparable_b) < _mean(comparable_a)
    )
    not_worse, shortfall = _compare_baselines(comparable_b, comparable_a, comparable_brier_b, comparable_brier_a)
    return {
        "by_snapshot": summary,
        "folds": fold_results,
        "test_seasons": evaluated_seasons,
        "beats_baseline": bool(beats),
        "not_worse_than_a": not_worse,
        "shortfall": shortfall,
        "calibration": {
            "bins": reliability_bins(calibration_pairs),
            "predictions": len(calibration_pairs),
            "positive_outcomes": int(sum(outcome for _probability, outcome in calibration_pairs)),
        },
        "log_loss_interval": _bootstrap_log_loss_diff(comparable_b, comparable_a),
        "feature_names": list(FEATURE_NAMES),
        "baseline_a_features": list(PRESEASON_FEATURE_NAMES),
        "note": (
            "基线 A 只用赛前可知的上场均、上场次和上季球队胜率，即使用赛季末快照打分也不看本季数据。"
            "基线 B 只在同一快照键的更早赛季上拟合，再用该快照当时的特征打分。"
            "这是赛季留出的基线对照，不与市场比较，也不声称优于市场或代表真实实力。"
        ),
        "other_player_id": OTHER_ID,
    }


def _compare_baselines(b_loss, a_loss, b_brier, a_brier) -> tuple[bool, str | None]:
    if not b_loss or not a_loss or len(b_loss) != len(a_loss) or len(b_brier) != len(a_brier):
        return False, "没有可同时比较基线 A 和基线 B 的折。"
    loss_gap = _mean(b_loss) - _mean(a_loss)
    brier_gap = _mean(b_brier) - _mean(a_brier)
    problems = []
    if loss_gap > 0:
        problems.append(f"基线 B 的平均 log loss 比基线 A 高 {loss_gap:.4f}")
    if brier_gap > 0:
        problems.append(f"基线 B 的平均 Brier 比基线 A 高 {brier_gap:.4f}")
    if problems:
        return False, "；".join(problems)
    return True, None


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return float(sum(values) / len(values))
