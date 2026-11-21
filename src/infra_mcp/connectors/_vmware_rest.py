"""VMware REST backend (vSphere Automation REST API, vCenter 7.0U2+ / 8.x). Selected with mode: rest."""
import requests

class _VCenter:
    def __init__(self, cfg: dict):
        self.base = f"https://{cfg['host']}"
        self.auth = (cfg["username"], cfg["password"])
        self.verify = cfg.get("verify_ssl", True)
        self.session_id: str | None = None

    def _login(self) -> None:
        r = requests.post(f"{self.base}/api/session", auth=self.auth, verify=self.verify, timeout=20)
        r.raise_for_status()
        self.session_id = r.json()

    def request(self, method: str, path: str, **kw):
        for attempt in (1, 2):
            if not self.session_id:
                self._login()
            r = requests.request(method, f"{self.base}{path}", verify=self.verify, timeout=30,
                                 headers={"vmware-api-session-id": self.session_id}, **kw)
            if r.status_code == 401 and attempt == 1:
                self.session_id = None  # session expired, log in again
                continue
            r.raise_for_status()
            return r.json() if r.content else {}


def register_rest(mcp, guard, cfg: dict) -> None:
    vc = _VCenter(cfg)

    @guard.tool(mcp)
    def vmware_list_vms(name_contains: str = "", power_state: str = "", limit: int = 100) -> dict:
        """List virtual machines. power_state: POWERED_ON, POWERED_OFF or SUSPENDED (optional)."""
        params = {}
        if power_state:
            params["power_states"] = power_state.upper()
        vms = vc.request("GET", "/api/vcenter/vm", params=params)
        if name_contains:
            vms = [v for v in vms if name_contains.lower() in v.get("name", "").lower()]
        vms = vms[: max(1, min(int(limit), 500))]
        return {"count": len(vms), "vms": [
            {"id": v["vm"], "name": v["name"], "power": v["power_state"],
             "cpu": v.get("cpu_count"), "memory_mb": v.get("memory_size_MiB")} for v in vms]}

    @guard.tool(mcp)
    def vmware_get_vm(vm_id: str) -> dict:
        """Get details of one VM by id (e.g. 'vm-123' from vmware_list_vms)."""
        return vc.request("GET", f"/api/vcenter/vm/{vm_id}")

    @guard.tool(mcp)
    def vmware_list_hosts() -> dict:
        """List ESXi hosts with connection and power state."""
        hosts = vc.request("GET", "/api/vcenter/host")
        return {"count": len(hosts), "hosts": hosts}

    @guard.tool(mcp)
    def vmware_list_clusters() -> dict:
        """List clusters with HA / DRS status."""
        return {"clusters": vc.request("GET", "/api/vcenter/cluster")}

    @guard.tool(mcp)
    def vmware_datastore_capacity(warn_below_percent_free: float = 15.0) -> dict:
        """Show datastore capacity and flag the ones running low on free space."""
        out = []
        for ds in vc.request("GET", "/api/vcenter/datastore"):
            cap, free = ds.get("capacity", 0), ds.get("free_space", 0)
            pct = round(free / cap * 100, 1) if cap else None
            out.append({"name": ds["name"], "type": ds.get("type"),
                        "capacity_gb": round(cap / 1024**3, 1), "free_gb": round(free / 1024**3, 1),
                        "free_percent": pct, "low": pct is not None and pct < warn_below_percent_free})
        return {"datastores": sorted(out, key=lambda d: d["free_percent"] or 0)}

    @guard.tool(mcp, write=True)
    def vmware_vm_power(vm_id: str, action: str) -> dict:
        """Change VM power state. action: start, stop (hard), reset or suspend. Blocked in read-only mode."""
        if action not in {"start", "stop", "reset", "suspend"}:
            raise ValueError("action must be start, stop, reset or suspend")
        vc.request("POST", f"/api/vcenter/vm/{vm_id}/power", params={"action": action})
        return {"vm": vm_id, "action": action, "status": "requested"}
