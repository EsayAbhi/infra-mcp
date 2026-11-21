"""Config loading: YAML file + ${ENV_VAR} expansion + optional .env file."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (no extra dependency). Real env vars always win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: str | None = None) -> dict:
    cfg_path = Path(path or os.environ.get("INFRA_MCP_CONFIG", "config.yaml"))
    _load_dotenv(cfg_path.parent / ".env")
    _load_dotenv(Path(".env"))
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"Config file not found: {cfg_path}. Copy config.example.yaml to config.yaml."
        )
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg = _expand(raw)
    cfg.setdefault("read_only", True)
    cfg.setdefault("audit_log", "audit.jsonl")
    cfg.setdefault("connectors", {})
    return cfg
