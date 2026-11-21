"""Network checks that need no credentials: ping, DNS, TCP port, TLS certificate expiry."""
import platform
import re
import socket
import ssl
import subprocess
from datetime import datetime, timezone

NAME = "network"
ENABLED_BY_DEFAULT = True

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-:]*[A-Za-z0-9])?$")


def register(mcp, guard, cfg: dict) -> None:
    allowed = cfg.get("allowed_hosts") or []

    def _check_host(host: str) -> str:
        host = host.strip()
        if not _HOST_RE.match(host):
            raise ValueError(f"Invalid host: {host!r}")
        if not guard.host_allowed(host, allowed):
            raise PermissionError(f"Host {host} is not in allowed_hosts")
        return host

    @guard.tool(mcp)
    def ping_host(host: str, count: int = 2) -> dict:
        """Ping a host and report whether it is reachable."""
        host = _check_host(host)
        count = max(1, min(int(count), 5))
        flag = "-n" if platform.system().lower() == "windows" else "-c"
        proc = subprocess.run(["ping", flag, str(count), host], capture_output=True, text=True, timeout=30)
        return {"host": host, "reachable": proc.returncode == 0, "output": proc.stdout.strip()[-600:]}

    @guard.tool(mcp)
    def dns_lookup(name_or_ip: str) -> dict:
        """Resolve a hostname to IPs (forward) or an IP to a hostname (reverse/PTR)."""
        name_or_ip = _check_host(name_or_ip)
        try:
            socket.inet_pton(socket.AF_INET, name_or_ip)
            is_ip = True
        except OSError:
            try:
                socket.inet_pton(socket.AF_INET6, name_or_ip)
                is_ip = True
            except OSError:
                is_ip = False
        if is_ip:
            try:
                host, aliases, _ = socket.gethostbyaddr(name_or_ip)
                return {"ip": name_or_ip, "ptr": host, "aliases": aliases}
            except socket.herror:
                return {"ip": name_or_ip, "ptr": None}
        infos = socket.getaddrinfo(name_or_ip, None)
        return {"name": name_or_ip, "addresses": sorted({i[4][0] for i in infos})}

    @guard.tool(mcp)
    def check_port(host: str, port: int, timeout_seconds: float = 3.0) -> dict:
        """Check whether a TCP port is open (e.g. 443 web, 3389 RDP, 5985 WinRM, 445 SMB)."""
        host = _check_host(host)
        try:
            with socket.create_connection((host, int(port)), timeout=min(float(timeout_seconds), 15)):
                return {"host": host, "port": port, "open": True}
        except OSError as exc:
            return {"host": host, "port": port, "open": False, "reason": str(exc)}

    def _cert(host: str, port: int) -> dict:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # we only read the expiry date
        with socket.create_connection((host, int(port)), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
        pem = ssl.DER_cert_to_PEM_cert(der)
        import os
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as fh:
            fh.write(pem)
            path = fh.name
        try:
            info = ssl._ssl._test_decode_cert(path)  # noqa: SLF001
        finally:
            os.unlink(path)
        not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), tz=timezone.utc)
        days = (not_after - datetime.now(timezone.utc)).days
        subject = dict(x[0] for x in info.get("subject", ()))
        return {"host": host, "common_name": subject.get("commonName"),
                "expires": not_after.isoformat(), "days_left": days, "expired": days < 0}

    @guard.tool(mcp)
    def tls_certificate_expiry(host: str, port: int = 443) -> dict:
        """Show when a server's TLS certificate expires and how many days are left."""
        return _cert(_check_host(host), port)

    watch = cfg.get("watch_tls") or []

    def _daily() -> dict:
        findings = []
        for item in watch:
            host, _, port = str(item).partition(":")
            try:
                c = _cert(_check_host(host), int(port or 443))
            except Exception as exc:
                findings.append({"severity": "warning", "component": f"certificate {item}",
                                 "message": f"could not read certificate: {exc}"})
                continue
            d = c["days_left"]
            if d < 0:
                findings.append({"severity": "critical", "component": f"certificate {item}", "message": f"EXPIRED {-d} days ago"})
            elif d < int(cfg.get("tls_critical_days", 7)):
                findings.append({"severity": "critical", "component": f"certificate {item}", "message": f"expires in {d} days"})
            elif d < int(cfg.get("tls_warn_days", 30)):
                findings.append({"severity": "warning", "component": f"certificate {item}", "message": f"expires in {d} days"})
        return {"summary": f"{len(watch)} certificates checked", "findings": findings}

    if watch:
        guard.register_check("certificates", _daily)
