"""Daily health report: runs every enabled connector's health check and merges the results into one summary.

Use it three ways:
  1. Ask the AI:      "Give me the daily infrastructure health report"   (tool: daily_health_report)
  2. Command line:    infra-mcp --config config.yaml --report [--output report.md] [--email]
  3. Scheduled:       run the command line from Windows Task Scheduler / cron each morning.
     Exit code: 0 = all OK, 1 = warnings, 2 = critical problems.

Optional email settings (config.yaml):
  report:
    smtp: { host: smtp.example.com, port: 587, starttls: true, username: ${SMTP_USER}, password: ${SMTP_PASSWORD},
            from: infra-mcp@example.com, to: [ops@example.com] }
"""
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

NAME = "report"
ENABLED_BY_DEFAULT = True

_ORDER = {"critical": 0, "warning": 1, "info": 2}


def build_report(guard) -> dict:
    if not guard.checks:
        return {"overall": "UNKNOWN", "note": "No checks available. Enable redfish (with hosts), vmware or network (watch_tls) first.",
                "sources": [], "findings": [], "counts": {"critical": 0, "warning": 0}}

    def run(item):
        name, fn = item
        try:
            r = fn()
            findings = [{**f, "source": name} for f in r.get("findings", [])]
            worst = min((_ORDER[f["severity"]] for f in findings), default=3)
            status = {0: "CRITICAL", 1: "WARNING", 2: "OK", 3: "OK"}[worst]
            return {"name": name, "status": status, "summary": r.get("summary", "")}, findings
        except Exception as exc:  # a check we cannot run is itself a problem: we are blind there
            msg = f"could not run this check: {type(exc).__name__}: {exc}"
            return ({"name": name, "status": "CRITICAL", "summary": msg},
                    [{"severity": "critical", "source": name, "component": name, "message": msg}])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, list(guard.checks.items())))
    sources = [r[0] for r in results]
    findings = sorted((f for r in results for f in r[1]), key=lambda f: (_ORDER[f["severity"]], f["source"], f["component"]))
    counts = {"critical": sum(f["severity"] == "critical" for f in findings),
              "warning": sum(f["severity"] == "warning" for f in findings)}
    overall = "CRITICAL" if counts["critical"] else "WARNING" if counts["warning"] else "OK"
    return {"generated": time.strftime("%Y-%m-%d %H:%M"), "overall": overall, "counts": counts,
            "sources": sources, "findings": findings}


def render_markdown(rep: dict) -> str:
    if rep.get("note"):
        return rep["note"]
    c = rep["counts"]
    lines = [f"# Infrastructure health report - {rep['generated']}", "",
             f"**Overall: {rep['overall']}** ({c['critical']} critical, {c['warning']} warnings)", ""]
    if rep["findings"]:
        lines.append("## Needs attention")
        for sev, title in (("critical", "Critical"), ("warning", "Warnings"), ("info", "Info")):
            items = [f for f in rep["findings"] if f["severity"] == sev]
            if items:
                lines += ["", f"### {title}"]
                lines += [f"- [{f['source']}] {f['component']}: {f['message']}" for f in items]
        lines.append("")
    else:
        lines += ["Everything checked is healthy.", ""]
    lines.append("## What was checked")
    lines += [f"- {s['name']}: {s['summary']} ({s['status']})" for s in rep["sources"]]
    return "\n".join(lines)


def send_email(smtp_cfg: dict, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, smtp_cfg["from"]
    to = smtp_cfg["to"]
    msg["To"] = ", ".join(to) if isinstance(to, list) else to
    msg.set_content(body)
    with smtplib.SMTP(smtp_cfg["host"], int(smtp_cfg.get("port", 587)), timeout=30) as s:
        if smtp_cfg.get("starttls", True):
            s.starttls()
        if smtp_cfg.get("username"):
            s.login(smtp_cfg["username"], smtp_cfg["password"])
        s.send_message(msg)


def register(mcp, guard, cfg: dict) -> None:
    @guard.tool(mcp)
    def daily_health_report() -> dict:
        """One combined health report: hardware (Redfish), VMware (hosts, datastores, alarms, old snapshots)
        and certificate expiry. Lists critical problems first. Use this for 'how is my infrastructure today?'"""
        rep = build_report(guard)
        rep["text"] = render_markdown(rep)
        return rep
