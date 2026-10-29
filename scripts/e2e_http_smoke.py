"""Run Phase 1 SOS over real localhost HTTP using disposable SQLite files.

Usage: python scripts/e2e_http_smoke.py
The script starts/stops Flask with disposable sender, receiver and fallback
databases. It never requires a project database to exist.
"""

import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import secrets
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
from werkzeug.security import generate_password_hash


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_DB = ROOT / "database" / "nexalert.db"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class HttpDemo:
    def __init__(self, base_url):
        self.base = base_url
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.csrf_token = None
        self.incident_capabilities = {}

    def request(self, method, path, body=None, headers=None):
        headers = dict(headers or {})
        if method.upper() == "POST" and path in {"/api/sos", "/api/fallback/sos"} and isinstance(body, dict):
            incident_id=body.get("incident_id")
            if incident_id:
                capability=self.incident_capabilities.setdefault(incident_id,secrets.token_urlsafe(32))
                headers.setdefault("X-Incident-Capability",capability)
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            if self.csrf_token is None and path != "/api/csrf":
                self.request("GET", "/api/csrf")
            if self.csrf_token:
                headers.setdefault("X-CSRF-Token", self.csrf_token)
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + path, data=data, method=method, headers=headers)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=4) as response:
                content = response.read()
                if not content:
                    parsed=None
                else:
                    try: parsed=json.loads(content)
                    except (UnicodeDecodeError,ValueError): parsed=content.decode(errors="replace")
                if isinstance(parsed,dict) and parsed.get("csrf_token"):
                    self.csrf_token=parsed["csrf_token"]
                return response.status, parsed
        except HTTPError as error:
            content = error.read()
            try:
                parsed = json.loads(content) if content else None
            except ValueError:
                parsed = content.decode(errors="replace")
            if isinstance(parsed,dict) and parsed.get("csrf_token"):
                self.csrf_token=parsed["csrf_token"]
            return error.code, parsed


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    try:
        import flask  # noqa: F401
        import flask_socketio  # noqa: F401
    except ImportError as error:
        raise SystemExit(
            "HTTP smoke test needs the declared Flask dependencies. "
            "Install them with `python -m pip install -r requirements.txt`. "
            f"Missing import: {error}"
        ) from error

    production_existed = PRODUCTION_DB.is_file()
    original_hash = sha256(PRODUCTION_DB) if production_existed else None
    with tempfile.TemporaryDirectory(prefix="nexalert-http-e2e-") as tmp:
        sender_db = Path(tmp) / "sender.sqlite"
        receiver_db = Path(tmp) / "receiver.sqlite"
        app_log = Path(tmp) / "app.log"
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
        except OSError as error:
            if isinstance(error, PermissionError):
                raise SystemExit(
                    "HTTP smoke test needs permission to bind a loopback socket; "
                    "run it outside a sandbox that blocks localhost networking."
                ) from error
            raise
        env = os.environ.copy()
        env.update({
            "NEXALERT_DB": str(sender_db),
            "NEXALERT_CC_DB": str(receiver_db),
            "NEXALERT_FALLBACK_DB": str(Path(tmp) / "fallback.sqlite"),
            "NEXALERT_LOG_DIR": str(Path(tmp) / "logs"),
            "NEXALERT_ENV": "development",
            "NEXALERT_ENABLE_SIM_CONTROLS": "1",
            "NEXALERT_HOST": "127.0.0.1",
            "NEXALERT_PORT": str(port),
            "NEXALERT_SECRET": secrets.token_urlsafe(48),
        })
        base = f"http://127.0.0.1:{port}"
        client = HttpDemo(base)
        anonymous = HttpDemo(base)
        operator = HttpDemo(base)
        log_handle = open(app_log, "w", encoding="utf-8")
        process = None

        def start():
            nonlocal process
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "backend" / "app.py")],
                cwd=ROOT, env=env, stdout=log_handle, stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Flask exited {process.returncode}:\n{app_log.read_text(errors='replace')[-5000:]}")
                try:
                    code, _ = client.request("GET", "/health")
                    if code == 200:
                        return
                except (URLError, TimeoutError, OSError):
                    pass
                time.sleep(0.2)
            raise RuntimeError(f"Flask startup timed out:\n{app_log.read_text(errors='replace')[-5000:]}")

        def stop():
            nonlocal process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=4)
            process = None

        def run_worker():
            subprocess.run([sys.executable, "-m", "backend.worker", "--drain-once"],
                           cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL)

        try:
            start()
            for path in ("/", "/operator/login", "/dashboard"):
                status, _ = client.request("GET", path)
                check(status == 200, f"HTTP smoke {path}: expected 200, got {status}")
            check(anonymous.request("GET","/api/incidents")[0]==401,"anonymous incident access")
            check(anonymous.request("GET","/api/users")[0]==401,"anonymous user-list access")
            status, citizen = client.request("POST","/api/register",{"display_name":"Smoke citizen"})
            check(status==201 and citizen["user"]["role"]=="citizen","pseudonymous citizen onboarding")
            operator_password=secrets.token_urlsafe(24)
            sender=sqlite3.connect(sender_db)
            sender.execute("INSERT INTO users(username,full_name,phone,role,password_hash) VALUES(?,?,'','operator',?)",
                           ("smoke-operator","Smoke Operator",generate_password_hash(operator_password,method="scrypt")))
            sender.commit();sender.close()
            status, login = operator.request("POST","/api/operator/login",{"username":"smoke-operator","password":operator_password})
            check(status==200 and login["user"]["role"]=="operator","operator login")
            check(operator.request("GET","/api/users")[0]==200,"operator user-list access")
            check(client.request("GET","/api/users")[0]==403,"citizen user-list restriction")
            print("PASS: public onboarding, private routes and operator-only dashboard access")

            status, control = operator.request("POST", "/api/transport/simulated", {"connected": False})
            check(status == 200 and control["connected"] is False, "simulated disconnect")
            first_id = "INC-E2E-" + str(int(time.time()))
            first_payload = {"incident_id": first_id, "alert_type": "medical", "description": "E2E queued report"}
            status, first = client.request("POST", "/api/sos", first_payload, {"Idempotency-Key": first_id})
            check(status == 201 and first["alert"]["delivery_status"] == "queued", "SOS should persist and queue")
            check(first["alert"]["latitude"] is None and first["alert"]["longitude"] is None, "unknown location must remain unknown")
            status, repeat = client.request("POST", "/api/sos", first_payload, {"Idempotency-Key": first_id})
            check(status == 200 and repeat["alert"]["incident_id"] == first_id, "retry should retain incident ID")
            print(f"PASS: disconnected submission persisted once and queued as {first_id}")

            stop()
            start()
            second_id = "INC-E2E-RESTART-" + str(int(time.time()))
            status, second = client.request("POST", "/api/sos", {"incident_id": second_id, "alert_type": "fire"}, {"Idempotency-Key": second_id})
            check(status == 201 and second["alert"]["delivery_status"] == "queued", "transport state should remain disconnected after restart")
            sender = sqlite3.connect(sender_db)
            queued = sender.execute("SELECT incident_id,state FROM outbound_queue WHERE incident_id=?", (first_id,)).fetchone()
            sender.close()
            check(queued == (first_id, "queued"), "sender queue did not survive process restart")
            print("PASS: restart recovered disconnected state and durable queue")

            status, control = operator.request("POST", "/api/transport/simulated", {"connected": True})
            check(status == 200 and control.get("connected") is True,
                  f"simulated reconnect returned HTTP {status}: {control!r}")
            run_worker()
            status, incidents = client.request("GET", "/api/incidents")
            check(status == 200 and {first_id, second_id}.issubset({i["incident_id"] for i in incidents}), "dashboard incident API after reconnect")
            destination = sqlite3.connect(receiver_db)
            for incident_id in (first_id, second_id):
                check(destination.execute("SELECT count(*) FROM command_centre_incidents WHERE incident_id=?", (incident_id,)).fetchone()[0] == 1, "receiver record missing/duplicated")
                check(destination.execute("SELECT count(*) FROM command_centre_receipts WHERE incident_id=?", (incident_id,)).fetchone()[0] == 1, "persistent receipt missing")
            destination.close()
            print("PASS: reconnect committed receiver records and receipts; dashboard API reflects delivery")

            lost_id = "INC-E2E-ACK-" + str(int(time.time()))
            status, _ = operator.request("POST", "/api/transport/simulated", {"lose_next_ack": True})
            check(status == 200, "configure lost acknowledgement")
            lost_payload = {"incident_id": lost_id, "alert_type": "flood", "description": "Lost ACK retry"}
            status, lost = client.request("POST", "/api/sos", lost_payload, {"Idempotency-Key": lost_id})
            check(status == 201 and lost["alert"]["delivery_status"] == "queued" and lost["alert"]["receipt_id"] is None, "lost ACK must not report receipt")
            run_worker()
            destination = sqlite3.connect(receiver_db)
            committed_receipt = destination.execute("SELECT receipt_id FROM command_centre_receipts WHERE incident_id=?", (lost_id,)).fetchone()
            destination.close()
            check(committed_receipt is not None, "receiver must persist before simulated lost ACK")
            operator.request("POST", "/api/transport/simulated", {"connected": False})
            operator.request("POST", "/api/transport/simulated", {"connected": True})
            run_worker()
            status, duplicate = client.request("POST", "/api/sos", lost_payload, {"Idempotency-Key": lost_id})
            check(status == 200 and duplicate["alert"]["incident_id"] == lost_id, "retry should retain original incident ID")
            destination = sqlite3.connect(receiver_db)
            check(destination.execute("SELECT count(*) FROM command_centre_incidents WHERE incident_id=?", (lost_id,)).fetchone()[0] == 1, "duplicate receiver incident")
            destination.close()
            print("PASS: lost ACK remained queued, retry returned original durable receipt, no duplicate")

            for incident_id in (first_id,):
                status, assigned = operator.request("POST", f"/api/incidents/{incident_id}/assign", {"responder": "E2E Unit"})
                check(status == 200 and assigned["lifecycle_status"] == "responder_assigned", "responder assignment")
                for event in ("dispatched", "arrived", "resolved"):
                    status, result = operator.request("POST", f"/api/incidents/{incident_id}/lifecycle", {"status": event})
                    check(status == 200 and result["lifecycle_status"] == event, f"lifecycle {event}")
            status, detail = client.request("GET", f"/api/incidents/{first_id}")
            check(status == 200 and detail["incident"]["lifecycle_status"] == "resolved", "final dashboard state")
            print("PASS: operator assignment, dispatch, arrival, resolution persisted separately")

            sender = sqlite3.connect(sender_db)
            check(sender.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "sender integrity")
            sender.close()
            destination = sqlite3.connect(receiver_db)
            check(destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "receiver integrity")
            destination.close()
            check(PRODUCTION_DB.is_file() == production_existed,
                  "smoke test changed whether the production database exists")
            if production_existed:
                check(sha256(PRODUCTION_DB) == original_hash,
                      "production database changed during E2E script")
                print("PASS: temporary DB integrity checks; production DB hash unchanged")
            else:
                print("PASS: temporary DB integrity checks; production DB remained absent")
        finally:
            stop()
            log_handle.close()


if __name__ == "__main__":
    main()
