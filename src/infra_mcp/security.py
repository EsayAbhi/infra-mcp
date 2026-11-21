"""Safety layer shared by every connector: read-only switch, audit log, redaction, host allowlist."""
from __future__ import annotations

import fnmatch
import functools
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

_SECRET_KEY = re.compile(r"pass|token|secret|key|authorization|credential", re.I)


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if _SECRET_KEY.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


class WriteBlocked(PermissionError):
    pass


class Guard:
    def __init__(self, read_only: bool = True, audit_log: str | None = None):
        self.read_only = read_only
        self.audit_path = Path(audit_log) if audit_log else None
        self.checks: dict[str, Callable[[], dict]] = {}  # daily health checks, filled by connectors

    def register_check(self, name: str, fn: Callable[[], dict]) -> None:
        """Connectors call this so the daily report can include them.
        fn() must return {"summary": str, "findings": [{"severity": "critical|warning|info",
        "component": str, "message": str}]}"""
        self.checks[name] = fn

    # ---- audit -------------------------------------------------------------
    def audit(self, **event: Any) -> None:
        if not self.audit_path:
            return
        event["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        try:
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(redact(event), default=str) + "\n")
        except OSError:
            pass  # never let logging break a tool call

    # ---- write protection ----------------------------------------------------
    def require_write(self, action: str) -> None:
        if self.read_only:
            raise WriteBlocked(
                f"'{action}' changes things, but the server is in read-only mode. "
                "Set read_only: false in config.yaml to allow it."
            )

    # ---- host allowlist ------------------------------------------------------
    @staticmethod
    def host_allowed(host: str, patterns: list[str] | None) -> bool:
        if not patterns:
            return True
        return any(fnmatch.fnmatch(host.lower(), p.lower()) for p in patterns)

    # ---- decorator ---------------------------------------------------------
    def tool(self, mcp, *, write: bool = False, name: str | None = None) -> Callable:
        """Register a function as an MCP tool with audit + (optional) write protection.

        @guard.tool(mcp)               -> read-only tool
        @guard.tool(mcp, write=True)   -> blocked while read_only is true
        """
        def decorator(fn: Callable) -> Callable:
            tool_name = name or fn.__name__

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                self.audit(tool=tool_name, write=write, args=kwargs)
                try:
                    if write:
                        self.require_write(tool_name)
                    return fn(*args, **kwargs)
                except Exception as exc:  # return errors as data so the AI can explain them
                    self.audit(tool=tool_name, error=str(exc))
                    return {"error": f"{type(exc).__name__}: {exc}"}

            mcp.tool(name=tool_name)(wrapper)
            return fn

        return decorator
