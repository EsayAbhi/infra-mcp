"""Prometheus-compatible connector (Prometheus, VictoriaMetrics, Grafana Mimir)."""
import requests

NAME = "prometheus"
ENABLED_BY_DEFAULT = False


def register(mcp, guard, cfg: dict) -> None:
    base = cfg["url"].rstrip("/")
    verify = cfg.get("verify_ssl", True)
    headers = {}
    if cfg.get("bearer_token"):
        headers["Authorization"] = f"Bearer {cfg['bearer_token']}"
    auth = (cfg["username"], cfg.get("password", "")) if cfg.get("username") else None

    def get(path: str, **params):
        r = requests.get(f"{base}{path}", params=params, headers=headers, auth=auth, verify=verify, timeout=30)
        r.raise_for_status()
        return r.json()

    @guard.tool(mcp)
    def prometheus_query(promql: str) -> dict:
        """Run an instant PromQL query, e.g. 'up == 0' or '100 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m]))*100'."""
        data = get("/api/v1/query", query=promql)["data"]
        return {"result_type": data["resultType"], "results": data["result"][:200]}

    @guard.tool(mcp)
    def prometheus_targets_down() -> dict:
        """List scrape targets that are currently DOWN."""
        targets = get("/api/v1/targets")["data"]["activeTargets"]
        down = [{"job": t["labels"].get("job"), "instance": t["labels"].get("instance"),
                 "error": t.get("lastError")} for t in targets if t["health"] != "up"]
        return {"total": len(targets), "down_count": len(down), "down": down}

    @guard.tool(mcp)
    def prometheus_active_alerts() -> dict:
        """List currently firing/pending alerts."""
        alerts = get("/api/v1/alerts")["data"]["alerts"]
        return {"count": len(alerts), "alerts": [
            {"name": a["labels"].get("alertname"), "state": a["state"],
             "severity": a["labels"].get("severity"), "instance": a["labels"].get("instance"),
             "summary": a.get("annotations", {}).get("summary")} for a in alerts]}
