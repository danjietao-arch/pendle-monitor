from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_config(path: Path | None = None) -> dict:
    cfg_path = path or (ROOT / "config.json")
    with cfg_path.open(encoding="utf-8") as handle:
        cfg = json.load(handle)
    cfg["_root"] = str(cfg_path.parent)
    cfg["_config_path"] = str(cfg_path)
    return cfg


def root_path(cfg: dict) -> Path:
    return Path(cfg["_root"])


def project_path(cfg: dict, key: str) -> Path:
    return root_path(cfg) / cfg["paths"][key]
