from __future__ import annotations

import math
import statistics

import numpy as np

from nba_mvp.model.baseline import ShrinkageNotFitted, shrink_rate
from nba_mvp.model.common import apply_ineligible, multiclass_scores, softmax
from nba_mvp.snapshot import OTHER_ID, aggregate_player_games
from nba_mvp.util import season_start_year


def run_monte_carlo(cfg: dict, backtest: dict | None, players: list[dict], shrinkage_w: float | None, *, n: int | None = None, rng=None) -> dict:
    """Season-remainder simulation. Disabled unless config and backtest both allow it."""
    if not cfg.get("monte_carlo", {}).get("enabled", False):
        return {"status": "disabled", "reason": "未启用：需 walk-forward 证明优于基线后才打开", "probabilities": None}
    if not backtest or not backtest.get("beats_baseline"):
        return {"status": "disabled", "reason": "回测未证明优于基线", "probabilities": None}
    if shrinkage_w is None:
        return {"status": "disabled", "reason": "收缩权重未拟合", "probabilities": None}
    if not players:
        return {"status": "disabled", "reason": "没有可模拟的球员", "probabilities": None}
    draws = int(n or cfg.get("monte_carlo", {}).get("simulations") or 2000)
    generator = rng or np.random.default_rng(0)
    prepared = []
    for player in players:
        if player["player_id"] == OTHER_ID:
            prepared.append(player)
            continue
        try:
            rate = shrink_rate(player.get("season_avg"), player.get("prior"), shrinkage_w)
        except ShrinkageNotFitted as exc:
            return {"status": "disabled", "reason": str(exc), "probabilities": None}
        if player.get("remaining_games") is None or player.get("attend_p") is None:
            return {"status": "disabled", "reason": "缺少剩余场次或出勤概率，不能把缺失当成 0", "probabilities": None}
        prepared.append({**player, "rate": rate})
    wins = {player["player_id"]: 0 for player in prepared}
    for _ in range(draws):
        best_id = None
        best_score = None
        for player in prepared:
            if player["player_id"] == OTHER_ID:
                score = float(player.get("score", 0.0))
            else:
                attended = generator.binomial(int(player["remaining_games"]), float(player["attend_p"]))
                production = generator.normal(player["rate"], max(player["rate"] * 0.15, 1.0), size=attended).sum() if attended else 0.0
                score = float(production)
            if best_score is None or score > best_score:
                best_score = score
                best_id = player["player_id"]
        wins[best_id] += 1
    probabilities = {player_id: count / draws for player_id, count in wins.items()}
    total = sum(probabilities.values())
    if abs(total - 1.0) > 1e-9:
        return {"status": "disabled", "reason": "模拟概率未能归一", "probabilities": None}
    return {"status": "ok", "reason": "蒙特卡洛已运行", "probabilities": probabilities, "draws": draws}


def fit_shrinkage_schedule(samples: list[dict]) -> dict | None:
    """Fit w(games) = games / (games + k) by historical error. No fixed game cutoff."""
    usable = []
    for sample in samples:
        games = sample.get("games_played")
        if games is None or sample.get("season_avg") is None or sample.get("prior") is None or sample.get("final_avg") is None:
            continue
        if float(games) < 0:
            continue
        usable.append(sample)
    if len(usable) < 10:
        return None
    best_k = None
    best_error = None
    for k in (1, 2, 5, 8, 12, 20, 30, 45, 60, 90, 120, 180, 250):
        error = 0.0
        for sample in usable:
            games = float(sample["games_played"])
            weight = 0.0 if games == 0 else games / (games + k)
            predicted = weight * float(sample["season_avg"]) + (1.0 - weight) * float(sample["prior"])
            error += (predicted - float(sample["final_avg"])) ** 2
        if best_error is None or error < best_error:
            best_k = k
            best_error = error
    residuals = []
    for sample in usable:
        games = float(sample["games_played"])
        weight = 0.0 if games == 0 else games / (games + best_k)
        predicted = weight * float(sample["season_avg"]) + (1.0 - weight) * float(sample["prior"])
        residuals.append(float(sample["final_avg"]) - predicted)
    residual_sd = math.sqrt(sum(item * item for item in residuals) / len(residuals))
    return {
        "k": best_k,
        "residual_sd": residual_sd,
        "samples": len(usable),
        "form": "games/(games+k)",
    }


def shrinkage_weight(schedule: dict | None, games_played: float | None) -> float | None:
    if schedule is None or games_played is None:
        return None
    games = float(games_played)
    if games < 0:
        return None
    if games == 0:
        return 0.0
    return games / (games + float(schedule["k"]))


def totals_as_of(games: list[dict], as_of: str) -> dict:
    """Aggregate only games already played by as_of."""
    visible = [game for game in games if game.get("game_date") and game["game_date"] <= as_of]
    return aggregate_player_games(visible)


def earlier_season_rows(rows: list[dict], season: str) -> list[dict]:
    year = season_start_year(season)
    return [row for row in rows if season_start_year(str(row["season"])) < year]


def games_still_needed(rule: dict | None, games_played: int | None) -> int | None:
    """Verified award minimum only. The stats-leaderboard 58-game line is ignored."""
    if rule is None or rule.get("status") != "verified" or rule.get("min_games") is None or games_played is None:
        return None
    return max(0, int(rule["min_games"]) - int(games_played))


def attend_probability(games_played: int | None, team_games: int | None, injury_status: str | None) -> float | None:
    """Observed attendance, scaled by an official status. Unknown injury is not zero."""
    if games_played is None or team_games is None or team_games <= 0:
        return None
    observed = min(1.0, max(0.0, float(games_played) / float(team_games)))
    status = str(injury_status or "unknown").strip().lower()
    scale = {"out": 0.2, "doubtful": 0.4, "questionable": 0.6, "probable": 0.85, "available": 1.0}.get(status)
    if scale is None:
        return observed
    return observed * scale


def with_extra_absences(players: list[dict], player_id: str, missed: int) -> list[dict] | None:
    """Copy states after removing future games. Missing remaining games stay missing."""
    copied = []
    found = False
    for player in players:
        item = dict(player)
        if player["player_id"] == player_id:
            found = True
            remaining = player.get("remaining_games")
            if remaining is None:
                return None
            item["remaining_games"] = max(0, int(remaining) - int(missed))
            item["scenario"] = f"再缺席 {missed} 场"
        copied.append(item)
    if not found:
        return None
    return copied


def promotion_decision(
    mc_log_loss: list[float],
    baseline_log_loss: list[float],
    mc_brier: list[float],
    baseline_brier: list[float],
    *,
    synthetic: bool,
) -> dict:
    """Promote Monte Carlo only when calibration and stability both beat Baseline B."""
    folds = len(mc_log_loss)
    result = {
        "promoted": False,
        "primary": "baseline_b",
        "folds": folds,
        "mc_log_loss": None if not mc_log_loss else statistics.fmean(mc_log_loss),
        "baseline_log_loss": None if not baseline_log_loss else statistics.fmean(baseline_log_loss),
        "mc_brier": None if not mc_brier else statistics.fmean(mc_brier),
        "baseline_brier": None if not baseline_brier else statistics.fmean(baseline_brier),
        "mc_log_loss_sd": None if len(mc_log_loss) < 2 else statistics.pstdev(mc_log_loss),
        "baseline_log_loss_sd": None if len(baseline_log_loss) < 2 else statistics.pstdev(baseline_log_loss),
    }
    if synthetic:
        result["reason"] = "合成样本不能把蒙特卡洛升成默认预测"
        return result
    if folds < 3 or len(baseline_log_loss) != folds or len(mc_brier) != folds or len(baseline_brier) != folds:
        result["reason"] = "对照折数不足，默认仍是基线 B"
        return result
    better = result["mc_log_loss"] < result["baseline_log_loss"] and result["mc_brier"] < result["baseline_brier"]
    stable = result["mc_log_loss_sd"] <= result["baseline_log_loss_sd"]
    if better and stable:
        result["promoted"] = True
        result["primary"] = "monte_carlo"
        result["reason"] = "滚动回测的校准和稳定性都优于基线 B"
        return result
    result["reason"] = "滚动回测没有同时优于基线 B 的校准和稳定性，默认仍是基线 B"
    return result


def _rate(player: dict, schedule: dict) -> float | None:
    games = player.get("games_played")
    average = player.get("pts_per_game")
    prior = player.get("prior_pts_per_game")
    weight = shrinkage_weight(schedule, 0 if not games else games)
    if weight is None:
        return None
    if games and average is not None and prior is not None:
        return shrink_rate(average, prior, weight)
    if games and average is not None and prior is None:
        return float(average)
    if (not games) and prior is not None:
        return float(prior)
    return None


def _feature_row(player: dict, pts_per_game: float | None, games_played: int | None, win_pct: float | None, ineligible: bool) -> dict:
    return {
        "player_id": player["player_id"],
        "player_name": player.get("player_name"),
        "team_abbr": player.get("team_abbr"),
        "games_played": games_played,
        "eligibility_status": "ineligible" if ineligible else player.get("eligibility_status") or "unknown",
        "player_features": {
            "pts_per_game": pts_per_game,
            "reb_per_game": player.get("reb_per_game"),
            "ast_per_game": player.get("ast_per_game"),
            "stl_per_game": player.get("stl_per_game"),
            "blk_per_game": player.get("blk_per_game"),
            "tov_per_game": player.get("tov_per_game"),
            "prior_pts_per_game": player.get("prior_pts_per_game"),
            "prior_games": player.get("prior_games"),
            "prior_win_pct": player.get("prior_win_pct"),
        },
        "team_features": {
            "win_pct": win_pct,
            "prior_win_pct": player.get("prior_win_pct"),
        },
    }


def simulate_remainder(
    players: list[dict],
    shrinkage: dict | None,
    award_models: list[dict],
    rule: dict | None,
    *,
    draws: int,
    seed: int,
) -> dict:
    """Draw final stat lines and one vote from the award model. Seeded and explicit about gaps."""
    if shrinkage is None:
        return {"status": "disabled", "reason": "收缩权重尚未由回测拟合，不能预设赛季中段完全相信当季数据", "probabilities": None, "zero_filled": False}
    if not award_models:
        return {"status": "待训练", "reason": "没有可用的获奖倾向模型", "probabilities": None, "zero_filled": False}
    if not players or not any(player["player_id"] == OTHER_ID for player in players):
        return {"status": "待采集", "reason": "候选里没有其他球员", "probabilities": None, "zero_filled": False}
    if draws <= 0:
        return {"status": "disabled", "reason": "模拟次数不足", "probabilities": None, "zero_filled": False}
    for player in players:
        if player["player_id"] == OTHER_ID:
            continue
        if player.get("remaining_games") is None or player.get("remaining_team_games") is None:
            return {
                "status": "待采集",
                "reason": f"{player['player_id']} 缺少剩余场次，不能当成 0",
                "probabilities": None,
                "zero_filled": False,
            }
    from nba_mvp.model.baseline import _score

    rng = np.random.default_rng(seed)
    ids = [player["player_id"] for player in players]
    wins = {player_id: 0 for player_id in ids}
    threshold_hits = {player_id: 0 for player_id in ids}
    official_zero = {player_id: 0 for player_id in ids}
    final_wins: dict[str, list[float]] = {}
    verified = bool(rule and rule.get("status") == "verified" and rule.get("min_games") is not None)
    minimum = int(rule["min_games"]) if verified else None
    sd = float(shrinkage.get("residual_sd") or 0.0)
    if sd <= 0:
        sd = 1e-6
    for _ in range(draws):
        team_win_pct: dict[str, float | None] = {}
        for player in players:
            team_id = str(player.get("team_id") or "")
            if not team_id or team_id in team_win_pct or player["player_id"] == OTHER_ID:
                continue
            remaining = int(player["remaining_team_games"])
            wins_now = player.get("wins")
            played = player.get("team_games")
            current = player.get("win_pct")
            prior = player.get("prior_win_pct")
            weight = shrinkage_weight(shrinkage, played)
            if wins_now is None or played is None or current is None or prior is None or weight is None:
                team_win_pct[team_id] = current
                continue
            rate = min(1.0, max(0.0, shrink_rate(current, prior, weight)))
            future = int(rng.binomial(remaining, rate)) if remaining else 0
            total_games = int(played) + remaining
            team_win_pct[team_id] = None if total_games <= 0 else (float(wins_now) + future) / total_games
            final_wins.setdefault(team_id, []).append(float(wins_now) + future)
        rows = []
        ineligible = set()
        for player in players:
            if player["player_id"] == OTHER_ID:
                rows.append(_feature_row(player, None, 0, None, False))
                continue
            official = player.get("official_eligible")
            if official == 0:
                ineligible.add(player["player_id"])
                official_zero[player["player_id"]] += 1
            remaining = int(player["remaining_games"])
            attend = player.get("attend_p")
            played_now = int(player.get("games_played") or 0)
            if attend is None and remaining > 0:
                attended = None
            elif remaining == 0 or attend == 0:
                attended = 0
            else:
                attended = int(rng.binomial(remaining, float(attend)))
            simulated_games = None if attended is None else played_now + attended
            if verified and official == 1:
                threshold_hits[player["player_id"]] += 1
            elif verified and simulated_games is not None and simulated_games >= minimum:
                threshold_hits[player["player_id"]] += 1
            rate = _rate(player, shrinkage)
            points_now = None
            if player.get("pts_per_game") is not None and played_now:
                points_now = float(player["pts_per_game"]) * played_now
            elif played_now == 0 and attended is not None:
                points_now = 0.0
            future_points = 0.0
            if rate is not None and attended:
                draws_pts = rng.normal(rate, sd, size=attended)
                future_points = float(np.maximum(draws_pts, 0).sum())
            pts_per_game = None
            if attended is not None and points_now is not None and rate is not None and simulated_games:
                pts_per_game = (points_now + future_points) / simulated_games
            elif player.get("pts_per_game") is not None and remaining == 0:
                pts_per_game = float(player["pts_per_game"])
            team_pct = team_win_pct.get(str(player.get("team_id") or ""))
            rows.append(_feature_row(player, pts_per_game, simulated_games, team_pct, player["player_id"] in ineligible))
        artifact = award_models[int(rng.integers(0, len(award_models)))]
        scores = _score(artifact, rows)
        if scores is None:
            continue
        probs = softmax(scores)
        if probs is None:
            continue
        adjusted = apply_ineligible(ids, probs, ineligible)
        if adjusted is None:
            continue
        pick = int(rng.choice(len(ids), p=adjusted))
        wins[ids[pick]] += 1
    total = sum(wins.values())
    if total <= 0:
        return {"status": "待训练", "reason": "获奖模型没有打出分数", "probabilities": None, "zero_filled": False}
    probabilities = {player_id: count / total for player_id, count in wins.items()}
    p_eligibility = {}
    p_games_threshold = {}
    needed = {}
    for player in players:
        player_id = player["player_id"]
        if player_id == OTHER_ID:
            p_eligibility[player_id] = None
            p_games_threshold[player_id] = None
            needed[player_id] = None
            continue
        if player.get("official_eligible") == 0:
            p_eligibility[player_id] = 0.0
        elif player.get("official_eligible") == 1 and verified:
            p_eligibility[player_id] = 1.0
        else:
            p_eligibility[player_id] = None
        p_games_threshold[player_id] = None if not verified else threshold_hits[player_id] / draws
        needed[player_id] = games_still_needed(rule, player.get("games_played"))
    bands = {}
    for team_id, samples in final_wins.items():
        ordered = sorted(samples)
        def quantile(q: float) -> float:
            index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
            return ordered[index]
        bands[team_id] = {"p10": quantile(0.1), "p50": quantile(0.5), "p90": quantile(0.9)}
    return {
        "status": "ok",
        "role": "experimental",
        "primary": False,
        "reason": "实验性赛季余量模拟。默认预测仍要看升格门禁。",
        "seed": seed,
        "draws": draws,
        "counted_draws": total,
        "probabilities": probabilities,
        "p_eligibility": p_eligibility,
        "p_games_threshold": p_games_threshold,
        "games_needed": needed,
        "record_bands": bands,
        "zero_filled": False,
        "official_zero_draws": official_zero,
    }


def score_fold(probabilities: dict[str, float] | None, winner_id: str | None) -> dict | None:
    if not probabilities or winner_id not in probabilities:
        return None
    order = list(probabilities)
    return multiclass_scores(order, np.asarray([probabilities[player_id] for player_id in order]), winner_id)
