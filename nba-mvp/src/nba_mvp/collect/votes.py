from __future__ import annotations

import json
import re

from nba_mvp.config import project_path
from nba_mvp.db import audit, transaction
from nba_mvp.http_client import HttpError, get_text
from nba_mvp.util import DataError, isoformat, normalize_name, utcnow

TAG_RE = re.compile(r"<[^>]+>")
HEADING_RE = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.I | re.S)
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.I | re.S)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.I | re.S)


def _strip(html: str) -> str:
    text = TAG_RE.sub(" ", html)
    return re.sub(r"\s+", " ", text).strip()


def extract_regular_season_mvp_section(html: str) -> str:
    headings = list(HEADING_RE.finditer(html))
    saw_finals = False
    for index, heading in enumerate(headings):
        title = _strip(heading.group(1)).casefold()
        if "finals" in title and ("mvp" in title or "most valuable" in title):
            saw_finals = True
            continue
        if "most valuable player" not in title and "mvp" not in title:
            continue
        if "finals" in title:
            saw_finals = True
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(html)
        return html[heading.end() : end]
    if saw_finals:
        raise DataError("仅发现总决赛 MVP，拒绝解析")
    raise DataError("未找到常规赛 MVP 投票表")


def _parse_table_rows(section: str) -> list[dict]:
    parsed = []
    for row_html in ROW_RE.findall(section):
        cells = [_strip(cell) for cell in CELL_RE.findall(row_html)]
        cells = [cell for cell in cells if cell]
        if len(cells) < 2:
            continue
        numbers = []
        name = None
        for cell in cells:
            if re.fullmatch(r"\d+", cell):
                numbers.append(int(cell))
            elif name is None and re.search(r"[A-Za-z\u4e00-\u9fff]", cell):
                name = cell
        if name is None or not numbers:
            continue
        parsed.append(
            {
                "player_name": name,
                "vote_points": numbers[0],
                "first_place_votes": numbers[1] if len(numbers) > 1 else None,
            }
        )
    if not parsed:
        raise DataError("投票表没有可解析的球员行")
    return parsed


def _mark_winner(rows: list[dict]) -> list[dict]:
    points = [row["vote_points"] for row in rows]
    best = max(points)
    if points.count(best) != 1:
        raise DataError("最高票并列，拒绝猜测获奖者")
    total = sum(points)
    for row in rows:
        row["is_winner"] = 1 if row["vote_points"] == best else 0
        row["vote_share"] = (row["vote_points"] / total) if total else None
    return rows


def parse_vote_html(html: str) -> list[dict]:
    section = extract_regular_season_mvp_section(html)
    return _mark_winner(_parse_table_rows(section))


def parse_vote_json(text: str) -> list[dict]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataError("投票 JSON 无法解析") from exc
    raw_rows = payload.get("votes") if isinstance(payload, dict) else None
    if not isinstance(raw_rows, list) or not raw_rows:
        raise DataError("投票 JSON 缺少 votes")
    rows = []
    for raw in raw_rows:
        if "player_name" not in raw or "vote_points" not in raw:
            raise DataError("投票行缺少 player_name 或 vote_points")
        rows.append(
            {
                "player_name": str(raw["player_name"]),
                "player_id": None if raw.get("player_id") in (None, "") else str(raw["player_id"]),
                "vote_points": float(raw["vote_points"]),
                "first_place_votes": None if raw.get("first_place_votes") is None else float(raw["first_place_votes"]),
            }
        )
    if any(row["player_id"] is None for row in rows):
        raise DataError("投票 JSON 缺少 player_id，拒绝按姓名猜测")
    return _mark_winner(rows)


def match_vote_player_ids(rows: list[dict], players_by_name: dict[str, str]) -> list[dict]:
    matched = []
    for row in rows:
        if row.get("player_id"):
            matched.append(row)
            continue
        player_id = players_by_name.get(normalize_name(row["player_name"]))
        if not player_id:
            raise DataError(f"未匹配到官方 player_id: {row['player_name']}")
        copied = dict(row)
        copied["player_id"] = player_id
        matched.append(copied)
    return matched


def _players_by_name(conn) -> dict[str, str]:
    found = {}
    for row in conn.execute("SELECT player_id, player_name FROM players"):
        if row["player_name"]:
            found[normalize_name(row["player_name"])] = row["player_id"]
    return found


def save_votes(conn, season: str, rows: list[dict], meta: dict) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM vote_totals WHERE season = ?", (season,))
        for row in rows:
            conn.execute(
                """
                INSERT INTO vote_totals (
                  season, player_id, player_name, vote_points, first_place_votes, vote_share,
                  is_winner, source_url, source_timestamp, parse_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok')
                """,
                (
                    season,
                    row["player_id"],
                    row["player_name"],
                    row["vote_points"],
                    row.get("first_place_votes"),
                    row.get("vote_share"),
                    row["is_winner"],
                    meta.get("url"),
                    meta.get("fetched_at"),
                ),
            )


def collect_votes(session, cfg: dict, conn, season: str, url: str) -> dict:
    raw_dir = project_path(cfg, "raw")
    try:
        text, meta = get_text(session, url, raw_dir=raw_dir)
        rows = parse_vote_json(text) if text.lstrip().startswith("{") else parse_vote_html(text)
        rows = match_vote_player_ids(rows, _players_by_name(conn))
        save_votes(conn, season, rows, meta)
    except (HttpError, DataError) as exc:
        audit(conn, "vote_fetch_failed", season, str(exc), isoformat(utcnow()))
        return {"status": "parse_failed", "season": season, "error": str(exc)}
    audit(conn, "vote_fetch_ok", season, f"rows={len(rows)}", meta["fetched_at"])
    return {"status": "ok", "season": season, "rows": len(rows), "fetched_at": meta["fetched_at"]}
