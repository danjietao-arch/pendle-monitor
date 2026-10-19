from __future__ import annotations

import numpy as np

from nba_mvp.model.baseline import ShrinkageNotFitted, shrink_rate
from nba_mvp.snapshot import OTHER_ID


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
