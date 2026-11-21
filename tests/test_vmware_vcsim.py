"""Integration tests against the free vcsim simulator. Skipped automatically if vcsim is not available.
Set VCSIM_BIN to the path of vcsim(.exe), or put vcsim on PATH."""
import asyncio
import os
import shutil
import socket
import subprocess
import textwrap
import time

import pytest

from infra_mcp.server import build_server

VCSIM = os.environ.get("VCSIM_BIN") or shutil.which("vcsim")
pytestmark = pytest.mark.skipif(not VCSIM, reason="vcsim not installed (set VCSIM_BIN)")
PORT = 18989


@pytest.fixture(scope="module")
def sim():
    proc = subprocess.Popen([VCSIM, "-l", f"127.0.0.1:{PORT}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.3).close()
            break
        except OSError:
            time.sleep(0.2)
    yield
    proc.terminate()


def _server(tmp_path, read_only=True):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(f"""
        read_only: {str(read_only).lower()}
        audit_log: {tmp_path}/a.jsonl
        connectors:
          vmware: {{enabled: true, mode: soap, host: 127.0.0.1, port: {PORT}, username: user, password: pass, verify_ssl: false}}
    """))
    return build_server(str(p))


def call(mcp, tool, args=None):
    return str(asyncio.run(mcp.call_tool(tool, args or {})))


def test_inventory_tools(sim, tmp_path):
    mcp = _server(tmp_path)
    assert "DC0_H0_VM0" in call(mcp, "vmware_list_vms")
    assert "DC0_H0" in call(mcp, "vmware_list_hosts")
    assert "LocalDS_0" in call(mcp, "vmware_datastore_capacity")
    assert "DC0_C0" in call(mcp, "vmware_list_clusters")
    assert "problem_count" in call(mcp, "vmware_host_health")
    assert "alarms" in call(mcp, "vmware_active_alarms")


def test_get_vm_and_snapshot(sim, tmp_path):
    from pyVim.connect import SmartConnect
    from pyVmomi import vim
    si = SmartConnect(host="127.0.0.1", port=PORT, user="user", pwd="pass", disableSslCertValidation=True)
    vm = next(iter(si.RetrieveContent().viewManager.CreateContainerView(si.content.rootFolder, [vim.VirtualMachine], True).view))
    task = vm.CreateSnapshot_Task(name="pytest-snap", description="", memory=False, quiesce=False)
    while task.info.state in ("queued", "running"):
        time.sleep(0.2)
    mcp = _server(tmp_path)
    assert "pytest-snap" in call(mcp, "vmware_list_snapshots", {"older_than_days": 0})
    assert "pytest-snap" in call(mcp, "vmware_get_vm", {"vm_id": vm._moId})


def test_power_blocked_then_allowed(sim, tmp_path):
    out = call(_server(tmp_path), "vmware_vm_power", {"vm_id": "vm-65", "action": "stop"})
    assert "read-only" in out
    rw = _server(tmp_path, read_only=False)
    assert "done" in call(rw, "vmware_vm_power", {"vm_id": "vm-65", "action": "stop"})
    assert "done" in call(rw, "vmware_vm_power", {"vm_id": "vm-65", "action": "start"})
