"""Generic REST connector: connect ANY tool with a REST API by editing config.yaml only.
Each service lists its endpoints; the AI can then call them by name.
"""
import re

import requests

NAME = "rest"
ENABLED_BY_DEFAULT = False


def _build_auth(auth_cfg: dict | None):
    auth_cfg = auth_cfg or {"type": "none"}
    kind = auth_cfg.get("type", "none")
    if kind == "bearer":
        return {"Authorization": f"Bearer {auth_cfg['token']}"}, None
    if kind == "basic":
        return {}, (auth_cfg["username"], auth_cfg["password"])
    if kind == "header":
        return {auth_cfg["name"]: auth_cfg["value"]}, None
    return {}, None


def register(mcp, guard, cfg: dict) -> None:
    services = cfg.get("services", {})

    def _find(service: str, endpoint: str):
        svc = services.get(service)
        if not svc:
            raise ValueError(f"Unknown service '{service}'. Known: {sorted(services)}")
        ep = next((e for e in svc.get("endpoints", []) if e["name"] == endpoint), None)
        if not ep:
            raise ValueError(f"Unknown endpoint '{endpoint}'. Known: {[e['name'] for e in svc.get('endpoints', [])]}")
        return svc, ep

    @guard.tool(mcp)
    def rest_list_endpoints() -> dict:
        """List every configured REST service and the endpoints you can call with rest_call."""
        return {s: [{"name": e["name"], "method": e.get("method", "GET"), "path": e["path"],
                     "write": bool(e.get("write") or e.get("method", "GET").upper() != "GET"),
                     "description": e.get("description", "")} for e in svc.get("endpoints", [])]
                for s, svc in services.items()}

    @guard.tool(mcp)
    def rest_call(service: str, endpoint: str, path_params: dict | None = None,
                  query: dict | None = None, body: dict | None = None) -> dict:
        """Call a configured REST endpoint. path_params fills {placeholders} in the path,
        query becomes URL parameters, body is sent as JSON. Non-GET endpoints need read_only: false."""
        svc, ep = _find(service, endpoint)
        method = ep.get("method", "GET").upper()
        if ep.get("write") or method != "GET":
            guard.require_write(f"{service}.{endpoint}")
        path = ep["path"]
        for key in re.findall(r"\{(\w+)\}", path):
            if not path_params or key not in path_params:
                raise ValueError(f"Missing path parameter '{key}'")
            path = path.replace("{" + key + "}", requests.utils.quote(str(path_params[key]), safe=""))
        headers, auth = _build_auth(svc.get("auth"))
        r = requests.request(method, svc["base_url"].rstrip("/") + path, params=query,
                             json=body if method != "GET" else None, headers=headers, auth=auth,
                             verify=svc.get("verify_ssl", True), timeout=svc.get("timeout", 30))
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            data = r.text[:4000]
        return {"status": r.status_code, "data": data}
