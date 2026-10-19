from __future__ import annotations

ALERT_PREFIX = "模型与市场分歧"


def update_streak(previous: dict | None, met: bool, as_of_hour: str, player_id: str, consecutive: int) -> dict:
    if not met:
        return {
            "condition_met": 0,
            "streak_start": None,
            "streak_length": 0,
            "fire": False,
            "ref_key": None,
        }
    if previous and int(previous.get("condition_met") or 0) == 1 and previous.get("streak_start"):
        start = previous["streak_start"]
        length = int(previous.get("streak_length") or 1) + 1
    else:
        start = as_of_hour
        length = 1
    return {
        "condition_met": 1,
        "streak_start": start,
        "streak_length": length,
        "fire": length >= consecutive,
        "ref_key": f"{player_id}:{start}",
    }


def alert_message(player_name: str, delta_pp: float) -> str:
    return f"{ALERT_PREFIX}：{player_name} {delta_pp:+.1f} pp"
