"""VMware SOAP backend (pyvmomi). Works with vCenter, ESXi and the vcsim simulator."""
from datetime import datetime, timezone

try:
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim, vmodl
except ImportError:  # pragma: no cover
    SmartConnect = None

_GB = 1024 ** 3


class _Conn:
    """Keeps one logged-in session and reconnects automatically if it expires."""

    def __init__(self, cfg: dict):
        if SmartConnect is None:
            raise RuntimeError("pyvmomi is not installed: pip install pyvmomi")
        self.cfg = cfg
        self.si = None

    def connect(self):
        c = self.cfg
        self.si = SmartConnect(host=c["host"], user=c["username"], pwd=c["password"],
                               port=int(c.get("port", 443)),
                               disableSslCertValidation=not c.get("verify_ssl", True))
        return self.si

    @property
    def content(self):
        last = None
        for _ in (1, 2):
            try:
                if self.si is None:
                    self.connect()
                self.si.CurrentTime()  # cheap call: raises if the session expired
                return self.si.RetrieveContent()
            except Exception as exc:
                last, self.si = exc, None
        raise RuntimeError(f"Cannot connect to VMware ({type(last).__name__}: {last}). "
                           "Check host, port, username/password and verify_ssl.")


def collect(content, vimtype, props: list[str]) -> list[dict]:
    """Fetch many objects with a few properties in ONE round trip (fast even with 1000s of VMs)."""
    view = content.viewManager.CreateContainerView(content.rootFolder, [vimtype], True)
    try:
        tspec = vmodl.query.PropertyCollector.TraversalSpec(name="traverse", path="view", skip=False, type=type(view))
        ospec = vmodl.query.PropertyCollector.ObjectSpec(obj=view, skip=True, selectSet=[tspec])
        pspec = vmodl.query.PropertyCollector.PropertySpec(type=vimtype, pathSet=props, all=False)
        fspec = vmodl.query.PropertyCollector.FilterSpec(objectSet=[ospec], propSet=[pspec])
        pc = content.propertyCollector
        res = pc.RetrievePropertiesEx([fspec], vmodl.query.PropertyCollector.RetrieveOptions(maxObjects=500))
        out = []
        while res:
            for o in res.objects:
                row = {"_obj": o.obj, "id": o.obj._moId}
                row.update({p.name: p.val for p in (o.propSet or [])})
                out.append(row)
            res = pc.ContinueRetrievePropertiesEx(res.token) if res.token else None
        return out
    finally:
        view.Destroy()


def _walk_snapshots(nodes, depth=0):
    for n in nodes or []:
        yield n, depth
        yield from _walk_snapshots(n.childSnapshotList, depth + 1)


def _wait(task, timeout=120):
    import time
    end = time.time() + timeout
    while task.info.state in ("queued", "running"):
        if time.time() > end:
            raise TimeoutError("VMware task is still running")
        time.sleep(1)
    if task.info.state == "error":
        raise RuntimeError(str(task.info.error.msg))


def register_soap(mcp, guard, cfg: dict) -> None:
    conn = _Conn(cfg)

    @guard.tool(mcp)
    def vmware_list_vms(name_contains: str = "", power_state: str = "", limit: int = 100) -> dict:
        """List virtual machines. power_state: POWERED_ON, POWERED_OFF or SUSPENDED (optional)."""
        rows = collect(conn.content, vim.VirtualMachine,
                       ["name", "runtime.powerState", "config.hardware.numCPU", "config.hardware.memoryMB", "overallStatus"])
        want = power_state.lower().replace("_", "") if power_state else ""
        out = []
        for r in rows:
            state = str(r.get("runtime.powerState", ""))
            if name_contains and name_contains.lower() not in str(r.get("name", "")).lower():
                continue
            if want and state.lower().replace("_", "") != want:
                continue
            out.append({"id": r["id"], "name": r.get("name"), "power": state, "cpu": r.get("config.hardware.numCPU"),
                        "memory_mb": r.get("config.hardware.memoryMB"), "status": str(r.get("overallStatus"))})
        total = len(out)
        return {"count": total, "vms": out[: max(1, min(int(limit), 500))], "truncated": total > limit}

    @guard.tool(mcp)
    def vmware_get_vm(vm_id: str) -> dict:
        """Details of one VM by id (e.g. 'vm-123' from vmware_list_vms): host, datastores, VMware Tools, IP, snapshots."""
        rows = collect(conn.content, vim.VirtualMachine,
                       ["name", "runtime.powerState", "runtime.host", "config.hardware.numCPU", "config.hardware.memoryMB",
                        "guest.toolsRunningStatus", "guest.ipAddress", "guest.guestFullName", "overallStatus",
                        "datastore", "snapshot"])
        r = next((x for x in rows if x["id"] == vm_id), None)
        if not r:
            raise ValueError(f"VM '{vm_id}' not found")
        snaps = [n.name for n, _ in _walk_snapshots(getattr(r.get("snapshot"), "rootSnapshotList", None))]
        host = r.get("runtime.host")
        return {"id": vm_id, "name": r.get("name"), "power": str(r.get("runtime.powerState")),
                "cpu": r.get("config.hardware.numCPU"), "memory_mb": r.get("config.hardware.memoryMB"),
                "os": r.get("guest.guestFullName"), "ip": r.get("guest.ipAddress"),
                "tools": str(r.get("guest.toolsRunningStatus")), "status": str(r.get("overallStatus")),
                "host": getattr(host, "name", None) if host else None,
                "datastores": [d.name for d in r.get("datastore", [])], "snapshots": snaps}

    @guard.tool(mcp)
    def vmware_list_hosts() -> dict:
        """ESXi hosts: connection state, maintenance mode, status, CPU/memory usage, version, hardware model."""
        rows = collect(conn.content, vim.HostSystem,
                       ["name", "runtime.connectionState", "runtime.inMaintenanceMode", "overallStatus",
                        "summary.hardware.vendor", "summary.hardware.model", "summary.hardware.cpuMhz",
                        "summary.hardware.numCpuCores", "summary.hardware.memorySize",
                        "summary.quickStats.overallCpuUsage", "summary.quickStats.overallMemoryUsage",
                        "summary.config.product.fullName"])
        hosts = []
        for r in rows:
            cores, mhz = r.get("summary.hardware.numCpuCores"), r.get("summary.hardware.cpuMhz")
            cpu_used, cpu_total = r.get("summary.quickStats.overallCpuUsage"), (cores or 0) * (mhz or 0)
            mem_used, mem_total = r.get("summary.quickStats.overallMemoryUsage"), (r.get("summary.hardware.memorySize") or 0) / 1024 ** 2
            hosts.append({"id": r["id"], "name": r.get("name"), "state": str(r.get("runtime.connectionState")),
                          "maintenance": r.get("runtime.inMaintenanceMode"), "status": str(r.get("overallStatus")),
                          "vendor": r.get("summary.hardware.vendor"), "model": r.get("summary.hardware.model"),
                          "version": r.get("summary.config.product.fullName"),
                          "cpu_used_percent": round(cpu_used / cpu_total * 100, 1) if cpu_used is not None and cpu_total else None,
                          "memory_used_percent": round(mem_used / mem_total * 100, 1) if mem_used is not None and mem_total else None})
        return {"count": len(hosts), "hosts": hosts}

    @guard.tool(mcp)
    def vmware_host_health() -> dict:
        """Hardware sensor problems reported by each ESXi host (fans, PSUs, temperature, memory...) plus hosts not 'green'."""
        rows = collect(conn.content, vim.HostSystem,
                       ["name", "overallStatus", "runtime.connectionState",
                        "runtime.healthSystemRuntime.systemHealthInfo.numericSensorInfo"])
        problems, checked = [], 0
        for r in rows:
            status = str(r.get("overallStatus"))
            conn_state = str(r.get("runtime.connectionState"))
            if conn_state != "connected":
                problems.append({"host": r.get("name"), "problem": f"connection state is {conn_state}"})
            elif status not in ("green", "gray"):
                problems.append({"host": r.get("name"), "problem": f"overall status is {status}"})
            for s in r.get("runtime.healthSystemRuntime.systemHealthInfo.numericSensorInfo", []) or []:
                checked += 1
                state = str(getattr(getattr(s, "healthState", None), "key", "")).lower()
                if state and state not in ("green", "unknown"):
                    problems.append({"host": r.get("name"), "sensor": s.name, "problem": f"{state}: {getattr(getattr(s, 'healthState', None), 'summary', '')}"})
        return {"hosts_checked": len(rows), "sensors_checked": checked, "problem_count": len(problems), "problems": problems}

    @guard.tool(mcp)
    def vmware_list_clusters() -> dict:
        """Clusters with host count and whether HA and DRS are enabled."""
        rows = collect(conn.content, vim.ClusterComputeResource,
                       ["name", "overallStatus", "host", "configuration.drsConfig.enabled", "configuration.dasConfig.enabled"])
        return {"clusters": [{"name": r.get("name"), "status": str(r.get("overallStatus")), "hosts": len(r.get("host", [])),
                              "drs": r.get("configuration.drsConfig.enabled"), "ha": r.get("configuration.dasConfig.enabled")}
                             for r in rows]}

    @guard.tool(mcp)
    def vmware_datastore_capacity(warn_below_percent_free: float = 15.0) -> dict:
        """Datastore capacity, sorted by least free space. Flags datastores below the free-space threshold."""
        rows = collect(conn.content, vim.Datastore,
                       ["name", "summary.capacity", "summary.freeSpace", "summary.type", "summary.accessible"])
        out = []
        for r in rows:
            cap, free = r.get("summary.capacity") or 0, r.get("summary.freeSpace") or 0
            pct = round(free / cap * 100, 1) if cap else None
            out.append({"name": r.get("name"), "type": r.get("summary.type"), "accessible": r.get("summary.accessible"),
                        "capacity_gb": round(cap / _GB, 1), "free_gb": round(free / _GB, 1), "free_percent": pct,
                        "low": (pct is not None and pct < warn_below_percent_free) or r.get("summary.accessible") is False})
        return {"datastores": sorted(out, key=lambda d: d["free_percent"] if d["free_percent"] is not None else 0)}

    @guard.tool(mcp)
    def vmware_active_alarms() -> dict:
        """Alarms currently triggered in vCenter (name, affected object, severity, since when)."""
        alarms = conn.content.rootFolder.triggeredAlarmState or []
        out = []
        for a in alarms:
            try:
                name = a.alarm.info.name
            except Exception:
                name = str(a.alarm)
            out.append({"alarm": name, "object": getattr(a.entity, "name", str(a.entity)),
                        "severity": str(a.overallStatus), "since": str(a.time), "acknowledged": bool(a.acknowledged)})
        order = {"red": 0, "yellow": 1}
        return {"count": len(out), "alarms": sorted(out, key=lambda x: order.get(x["severity"], 2))}

    @guard.tool(mcp)
    def vmware_list_snapshots(older_than_days: int = 3) -> dict:
        """VMs that have snapshots older than N days (old snapshots slow VMs and fill datastores)."""
        rows = collect(conn.content, vim.VirtualMachine, ["name", "snapshot"])
        now, out = datetime.now(timezone.utc), []
        for r in rows:
            snap = r.get("snapshot")
            for node, _ in _walk_snapshots(getattr(snap, "rootSnapshotList", None)):
                created = node.createTime if node.createTime.tzinfo else node.createTime.replace(tzinfo=timezone.utc)
                age = (now - created).days
                if age >= older_than_days:
                    out.append({"vm": r.get("name"), "vm_id": r["id"], "snapshot": node.name, "age_days": age,
                                "created": created.isoformat()})
        return {"count": len(out), "snapshots": sorted(out, key=lambda x: -x["age_days"])}

    @guard.tool(mcp, write=True)
    def vmware_vm_power(vm_id: str, action: str) -> dict:
        """Change VM power state. action: start, stop (hard), reset or suspend. Blocked in read-only mode."""
        if action not in {"start", "stop", "reset", "suspend"}:
            raise ValueError("action must be start, stop, reset or suspend")
        content = conn.content
        vm = vim.VirtualMachine(vm_id, content.rootFolder._stub)
        task = {"start": vm.PowerOnVM_Task, "stop": vm.PowerOffVM_Task,
                "reset": vm.ResetVM_Task, "suspend": vm.SuspendVM_Task}[action]()
        _wait(task)
        return {"vm": vm_id, "action": action, "status": "done"}

    # ---- daily health report check ----------------------------------------
    warn_ds = float(cfg.get("datastore_warn_percent", 15))
    crit_ds = float(cfg.get("datastore_critical_percent", 5))
    snap_warn = int(cfg.get("snapshot_warn_days", 3))
    snap_crit = int(cfg.get("snapshot_critical_days", 14))
    usage_warn = float(cfg.get("host_usage_warn_percent", 90))

    def _daily() -> dict:
        f = []
        hosts = vmware_list_hosts()["hosts"]
        for h in hosts:
            for label, key in (("CPU", "cpu_used_percent"), ("memory", "memory_used_percent")):
                if h.get(key) is not None and h[key] >= usage_warn:
                    f.append({"severity": "warning", "component": f"host {h['name']}", "message": f"{label} usage {h[key]}%"})
        for p in vmware_host_health()["problems"]:
            sev = "critical" if ("red" in p["problem"] or "connection state" in p["problem"]) else "warning"
            comp = f"host {p['host']}" + (f" / {p['sensor']}" if p.get("sensor") else "")
            f.append({"severity": sev, "component": comp, "message": p["problem"]})
        for d in vmware_datastore_capacity(warn_ds)["datastores"]:
            pct = d["free_percent"]
            if d["accessible"] is False:
                f.append({"severity": "critical", "component": f"datastore {d['name']}", "message": "not accessible"})
            elif pct is not None and pct < crit_ds:
                f.append({"severity": "critical", "component": f"datastore {d['name']}", "message": f"only {pct}% free ({d['free_gb']} GB)"})
            elif d["low"]:
                f.append({"severity": "warning", "component": f"datastore {d['name']}", "message": f"{pct}% free ({d['free_gb']} GB)"})
        for a in vmware_active_alarms()["alarms"]:
            f.append({"severity": "critical" if a["severity"] == "red" else "warning",
                      "component": f"{a['object']}", "message": f"alarm: {a['alarm']}"})
        for s in vmware_list_snapshots(snap_warn)["snapshots"]:
            f.append({"severity": "critical" if s["age_days"] >= snap_crit else "warning",
                      "component": f"VM {s['vm']}", "message": f"snapshot '{s['snapshot']}' is {s['age_days']} days old"})
        n_vms = vmware_list_vms(limit=1)["count"]
        return {"summary": f"{len(hosts)} hosts, {n_vms} VMs checked", "findings": f}

    guard.register_check("virtualization (vmware)", _daily)
