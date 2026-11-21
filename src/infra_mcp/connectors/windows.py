"""Windows server connector over WinRM. Uses a fixed set of SAFE diagnostic checks.
Arbitrary commands are only possible if allow_custom_command: true AND read_only: false.
Requires:  pip install "infra-mcp[windows]"
"""
NAME = "windows"
ENABLED_BY_DEFAULT = False

CHECKS = {
    "uptime": "(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime | "
              "Select Days,Hours,Minutes | ConvertTo-Json -Compress",
    "disks": "Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select DeviceID,"
             "@{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}},@{n='FreeGB';e={[math]::Round($_.FreeSpace/1GB,1)}} "
             "| ConvertTo-Json -Compress",
    "stopped_auto_services": "Get-CimInstance Win32_Service -Filter \"StartMode='Auto' AND State<>'Running'\" | "
                             "Select Name,DisplayName,State | ConvertTo-Json -Compress",
    "pending_reboot": "$p=(Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending') "
                      "-or (Test-Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired'); "
                      "@{PendingReboot=$p} | ConvertTo-Json -Compress",
    "top_cpu_processes": "Get-Process | Sort CPU -Descending | Select -First 10 Name,Id,"
                         "@{n='CPU_s';e={[math]::Round($_.CPU,1)}},@{n='MemMB';e={[math]::Round($_.WS/1MB,1)}} "
                         "| ConvertTo-Json -Compress",
    "recent_errors": "Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2;StartTime=(Get-Date).AddHours(-24)} "
                     "-MaxEvents 20 | Select TimeCreated,Id,ProviderName,Message | ConvertTo-Json -Compress",
    "os_info": "Get-CimInstance Win32_OperatingSystem | Select Caption,Version,OSArchitecture | ConvertTo-Json -Compress",
}


def register(mcp, guard, cfg: dict) -> None:
    allowed = cfg.get("allowed_hosts") or []

    def _run(host: str, script: str) -> dict:
        try:
            import winrm  # lazy import: only needed when this connector is enabled
        except ImportError as exc:
            raise RuntimeError('pywinrm missing: pip install "infra-mcp[windows]"') from exc
        if not guard.host_allowed(host, allowed):
            raise PermissionError(f"Host {host} is not in allowed_hosts")
        use_ssl = cfg.get("use_ssl", False)
        session = winrm.Session(
            f"{'https' if use_ssl else 'http'}://{host}:{5986 if use_ssl else 5985}/wsman",
            auth=(cfg["username"], cfg["password"]),
            transport=cfg.get("transport", "ntlm"),
            server_cert_validation="validate" if cfg.get("verify_ssl", True) else "ignore",
        )
        r = session.run_ps(script)
        out, err = r.std_out.decode(errors="replace").strip(), r.std_err.decode(errors="replace").strip()
        return {"host": host, "exit_code": r.status_code, "output": out[-6000:], "error": err[-1500:] or None}

    @guard.tool(mcp)
    def windows_list_checks() -> dict:
        """List the diagnostic checks available for windows_check."""
        return {"checks": sorted(CHECKS)}

    @guard.tool(mcp)
    def windows_check(host: str, check: str) -> dict:
        """Run a safe read-only diagnostic on a Windows server. Use windows_list_checks to see the options."""
        if check not in CHECKS:
            raise ValueError(f"Unknown check '{check}'. Options: {sorted(CHECKS)}")
        return _run(host, CHECKS[check])

    @guard.tool(mcp, write=True)
    def windows_run_command(host: str, powershell: str) -> dict:
        """Run ANY PowerShell command on a server. Disabled unless allow_custom_command: true and read_only: false."""
        if not cfg.get("allow_custom_command", False):
            raise PermissionError("Custom commands are disabled (allow_custom_command: false)")
        return _run(host, powershell)
