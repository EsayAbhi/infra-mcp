"""Fake Redfish server (pretends to be a server's management controller). Python standard library only.

Run:   python tests/mock/redfish_mock.py
It starts two fake servers:
   http://127.0.0.1:8001   healthy server   (login admin / admin)
   http://127.0.0.1:8002   faulty server    (failed PSU, hot CPU, degraded disk, critical log entries)
Press Ctrl+C to stop.
"""
import argparse
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USER, PASSWORD = "admin", "admin"


def _ref(path):
    return {"@odata.id": path}


def build(profile: str) -> dict:
    faulty = profile == "faulty"
    S = "/redfish/v1/Systems/1"
    C = "/redfish/v1/Chassis/1"
    ok, warn, crit = {"State": "Enabled", "Health": "OK"}, {"State": "Enabled", "Health": "Warning"}, {"State": "Enabled", "Health": "Critical"}
    d = {
        "/redfish/v1": {"RedfishVersion": "1.15.0", "Systems": _ref("/redfish/v1/Systems"), "Chassis": _ref("/redfish/v1/Chassis")},
        "/redfish/v1/Systems": {"Members": [_ref(S)]},
        S: {"Id": "1", "Name": "System", "Manufacturer": "MockCorp", "Model": "MockServer R760" if faulty else "MockServer R750",
            "SerialNumber": "SN-FAULTY-02" if faulty else "SN-HEALTHY-01", "PowerState": "On", "BiosVersion": "2.10.1",
            "Status": {"State": "Enabled", "Health": "OK", "HealthRollup": "Critical" if faulty else "OK"},
            "ProcessorSummary": {"Count": 2, "Model": "Intel Xeon Gold 6430", "Status": crit if faulty else ok},
            "MemorySummary": {"TotalSystemMemoryGiB": 512, "Status": ok},
            "Storage": _ref(S + "/Storage"), "LogServices": _ref(S + "/LogServices"),
            "Links": {"Chassis": [_ref(C)]}},
        S + "/Storage": {"Members": [_ref(S + "/Storage/RAID1")]},
        S + "/Storage/RAID1": {"Id": "RAID1", "Name": "PERC H755", "Status": warn if faulty else ok,
                               "Drives": [_ref(S + "/Drives/0"), _ref(S + "/Drives/1")]},
        S + "/Drives/0": {"Name": "Disk 0", "MediaType": "SSD", "CapacityBytes": 960197124096, "Status": ok},
        S + "/Drives/1": {"Name": "Disk 1", "MediaType": "SSD", "CapacityBytes": 960197124096, "Status": crit if faulty else ok},
        S + "/LogServices": {"Members": [_ref(S + "/LogServices/SEL")]},
        S + "/LogServices/SEL": {"Id": "SEL", "Entries": _ref(S + "/LogServices/SEL/Entries")},
        S + "/LogServices/SEL/Entries": {"Members": [
            {"Id": "1", "Created": "2026-11-18T09:00:00Z", "Severity": "OK", "Message": "System boot completed"},
            {"Id": "2", "Created": "2026-11-19T14:30:00Z", "Severity": "Warning" if faulty else "OK",
             "Message": "Drive 1 predictive failure" if faulty else "Routine self-test passed"},
            {"Id": "3", "Created": "2026-11-20T02:15:00Z", "Severity": "Critical" if faulty else "OK",
             "Message": "Power supply 2 lost input power" if faulty else "Fan speed normal"}]},
        "/redfish/v1/Chassis": {"Members": [_ref(C)]},
        C: {"Id": "1", "Thermal": _ref(C + "/Thermal"), "Power": _ref(C + "/Power")},
        C + "/Thermal": {
            "Temperatures": [
                {"Name": "Inlet Temp", "ReadingCelsius": 24, "UpperThresholdCritical": 42, "Status": ok},
                {"Name": "CPU1 Temp", "ReadingCelsius": 96 if faulty else 55, "UpperThresholdCritical": 90, "Status": ok}],
            "Fans": [{"Name": "Fan 1", "Reading": 8400, "Status": ok},
                     {"Name": "Fan 2", "Reading": 0 if faulty else 8350, "Status": crit if faulty else ok}]},
        C + "/Power": {
            "PowerControl": [{"PowerConsumedWatts": 640 if faulty else 410}],
            "PowerSupplies": [{"Name": "PSU 1", "Status": ok},
                              {"Name": "PSU 2", "Status": crit if faulty else ok}]},
    }
    return d


def make_handler(profile):
    data = build(profile)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            auth = self.headers.get("Authorization", "")
            expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
            if auth != expected:
                self.send_response(401); self.end_headers(); return
            doc = data.get(self.path.rstrip("/") or "/redfish/v1")
            if doc is None:
                self.send_response(404); self.end_headers(); return
            body = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


def start(port: int, profile: str) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(profile))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    start(8001, "healthy"); start(8002, "faulty")
    print("Fake servers running:\n  healthy  http://127.0.0.1:8001\n  faulty   http://127.0.0.1:8002\nLogin: admin / admin   (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
