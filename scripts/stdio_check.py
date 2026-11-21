"""Test the server exactly like Claude would: start it as a subprocess, talk MCP over stdio,
list the tools and call a few. No Claude, no Node.js needed.

Run from the project folder:
  python scripts/stdio_check.py config.claude-demo.yaml
"""
import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def show(result) -> str:
    try:
        text = "\n".join(c.text for c in result.content)
        return json.dumps(json.loads(text), indent=2)
    except Exception:
        return str(result)


async def main(config: str):
    params = StdioServerParameters(command=sys.executable, args=["-m", "infra_mcp.server", "--config", config])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"CONNECTED - {len(tools)} tools:")
            for t in tools:
                print("  -", t.name)
            tests = [("infra_status", {}), ("redfish_fleet_check", {}),
                     ("vmware_list_snapshots", {"older_than_days": 0}),
                     ("vmware_vm_power", {"vm_id": "vm-62", "action": "stop"})]
            names = {t.name for t in tools}
            for name, args in tests:
                if name in names:
                    print(f"\n=== {name} {args}")
                    print(show(await session.call_tool(name, args)))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "config.yaml"))
