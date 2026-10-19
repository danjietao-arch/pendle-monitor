from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone

import joblib
import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from nba_mvp.config import project_path
from nba_mvp.db import transaction
from nba_mvp.model.common import FEATURE_NAMES, apply_ineligible, feature_vector, softmax
from nba_mvp.model.backtest import walk_forward
from nba_mvp.snapshot import OTHER_ID, load_snapshot_frame
from nba_mvp.util import isoformat, is_synthetic_season, utcnow

UNTRAINED = "untrained"


class ShrinkageNotFitted(Exception):
    """Raised when a shrinkage weight has not been fit on history."""


def shrink_rate(season_avg: float | None, prior: float | None, weight: float | None) -> float:
    if weight is None:
        raise ShrinkageNotFitted("收缩权重尚未由回测拟合，不能预设赛季中段完全相信当季数据")
    if season_avg is None or prior is None:
        raise ShrinkageNotFitted("缺少当季或赛前场均，不能把缺失填成 0")
    return float(weight) * float(season_avg) + (1.0 - float(weight)) * float(prior)


def fit_shrinkage_w(samples: list[dict]) -> float | None:
    usable = [
        sample
        for sample in samples
        if sample.get("season_avg") is not None and sample.get("prior") is not None and sample.get("final_avg") is not None
    ]
    if len(usable) < 10:
        return None
    best_weight = None
    best_error = None
    for step in range(0, 11):
        weight = step / 10
        error = 0.0
        for sample in usable:
            predicted = weight * float(sample["season_avg"]) + (1.0 - weight) * float(sample["prior"])
            error += (predicted - float(sample["final_avg"])) ** 2
        if best_error is None or error < best_error:
            best_weight = weight
            best_error = error
    return best_weight


def make_pipeline() -> Pipeline:
    return Pipeline(
        [
            ("impute", SimpleImputer(strategy="median")),
            ("clf", LogisticRegression(C=0.1, solver="lbfgs", max_iter=500, random_state=0)),
        ]
    )


def _matrix(rows: list[dict]) -> np.ndarray:
    return np.asarray([feature_vector(row) for row in rows], dtype=float)


def _fit_pipeline(rows: list[dict]):
    if len({int(row["award_winner"]) for row in rows}) < 2:
        return None
    matrix = _matrix(rows)
    keep = ~np.all(np.isnan(matrix), axis=0)
    if not np.any(keep):
        return None
    pipeline = make_pipeline()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pipeline.fit(matrix[:, keep], np.asarray([int(row["award_winner"]) for row in rows], dtype=int))
    return {"pipeline": pipeline, "keep": keep.tolist(), "feature_names": list(FEATURE_NAMES)}


def _score(artifact: dict, rows: list[dict]) -> np.ndarray | None:
    if not rows:
        return None
    matrix = _matrix(rows)
    keep = np.asarray(artifact["keep"], dtype=bool)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        scores = artifact["pipeline"].decision_function(matrix[:, keep])
    return np.asarray(scores, dtype=float).reshape(-1)


def _labeled_rows(conn) -> list[dict]:
    rows = []
    for raw in conn.execute("SELECT * FROM feature_snapshots WHERE award_winner IS NOT NULL"):
        row = dict(raw)
        row["player_features"] = json.loads(row["player_features"] or "{}")
        row["team_features"] = json.loads(row["team_features"] or "{}")
        rows.append(row)
    return rows


def _shrinkage_samples(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        if row["player_id"] == OTHER_ID:
            continue
        grouped.setdefault((row["season"], row["player_id"]), {})[row["snapshot_key"]] = row
    samples = []
    for snapshots in grouped.values():
        early = snapshots.get("tg_20") or snapshots.get("tg_10")
        final = snapshots.get("end")
        if not early or not final:
            continue
        samples.append(
            {
                "season_avg": early["player_features"].get("pts_per_game"),
                "prior": early["player_features"].get("prior_pts_per_game"),
                "final_avg": final["player_features"].get("pts_per_game"),
            }
        )
    return samples


def artifact_allowed(meta: dict | None, cfg: dict) -> bool:
    if not meta or meta.get("status") != "ok":
        return False
    seasons = meta.get("seasons") or []
    if len(seasons) < int(cfg["min_historical_seasons"]):
        return False
    if meta.get("synthetic") and not is_synthetic_season(cfg["target_season"]):
        return False
    if any(name for name in meta.get("feature_names") or [] if "market" in name or "polymarket" in name):
        return False
    return True


def train_baselines(conn, cfg: dict, *, min_seasons: int | None = None) -> dict:
    if cfg.get("polymarket", {}).get("use_in_training"):
        return {
            "status": "refused",
            "reason": "Polymarket 价格不能用于训练。对照必须保留不使用市场的独立基线。",
            "model_version": UNTRAINED,
        }
    required = int(cfg["min_historical_seasons"] if min_seasons is None else min_seasons)
    rows = _labeled_rows(conn)
    seasons = sorted({row["season"] for row in rows})
    report = walk_forward(rows)
    weight = fit_shrinkage_w(_shrinkage_samples(rows))
    synthetic = bool(seasons) and all(is_synthetic_season(season) for season in seasons)
    trained_at = isoformat(utcnow())
    if len(seasons) < required:
        _record_run(
            conn,
            model_version=UNTRAINED,
            kind="baseline_b",
            trained_at=trained_at,
            seasons=seasons,
            metrics=report,
            weight=weight,
            status="pending_training",
            beats=False,
            synthetic=synthetic,
            artifact_path=None,
        )
        return {
            "status": "pending_training",
            "reason": f"有标签赛季 {len(seasons)} 个，少于要求的 {required} 个",
            "model_version": UNTRAINED,
            "metrics": report,
            "shrinkage_w": weight,
        }
    artifact_b = _fit_pipeline(rows)
    preseason_rows = [row for row in rows if row["snapshot_key"] == "preseason"]
    artifact_a = _fit_pipeline(preseason_rows)
    if artifact_b is None:
        return {
            "status": "pending_training",
            "reason": "样本无法拟合基线 B",
            "model_version": UNTRAINED,
            "metrics": report,
        }
    models_dir = project_path(cfg, "models")
    models_dir.mkdir(parents=True, exist_ok=True)
    version = "baseline-b-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_path = models_dir / f"{version}.joblib"
    meta = {
        "model_version": version,
        "kind": "baseline_b",
        "status": "ok",
        "trained_at": trained_at,
        "seasons": seasons,
        "synthetic": synthetic,
        "feature_names": list(FEATURE_NAMES),
        "shrinkage_w": weight,
        "metrics": report,
        "use_market_prices": False,
    }
    joblib.dump({"meta": meta, "baseline_b": artifact_b, "baseline_a": artifact_a}, artifact_path)
    (models_dir / f"{version}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    beats = bool(report.get("beats_baseline")) and not synthetic
    _record_run(
        conn,
        model_version=version,
        kind="baseline_b",
        trained_at=trained_at,
        seasons=seasons,
        metrics=report,
        weight=weight,
        status="ok",
        beats=beats,
        synthetic=synthetic,
        artifact_path=str(artifact_path),
    )
    return {"status": "ok", "model_version": version, "metrics": report, "shrinkage_w": weight, "synthetic": synthetic}


def _record_run(conn, **fields) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT INTO model_runs (
              model_version, kind, trained_at, train_seasons, metrics_json, shrinkage_w,
              status, beats_baseline, synthetic, artifact_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_version) DO UPDATE SET
              trained_at=excluded.trained_at,
              train_seasons=excluded.train_seasons,
              metrics_json=excluded.metrics_json,
              shrinkage_w=excluded.shrinkage_w,
              status=excluded.status,
              beats_baseline=excluded.beats_baseline,
              synthetic=excluded.synthetic,
              artifact_path=excluded.artifact_path
            """,
            (
                fields["model_version"],
                fields["kind"],
                fields["trained_at"],
                json.dumps(fields["seasons"], ensure_ascii=False),
                json.dumps(fields["metrics"], ensure_ascii=False),
                fields["weight"],
                fields["status"],
                1 if fields["beats"] else 0,
                1 if fields["synthetic"] else 0,
                fields["artifact_path"],
            ),
        )


def latest_model(conn) -> dict | None:
    row = conn.execute(
        """
        SELECT * FROM model_runs
        WHERE kind = 'baseline_b' AND status = 'ok' AND artifact_path IS NOT NULL
        ORDER BY trained_at DESC LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def load_artifact(path: str) -> dict:
    return joblib.load(path)


def _group_probabilities(artifact: dict, rows: list[dict]) -> dict[str, float] | None:
    scores = _score(artifact, rows)
    if scores is None:
        return None
    probs = softmax(scores)
    if probs is None:
        return None
    player_ids = [row["player_id"] for row in rows]
    ineligible = {row["player_id"] for row in rows if row.get("eligibility_status") == "ineligible"}
    adjusted = apply_ineligible(player_ids, probs, ineligible)
    if adjusted is None:
        return None
    return {player_id: float(prob) for player_id, prob in zip(player_ids, adjusted)}


def infer(conn, cfg: dict, season: str, *, snapshot_key: str | None = None, model_as_of: str | None = None) -> dict:
    run = latest_model(conn)
    artifact = None
    meta = None
    if run and run.get("artifact_path"):
        try:
            bundle = load_artifact(run["artifact_path"])
            meta = bundle.get("meta")
            artifact = bundle.get("baseline_b")
        except Exception:
            artifact = None
    allowed = artifact_allowed(meta, cfg) and artifact is not None
    keys = [snapshot_key] if snapshot_key else _existing_keys(conn, season)
    if not keys:
        return {"status": "pending_collection", "model_version": UNTRAINED, "model_as_of": None, "rows": 0}
    chosen_status = "pending_training"
    version = UNTRAINED
    written = 0
    resolved_as_of = model_as_of
    created_at = isoformat(utcnow())
    for key in keys:
        rows = load_snapshot_frame(conn, season, key)
        probabilities = _group_probabilities(artifact, rows) if allowed else None
        if probabilities is None:
            status = "pending_collection" if not rows else "pending_training"
            reason = "待采集" if not rows else "待训练"
            if meta and meta.get("synthetic") and not is_synthetic_season(cfg["target_season"]):
                reason = "合成样本模型不能用于目标赛季"
        else:
            status = "ok"
            reason = "基线 B"
            version = meta["model_version"]
            chosen_status = "ok"
        as_of = model_as_of or _snapshot_as_of(rows)
        if resolved_as_of is None and as_of is not None:
            resolved_as_of = as_of
        with transaction(conn):
            conn.execute("DELETE FROM predictions WHERE season = ? AND snapshot_key = ?", (season, key))
            for row in rows:
                p_award = None if probabilities is None else probabilities.get(row["player_id"])
                if probabilities is not None and row.get("eligibility_status") == "ineligible":
                    row_status = "ineligible"
                    row_reason = "官方认定无资格"
                elif probabilities is None and row.get("eligibility_status") == "ineligible":
                    row_status = "ineligible"
                    row_reason = "官方认定无资格；模型未输出完整分布"
                    p_award = None
                else:
                    row_status = status
                    row_reason = reason
                p_eligibility = None
                if row.get("eligibility_status") == "eligible":
                    p_eligibility = 1.0
                elif row.get("eligibility_status") == "ineligible":
                    p_eligibility = 0.0
                conn.execute(
                    """
                    INSERT INTO predictions (
                      season, snapshot_key, model_version, player_id, player_name, team_abbr,
                      as_of, p_award, p_eligibility, status, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        season,
                        key,
                        version if probabilities is not None else UNTRAINED,
                        row["player_id"],
                        row["player_name"],
                        row["team_abbr"],
                        as_of,
                        p_award,
                        p_eligibility,
                        row_status,
                        row_reason,
                        created_at,
                    ),
                )
                written += 1
    return {
        "status": chosen_status if allowed else ("pending_collection" if not written else "pending_training"),
        "model_version": version if allowed else UNTRAINED,
        "model_as_of": resolved_as_of,
        "rows": written,
    }


def _existing_keys(conn, season: str) -> list[str]:
    found = [row["snapshot_key"] for row in conn.execute(
        "SELECT DISTINCT snapshot_key FROM feature_snapshots WHERE season = ?", (season,)
    )]
    order = {"end": 0, "tg_60": 1, "tg_40": 2, "tg_20": 3, "tg_10": 4, "preseason": 5}
    return sorted(found, key=lambda key: order.get(key, 9))


def current_snapshot_key(conn, season: str) -> str | None:
    keys = _existing_keys(conn, season)
    return keys[0] if keys else None


def _snapshot_as_of(rows: list[dict]) -> str | None:
    stamps = [row.get("source_timestamp") for row in rows if row.get("source_timestamp")]
    if stamps:
        return max(stamps)
    dates = [row.get("snapshot_date") for row in rows if row.get("snapshot_date")]
    return max(dates) if dates else None
