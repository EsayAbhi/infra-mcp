import asyncio
import os
import textwrap

import pytest

from infra_mcp.config import load_config
from infra_mcp.security import Guard, WriteBlocked, redact
from infra_mcp.server import build_server


def _cfg(tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_PASS", "s3cret")
    cfg = load_config(_cfg(tmp_path, "connectors:\n  x:\n    password: ${MY_PASS}\n"))
    assert cfg["connectors"]["x"]["password"] == "s3cret"
    assert cfg["read_only"] is True  # safe default


def test_redaction():
    assert redact({"password": "a", "host": "b"}) == {"password": "***", "host": "b"}


def test_write_blocked_in_read_only():
    with pytest.raises(WriteBlocked):
        Guard(read_only=True).require_write("x")
    Guard(read_only=False).require_write("x")


def test_host_allowlist():
    assert Guard.host_allowed("a.corp.local", ["*.corp.local"])
    assert not Guard.host_allowed("evil.com", ["*.corp.local"])
    assert Guard.host_allowed("anything", [])


def test_server_loads_enabled_connectors(tmp_path):
    path = _cfg(tmp_path, f"""
        audit_log: {tmp_path}/audit.jsonl
        connectors:
          rest:
            enabled: true
            services: {{}}
          prometheus:
            enabled: true
            url: http://localhost:9090
    """)
    names = {t.name for t in asyncio.run(build_server(path).list_tools())}
    assert {"infra_status", "ping_host", "rest_call", "prometheus_query"} <= names
    assert "vmware_list_vms" not in names  # disabled -> not exposed


def test_rest_write_blocked_and_audit(tmp_path):
    path = _cfg(tmp_path, f"""
        audit_log: {tmp_path}/audit.jsonl
        connectors:
          rest:
            enabled: true
            services:
              demo:
                base_url: http://127.0.0.1:9
                auth: {{type: bearer, token: abc}}
                endpoints:
                  - {{name: create, method: POST, path: /items, write: true}}
    """)
    mcp = build_server(path)
    result = asyncio.run(mcp.call_tool("rest_call", {"service": "demo", "endpoint": "create"}))
    assert "read-only" in str(result)
    assert (tmp_path / "audit.jsonl").exists()


def test_dns_lookup_localhost(tmp_path):
    mcp = build_server(_cfg(tmp_path, f"audit_log: {tmp_path}/a.jsonl\n"))
    result = asyncio.run(mcp.call_tool("dns_lookup", {"name_or_ip": "localhost"}))
    assert "127.0.0.1" in str(result) or "::1" in str(result)


def test_grafana_create_and_import(tmp_path, monkeypatch):
    import requests
    calls = []

    class R:
        def __init__(self, data): self._d, self.content = data, b"x"
        def raise_for_status(self): pass
        def json(self): return self._d

    def fake(method, url, **kw):
        calls.append((method, url, kw.get("json")))
        if "/api/datasources/uid/" in url:
            return R({"type": "prometheus"})
        if url.endswith("/api/dashboards/db"):
            return R({"status": "success", "uid": "abc", "url": "/d/abc/x"})
        if url.endswith("/api/dashboards/import"):
            return R({"title": "T", "uid": "u1", "importedUrl": "/d/u1/t"})
        return R({})

    monkeypatch.setattr(requests, "request", fake)
    monkeypatch.setattr(requests, "get", lambda *a, **k: R({"title": "T", "__inputs": [
        {"name": "DS_PROM", "type": "datasource", "pluginId": "prometheus"}]}))
    path = _cfg(tmp_path, f"""
        read_only: false
        audit_log: {tmp_path}/a.jsonl
        connectors:
          grafana: {{enabled: true, url: "http://g:3000", api_token: tok}}
    """)
    mcp = build_server(path)
    asyncio.run(mcp.call_tool("grafana_create_dashboard", {
        "title": "Demo", "datasource_uid": "ds1",
        "panels": [{"title": "CPU", "query": "up", "unit": "percent"}, {"title": "B", "query": "up"}]}))
    created = [c for c in calls if c[1].endswith("/api/dashboards/db")][0][2]
    assert created["dashboard"]["panels"][1]["gridPos"]["x"] == 12
    assert created["dashboard"]["panels"][0]["targets"][0]["expr"] == "up"
    asyncio.run(mcp.call_tool("grafana_import_community_dashboard", {"dashboard_id": 1860, "datasource_uid": "ds1"}))
    imported = [c for c in calls if c[1].endswith("/api/dashboards/import")][0][2]
    assert imported["inputs"][0]["value"] == "ds1"


def test_grafana_create_blocked_in_read_only(tmp_path):
    path = _cfg(tmp_path, f"""
        audit_log: {tmp_path}/a.jsonl
        connectors:
          grafana: {{enabled: true, url: "http://g:3000", api_token: tok}}
    """)
    r = asyncio.run(build_server(path).call_tool("grafana_create_dashboard",
        {"title": "x", "datasource_uid": "d", "panels": [{"title": "a", "query": "up"}]}))
    assert "read-only" in str(r)
