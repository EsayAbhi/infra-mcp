"""TEMPLATE - copy this file to add a new connector (e.g. graylog.py, veeam.py, pure_storage.py).

1. Copy to connectors/<yourtool>.py  (files starting with _ are ignored)
2. Set NAME (must match the key under `connectors:` in config.yaml)
3. Add tools inside register(). Use @guard.tool(mcp) for read tools,
   @guard.tool(mcp, write=True) for anything that changes state.
4. Add a block for it in config.example.yaml. Done - no other file needs editing.
"""
import requests

NAME = "template"
ENABLED_BY_DEFAULT = False


def register(mcp, guard, cfg: dict) -> None:
    base = cfg["url"].rstrip("/")
    verify = cfg.get("verify_ssl", True)

    @guard.tool(mcp)
    def template_ping() -> dict:
        """Describe what this tool does - the AI reads this text to decide when to use it."""
        r = requests.get(f"{base}/health", verify=verify, timeout=15)
        r.raise_for_status()
        return r.json()

    @guard.tool(mcp, write=True)
    def template_restart_service(service: str) -> dict:
        """Example write action: blocked automatically when read_only is true."""
        r = requests.post(f"{base}/services/{service}/restart", verify=verify, timeout=15)
        r.raise_for_status()
        return {"restarted": service}
