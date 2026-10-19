from __future__ import annotations

import json
import time
import hashlib
from datetime import datetime
from pathlib import Path

from nba_mvp.util import isoformat, utcnow


class HttpError(Exception):
    def __init__(self, status: int, url: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.url = url


def request_with_retry(
    session,
    method: str,
    url: str,
    *,
    retries: int = 4,
    backoff: float = 0.5,
    timeout: float = 60,
    headers: dict | None = None,
    params: dict | None = None,
):
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            response = session.request(method, url, timeout=timeout, headers=headers, params=params)
        except Exception as exc:  # network errors are retryable
            last_error = exc
            time.sleep(backoff * (2**attempt))
            continue
        if response.status_code in {429, 500, 502, 503, 504}:
            last_error = HttpError(response.status_code, url, f"HTTP {response.status_code}")
            time.sleep(backoff * (2**attempt))
            continue
        return response
    status = last_error.status if isinstance(last_error, HttpError) else 0
    raise HttpError(status, url, f"请求失败: {last_error}")


def archive_raw(raw_dir: Path, url: str, status: int, body, fetched_at: datetime) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    path = raw_dir / f"{fetched_at.strftime('%Y%m%dT%H%M%SZ')}_{digest}.json"
    payload = {
        "url": url,
        "status": status,
        "fetched_at": isoformat(fetched_at),
        "body": body,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def get_json(
    session,
    url: str,
    *,
    raw_dir: Path,
    headers: dict | None = None,
    params: dict | None = None,
    retries: int = 4,
    backoff: float = 0.5,
    timeout: float = 60,
) -> tuple[dict, dict]:
    fetched_at = utcnow()
    try:
        response = request_with_retry(
            session,
            "GET",
            url,
            retries=retries,
            backoff=backoff,
            timeout=timeout,
            headers=headers,
            params=params,
        )
    except HttpError as exc:
        archive_raw(raw_dir, url, exc.status, {"error": str(exc)}, fetched_at)
        raise
    text = response.text
    try:
        body = response.json()
    except Exception:
        body = {"unparsed": text[:4000]}
    archive_raw(raw_dir, url, response.status_code, body, fetched_at)
    meta = {"url": str(response.url), "fetched_at": isoformat(fetched_at), "status": response.status_code}
    if response.status_code >= 400:
        raise HttpError(response.status_code, url, f"HTTP {response.status_code}")
    if not isinstance(body, (dict, list)):
        raise HttpError(response.status_code, url, "响应不是 JSON")
    return body, meta


def get_text(
    session,
    url: str,
    *,
    raw_dir: Path,
    headers: dict | None = None,
    retries: int = 4,
    backoff: float = 0.5,
    timeout: float = 60,
) -> tuple[str, dict]:
    fetched_at = utcnow()
    try:
        response = request_with_retry(
            session,
            "GET",
            url,
            retries=retries,
            backoff=backoff,
            timeout=timeout,
            headers=headers,
        )
    except HttpError as exc:
        archive_raw(raw_dir, url, exc.status, {"error": str(exc)}, fetched_at)
        raise
    archive_raw(raw_dir, url, response.status_code, {"text": response.text}, fetched_at)
    meta = {"url": str(response.url), "fetched_at": isoformat(fetched_at), "status": response.status_code}
    if response.status_code >= 400:
        raise HttpError(response.status_code, url, f"HTTP {response.status_code}")
    return response.text, meta
