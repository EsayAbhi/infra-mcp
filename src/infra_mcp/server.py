"""Server entry point. Auto-discovers every connector module in infra_mcp/connectors/."""
from __future__ import annotations

import argparse
import importlib
import pkgutil
import socket
import sys

try:  # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP

from . import __version__, connectors
from .config import load_config
from .security import Guard


def build_server(config_path: str | None = None) -> FastMCP:
    return build(config_path)[0]


def build(config_path: str | None = None):
    cfg = load_config(config_path)
    guard = Guard(read_only=cfg["read_only"], audit_log=cfg.get("audit_log"))
    mcp = FastMCP("infra-mcp")
    enabled: list[str] = []

    for mod_info in pkgutil.iter_modules(connectors.__path__):
        if mod_info.name.startswith("_"):
            continue  # _template.py and helpers are skipped
        module = importlib.import_module(f"{connectors.__name__}.{mod_info.name}")
        name = getattr(module, "NAME", mod_info.name)
        default_on = getattr(module, "ENABLED_BY_DEFAULT", False)
        ccfg = cfg["connectors"].get(name, {}) or {}
        if not ccfg.get("enabled", default_on):
            continue
        try:
            module.register(mcp, guard, ccfg)
            enabled.append(name)
        except Exception as exc:  # one broken connector must not kill the rest
            print(f"[infra-mcp] connector '{name}' failed to load: {exc}", file=sys.stderr)

    @guard.tool(mcp)
    def infra_status() -> dict:
        """Show server version, read-only mode and which connectors are enabled."""
        return {"version": __version__, "read_only": guard.read_only, "connectors": enabled}

    return mcp, guard, cfg


def main() -> None:
    parser = argparse.ArgumentParser(prog="infra-mcp", description="MCP server for IT infrastructure tools")
    parser.add_argument("--config", help="path to config.yaml (or set INFRA_MCP_CONFIG)")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--check", action="store_true", help="validate config, list tools, then exit")
    parser.add_argument("--report", action="store_true", help="print the daily health report and exit (exit code 0 OK, 1 warning, 2 critical)")
    parser.add_argument("--output", help="with --report: also save the report to this file")
    parser.add_argument("--email", action="store_true", help="with --report: email it using report.smtp from config.yaml")
    args = parser.parse_args()

    mcp, guard, cfg = build(args.config)
    if args.report:
        from .connectors.report import build_report, render_markdown, send_email
        rep = build_report(guard)
        text = render_markdown(rep)
        print(text)
        if rep["overall"] == "UNKNOWN":
            sys.exit(3)  # nothing was checked, so there is nothing to save or email
        if args.output:
            from pathlib import Path
            Path(args.output).write_text(text, encoding="utf-8")
        if args.email:
            import smtplib
            smtp = (cfg["connectors"].get("report") or {}).get("smtp")
            if not smtp:
                sys.exit("--email needs connectors.report.smtp in config.yaml")
            try:
                send_email(smtp, f"[{rep['overall']}] Infrastructure health report {rep.get('generated', '')}", text)
            except socket.gaierror:
                sys.exit(f"\nEmail failed: cannot find the mail server '{smtp.get('host')}'. Check 'host' under connectors.report.smtp.")
            except smtplib.SMTPAuthenticationError:
                sys.exit("\nEmail failed: the mail server rejected the username/password (Gmail needs an app password).")
            except (OSError, smtplib.SMTPException) as exc:
                sys.exit(f"\nEmail failed: {type(exc).__name__}: {exc}. Check host, port, starttls and firewall.")
            print("\nEmail sent.")
        sys.exit({"OK": 0, "WARNING": 1}.get(rep["overall"], 2))
    if args.check:
        import asyncio
        tools = asyncio.run(mcp.list_tools())
        print(f"OK - {len(tools)} tools loaded:")
        for t in tools:
            print(f"  - {t.name}")
        return
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
