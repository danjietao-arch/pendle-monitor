"""Archive and offline replay for historical MVP seasons.

Fetch stores raw official responses under data/archive/stage1. Replay rebuilds
game logs, votes, and as-of snapshots without contacting the network.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

from nba_mvp.collect.nba_stats import _upsert_games, _upsert_players, _upsert_teams
from nba_mvp.collect.official import (
    boxscore_url,
    extract_boxscore_game,
    parse_boxscore_players,
    parse_mvp_results_html,
    parse_schedule,
    parse_voter_ballot_text,
    parse_voting_results_text,
    merge_final_score,
    schedule_team_rows,
    schedule_url,
    season_start_year,
)
from nba_mvp.collect.votes import save_votes
from nba_mvp.config import project_path
from nba_mvp.db import audit, transaction
from nba_mvp.snapshot import OTHER_ID, build_season_snapshots, load_snapshot_frame
from nba_mvp.util import DataError, isoformat, normalize_name, utcnow

STAGE1_SEASONS = ("2022-23", "2023-24", "2024-25")
# 2014-15 and earlier schedules return HTTP 403 from this environment, so the
# labeled history that can actually be fetched starts at 2015-16.
HISTORICAL_SEASONS = (
    "2015-16",
    "2016-17",
    "2017-18",
    "2018-19",
    "2019-20",
    "2020-21",
    "2021-22",
    "2022-23",
    "2023-24",
    "2024-25",
)
DOCUMENTED_BLOCKERS = (
    {
        "season": "2014-15",
        "item": "schedule",
        "url": "https://data.nba.com/data/10s/v2015/json/mobile_teams/nba/2014/league/00_full_schedule.json",
        "error": "HTTP 403",
        "note": "data.nba.com 拒绝 2014 及更早的赛程。没有赛程就不能对齐箱分，这些赛季不入库，也不补造比赛或选票。",
    },
)
EXPECTED_REGULAR_SEASON_GAMES = 1230
PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
VOTE_SOURCES = {
    "2015-16": {
        "page": "https://pr.nba.com/stephen-curry-2015-16-kia-nba-mvp-award-warriors/",
        "document": "https://pr.nba.com/stephen-curry-2015-16-kia-nba-mvp-award-warriors/",
        "kind": "html",
        "filename": "2015-16-mvp-voting-results.html",
    },
    "2016-17": {
        "page": "https://pr.nba.com/inaugural-nba-awards-winners/",
        "document": "https://ak-static.cms.nba.com/wp-content/uploads/sites/4/2017/06/2016-17-Kia-NBA-Most-Valuable-Player-of-the-Year-Award.pdf",
        "kind": "results",
        "filename": "2016-17-mvp-voting-results.pdf",
    },
    "2017-18": {
        "page": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2018/06/2017-18-Kia-Most-Valuable-Player-Award-Voting-Results.pdf",
        "document": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2018/06/2017-18-Kia-Most-Valuable-Player-Award-Voting-Results.pdf",
        "kind": "results",
        "filename": "2017-18-mvp-voting-results.pdf",
    },
    "2018-19": {
        "page": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2019/06/2018-19-NBA-Most-Valuable-Player-Award-Voting-Totals.pdf",
        "document": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2019/06/2018-19-NBA-Most-Valuable-Player-Award-Voting-Totals.pdf",
        "kind": "results",
        "filename": "2018-19-mvp-voting-results.pdf",
    },
    "2019-20": {
        "page": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2020/09/2019-20-Kia-NBA-Most-Valuable-Player-Voter-Selections.pdf",
        "document": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2020/09/2019-20-Kia-NBA-Most-Valuable-Player-Voter-Selections.pdf",
        "kind": "ballots",
        "filename": "2019-20-mvp-voter-selections.pdf",
        "note": "官方结果 PDF 是扫描件，文本提取没有球员行。总分由选票 PDF 按 10/7/5/3/1 汇总，不对结果图做 OCR。",
    },
    "2020-21": {
        "page": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2021/06/2020-21-NBA-MVP-Voting-Results-Announcement.pdf",
        "document": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2021/06/2020-21-NBA-MVP-Voting-Results-Announcement.pdf",
        "kind": "results",
        "filename": "2020-21-mvp-voting-results.pdf",
    },
    "2021-22": {
        "page": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2022/05/2021-22-Kia-NBA-Most-Valuable-Player-Voting-Results.pdf",
        "document": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2022/05/2021-22-Kia-NBA-Most-Valuable-Player-Voting-Results.pdf",
        "kind": "results",
        "filename": "2021-22-mvp-voting-results.pdf",
    },
    "2022-23": {
        "page": "https://pr.nba.com/voting-results-2022-23-nba-regular-season-awards/",
        "document": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2023/05/2022-23-Kia-NBA-Most-Valuable-Player-Voting-Results.pdf",
        "kind": "results",
        "filename": "2022-23-mvp-voting-results.pdf",
    },
    "2023-24": {
        "page": "https://pr.nba.com/voting-results-2023-24-nba-regular-season-awards/",
        "document": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2024/05/2023-24-Kia-NBA-MVP-Voting-Results.pdf",
        "kind": "results",
        "filename": "2023-24-mvp-voting-results.pdf",
    },
    "2024-25": {
        "page": "https://pr.nba.com/voting-results-2024-25-nba-regular-season-awards/",
        "document": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2025/05/2024-25-Kia-NBA-Most-Valuable-Player-Voter-Selections.pdf",
        "kind": "ballots",
        "filename": "2024-25-mvp-voter-selections.pdf",
        "image": "https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2025/05/2024-25-Kia-NBA-Most-Valuable-Player-Voting-Results-.jpg",
        "image_filename": "2024-25-mvp-voting-results.jpg",
    },
}


def archive_root(cfg: dict) -> Path:
    return project_path(cfg, "archive")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def iter_pdf_texts(path: Path):
    """Yield layout text from pdftotext, then pdfminer, then pypdf.

    2017-18 and 2018-19 results are rotated. pypdf returns an empty string for
    those files; pdftotext -layout and pdfminer with boxes_flow disabled do not.
    Later extractors are only needed when an earlier one is missing or unusable.
    """
    import shutil
    import subprocess

    if shutil.which("pdftotext"):
        proc = subprocess.run(
            ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            yield proc.stdout
    try:
        from pdfminer.high_level import extract_text
        from pdfminer.layout import LAParams

        mined = extract_text(str(path), laparams=LAParams(boxes_flow=None)) or ""
        if mined.strip():
            yield mined
    except Exception:
        pass
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        extracted = "\n".join((page.extract_text(extraction_mode="layout") or "") for page in reader.pages)
        if extracted.strip():
            yield extracted
    except Exception:
        pass


def pdf_layout_text(path: Path) -> str:
    for text in iter_pdf_texts(path):
        return text
    raise DataError(f"无法提取 PDF 文本: {path.name}")


def parse_vote_document(path: Path, kind: str) -> list[dict]:
    if kind == "html":
        return parse_mvp_results_html(path.read_text(encoding="utf-8", errors="replace"))
    if kind not in {"results", "ballots"}:
        raise DataError(f"未知投票文件类型: {kind}")
    parser = parse_voting_results_text if kind == "results" else parse_voter_ballot_text
    last_error = None
    for text in iter_pdf_texts(path):
        try:
            return parser(text)
        except DataError as exc:
            last_error = exc
    if last_error is not None:
        raise DataError(f"{path.name}: {last_error}")
    raise DataError(f"无法提取 PDF 文本: {path.name}")


def season_paths(root: Path, season: str) -> dict[str, Path]:
    return {
        "schedule": root / "schedules" / f"{season}.json",
        "boxscores": root / "boxscores" / f"{season}.jsonl.gz",
        "parts": root / "boxscores" / "parts" / season,
        "votes": root / "votes" / VOTE_SOURCES[season]["filename"],
        "vote_meta": root / "votes" / f"{season}.json",
    }


def _request(session, url: str, timeout: float = 25):
    response = session.get(url, headers=PAGE_HEADERS, timeout=timeout)
    return response


def _fetch_bytes(session, url: str, retries: int = 4) -> tuple[int, bytes, str]:
    last_error = None
    for attempt in range(retries):
        fetched_at = isoformat(utcnow())
        try:
            response = _request(session, url)
        except Exception as exc:
            last_error = exc
            time.sleep(0.4 * (2**attempt))
            continue
        if response.status_code in {429, 500, 502, 503, 504}:
            last_error = f"HTTP {response.status_code}"
            time.sleep(0.4 * (2**attempt))
            continue
        return response.status_code, response.content, fetched_at
    raise DataError(f"请求失败: {url} {last_error}")


def fetch_schedule(session, root: Path, season: str) -> dict:
    url = schedule_url(season)
    status, body, fetched_at = _fetch_bytes(session, url)
    path = season_paths(root, season)["schedule"]
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _write_json(path, {"url": url, "status": status, "fetched_at": fetched_at, "body": None, "error": "json"})
        raise DataError(f"赛程不是 JSON: {season}") from exc
    record = {"url": url, "status": status, "fetched_at": fetched_at, "body": payload if status == 200 else None}
    if status != 200:
        record["error"] = f"HTTP {status}"
    _write_json(path, record)
    if status != 200:
        raise DataError(f"赛程 HTTP {status}: {season}")
    games = parse_schedule(payload)
    return {"season": season, "games": len(games), "fetched_at": fetched_at, "url": url}


def _load_done_ids(parts: Path) -> set[str]:
    if not parts.exists():
        return set()
    return {path.stem for path in parts.glob("*.json")}


def _fetch_one_box(game: dict) -> dict:
    import requests

    url = boxscore_url(game["game_id"])
    session = requests.Session()
    try:
        status, body, fetched_at = _fetch_bytes(session, url, retries=3)
    except DataError as exc:
        return {
            "url": url,
            "status": 0,
            "fetched_at": isoformat(utcnow()),
            "body": None,
            "error": str(exc),
            "game_id": game["game_id"],
        }
    record = {"url": url, "status": status, "fetched_at": fetched_at, "game_id": game["game_id"]}
    if status != 200:
        record["body"] = None
        record["error"] = f"HTTP {status}"
        return record
    try:
        game_body = extract_boxscore_game(body.decode("utf-8", errors="replace"))
    except DataError as exc:
        record["body"] = None
        record["error"] = str(exc)
        return record
    record["body"] = game_body
    return record


def fetch_boxscores(root: Path, season: str, games: list[dict], workers: int = 8) -> dict:
    parts = season_paths(root, season)["parts"]
    parts.mkdir(parents=True, exist_ok=True)
    target = season_paths(root, season)["boxscores"]
    archived_ok = set()
    if target.exists() and not any(parts.glob("*.json")):
        archived = list(iter_boxscore_records(target))
        archived_ok = {str(record.get("game_id")) for record in archived if record.get("body")}
        if len(archived_ok) == len(games):
            failed = [
                {"game_id": record.get("game_id"), "error": record.get("error")}
                for record in archived
                if not record.get("body")
            ]
            return {
                "season": season,
                "games": len(games),
                "packed": len(archived_ok),
                "failures": failed,
                "skipped": True,
            }
    done = _load_done_ids(parts) | archived_ok
    pending = [game for game in games if game["game_id"] not in done]
    lock = threading.Lock()
    failures = []
    completed = len(done)

    def store(record: dict) -> None:
        nonlocal completed
        path = parts / f"{record['game_id']}.json"
        _write_json(path, record)
        with lock:
            completed += 1
            if record.get("body") is None:
                failures.append({"game_id": record["game_id"], "error": record.get("error")})
            if completed % 50 == 0 or completed == len(games):
                print(f"{season} boxscores {completed}/{len(games)} failures={len(failures)}", flush=True)

    if pending:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_fetch_one_box, game) for game in pending]
            for future in as_completed(futures):
                store(future.result())
    packed = _pack_boxscores(root, season)
    for path in parts.glob("*.json"):
        path.unlink()
    return {"season": season, "games": len(games), "packed": packed, "failures": failures}


def _pack_boxscores(root: Path, season: str) -> int:
    parts = season_paths(root, season)["parts"]
    target = season_paths(root, season)["boxscores"]
    target.parent.mkdir(parents=True, exist_ok=True)
    records = {}
    if target.exists():
        for record in iter_boxscore_records(target):
            game_id = str(record.get("game_id") or "")
            if game_id:
                records[game_id] = record
    for path in parts.glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        records[str(record.get("game_id") or path.stem)] = record
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        for game_id in sorted(records):
            handle.write(json.dumps(records[game_id], ensure_ascii=False) + "\n")
    return len(records)


def iter_boxscore_records(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def fetch_votes(session, root: Path, season: str) -> dict:
    spec = VOTE_SOURCES[season]
    status, body, fetched_at = _fetch_bytes(session, spec["document"], retries=3)
    path = season_paths(root, season)["votes"]
    path.parent.mkdir(parents=True, exist_ok=True)
    kind = spec["kind"]
    if status != 200 or not body:
        raise DataError(f"投票文件下载失败: {season} HTTP {status}")
    if kind == "html":
        if b"<html" not in body[:800].lower() and b"<table" not in body[:4000].lower():
            raise DataError(f"投票页面不是 HTML: {season} HTTP {status}")
    elif not body.startswith(b"%PDF"):
        raise DataError(f"投票文件不是 PDF: {season} HTTP {status}")
    path.write_bytes(body)
    image_meta = None
    if spec.get("image"):
        image_status, image_body, image_fetched = _fetch_bytes(session, spec["image"], retries=3)
        image_path = root / "votes" / spec["image_filename"]
        if image_status == 200 and image_body:
            image_path.write_bytes(image_body)
            image_meta = {
                "url": spec["image"],
                "status": image_status,
                "fetched_at": image_fetched,
                "sha256": hashlib.sha256(image_body).hexdigest(),
                "parsed": False,
                "note": "官方投票结果图片仅存档，不 OCR。2024-25 分票由选票 PDF 汇总。",
            }
        else:
            image_meta = {"url": spec["image"], "status": image_status, "parsed": False, "error": "fetch_failed"}
    try:
        rows = parse_vote_document(path, kind)
    except DataError as exc:
        meta = {
            "season": season,
            "url": spec["document"],
            "page": spec["page"],
            "kind": kind,
            "status": status,
            "fetched_at": fetched_at,
            "sha256": hashlib.sha256(body).hexdigest(),
            "rows": 0,
            "winner": None,
            "parsed": False,
            "error": str(exc),
            "note": spec.get("note"),
            "image": image_meta,
        }
        _write_json(season_paths(root, season)["vote_meta"], meta)
        return meta
    meta = {
        "season": season,
        "url": spec["document"],
        "page": spec["page"],
        "kind": kind,
        "status": status,
        "fetched_at": fetched_at,
        "sha256": hashlib.sha256(body).hexdigest(),
        "rows": len(rows),
        "winner": next(row["player_name"] for row in rows if row["is_winner"]),
        "parsed": True,
        "note": spec.get("note"),
        "image": image_meta,
    }
    _write_json(season_paths(root, season)["vote_meta"], meta)
    return meta


def probe_stats_api(session) -> dict:
    url = (
        "https://stats.nba.com/stats/playergamelog?PlayerID=203999"
        "&Season=2024-25&SeasonType=Regular%20Season"
    )
    fetched_at = isoformat(utcnow())
    try:
        response = session.get(url, headers=PAGE_HEADERS, timeout=8)
        return {"url": url, "status": response.status_code, "fetched_at": fetched_at, "ok": response.status_code == 200}
    except Exception as exc:
        return {"url": url, "status": 0, "fetched_at": fetched_at, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def fetch_stage1(cfg: dict, seasons: list[str] | None = None, workers: int = 8) -> dict:
    import requests

    root = archive_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    chosen = list(seasons or STAGE1_SEASONS)
    session = requests.Session()
    stats_probe = probe_stats_api(session)
    summary = {"stats_api": stats_probe, "seasons": {}}
    for season in chosen:
        try:
            print(f"schedule {season}", flush=True)
            schedule = fetch_schedule(session, root, season)
            games = parse_schedule(_read_json(season_paths(root, season)["schedule"])["body"])
            print(f"boxscores {season} {len(games)}", flush=True)
            boxes = fetch_boxscores(root, season, games, workers=workers)
            print(f"votes {season}", flush=True)
            votes = fetch_votes(session, root, season)
            summary["seasons"][season] = {"schedule": schedule, "boxscores": boxes, "votes": votes}
        except DataError as exc:
            print(f"{season} failed: {exc}", flush=True)
            summary["seasons"][season] = {"error": str(exc)}
    present = _archived_seasons(root)
    _write_json(root / "manifest.json", _manifest(root, present, stats_probe))
    return summary


def _archived_seasons(root: Path) -> list[str]:
    found = []
    for season in HISTORICAL_SEASONS:
        schedule = root / "schedules" / f"{season}.json"
        if schedule.exists():
            found.append(season)
    return found


def _manifest(root: Path, seasons: list[str], stats_probe: dict) -> dict:
    document = {
        "stage": 2,
        "created_at": isoformat(utcnow()),
        "stats_api": stats_probe,
        "blockers": list(DOCUMENTED_BLOCKERS),
        "note": (
            "stats.nba.com/stats 在此环境读超时。逐场来自 data.nba.com 官方赛程和 "
            "www.nba.com 箱分页内嵌的官方 game 对象。投票来自 pr.nba.com 链出的 NBA Communications 文件。"
            "2014-15 及更早赛程返回 HTTP 403，没有补造。"
        ),
        "seasons": {},
    }
    for season in seasons:
        paths = season_paths(root, season)
        schedule = _read_json(paths["schedule"])
        games = parse_schedule(schedule["body"]) if schedule.get("body") else []
        failures = []
        ok = 0
        if paths["boxscores"].exists():
            for record in iter_boxscore_records(paths["boxscores"]):
                if record.get("body"):
                    ok += 1
                else:
                    failures.append({"game_id": record.get("game_id"), "error": record.get("error")})
        vote_meta = _read_json(paths["vote_meta"]) if paths["vote_meta"].exists() else None
        document["seasons"][season] = {
            "schedule": {
                "path": str(paths["schedule"].relative_to(root)),
                "url": schedule.get("url"),
                "fetched_at": schedule.get("fetched_at"),
                "sha256": _sha256(paths["schedule"]) if paths["schedule"].exists() else None,
                "regular_season_games": len(games),
            },
            "boxscores": {
                "path": str(paths["boxscores"].relative_to(root)),
                "sha256": _sha256(paths["boxscores"]) if paths["boxscores"].exists() else None,
                "ok": ok,
                "failures": failures,
            },
            "votes": vote_meta,
        }
    return document


GENERATIONAL_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}


def _players_for_season(conn, season: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    query = "SELECT player_id, player_name FROM game_logs WHERE season = ? AND player_name IS NOT NULL"
    for row in conn.execute(query, (season,)):
        found.setdefault(normalize_name(row["player_name"]), []).append(row["player_id"])
    return {name: sorted(set(ids)) for name, ids in found.items()}


def resolve_vote_player_id(player_name: str, names: dict[str, list[str]]) -> str:
    """Match a vote name to one official player id.

    A generational suffix on the box score (III, Jr.) still matches the vote
    name when exactly one player fits. Anything else is left unmatched.
    """
    key = normalize_name(player_name)
    exact = names.get(key, [])
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise DataError(f"投票姓名对应多名球员: {player_name}")
    matches = []
    prefix = f"{key} "
    for name, ids in names.items():
        if not name.startswith(prefix):
            continue
        extra = name[len(prefix) :].split()
        if extra and all(part in GENERATIONAL_SUFFIXES for part in extra):
            matches.extend(ids)
    distinct = sorted(set(matches))
    if len(distinct) == 1:
        return distinct[0]
    folded = key.replace(".", "")
    punct = []
    for name, ids in names.items():
        if name.replace(".", "") == folded:
            punct.extend(ids)
    punct = sorted(set(punct))
    if len(punct) == 1:
        return punct[0]
    raise DataError(f"未匹配到官方 player_id: {player_name}")


def load_season_archive(root: Path, season: str) -> dict:
    paths = season_paths(root, season)
    schedule_record = _read_json(paths["schedule"])
    if schedule_record.get("status") != 200 or not schedule_record.get("body"):
        raise DataError(f"赛程存档不可用: {season}")
    if not paths["boxscores"].exists():
        raise DataError(f"缺少箱分存档: {season}")
    vote_meta = _read_json(paths["vote_meta"])
    try:
        vote_rows = parse_vote_document(paths["votes"], vote_meta["kind"])
        vote_error = None
    except DataError as exc:
        vote_rows = []
        vote_error = str(exc)
    return {
        "schedule_record": schedule_record,
        "schedule_games": parse_schedule(schedule_record["body"]),
        "box_records": list(iter_boxscore_records(paths["boxscores"])),
        "vote_meta": vote_meta,
        "vote_rows": vote_rows,
        "vote_error": vote_error,
    }


def replay_season(conn, cfg: dict, season: str, archive: dict | None = None) -> dict:
    root = archive_root(cfg)
    loaded = archive or load_season_archive(root, season)
    box_by_id = {
        str(record.get("game_id") or ""): record.get("body")
        for record in loaded["box_records"]
        if record.get("body")
    }
    schedule_list = [merge_final_score(game, box_by_id.get(game["game_id"])) for game in loaded["schedule_games"]]
    schedule_games = {game["game_id"]: game for game in schedule_list}
    schedule_meta = {
        "url": loaded["schedule_record"].get("url"),
        "fetched_at": loaded["schedule_record"].get("fetched_at"),
    }
    team_rows = schedule_team_rows(schedule_list)
    player_groups: dict[tuple[str, str], list[dict]] = {}
    unparsed = []
    failed_games = []
    for record in loaded["box_records"]:
        game_id = str(record.get("game_id") or (record.get("body") or {}).get("gameId") or "")
        schedule_game = schedule_games.get(game_id)
        if record.get("body") is None or schedule_game is None:
            failed_games.append({"game_id": game_id, "error": record.get("error") or "missing schedule"})
            continue
        try:
            played, skipped = parse_boxscore_players(record["body"], schedule_game)
        except DataError as exc:
            failed_games.append({"game_id": game_id, "error": str(exc)})
            continue
        unparsed.extend(skipped)
        meta_key = (record.get("url") or boxscore_url(game_id), record.get("fetched_at") or "")
        player_groups.setdefault(meta_key, []).extend(played)
    dates = [game["game_date"] for game in loaded["schedule_games"]]
    season_start = (date.fromisoformat(min(dates)) - timedelta(days=1)).isoformat() if dates else None
    with transaction(conn):
        _upsert_teams(conn, season, team_rows, schedule_meta)
        for (url, fetched_at), games in player_groups.items():
            meta = {"url": url, "fetched_at": fetched_at}
            _upsert_players(conn, games, fetched_at)
            _upsert_games(conn, season, games, meta)
        audit(
            conn,
            "stage1_replay_games",
            season,
            f"team_games={len(team_rows)} player_files={len(player_groups)} failed={len(failed_games)} unparsed={len(unparsed)}",
            schedule_meta.get("fetched_at") or isoformat(utcnow()),
        )
    vote_error = loaded.get("vote_error")
    try:
        if vote_error:
            raise DataError(vote_error)
        names = _players_for_season(conn, season)
        vote_rows = []
        for row in loaded["vote_rows"]:
            copied = dict(row)
            copied["player_id"] = resolve_vote_player_id(row["player_name"], names)
            vote_rows.append(copied)
        save_votes(
            conn,
            season,
            vote_rows,
            {"url": loaded["vote_meta"].get("url"), "fetched_at": loaded["vote_meta"].get("fetched_at")},
        )
        vote_status = "ok"
        vote_error = None
    except DataError as exc:
        vote_rows = []
        vote_status = "parse_failed"
        vote_error = str(exc)
        with transaction(conn):
            conn.execute("DELETE FROM vote_totals WHERE season = ?", (season,))
        audit(conn, "vote_fetch_failed", season, str(exc), loaded["vote_meta"].get("fetched_at") or isoformat(utcnow()))
    snapshots = build_season_snapshots(conn, cfg, season, season_start=season_start)
    frames = {
        key: load_snapshot_frame(conn, season, key)
        for key in ("preseason", "tg_10", "tg_20", "tg_40", "tg_60", "end")
    }
    problems = audit_frames(conn, season, frames)
    return {
        "season": season,
        "season_start": season_start,
        "schedule_games": len(loaded["schedule_games"]),
        "failed_games": failed_games,
        "unparsed_players": unparsed,
        "votes": vote_status,
        "vote_error": vote_error,
        "vote_rows": len(vote_rows),
        "winner": next((row["player_name"] for row in vote_rows if row.get("is_winner")), None),
        "snapshots": snapshots,
        "leakage_problems": problems,
        "frames": frames,
    }


def audit_frames(conn, season: str, frames: dict[str, list[dict]]) -> list[str]:
    from nba_mvp.snapshot import previous_season_label

    seasons = [season]
    prior = previous_season_label(season)
    if prior:
        seasons.append(prior)
    placeholders = ",".join("?" for _ in seasons)
    games = [
        dict(row)
        for row in conn.execute(f"SELECT * FROM game_logs WHERE season IN ({placeholders})", seasons)
    ]
    teams = [
        dict(row)
        for row in conn.execute(f"SELECT * FROM team_results WHERE season IN ({placeholders})", seasons)
    ]
    current_games = [game for game in games if game["season"] == season]
    game_index = {(str(game["player_id"]), str(game["game_id"])): game for game in current_games}
    stamps = {game["source_timestamp"] for game in games if game.get("source_timestamp")}
    stamps.update(team["source_timestamp"] for team in teams if team.get("source_timestamp"))
    problems = []
    for key, rows in frames.items():
        if not rows:
            problems.append(f"{season} {key} 没有快照行")
            continue
        for row in rows:
            if row["player_id"] == OTHER_ID:
                continue
            snapshot_date = row["snapshot_date"]
            for game_id in row["player_features"].get("game_ids") or []:
                game = game_index.get((str(row["player_id"]), str(game_id)))
                if game is None:
                    problems.append(f"{season} {key} {row['player_id']} 缺少比赛 {game_id}")
                    continue
                if snapshot_date and game["game_date"] > snapshot_date:
                    problems.append(f"{season} {key} {row['player_id']} 泄漏 {game_id}")
            if key == "preseason" and row["player_features"].get("game_ids"):
                problems.append(f"{season} preseason 写入了本季比赛 {row['player_id']}")
            if key.startswith("tg_") and row.get("team_id"):
                needed = int(key.split("_", 1)[1])
                current_teams = [team for team in teams if team["season"] == season]
                visible = [
                    team
                    for team in current_teams
                    if str(team["team_id"]) == str(row["team_id"]) and team["game_date"] <= snapshot_date
                ]
                season_team_games = [
                    team for team in current_teams if str(team["team_id"]) == str(row["team_id"])
                ]
                if len(visible) < needed:
                    problems.append(f"{season} {key} {row['player_id']} 球队场次不足")
                if row["team_features"].get("team_games") != len(visible):
                    problems.append(f"{season} {key} {row['player_id']} 球队场次与截止日期不一致")
                if len(season_team_games) > len(visible) and row["team_features"].get("team_games") == len(season_team_games):
                    problems.append(f"{season} {key} {row['player_id']} 使用了全季球队场次")
            used_games = (row["player_features"].get("game_ids") or []) or []
            prior_games = row["player_features"].get("prior_games") or 0
            if (used_games or prior_games or row.get("team_games")) and not row.get("source_timestamp"):
                problems.append(f"{season} {key} {row['player_id']} 缺少 source_timestamp")
            if row.get("source_timestamp") and row["source_timestamp"] not in stamps:
                problems.append(f"{season} {key} {row['player_id']} source_timestamp 不在原始存档")
    return problems


def _frame_brief(rows: list[dict]) -> dict:
    winner = next((row for row in rows if row.get("award_winner") == 1), None)
    dates = sorted({row["snapshot_date"] for row in rows if row.get("snapshot_date")})
    return {
        "rows": len(rows),
        "snapshot_date_min": dates[0] if dates else None,
        "snapshot_date_max": dates[-1] if dates else None,
        "award_winner": None
        if winner is None
        else {
            "player_id": winner["player_id"],
            "player_name": winner["player_name"],
            "games_played": winner["games_played"],
            "team_games": winner["team_games"],
            "team_abbr": winner["team_abbr"],
            "pts_per_game": (winner["player_features"] or {}).get("pts_per_game"),
            "win_pct": (winner["team_features"] or {}).get("win_pct"),
            "source_timestamp": winner.get("source_timestamp"),
            "injury_status": winner.get("injury_status"),
            "official_eligible": winner.get("official_eligible"),
        },
    }


def replay_stage1(cfg: dict, conn, seasons: list[str] | None = None) -> dict:
    chosen = list(seasons or STAGE1_SEASONS)
    details = []
    for season in chosen:
        result = replay_season(conn, cfg, season)
        frames = result.pop("frames")
        result["snapshots_brief"] = {key: _frame_brief(rows) for key, rows in frames.items()}
        details.append(result)
    summary = {
        "stage": 1,
        "model_version": None,
        "model_status": "待训练",
        "target_season_probabilities": "待采集 / 待训练",
        "seasons": details,
        "leakage_ok": all(not item["leakage_problems"] for item in details),
        "incomplete_games": {
            item["season"]: [game["game_id"] for game in item["failed_games"]] for item in details
        },
    }
    report = project_path(cfg, "reports") / "stage1_summary.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def expected_game_id_prefix(season: str) -> str:
    return f"002{season_start_year(season) % 100:02d}"
