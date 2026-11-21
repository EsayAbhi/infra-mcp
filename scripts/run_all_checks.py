"""Run ALL checks in one go and print PASS / FAIL for each. Needs no Claude and no manual setup.

  python scripts/run_all_checks.py                      (finds vcsim automatically)
  python scripts/run_all_checks.py --vcsim D:\\vcsim\\vcsim.exe

It starts the fake VMware (vcsim) and fake hardware (Redfish mock) servers itself, unless they are
already running, and stops only the ones it started. Email sending is not tested here.
"""
import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "mock"))

import redfish_mock  # noqa: E402
from infra_mcp.server import build  # noqa: E402

RESULTS = []


def port_open(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def check(name):
    def deco(fn):
        try:
            detail = fn()
            RESULTS.append((name, True, detail or ""))
        except Exception as exc:  # noqa: BLE001
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        status = "PASS" if RESULTS[-1][1] else "FAIL"
        print(f"[{status}] {name}" + (f"  - {RESULTS[-1][2]}" if RESULTS[-1][2] else ""), flush=True)
        return fn
    return deco


def text_of(result) -> str:
    items = result[0] if isinstance(result, tuple) else result
    items = getattr(result, "content", items)
    try:
        return "\n".join(i.text for i in items)
    except Exception:  # noqa: BLE001
        return str(result)


def call(mcp, tool, args=None):
    out = text_of(asyncio.run(mcp.call_tool(tool, args or {})))
    try:
        return json.loads(out)
    except ValueError:
        return {"_raw": out}


def ensure_demo_data():
    """Make sure the simulator has 2 snapshots and 1 powered-off VM (safe to run repeatedly)."""
    from pyVim.connect import SmartConnect
    from pyVmomi import vim

    from infra_mcp.connectors._vmware_soap import collect
    si = SmartConnect(host="127.0.0.1", port=8989, user="user", pwd="pass", disableSslCertValidation=True)
    rows = collect(si.RetrieveContent(), vim.VirtualMachine, ["name", "runtime.powerState", "snapshot"])
    rows.sort(key=lambda r: r["id"])

    def wait(task):
        while task.info.state in ("queued", "running"):
            time.sleep(0.2)

    for row, name in ((rows[0], "before-patching"), (rows[1], "test-snapshot")):
        if row.get("snapshot") is None:
            wait(row["_obj"].CreateSnapshot_Task(name=name, description="demo", memory=False, quiesce=False))
    if str(rows[2].get("runtime.powerState")) == "poweredOn":
        wait(rows[2]["_obj"].PowerOffVM_Task())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vcsim", help="path to vcsim(.exe)")
    args = ap.parse_args()
    started = []

    # ---- start the fake systems (only if not already running) ----------------------------
    if not (port_open(8001) and port_open(8002)):
        started += [redfish_mock.start(8001, "healthy"), redfish_mock.start(8002, "faulty")]
    vcsim_proc = None
    if not port_open(8989):
        exe = args.vcsim or os.environ.get("VCSIM_BIN") or shutil.which("vcsim") or r"D:\vcsim\vcsim.exe"
        if not Path(exe).exists():
            sys.exit(f"vcsim not found at {exe}. Use --vcsim <path>.")
        vcsim_proc = subprocess.Popen([exe, "-l", "127.0.0.1:8989"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if port_open(8989):
                break
            time.sleep(0.2)
    tmp = Path(tempfile.mkdtemp())
    base = f"""
audit_log: {(tmp / 'audit.jsonl').as_posix()}
read_only: {{RO}}
connectors:
  network:
    enabled: true
  redfish:
    enabled: true
    username: admin
    password: admin
    hosts:
      - {{name: good-server, url: "http://127.0.0.1:8001"}}
      - {{name: bad-server, url: "http://127.0.0.1:8002"}}
      - {{name: offline-server, url: "http://127.0.0.1:8009"}}
  vmware:
    enabled: true
    mode: soap
    host: 127.0.0.1
    port: 8989
    username: user
    password: pass
    verify_ssl: false
    snapshot_warn_days: 0
"""
    ro_cfg, rw_cfg = tmp / "ro.yaml", tmp / "rw.yaml"
    ro_cfg.write_text(base.replace("{RO}", "true"))
    rw_cfg.write_text(base.replace("{RO}", "false"))

    try:
        print("\n=== infra-mcp full check ===\n")

        @check("1. Server starts and loads 20 tools")
        def _():
            mcp, _g, _c = build(str(ro_cfg))
            n = len(asyncio.run(mcp.list_tools()))
            assert n == 20, f"expected 20 tools, found {n}"
            return "20 tools"

        mcp, guard, _cfg = build(str(ro_cfg))

        @check("2. infra_status shows read-only and 3 connectors")
        def _():
            r = call(mcp, "infra_status")
            assert r["read_only"] is True and {"network", "redfish", "vmware"} <= set(r["connectors"]), r
            return "read_only=true"

        @check("3. Network: DNS lookup and port check")
        def _():
            assert "127.0.0.1" in str(call(mcp, "dns_lookup", {"name_or_ip": "localhost"})) or "::1" in str(call(mcp, "dns_lookup", {"name_or_ip": "localhost"}))
            assert call(mcp, "check_port", {"host": "127.0.0.1", "port": 8001})["open"] is True

        @check("4. Hardware: fleet check finds the faulty and the offline server")
        def _():
            r = call(mcp, "redfish_fleet_check")
            assert r["checked"] == 3 and r["healthy"] == 1, r
            assert len(r["unhealthy"][0]["problems"]) == 7, r["unhealthy"]
            assert r["unreachable"][0]["host"] == "offline-server"
            return "1 healthy, 1 faulty (7 problems), 1 unreachable"

        @check("5. Hardware: server summary and critical event log")
        def _():
            assert call(mcp, "redfish_server_summary", {"host": "good-server"})["serial"] == "SN-HEALTHY-01"
            ev = call(mcp, "redfish_event_log", {"host": "bad-server", "min_severity": "Critical"})
            assert "Power supply 2" in str(ev), ev

        @check("6. VMware: VMs, hosts, datastore, clusters")
        def _():
            assert call(mcp, "vmware_list_vms")["count"] == 4
            assert call(mcp, "vmware_list_hosts")["count"] == 4
            assert call(mcp, "vmware_datastore_capacity")["datastores"][0]["name"] == "LocalDS_0"
            assert call(mcp, "vmware_list_clusters")["clusters"]
            return "4 VMs, 4 hosts, LocalDS_0"

        ensure_demo_data()

        @check("7. VMware: snapshots and powered-off VM found")
        def _():
            assert call(mcp, "vmware_list_snapshots", {"older_than_days": 0})["count"] >= 2
            assert call(mcp, "vmware_list_vms", {"power_state": "POWERED_OFF"})["count"] >= 1

        @check("8. VMware: host hardware health and alarms")
        def _():
            r = call(mcp, "vmware_host_health")
            assert r["hosts_checked"] == 4 and r["problem_count"] == 0, r
            assert "alarms" in call(mcp, "vmware_active_alarms")

        @check("9. Safety: power action is BLOCKED in read-only mode")
        def _():
            assert "read-only" in str(call(mcp, "vmware_vm_power", {"vm_id": "vm-65", "action": "stop"}))

        @check("10. Safety: power action works when read_only is false (then restored)")
        def _():
            rw, _g, _c = build(str(rw_cfg))
            assert "done" in str(call(rw, "vmware_vm_power", {"vm_id": "vm-65", "action": "stop"}))
            assert "done" in str(call(rw, "vmware_vm_power", {"vm_id": "vm-65", "action": "start"}))

        @check("11. Daily report: CRITICAL with hardware and snapshot findings")
        def _():
            r = call(mcp, "daily_health_report")
            assert r["overall"] == "CRITICAL", r["overall"]
            assert "PSU 2" in r["text"] and "snapshot" in r["text"], r["text"]
            return f"{r['counts']['critical']} critical, {r['counts']['warning']} warnings"

        @check("12. Audit log written and passwords hidden")
        def _():
            log = (tmp / "audit.jsonl").read_text()
            assert "vmware_vm_power" in log and "WriteBlocked" not in "" and "pass" not in log.replace("password", "")
            return f"{len(log.splitlines())} entries"

        @check("13. Command line: --report exits with code 2 and saves a file")
        def _():
            out = tmp / "report.md"
            p = subprocess.run([sys.executable, "-m", "infra_mcp.server", "--config", str(ro_cfg), "--report", "--output", str(out)],
                               capture_output=True, text=True, timeout=120)
            assert p.returncode == 2, (p.returncode, p.stderr[-300:])
            assert "Overall: CRITICAL" in out.read_text()

        @check("14. Real MCP protocol over stdio (what Claude would use)")
        def _():
            p = subprocess.run([sys.executable, str(ROOT / "scripts" / "stdio_check.py"), str(ro_cfg)],
                               capture_output=True, text=True, timeout=180)
            assert "CONNECTED - 20 tools" in p.stdout, (p.stdout[-300:] + p.stderr[-300:])

        @check("15. Automated test suite (pytest)")
        def _():
            env = dict(os.environ)
            if vcsim_proc is not None or args.vcsim or os.environ.get("VCSIM_BIN"):
                env["VCSIM_BIN"] = args.vcsim or os.environ.get("VCSIM_BIN") or r"D:\vcsim\vcsim.exe"
            p = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True, env=env, timeout=300)
            tail = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else p.stderr[-200:]
            assert p.returncode == 0, tail
            return tail
    finally:
        for s in started:
            s.shutdown()
        if vcsim_proc:
            vcsim_proc.terminate()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n=== {len(RESULTS) - len(failed)} of {len(RESULTS)} checks passed ===")
    if failed:
        print("FAILED:", ", ".join(r[0].split('.')[0] for r in failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
