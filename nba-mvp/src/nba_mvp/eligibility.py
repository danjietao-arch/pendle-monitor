from __future__ import annotations

import json
from pathlib import Path


def load_rules(path: Path) -> dict[str, dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return {rule["season"]: rule for rule in document.get("rules", [])}


def eligibility_for(rule: dict | None, games_played: int | None, official_eligible: int | None) -> dict:
    """Return eligibility without treating unknown as ineligible.

    official_eligible: 1 confirmed eligible, 0 confirmed ineligible, None unknown.
    """
    if rule is None or rule.get("status") != "verified":
        return {
            "eligibility_status": "unverified",
            "p_eligibility": None,
            "force_award_zero": False,
            "reason": "资格规则未核实，不置 0",
        }
    if official_eligible == 0:
        return {
            "eligibility_status": "ineligible",
            "p_eligibility": 0.0,
            "force_award_zero": True,
            "reason": "官方认定无资格",
        }
    min_games = rule.get("min_games")
    already = official_eligible == 1 or (
        min_games is not None and games_played is not None and games_played >= min_games
    )
    if already:
        return {
            "eligibility_status": "eligible",
            "p_eligibility": 1.0,
            "force_award_zero": False,
            "reason": "已达核实后的资格门槛或官方认定有资格",
        }
    return {
        "eligibility_status": "unknown",
        "p_eligibility": None,
        "force_award_zero": False,
        "reason": "规则已核实但资格未确认，不置 0",
    }
