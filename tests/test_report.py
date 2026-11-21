import asyncio
import datetime
import ipaddress
import json
import socket
import ssl
import tempfile
import textwrap
import threading
from unittest import mock

import pytest

from infra_mcp.connectors import report
from infra_mcp.security import Guard
from infra_mcp.server import build, build_server


def test_report_merges_and_sorts():
    g = Guard()
    g.register_check("a", lambda: {"summary": "ok", "findings": [{"severity": "warning", "component": "x", "message": "m"}]})
    g.register_check("b", lambda: {"summary": "bad", "findings": [{"severity": "critical", "component": "y", "message": "boom"}]})
    rep = report.build_report(g)
    assert rep["overall"] == "CRITICAL" and rep["counts"] == {"critical": 1, "warning": 1}
    assert rep["findings"][0]["severity"] == "critical"
    text = report.render_markdown(rep)
    assert "### Critical" in text and "boom" in text and "What was checked" in text


def test_failing_check_is_critical():
    g = Guard()
    g.register_check("broken", lambda: 1 / 0)
    rep = report.build_report(g)
    assert rep["overall"] == "CRITICAL" and "could not run" in rep["findings"][0]["message"]


def test_all_ok_and_empty():
    g = Guard()
    assert report.build_report(g)["overall"] == "UNKNOWN"
    g.register_check("a", lambda: {"summary": "fine", "findings": []})
    rep = report.build_report(g)
    assert rep["overall"] == "OK" and "healthy" in report.render_markdown(rep)


def test_email_sent():
    with mock.patch("smtplib.SMTP") as smtp:
        report.send_email({"host": "h", "port": 25, "starttls": False, "from": "a@x", "to": ["b@x", "c@x"]}, "subj", "body")
    smtp.return_value.__enter__.return_value.send_message.assert_called_once()
    msg = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
    assert msg["Subject"] == "subj" and "c@x" in msg["To"]


def _self_signed(days: int):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
            .sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    c, k = f"{d}/c.pem", f"{d}/k.pem"
    open(c, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(k, "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return c, k


def test_certificate_watch(tmp_path):
    c, k = _self_signed(days=5)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(c, k)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0)); srv.listen(5)
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
                try:
                    ctx.wrap_socket(conn, server_side=True).close()
                except Exception:
                    pass
            except OSError:
                return
    threading.Thread(target=serve, daemon=True).start()
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(f"""
        audit_log: {tmp_path}/a.jsonl
        connectors:
          network:
            enabled: true
            watch_tls: ["127.0.0.1:{port}"]
    """))
    mcp = build_server(str(p))
    out = asyncio.run(mcp.call_tool("daily_health_report", {}))
    srv.close()
    assert "expires in 4 days" in str(out) or "expires in 5 days" in str(out)
    assert "CRITICAL" in str(out)  # < 7 days left


def test_report_via_redfish_mock(tmp_path):
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("m", pathlib.Path(__file__).parent / "mock" / "redfish_mock.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    a, b = m.start(18011, "healthy"), m.start(18012, "faulty")
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(f"""
        audit_log: {tmp_path}/a.jsonl
        connectors:
          redfish:
            enabled: true
            username: admin
            password: admin
            hosts:
              - {{name: good, url: "http://127.0.0.1:18011"}}
              - {{name: bad, url: "http://127.0.0.1:18012"}}
    """))
    mcp, guard, _ = build(str(p))
    rep = report.build_report(guard)
    a.shutdown(); b.shutdown()
    assert rep["overall"] == "CRITICAL"
    assert any("PSU 2" in f["message"] for f in rep["findings"])
    assert not any("good" in f["component"] for f in rep["findings"])
