# infra-mcp

> \\\*\\\*Status: v0.1.0.\\\*\\\* Tested against simulators (VMware vcsim and a Redfish mock server).
> Real-hardware pilots are welcome: please open an issue with your results.

One MCP server that lets AI assistants (Claude, ChatGPT, Cursor, etc.) safely work with your IT infrastructure.
One MCP server that lets AI assistants (Claude, ChatGPT, Cursor, etc.) safely work with your IT infrastructure.

* **Safe by default:** read-only mode, every call is audit-logged, secrets are redacted, host allowlists.
* **Works anywhere:** Windows, Linux, macOS, Docker. Credentials come from environment variables, never from code.
* **Modular:** enable only the connectors you need. Add a new one by dropping in one file.

|Connector|What it does|Credentials|
|-|-|-|
|`network`|ping, DNS, port check, TLS certificate expiry|none|
|`redfish`|**physical server health** via BMC (iDRAC/iLO/CIMC/XCC): CPU, memory, temps, fans, PSUs, disks, event log, fleet check|read-only BMC account|
|`vmware`|VMs, hosts, host hardware sensors, clusters, datastores, alarms, old snapshots, power actions (write)|vCenter/ESXi user|
|`prometheus`|PromQL queries, down targets, firing alerts|URL (+ token)|
|`grafana`|list dashboards/data sources, **create dashboards from a simple list, import community dashboards by ID**|service-account token|
|`windows`|uptime, disks, stopped services, pending reboot, event errors (WinRM)|service account|
|`rest`|**any tool with a REST API** (ServiceNow, Jira, Graylog, Veeam...) via config only|per service|

## Quick start (5 minutes)

```bash
git clone https://github.com/EsayAbhi/infra-mcp.git
cd infra-mcp
python -m venv .venv
# Windows: .venv\\\\Scripts\\\\activate      Linux/macOS: source .venv/bin/activate
pip install -e .              # add ".\\\[windows]" for the Windows connector
cp config.example.yaml config.yaml    # Windows: copy config.example.yaml config.yaml
cp .env.example .env                  # put your passwords here
infra-mcp --check                     # validates config and lists tools
```

Enable the connectors you want in `config.yaml` (`enabled: true`).

## Daily health report

One report that merges hardware (Redfish), VMware (hosts, sensors, datastores, alarms, old snapshots) and certificate expiry, critical problems first.

* Ask your AI: *"Give me the daily infrastructure health report."*
* Or run it yourself:

```bash
  infra-mcp --config config.yaml --report --output report.md --email
  ```

* Schedule it every morning with Windows Task Scheduler or cron. Exit code: `0` OK, `1` warnings, `2` critical.
* Thresholds (datastore free %, snapshot age, certificate days) are configurable in `config.yaml`.

## Sample output
* 
* Demo environment (simulators), not real servers. Demo thresholds are set so even brand-new snapshots are flagged.
* !\[Daily health report](docs/report.png)
* A power action is refused while read\_only is true:
* !\[Read-only lock](docs/read-only-lock.png)
* Every call is recorded in an audit log, with secrets hidden:
* !\[Audit log](docs/audit-log.png)

## Connect to Claude Desktop

Edit `claude\\\_desktop\\\_config.json` (Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "infra": {
      "command": "C:\\\\\\\\path\\\\\\\\to\\\\\\\\infra-mcp\\\\\\\\.venv\\\\\\\\Scripts\\\\\\\\infra-mcp.exe",
      "args": \\\["--config", "C:\\\\\\\\path\\\\\\\\to\\\\\\\\infra-mcp\\\\\\\\config.yaml"]
    }
  }
}
```

Restart Claude Desktop, then ask: *"Which datastores are below 15% free?"* or *"Check disks on SRV-APP01."*

## Remote / team use (Docker)

```bash
docker build -t infra-mcp .
docker run -p 8000:8000 -v $(pwd)/config.yaml:/config/config.yaml --env-file .env infra-mcp
```

Put it behind your own reverse proxy with authentication before exposing it beyond localhost.

## Safety model

1. `read\\\_only: true` (default) blocks every action that changes something, in every connector.
2. To allow changes, set `read\\\_only: false` **and** use accounts with least privilege.
3. `audit.jsonl` records who/what/when for every call. Passwords and tokens are masked.
4. Use `allowed\\\_hosts` in `network` and `windows` to limit where the AI can reach.
5. Use a dedicated read-only service account per system.

## Add your own connector

1. Copy `src/infra\\\_mcp/connectors/\\\_template.py` to `connectors/yourtool.py`.
2. Set `NAME`, write tools inside `register()`:

```python
   @guard.tool(mcp)                 # read tool
   def yourtool\\\_status() -> dict:
       """The AI reads this docstring to know when to use the tool."""
   @guard.tool(mcp, write=True)     # blocked in read-only mode
   def yourtool\\\_restart(name: str) -> dict: ...
   ```

3. Add a block under `connectors:` in `config.yaml`. No other file changes needed.

For tools with a plain REST API you do not even need code: add them under `rest:` in `config.yaml`.

## Development and testing

```bash
pip install -e ".\\\[dev]"
pytest                              # unit and simulator tests
python scripts/run\\\_all\\\_checks.py    # full check with fake VMware + hardware servers (needs vcsim)
```

## License

MIT

