"""Grafana connector: find dashboards and CREATE / IMPORT dashboards without knowing JSON, PromQL or SQL.

Easiest path for beginners:
  1. grafana_list_datasources            -> copy the uid of your data source
  2. grafana_import_community_dashboard  -> import a ready-made dashboard by its grafana.com ID
     (e.g. 1860 = Linux Node Exporter Full, 14694 = Windows Exporter) - needs NO queries.
Needs a Grafana service-account token (Editor role to create, Viewer to read).
"""
import requests

NAME = "grafana"
ENABLED_BY_DEFAULT = False

GRAFANA_COM = "https://grafana.com/api/dashboards"
SQL_TYPES = {"mysql", "postgres", "grafana-postgresql-datasource", "mssql"}


def _panel(i: int, spec: dict, ds_uid: str, ds_type: str) -> dict:
    kind = spec.get("type", "timeseries")
    ds = {"type": ds_type, "uid": ds_uid}
    if ds_type in SQL_TYPES:
        target = {"refId": "A", "datasource": ds, "rawQuery": True, "editorMode": "code",
                  "rawSql": spec["query"], "format": "time_series" if kind == "timeseries" else "table"}
    else:  # Prometheus, Loki, InfluxDB-flux style: single expression field
        target = {"refId": "A", "datasource": ds, "expr": spec["query"]}
    panel = {"id": i + 1, "type": kind, "title": spec["title"], "datasource": ds, "targets": [target],
             "gridPos": {"x": (i % 2) * 12, "y": (i // 2) * 8, "w": 12, "h": 8}}
    if spec.get("unit"):  # e.g. percent, bytes, short, s
        panel["fieldConfig"] = {"defaults": {"unit": spec["unit"]}, "overrides": []}
    return panel


def register(mcp, guard, cfg: dict) -> None:
    base = cfg["url"].rstrip("/")
    verify = cfg.get("verify_ssl", True)
    headers = {"Authorization": f"Bearer {cfg['api_token']}", "Content-Type": "application/json"}

    def call(method: str, path: str, **kw):
        r = requests.request(method, f"{base}{path}", headers=headers, verify=verify, timeout=30, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    @guard.tool(mcp)
    def grafana_health() -> dict:
        """Check that Grafana is reachable and show its version."""
        return call("GET", "/api/health")

    @guard.tool(mcp)
    def grafana_list_datasources() -> dict:
        """List data sources (name, type, uid). You need a uid to create or import dashboards."""
        return {"datasources": [{"name": d["name"], "type": d["type"], "uid": d["uid"],
                                 "default": d.get("isDefault", False)} for d in call("GET", "/api/datasources")]}

    @guard.tool(mcp)
    def grafana_list_folders() -> dict:
        """List dashboard folders (name, uid)."""
        return {"folders": [{"title": f["title"], "uid": f["uid"]} for f in call("GET", "/api/folders")]}

    @guard.tool(mcp)
    def grafana_list_dashboards(search: str = "", limit: int = 50) -> dict:
        """Search dashboards by name. Returns title, uid, folder and URL."""
        rows = call("GET", "/api/search", params={"type": "dash-db", "query": search,
                                                   "limit": max(1, min(int(limit), 200))})
        return {"count": len(rows), "dashboards": [
            {"title": d["title"], "uid": d["uid"], "folder": d.get("folderTitle", "General"),
             "url": base + d["url"]} for d in rows]}

    @guard.tool(mcp)
    def grafana_get_dashboard(uid: str) -> dict:
        """Show a dashboard's panels (title, type, queries) so you can review or copy it."""
        d = call("GET", f"/api/dashboards/uid/{uid}")["dashboard"]
        return {"title": d["title"], "uid": uid, "panels": [
            {"title": p.get("title"), "type": p.get("type"),
             "queries": [t.get("expr") or t.get("rawSql") for t in p.get("targets", [])]}
            for p in d.get("panels", [])]}

    @guard.tool(mcp, write=True)
    def grafana_create_dashboard(title: str, datasource_uid: str, panels: list[dict],
                                 folder_uid: str = "", overwrite: bool = False) -> dict:
        """Create a dashboard from a simple list. Each panel: {"title": "...", "query": "...",
        "type": "timeseries|stat|gauge|table" (optional), "unit": "percent|bytes|short" (optional)}.
        'query' is a PromQL expression for Prometheus data sources or a SQL query for SQL data sources.
        Blocked in read-only mode."""
        if not panels:
            raise ValueError("Give at least one panel")
        ds_type = call("GET", f"/api/datasources/uid/{datasource_uid}")["type"]
        dashboard = {"title": title, "schemaVersion": 39, "time": {"from": "now-6h", "to": "now"},
                     "refresh": "1m", "tags": ["infra-mcp"],
                     "panels": [_panel(i, p, datasource_uid, ds_type) for i, p in enumerate(panels)]}
        body = {"dashboard": dashboard, "overwrite": overwrite, "message": "Created by infra-mcp"}
        if folder_uid:
            body["folderUid"] = folder_uid
        res = call("POST", "/api/dashboards/db", json=body)
        return {"status": res.get("status"), "uid": res.get("uid"), "url": base + res.get("url", "")}

    @guard.tool(mcp, write=True)
    def grafana_import_community_dashboard(dashboard_id: int, datasource_uid: str,
                                           folder_uid: str = "", overwrite: bool = True) -> dict:
        """Import a ready-made dashboard from grafana.com by its numeric ID (no queries needed).
        Examples: 1860 Node Exporter Full (Linux), 14694 Windows Exporter, 12239 NVIDIA DCGM.
        Needs internet access from the machine running infra-mcp. Blocked in read-only mode."""
        src = requests.get(f"{GRAFANA_COM}/{int(dashboard_id)}/revisions/latest/download", timeout=30)
        src.raise_for_status()
        return _import(src.json(), datasource_uid, folder_uid, overwrite)

    @guard.tool(mcp, write=True)
    def grafana_import_dashboard_json(dashboard_json: dict, datasource_uid: str,
                                      folder_uid: str = "", overwrite: bool = True) -> dict:
        """Import a dashboard from exported JSON (e.g. one the client already has). Blocked in read-only mode."""
        return _import(dashboard_json, datasource_uid, folder_uid, overwrite)

    def _import(dash: dict, datasource_uid: str, folder_uid: str, overwrite: bool) -> dict:
        inputs = [{"name": i["name"], "type": "datasource", "pluginId": i.get("pluginId", ""),
                   "value": datasource_uid}
                  for i in dash.get("__inputs", []) if i.get("type") == "datasource"]
        body = {"dashboard": {**dash, "id": None}, "overwrite": overwrite, "inputs": inputs,
                "folderUid": folder_uid or ""}
        res = call("POST", "/api/dashboards/import", json=body)
        return {"title": res.get("title"), "uid": res.get("uid"), "url": base + res.get("importedUrl", "")}
