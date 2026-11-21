"""Hardware connector using Redfish - the standard API on server management controllers (BMC):
Dell iDRAC, HPE iLO, Cisco CIMC, Lenovo XCC, Supermicro, and more. READ-ONLY in this release.

Config (config.yaml):
  redfish:
    enabled: true
    username: ${BMC_USER}
    password: ${BMC_PASSWORD}
    verify_ssl: false          # BMCs usually have self-signed certificates
    hosts:                     # your named servers
      - { name: esx01-idrac, url: "https://10.0.0.11" }
    allowed_hosts: []          # optional: patterns for ad-hoc addresses, e.g. ["10.0.0.*"]
"""
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

NAME = "redfish"
ENABLED_BY_DEFAULT = False

_SEVERITY_RANK = {"OK": 0, "Warning": 1, "Critical": 2}
_IGNORED_STATES = {"Absent", "Disabled", "StandbyOffline", "UnavailableOffline"}


class _Client:
    def __init__(self, base_url: str, user: str, password: str, verify: bool):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.session.verify = verify
        self.session.headers["Accept"] = "application/json"

    def get(self, path: str) -> dict:
        r = self.session.get(self.base + path, timeout=20)
        r.raise_for_status()
        return r.json()

    def members(self, path: str) -> list[str]:
        return [m["@odata.id"] for m in self.get(path).get("Members", [])]


def _bad(status: dict | None) -> str | None:
    """Return the health text if the status is not OK, else None."""
    if not status:
        return None
    if status.get("State") in _IGNORED_STATES:
        return None
    health = status.get("HealthRollup") or status.get("Health")
    return health if health not in (None, "OK") else None


def register(mcp, guard, cfg: dict) -> None:
    hosts = {h["name"]: h for h in cfg.get("hosts", [])}
    allowed = cfg.get("allowed_hosts") or []

    def _client(host: str) -> tuple[_Client, str]:
        entry = hosts.get(host)
        if entry:
            url, user, pw = entry["url"], entry.get("username", cfg.get("username")), entry.get("password", cfg.get("password"))
        else:
            if not allowed or not guard.host_allowed(host.split("://")[-1].split(":")[0], allowed):
                raise PermissionError(
                    f"'{host}' is not a configured host name. Known: {sorted(hosts)}. "
                    "Add it under redfish.hosts, or list its address in allowed_hosts.")
            url = host if "://" in host else f"https://{host}"
            user, pw = cfg.get("username"), cfg.get("password")
        verify = entry.get("verify_ssl", cfg.get("verify_ssl", True)) if entry else cfg.get("verify_ssl", True)
        return _Client(url, user, pw, verify), urlparse(url).netloc

    def _first_system(c: _Client) -> tuple[str, dict]:
        paths = c.members("/redfish/v1/Systems")
        if not paths:
            raise RuntimeError("No systems found on this controller")
        return paths[0], c.get(paths[0])

    # ---- tools -------------------------------------------------------------
    @guard.tool(mcp)
    def redfish_list_hosts() -> dict:
        """List the servers (BMCs) configured for hardware checks."""
        return {"hosts": [{"name": n, "url": h["url"]} for n, h in hosts.items()]}

    @guard.tool(mcp)
    def redfish_server_summary(host: str) -> dict:
        """Model, serial number, power state, BIOS version, CPU and memory summary of one server."""
        c, addr = _client(host)
        _, s = _first_system(c)
        return {"host": host, "address": addr, "manufacturer": s.get("Manufacturer"), "model": s.get("Model"),
                "serial": s.get("SerialNumber"), "power_state": s.get("PowerState"), "bios": s.get("BiosVersion"),
                "overall_health": (s.get("Status") or {}).get("HealthRollup") or (s.get("Status") or {}).get("Health"),
                "cpu": s.get("ProcessorSummary", {}).get("Model"), "cpu_count": s.get("ProcessorSummary", {}).get("Count"),
                "memory_gib": s.get("MemorySummary", {}).get("TotalSystemMemoryGiB")}

    def _health(host: str) -> dict:
        c, addr = _client(host)
        sys_path, s = _first_system(c)
        problems: list[dict] = []
        errors: list[str] = []
        readings = {"max_temp_c": None, "power_watts": None, "fans": 0, "power_supplies": 0, "drives": 0}

        def flag(component: str, name: str, detail: str):
            problems.append({"component": component, "name": name, "problem": detail})

        status = s.get("Status") or {}
        for label, st in (("System", status), ("Processors", s.get("ProcessorSummary", {}).get("Status")),
                          ("Memory", s.get("MemorySummary", {}).get("Status"))):
            if (h := _bad(st)):
                flag(label, label, f"health is {h}")

        # chassis: thermal + power
        chassis_links = [x["@odata.id"] for x in s.get("Links", {}).get("Chassis", [])]
        if not chassis_links:
            try:
                chassis_links = c.members("/redfish/v1/Chassis")
            except Exception as exc:
                errors.append(f"chassis list: {exc}")
        for ch in chassis_links[:2]:
            try:
                chassis = c.get(ch)
            except Exception as exc:
                errors.append(f"{ch}: {exc}")
                continue
            if "Thermal" in chassis:
                try:
                    t = c.get(chassis["Thermal"]["@odata.id"])
                    for temp in t.get("Temperatures", []):
                        val, crit = temp.get("ReadingCelsius"), temp.get("UpperThresholdCritical")
                        if val is not None:
                            readings["max_temp_c"] = max(val, readings["max_temp_c"] or val)
                        if (h := _bad(temp.get("Status"))):
                            flag("Temperature", temp.get("Name", "?"), f"health {h} at {val} C")
                        elif val is not None and crit is not None and val >= crit:
                            flag("Temperature", temp.get("Name", "?"), f"{val} C is above critical limit {crit} C")
                    for fan in t.get("Fans", []):
                        readings["fans"] += 1
                        if (h := _bad(fan.get("Status"))):
                            flag("Fan", fan.get("Name") or fan.get("FanName", "?"), f"health is {h}")
                except Exception as exc:
                    errors.append(f"thermal: {exc}")
            if "Power" in chassis:
                try:
                    p = c.get(chassis["Power"]["@odata.id"])
                    for ctl in p.get("PowerControl", []):
                        if ctl.get("PowerConsumedWatts") is not None:
                            readings["power_watts"] = ctl["PowerConsumedWatts"]
                    for psu in p.get("PowerSupplies", []):
                        readings["power_supplies"] += 1
                        if (h := _bad(psu.get("Status"))):
                            flag("Power supply", psu.get("Name", "?"), f"health is {h}")
                except Exception as exc:
                    errors.append(f"power: {exc}")

        # storage
        if "Storage" in s:
            try:
                for ctrl in c.members(s["Storage"]["@odata.id"]):
                    cdata = c.get(ctrl)
                    if (h := _bad(cdata.get("Status"))):
                        flag("Storage controller", cdata.get("Name", ctrl), f"health is {h}")
                    for d in cdata.get("Drives", []):
                        drive = c.get(d["@odata.id"])
                        readings["drives"] += 1
                        if (h := _bad(drive.get("Status"))):
                            flag("Drive", drive.get("Name", "?"), f"health is {h}")
            except Exception as exc:
                errors.append(f"storage: {exc}")

        return {"host": host, "address": addr, "overall": status.get("HealthRollup") or status.get("Health"),
                "healthy": not problems, "problem_count": len(problems), "problems": problems,
                "readings": readings, "partial_errors": errors or None}

    @guard.tool(mcp)
    def redfish_hardware_health(host: str) -> dict:
        """Check one server: CPU, memory, temperatures, fans, power supplies, disks. Lists only what is wrong."""
        return _health(host)

    @guard.tool(mcp)
    def redfish_event_log(host: str, limit: int = 20, min_severity: str = "Warning") -> dict:
        """Recent hardware event log entries (newest first). min_severity: OK, Warning or Critical."""
        if min_severity not in _SEVERITY_RANK:
            raise ValueError("min_severity must be OK, Warning or Critical")
        c, _ = _client(host)
        sys_path, s = _first_system(c)
        entries = []
        svc_root = (s.get("LogServices") or {}).get("@odata.id")
        if not svc_root:
            return {"host": host, "entries": [], "note": "No log service exposed by this controller"}
        for svc in c.members(svc_root):
            ent = c.get(svc).get("Entries", {}).get("@odata.id")
            if not ent:
                continue
            for e in c.get(ent).get("Members", []):
                sev = e.get("Severity") or "OK"
                if _SEVERITY_RANK.get(sev, 0) >= _SEVERITY_RANK[min_severity]:
                    entries.append({"time": e.get("Created"), "severity": sev, "message": e.get("Message"),
                                    "id": e.get("Id")})
        entries.sort(key=lambda e: e["time"] or "", reverse=True)
        return {"host": host, "count": len(entries[:limit]), "entries": entries[: max(1, min(int(limit), 200))]}

    def _fleet(names: list[str] | None) -> dict:
        names = names or list(hosts)
        if not names:
            raise ValueError("No hosts configured. Add them under redfish.hosts in config.yaml")

        def one(name: str):
            try:
                return _health(name)
            except Exception as exc:
                return {"host": name, "unreachable": True, "error": f"{type(exc).__name__}: {exc}"}

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(one, names))
        unreachable = [r for r in results if r.get("unreachable")]
        unhealthy = [r for r in results if not r.get("unreachable") and not r["healthy"]]
        return {"checked": len(names), "healthy": len(names) - len(unreachable) - len(unhealthy),
                "unhealthy": [{"host": r["host"], "problems": r["problems"]} for r in unhealthy],
                "unreachable": [{"host": r["host"], "error": r["error"]} for r in unreachable]}

    @guard.tool(mcp)
    def redfish_fleet_check(hosts_to_check: list[str] | None = None) -> dict:
        """Check many servers at once (default: all configured hosts). Reports only the unhealthy or unreachable ones."""
        return _fleet(hosts_to_check)

    def _daily() -> dict:
        r = _fleet(None)
        findings = []
        for u in r["unhealthy"]:
            for p in u["problems"]:
                bad = "Critical" in p["problem"] or "above critical" in p["problem"]
                findings.append({"severity": "critical" if bad else "warning",
                                 "component": f"{u['host']} / {p['component']}",
                                 "message": f"{p['name']}: {p['problem']}"})
        for x in r["unreachable"]:
            findings.append({"severity": "critical", "component": f"{x['host']} / BMC",
                             "message": "management controller unreachable (" + x["error"].split(":")[0] + ")"})
        return {"summary": f"{r['checked']} servers checked, {r['healthy']} healthy", "findings": findings}

    if hosts:
        guard.register_check("hardware (redfish)", _daily)
