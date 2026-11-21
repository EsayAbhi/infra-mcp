import asyncio
import importlib.util
import pathlib
import textwrap

import pytest

from infra_mcp.server import build_server

_spec = importlib.util.spec_from_file_location("redfish_mock", pathlib.Path(__file__).parent / "mock" / "redfish_mock.py")
mock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mock)


@pytest.fixture(scope="module")
def servers():
    a, b = mock.start(18001, "healthy"), mock.start(18002, "faulty")
    yield
    a.shutdown(); b.shutdown()


@pytest.fixture
def mcp(tmp_path, servers):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(f"""
        audit_log: {tmp_path}/a.jsonl
        connectors:
          redfish:
            enabled: true
            username: admin
            password: admin
            hosts:
              - {{name: good, url: "http://127.0.0.1:18001"}}
              - {{name: bad, url: "http://127.0.0.1:18002"}}
              - {{name: dead, url: "http://127.0.0.1:18009"}}
    """))
    return build_server(str(p))


def call(mcp, tool, args=None):
    res = asyncio.run(mcp.call_tool(tool, args or {}))
    return str(res)


def test_summary(mcp):
    out = call(mcp, "redfish_server_summary", {"host": "good"})
    assert "SN-HEALTHY-01" in out and "Xeon" in out


def test_healthy_server_has_no_problems(mcp):
    out = call(mcp, "redfish_hardware_health", {"host": "good"})
    assert "'healthy': True" in out or '"healthy": true' in out


def test_faulty_server_problems_found(mcp):
    out = call(mcp, "redfish_hardware_health", {"host": "bad"})
    for expected in ("Power supply", "PSU 2", "CPU1 Temp", "Drive", "Fan 2"):
        assert expected in out


def test_event_log_filters_severity(mcp):
    out = call(mcp, "redfish_event_log", {"host": "bad", "min_severity": "Critical"})
    assert "Power supply 2 lost input power" in out and "System boot completed" not in out


def test_fleet_check(mcp):
    out = call(mcp, "redfish_fleet_check")
    assert "bad" in out and "dead" in out and "unreachable" in out


def test_unknown_host_blocked(mcp):
    out = call(mcp, "redfish_server_summary", {"host": "10.9.9.9"})
    assert "not a configured host" in out


def test_wrong_password(tmp_path, servers):
    p = tmp_path / "c.yaml"
    p.write_text(f"audit_log: {tmp_path}/a.jsonl\nconnectors:\n  redfish:\n    enabled: true\n    username: admin\n    password: wrong\n    hosts:\n      - {{name: good, url: 'http://127.0.0.1:18001'}}\n")
    assert "401" in call(build_server(str(p)), "redfish_server_summary", {"host": "good"})
