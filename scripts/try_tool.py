"""Call one tool from the command line - no Claude needed. Great for testing.

Examples (run from the project folder):
  python scripts/try_tool.py --config config.demo.yaml redfish_fleet_check
  python scripts/try_tool.py --config config.demo.yaml redfish_hardware_health host=bad-server
  python scripts/try_tool.py --config config.demo.yaml redfish_event_log host=bad-server min_severity=Critical
Arguments are name=value pairs (numbers are converted automatically).
"""
import argparse
import asyncio
import json
import sys

from infra_mcp.server import build_server


def _convert(v: str):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return {"true": True, "false": False}.get(v.lower(), v)


def _text(result) -> str:
    items = result[0] if isinstance(result, tuple) else result
    items = getattr(result, "content", items)
    try:
        return "\n".join(i.text for i in items)
    except Exception:
        return str(result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("tool")
    ap.add_argument("args", nargs="*")
    ns = ap.parse_args()
    args = dict(a.split("=", 1) for a in ns.args)
    mcp = build_server(ns.config)
    out = _text(asyncio.run(mcp.call_tool(ns.tool, {k: _convert(v) for k, v in args.items()})))
    try:
        out = json.dumps(json.loads(out), indent=2)
    except ValueError:
        pass
    sys.stdout.write(out + "\n")


if __name__ == "__main__":
    main()
