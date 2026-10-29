"""Integration tests for NexAlert's durable simulated SOS workflow.

These use Flask's test client and temporary SQLite files; no physical transport
or radio configuration is involved.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PATH = os.path.join(ROOT, "backend", "app.py")
SPEC = importlib.util.spec_from_file_location("nexalert_app", APP_PATH)
appmod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(appmod)


class CsrfTestClient(FlaskClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.incident_capabilities={}

    def open(self, *args, **kwargs):
        without_csrf=kwargs.pop("without_csrf",False)
        without_capability=kwargs.pop("without_capability",False)
        method=(kwargs.get("method") or "GET").upper()
        path=args[0] if args else kwargs.get("path", "")
        if not without_capability and path and (path.endswith("/api/sos") or path.endswith("/api/fallback/sos") or "/api/incidents/" in path):
            headers=dict(kwargs.get("headers") or {})
            if "X-Incident-Capability" not in headers:
                import base64, secrets
                body=kwargs.get("json") or {}
                incident=(body.get("incident_id") if isinstance(body,dict) else None) or headers.get("Idempotency-Key")
                if incident:
                    token=self.incident_capabilities.setdefault(str(incident),base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))
                    headers["X-Incident-Capability"]=token
                    kwargs["headers"]=headers
        if not without_csrf and method not in {"GET","HEAD","OPTIONS"}:
            try:
                with self.session_transaction() as current_session:
                    token=current_session.get("csrf_token")
                if token:
                    headers=dict(kwargs.get("headers") or {})
                    headers.setdefault("X-CSRF-Token",token)
                    kwargs["headers"]=headers
            except Exception:
                pass
        return super().open(*args,**kwargs)


appmod.app.test_client_class=CsrfTestClient
TEST_OPERATOR_PASSWORD="test-only-operator-password"
TEST_OPERATOR_HASH=generate_password_hash(TEST_OPERATOR_PASSWORD,method="scrypt")


class AppFakeRadioLink:
    """In-memory packet link used only by Flask acceptance tests."""
    def __init__(self):
        self.connected = True
        self.peer = None
        self.on_packet = None
        self.incoming = []
        self.sent = []
        self.drop_packet = None

    def send(self, packet):
        from backend.radio_transport import RadioTxResult
        self.sent.append(packet)
        if not self.connected:
            raise ConnectionError("fake link disconnected")
        if self.drop_packet and self.drop_packet(packet):
            return RadioTxResult(tx_done=True)
        if self.peer:
            if self.peer.on_packet:
                self.peer.on_packet(packet)
            else:
                self.peer.incoming.append(packet)
        return RadioTxResult(tx_done=True)

    def receive(self, timeout_s):
        return self.incoming.pop(0) if self.incoming else None


class SosWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db_path = appmod.DB_PATH
        self.old_cc_path = appmod.CC_DB_PATH
        self.old_fallback_path = appmod.FALLBACK_DB_PATH
        self.old_testing = appmod.app.config.get("TESTING", False)
        appmod.DB_PATH = os.path.join(self.tmp.name, "nexalert.db")
        appmod.CC_DB_PATH = os.path.join(self.tmp.name, "command-centre.db")
        appmod.FALLBACK_DB_PATH = os.path.join(self.tmp.name, "fallback.db")
        appmod.transport = appmod.SimulatedTransport()
        appmod.ACTIVE_CALLS.clear()
        appmod.ACTIVE_SOCKETS.clear();appmod.ACTIVE_SOCKET_IDENTITIES.clear()
        appmod.app.config.update(TESTING=True)
        appmod.init_db()
        self.client = appmod.app.test_client()
        self.client.get("/api/csrf")
        self.register("Test Citizen")
        self.operator_client=self.provision_operator()

    def tearDown(self):
        appmod.DB_PATH = self.old_db_path
        appmod.CC_DB_PATH = self.old_cc_path
        appmod.FALLBACK_DB_PATH = self.old_fallback_path
        appmod.app.config.update(TESTING=self.old_testing)
        self.tmp.cleanup()

    def register(self, display_name="Test Citizen", client=None):
        client=client or self.client
        if not client.get("/api/csrf").is_json:
            self.fail("CSRF bootstrap failed")
        response = client.post("/api/register", json={"display_name":display_name})
        self.assertEqual(response.status_code,201,response.get_json())
        return response.get_json()["user"]

    def provision_operator(self):
        db=sqlite3.connect(appmod.DB_PATH)
        db.execute("INSERT INTO users(identity_id,username,full_name,phone,role,password_hash) VALUES(?,?,?,'','operator',?)",
                   (str(__import__('uuid').uuid4()),"operator-test","Test Operator",TEST_OPERATOR_HASH))
        db.commit();db.close()
        client=appmod.app.test_client();client.get("/api/csrf")
        response=client.post("/api/operator/login",json={"username":"operator-test","password":TEST_OPERATOR_PASSWORD})
        if response.status_code!=200:raise AssertionError(response.get_json())
        return client

    def sos(self, incident_id="INC-test-incident", **extra):
        payload = {
            "incident_id": incident_id,
            "alert_type": "medical", "description": "Needs help",
            "latitude": 12.9, "longitude": 77.5,
        }
        payload.update(extra)
        response = self.client.post("/api/sos", json=payload)
        # Exercise the separately supervised worker deterministically after
        # the endpoint has returned; no sleeps or background thread required.
        if response.status_code in (200, 201):
            try:
                appmod.transport.process_queue(limit=500)
            except TypeError:
                appmod.transport.process_queue()
            body = response.get_json()
            if body and body.get("alert"):
                row = self.db_rows("SELECT delivery_status,lifecycle_status,receipt_id FROM alerts WHERE incident_id=?",
                                   (body["alert"]["incident_id"],))
                if row:
                    body["alert"].update(dict(row[0]))
                    response.set_data(json.dumps(body))
        return response

    def db_rows(self, sql, params=()):
        conn = appmod.open_db_connection()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def install_radio_adapter(self, receipt_drop=None, sender_id=b"APPNODE1", peer_id=b"GATEWAY1"):
        """Bind the adapter to real app DB callbacks; only the radio link is fake."""
        from backend.radio_protocol import decode
        from backend.radio_transport import RadioSosTransport

        sender_link, receiver_link = AppFakeRadioLink(), AppFakeRadioLink()
        sender_link.peer, receiver_link.peer = receiver_link, sender_link
        receiver_link.drop_packet = receipt_drop

        def pending_reports():
            conn = appmod.open_db_connection()
            try:
                rows = conn.execute("""
                    SELECT a.incident_id,a.alert_type,a.description,a.latitude,a.longitude,
                           a.landmark,a.timestamp
                    FROM alerts a JOIN outbound_queue q USING(incident_id)
                    WHERE q.state!='delivered' ORDER BY q.id
                """).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

        def record_attempt(report, outcome, detail):
            conn = appmod.open_db_connection()
            try:
                if outcome == "radio_tx_done":
                    conn.execute("UPDATE outbound_queue SET attempts=attempts+1,last_attempt_at=datetime('now') WHERE incident_id=?",
                                 (report["incident_id"],))
                conn.execute("INSERT INTO delivery_attempts(incident_id,outcome,detail) VALUES(?,?,?)",
                             (report["incident_id"], outcome, str(detail)))
                conn.commit()
            finally:
                conn.close()

        def persist_received(frame):
            body = json.loads(frame.payload.decode("ascii"))
            report = {"incident_id": frame.incident_id, "alert_type": body.get("c", "other"),
                      "description": body.get("d", ""), "latitude": body.get("a"),
                      "longitude": body.get("o"), "landmark": body.get("l", ""),
                      "timestamp": body.get("t"), "source_node_id": frame.source.hex(),
                      "origin_mode": "primary"}
            conn = sqlite3.connect(appmod.CC_DB_PATH)
            conn.row_factory = sqlite3.Row
            try:
                receipt = appmod.persist_command_centre_incident(conn, report)
                return receipt
            finally:
                conn.close()

        def record_receipt(report, receipt_id):
            sender = appmod.open_db_connection()
            receiver = sqlite3.connect(appmod.CC_DB_PATH)
            try:
                if not appmod.record_command_centre_receipt(
                        sender, receiver, report["incident_id"], receipt_id,
                        "fake-link integration received durable command-centre receipt"):
                    raise ValueError("receipt does not match persistent command-centre record")
            finally:
                sender.close()
                receiver.close()

        self.radio_receiver = RadioSosTransport(
            receiver_link, peer_id, sender_id, lambda: (), lambda *args: None,
            lambda *args: None, persist_received, lambda: True,
            max_attempts=3, ack_timeout_s=0.01, base_backoff_s=0, max_backoff_s=0)
        receiver_link.on_packet = self.radio_receiver.handle_packet
        self.radio_sender = RadioSosTransport(
            sender_link, sender_id, peer_id, pending_reports, record_attempt,
            record_receipt, lambda frame: "RCP-unexpected", lambda: True,
            max_attempts=3, ack_timeout_s=0.01, base_backoff_s=0, max_backoff_s=0)
        appmod.transport = self.radio_sender
        return sender_link, receiver_link

    def test_radio_adapter_is_not_the_default_application_transport(self):
        self.assertIsInstance(appmod.transport, appmod.SimulatedTransport)
        self.assertEqual(appmod.transport.status()["kind"], "simulated")
        self.assertEqual(self.client.get("/api/status").get_json()["transport"], "simulated")

    def test_sos_route_radio_boundary_persists_receipt_and_operator_lifecycle(self):
        self.install_radio_adapter()
        response = self.sos("INC-app-radio-accepted")
        body = response.get_json()["alert"]
        self.assertEqual(response.status_code, 201)
        self.assertEqual(body["delivery_status"], "received_by_command_centre")
        self.assertEqual(body["receipt_id"], "RCP-INC-app-radio-accepted")
        state = self.client.get("/api/status").get_json()
        self.assertEqual(state["transport"], "physical_radio_adapter")
        self.assertFalse(state["radio_verified"])
        self.assertFalse(state["simulated_transport_connected"])
        source = self.db_rows("SELECT state,attempts FROM outbound_queue WHERE incident_id=?",
                              ("INC-app-radio-accepted",))[0]
        self.assertEqual(source[0], "delivered")
        self.assertGreaterEqual(source[1], 1)
        self.assertLessEqual(source[1], 3)
        receiver = sqlite3.connect(appmod.CC_DB_PATH)
        self.assertEqual(receiver.execute("SELECT count(*) FROM command_centre_incidents WHERE incident_id=?",
                                           ("INC-app-radio-accepted",)).fetchone()[0], 1)
        self.assertEqual(receiver.execute("SELECT receipt_id FROM command_centre_receipts WHERE incident_id=?",
                                           ("INC-app-radio-accepted",)).fetchone()[0], body["receipt_id"])
        receiver.close()
        dashboard = self.client.get("/api/incidents").get_json()
        incident = next(item for item in dashboard if item["incident_id"] == "INC-app-radio-accepted")
        self.assertEqual(incident["delivery_status"], "received_by_command_centre")
        assigned = self.operator_client.post("/api/incidents/INC-app-radio-accepted/assign",
                                    json={"responder": "Radio Test Unit"})
        self.assertEqual(assigned.get_json()["lifecycle_status"], "responder_assigned")
        self.assertEqual(self.db_rows("SELECT delivery_status,lifecycle_status FROM alerts WHERE incident_id=?",
                                      ("INC-app-radio-accepted",))[0][:],
                         ("received_by_command_centre", "responder_assigned"))
        duplicate = self.sos("INC-app-radio-accepted")
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.get_json()["alert"]["lifecycle_status"], "responder_assigned")
        self.assertEqual(len(self.db_rows("SELECT * FROM incident_events WHERE incident_id=? AND status='received_by_command_centre'",
                                          ("INC-app-radio-accepted",))), 1)

    def test_tx_and_ack_without_receipt_stays_queued_then_reconnects(self):
        from backend.radio_protocol import MessageType, decode
        _, receiver_link = self.install_radio_adapter(
            receipt_drop=lambda packet: decode(packet).message_type == MessageType.RECEIPT)
        response = self.sos("INC-app-radio-no-receipt")
        self.assertEqual(response.get_json()["alert"]["delivery_status"], "queued")
        self.assertEqual(self.db_rows("SELECT state FROM outbound_queue WHERE incident_id=?",
                                      ("INC-app-radio-no-receipt",))[0][0], "queued")
        self.assertEqual(self.db_rows("SELECT delivery_status,receipt_id FROM alerts WHERE incident_id=?",
                                      ("INC-app-radio-no-receipt",))[0][:], ("queued", None))
        receiver = sqlite3.connect(appmod.CC_DB_PATH)
        self.assertEqual(receiver.execute("SELECT count(*) FROM command_centre_incidents WHERE incident_id=?",
                                           ("INC-app-radio-no-receipt",)).fetchone()[0], 1)
        receiver.close()
        receiver_link.drop_packet = None
        self.assertEqual(appmod.transport.process_queue(), 1)
        self.assertEqual(self.db_rows("SELECT delivery_status FROM alerts WHERE incident_id=?",
                                      ("INC-app-radio-no-receipt",))[0][0],
                         "received_by_command_centre")

    def test_disconnected_or_throwing_radio_keeps_http_sos_recoverable(self):
        sender_link, _ = self.install_radio_adapter()
        original_send = sender_link.send

        def fail_send(packet):
            raise OSError("fake serial disconnection")

        sender_link.send = fail_send
        response = self.sos("INC-app-radio-disconnect")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["alert"]["delivery_status"], "queued")
        self.assertEqual(self.db_rows("SELECT state FROM outbound_queue WHERE incident_id=?",
                                      ("INC-app-radio-disconnect",))[0][0], "queued")
        self.assertIn("fake serial disconnection", appmod.transport.last_error)
        sender_link.send = original_send
        self.assertEqual(appmod.transport.process_queue(), 1)
        self.assertEqual(self.db_rows("SELECT delivery_status FROM alerts WHERE incident_id=?",
                                      ("INC-app-radio-disconnect",))[0][0],
                         "received_by_command_centre")

    def test_radio_sender_restart_retries_original_incident_and_keeps_distinct_reports(self):
        from backend.radio_protocol import MessageType, decode
        sender_link, receiver_link = self.install_radio_adapter(
            receipt_drop=lambda packet: decode(packet).message_type == MessageType.RECEIPT)
        first = self.sos("INC-radio-restart-first")
        self.assertEqual(first.get_json()["alert"]["delivery_status"], "queued")
        original_frames = [packet for packet in sender_link.sent if decode(packet).message_type == MessageType.SOS]
        self.assertGreaterEqual(len(original_frames), 2)
        receiver_link.drop_packet = None
        # Rebuild the sender adapter to emulate process restart; the DB queue is authoritative.
        self.install_radio_adapter()
        self.assertEqual(appmod.transport.process_queue(), 1)
        second = self.sos("INC-radio-restart-second")
        self.assertEqual(second.get_json()["alert"]["delivery_status"], "received_by_command_centre")
        self.assertEqual(len(self.db_rows("SELECT * FROM alerts WHERE incident_id IN (?,?)",
                                           ("INC-radio-restart-first", "INC-radio-restart-second"))), 2)
        receiver = sqlite3.connect(appmod.CC_DB_PATH)
        self.assertEqual(receiver.execute("SELECT count(*) FROM command_centre_incidents WHERE incident_id IN (?,?)",
                                           ("INC-radio-restart-first", "INC-radio-restart-second")).fetchone()[0], 2)
        receiver.close()

    def test_valid_sos_is_persisted_before_delivery(self):
        appmod.transport.configure(connected=False)
        response = self.sos()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["alert"]["delivery_status"], "queued")
        self.assertEqual(len(self.db_rows("SELECT * FROM alerts")), 1)
        self.assertEqual(len(self.db_rows("SELECT * FROM outbound_queue")), 1)

    def activate_fallback_api(self):
        ctl = appmod.failover_controller
        ctl.monitor.last_received_at -= 30
        ctl.set_simulation(primary_running=False, drop_heartbeats=True)
        self.operator_client.get("/api/failover/status")  # timeout -> activating
        ctl.activation_started_at -= 3
        self.operator_client.get("/api/failover/status")  # readiness -> active
        self.assertTrue(ctl.fallback_service_ready)

    def test_fallback_sos_api_persists_locally_before_acceptance(self):
        self.activate_fallback_api()
        response = self.client.post("/api/fallback/sos", json={
            "incident_id": "INC-fallback-api", "alert_type": "medical", "description": "local help"})
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(body["status"], "accepted_locally")
        self.assertIsNone(body["command_centre_receipt"])
        self.assertEqual(appmod.fallback_store.pending_count(), 1)
        self.assertEqual(self.client.get("/api/fallback/incidents").get_json()[0]["incident_id"],"INC-fallback-api")
        other=appmod.app.test_client();other.get("/api/csrf");self.register("Fallback bystander",other)
        self.assertEqual(other.get("/api/fallback/incidents").get_json(),[])

    def test_dashboard_failover_api_reports_persisted_runtime_and_queue(self):
        self.activate_fallback_api()
        self.client.post("/api/fallback/sos", json={"incident_id": "INC-dashboard-fallback", "alert_type": "other"})
        state = self.operator_client.get("/api/failover/status").get_json()
        self.assertEqual(state["state"], "FALLBACK_ACTIVE")
        self.assertEqual(state["pending_fallback_incidents"], 1)
        self.assertFalse(state["physical_hardware_verified"])

    def test_failover_simulation_controls_are_disabled_outside_test_mode(self):
        old_testing = appmod.app.config["TESTING"]
        appmod.app.config.update(TESTING=False)
        try:
            appmod.init_db()
            response = self.operator_client.post("/api/failover/simulate", json={"action": "power_off"})
            self.assertEqual(response.status_code, 404)
        finally:
            appmod.app.config.update(TESTING=old_testing)
            # Rebuild process-local controller references against this test's
            # isolated database; later tests replace all three database paths.
            appmod.init_db()

    def test_failed_recovery_transfer_keeps_backup_outbox(self):
        self.activate_fallback_api()
        self.client.post("/api/fallback/sos", json={"incident_id": "INC-transfer-fail", "alert_type": "other"})
        ctl = appmod.failover_controller
        ctl.set_simulation(primary_running=True, primary_ready=True, drop_heartbeats=False)
        ctl.tick(); ctl.tick()
        self.operator_client.post("/api/failover/simulate", json={"action": "fail_next_transfer"})
        result = self.operator_client.post("/api/failover/simulate", json={"action": "sync"})
        self.assertEqual(result.get_json()["pending"], 1)
        self.assertEqual(appmod.fallback_store.pending_count(), 1)
        self.assertEqual(ctl.state.value, "DEGRADED")

    def test_recovery_sync_commits_same_fallback_incident_to_destination(self):
        self.activate_fallback_api()
        report = self.client.post("/api/fallback/sos", json={
            "incident_id": "INC-fallback-sync", "alert_type": "flood", "description": "rising water"})
        self.assertEqual(report.status_code, 201, report.get_json())
        ctl = appmod.failover_controller
        ctl.set_simulation(primary_running=True, primary_ready=True, drop_heartbeats=False)
        ctl.tick()  # authenticated heartbeat -> recovery
        ctl.tick()  # readiness -> syncing
        self.assertEqual(ctl.state.value, "SYNCING_INCIDENTS")
        response = self.operator_client.post("/api/failover/simulate", json={"action": "sync"})
        self.assertEqual(response.status_code, 200, response.get_json())
        cc = __import__('sqlite3').connect(appmod.CC_DB_PATH)
        try:
            destination = cc.execute("SELECT incident_id,origin_mode,source_node_id,owner_identity_id,ownership_state,ownership_assertion_digest FROM command_centre_incidents WHERE incident_id=?", ("INC-fallback-sync",)).fetchone()
        finally: cc.close()
        self.assertIsNotNone(destination)
        self.assertEqual(destination[1], "fallback")
        self.assertEqual(destination[2], "esp32-backup-sim")
        expected=sqlite3.connect(appmod.DB_PATH).execute("SELECT identity_id FROM users WHERE id=?",(self.client.get("/api/session").get_json()["user"]["id"],)).fetchone()[0]
        self.assertEqual(destination[3],expected)
        self.assertEqual(destination[4],"verified")
        primary=sqlite3.connect(appmod.DB_PATH)
        primary_owner=primary.execute("SELECT owner_identity_id,ownership_state,ownership_assertion_digest FROM alerts WHERE incident_id=?",("INC-fallback-sync",)).fetchone()
        self.assertEqual(primary_owner[:2],(expected,"verified"));self.assertTrue(primary_owner[2])
        self.assertEqual(destination[5],primary_owner[2])
        primary.close()
        # Reopen both durable stores through the normal startup path; the
        # reconciled owner and destination record must remain unchanged.
        appmod.fallback_store=appmod.FallbackStore(appmod.FALLBACK_DB_PATH)
        appmod.init_db()
        primary=sqlite3.connect(appmod.DB_PATH)
        self.assertEqual(primary.execute("SELECT owner_identity_id,ownership_state FROM alerts WHERE incident_id=?",("INC-fallback-sync",)).fetchone(),(expected,"verified"))
        primary.close()
        cc=sqlite3.connect(appmod.CC_DB_PATH)
        self.assertEqual(cc.execute("SELECT owner_identity_id,ownership_state FROM command_centre_incidents WHERE incident_id=?",("INC-fallback-sync",)).fetchone(),(expected,"verified"));cc.close()
        self.assertEqual(appmod.fallback_store.pending_count(), 0)

    def test_operator_lifecycle_update_preserves_original_citizen_owner(self):
        self.sos("INC-owner-lifecycle")
        before=self.db_rows("SELECT user_id,owner_identity_id,ownership_state FROM alerts WHERE incident_id=?",("INC-owner-lifecycle",))[0]
        self.operator_client.post("/api/incidents/INC-owner-lifecycle/assign",json={"responder":"Crew"})
        self.operator_client.post("/api/incidents/INC-owner-lifecycle/lifecycle",json={"status":"dispatched"})
        after=self.db_rows("SELECT user_id,owner_identity_id,ownership_state FROM alerts WHERE incident_id=?",("INC-owner-lifecycle",))[0]
        self.assertEqual(after,before)

    def test_fallback_assertion_is_incident_and_node_bound_and_duplicate_owner_is_immutable(self):
        self.activate_fallback_api()
        incident="INC-ownership-bound"
        self.assertEqual(self.client.post("/api/fallback/sos",json={"incident_id":incident,"alert_type":"other"}).status_code,201)
        row=appmod.fallback_store.pending()[0]
        user_id=self.client.get("/api/session").get_json()["user"]["id"]
        owner=sqlite3.connect(appmod.DB_PATH).execute("SELECT identity_id FROM users WHERE id=?",(user_id,)).fetchone()[0]
        self.assertEqual(row["owner_identity_id"],owner);self.assertEqual(row["ownership_state"],"verified")
        self.assertTrue(row["ownership_assertion_digest"])
        spoof=self.client.post("/api/sos",json={"incident_id":"INC-owner-spoof","owner_identity_id":"00000000-0000-0000-0000-000000000001"})
        self.assertEqual(spoof.status_code,400)
        wrong=dict(row);wrong["owner_identity_id"]="00000000-0000-0000-0000-000000000001"
        with self.assertRaises(appmod.FailoverError):
            appmod.fallback_store.accept(wrong,appmod.FALLBACK_NODE_ID,appmod.time.time())
        bad=dict(row);bad["ownership_assertion"]="0"*64
        internal={"incident_id":incident,"idempotency_key":incident,"username":"fallback-esp32-backup-sim",
            "alert_type":"other","origin_mode":"fallback","source_node_id":appmod.FALLBACK_NODE_ID,
            "owner_identity_id":owner,"ownership_issuer":appmod.FALLBACK_NODE_ID,"ownership_assertion":bad["ownership_assertion"],
            "incident_capability_hash":row["incident_capability_hash"]}
        response=appmod.app.test_client().post("/api/sos",json=internal,headers={"X-NexAlert-Internal":appmod.INTERNAL_FORWARD_TOKEN})
        self.assertEqual(response.status_code,403)
        self.assertEqual(appmod.fallback_store.pending_count(),1)

    def test_new_citizen_without_preprovisioned_fallback_credential_is_unclaimed(self):
        self.activate_fallback_api()
        late=appmod.app.test_client();late.get("/api/csrf");self.register("Joined during outage",late)
        response=late.post("/api/fallback/sos",json={"incident_id":"INC-unprovisioned-owner","alert_type":"other"})
        self.assertEqual(response.status_code,201,response.get_json())
        row=appmod.fallback_store.pending()[0]
        self.assertEqual(row["ownership_state"],"unclaimed")
        self.assertIsNone(row["owner_identity_id"])

    def test_unclaimed_fallback_incident_remains_operator_visible_after_sync(self):
        self.activate_fallback_api()
        late=appmod.app.test_client();late.get("/api/csrf");self.register("Late joiner",late)
        self.assertEqual(late.post("/api/fallback/sos",json={"incident_id":"INC-visible-unclaimed","alert_type":"other"}).status_code,201)
        ctl=appmod.failover_controller;ctl.set_simulation(primary_running=True,primary_ready=True,drop_heartbeats=False)
        ctl.tick();ctl.tick()
        synced=self.operator_client.post("/api/failover/simulate",json={"action":"sync"})
        self.assertEqual(synced.status_code,200,synced.get_json())
        incident=self.db_rows("SELECT ownership_state,owner_identity_id FROM alerts WHERE incident_id=?",("INC-visible-unclaimed",))[0]
        self.assertEqual(incident["ownership_state"],"unclaimed");self.assertIsNone(incident["owner_identity_id"])
        visible=self.operator_client.get("/api/incidents").get_json()
        self.assertTrue(any(row["incident_id"]=="INC-visible-unclaimed" for row in visible))
        self.assertEqual(late.get("/api/incidents/INC-visible-unclaimed").status_code,404)

    def test_operator_socket_is_not_sent_events_after_role_or_session_change(self):
        sock=appmod.socketio.test_client(appmod.app,flask_test_client=self.operator_client)
        operator=self.operator_client.get("/api/session").get_json()["user"]
        db=sqlite3.connect(appmod.DB_PATH)
        db.execute("UPDATE users SET role='citizen',session_version=session_version+1 WHERE id=?",(operator["id"],));db.commit();db.close()
        appmod.emit_to_operators("new_alert",{"description":"private"})
        self.assertFalse(sock.is_connected())
        self.assertNotIn(operator["id"],appmod.ACTIVE_SOCKETS)

    def test_revoked_operator_cannot_receive_private_incident_update(self):
        sock=appmod.socketio.test_client(appmod.app,flask_test_client=self.operator_client)
        sock.get_received()
        operator=self.operator_client.get("/api/session").get_json()["user"]
        db=sqlite3.connect(appmod.DB_PATH)
        db.execute("UPDATE users SET role='citizen' WHERE id=?",(operator["id"],));db.commit();db.close()
        appmod.emit_to_operators("incident_updated",{"incident_id":"INC-private","description":"private location details"})
        self.assertFalse(sock.is_connected())

    def test_old_operator_socket_gets_no_new_alert_after_second_login(self):
        old_socket=appmod.socketio.test_client(appmod.app,flask_test_client=self.operator_client)
        old_socket.get_received()
        replacement=appmod.app.test_client();replacement.get("/api/csrf")
        logged=replacement.post("/api/operator/login",json={"username":"operator-test","password":TEST_OPERATOR_PASSWORD})
        self.assertEqual(logged.status_code,200)
        appmod.emit_to_operators("new_alert",{"description":"secret"})
        self.assertFalse(old_socket.is_connected())

    def test_cli_style_version_bump_revokes_socket_pushes(self):
        sock=appmod.socketio.test_client(appmod.app,flask_test_client=self.operator_client)
        sock.get_received()
        operator=self.operator_client.get("/api/session").get_json()["user"]
        db=sqlite3.connect(appmod.DB_PATH);db.execute("UPDATE users SET session_version=session_version+1 WHERE id=?",(operator["id"],));db.commit();db.close()
        appmod.emit_to_operators("new_alert",{"incident_id":"private"})
        self.assertFalse(sock.is_connected())

    def test_socket_closed_after_session_lifetime(self):
        sock=appmod.socketio.test_client(appmod.app,flask_test_client=self.operator_client)
        sid=next(iter(appmod.ACTIVE_SOCKET_IDENTITIES))
        appmod._reap_expired_sockets(now=appmod.ACTIVE_SOCKET_IDENTITIES[sid]["expires_at"]+0.01)
        self.assertFalse(sock.is_connected())

    def test_logout_still_disconnects_operator_socket(self):
        sock=appmod.socketio.test_client(appmod.app,flask_test_client=self.operator_client)
        self.assertEqual(self.operator_client.post("/api/logout",json={}).status_code,200)
        self.assertFalse(sock.is_connected())

    def test_announcement_reaches_valid_citizen_sockets_only(self):
        citizen_socket=appmod.socketio.test_client(appmod.app,flask_test_client=self.client)
        operator_socket=appmod.socketio.test_client(appmod.app,flask_test_client=self.operator_client)
        citizen_socket.get_received();operator_socket.get_received()
        response=self.operator_client.post("/api/announcements",json={"content":"Evacuate","category":"flood_alert"})
        self.assertEqual(response.status_code,201)
        self.assertTrue(any(event["name"]=="system_announcement" for event in citizen_socket.get_received()))
        self.assertFalse(any(event["name"]=="system_announcement" for event in operator_socket.get_received()))

    def test_session_loss_retry_does_not_reassign_or_duplicate_incident(self):
        self.assertEqual(self.sos("INC-session-recovery").status_code,201)
        original=self.db_rows("SELECT user_id,owner_identity_id,ownership_state FROM alerts WHERE incident_id=?",("INC-session-recovery",))[0]
        self.client.post("/api/logout",json={})
        replacement=appmod.app.test_client();replacement.get("/api/csrf");self.register("Replacement identity",replacement)
        denied=replacement.get("/api/incidents/INC-session-recovery")
        retry=replacement.post("/api/sos",json={"incident_id":"INC-session-recovery","description":"retry"})
        self.assertEqual(denied.status_code,404);self.assertEqual(retry.status_code,404)
        restored=self.client.post("/api/sos",json={"incident_id":"INC-session-recovery","description":"same owner recovery"})
        self.assertEqual(restored.status_code,200,restored.get_json())
        self.assertEqual(self.db_rows("SELECT count(*) AS n FROM alerts WHERE incident_id=?",("INC-session-recovery",))[0]["n"],1)
        self.assertEqual(self.db_rows("SELECT user_id,owner_identity_id,ownership_state FROM alerts WHERE incident_id=?",("INC-session-recovery",))[0],original)

    def test_incident_capability_survives_session_logout_but_not_a_guess(self):
        self.assertEqual(self.sos("INC-capability-recovery").status_code,201)
        capability=self.client.incident_capabilities["INC-capability-recovery"]
        committed=self.db_rows("SELECT incident_capability_hash FROM alerts WHERE incident_id=?",("INC-capability-recovery",))[0][0]
        self.assertEqual(committed,appmod.hashlib.sha256(__import__("base64").urlsafe_b64decode(capability+"=")).hexdigest())
        anonymous=appmod.app.test_client()
        self.assertEqual(anonymous.get("/api/incidents/INC-capability-recovery").status_code,401)
        self.client.post("/api/logout",json={})
        recovered=self.client.get("/api/incidents/INC-capability-recovery",headers={"X-Incident-Capability":capability})
        self.assertEqual(recovered.status_code,200,recovered.get_json())
        self.assertNotIn("incident_capability_hash",recovered.get_json()["incident"])

    def test_fallback_retry_after_session_loss_preserves_verified_owner(self):
        self.activate_fallback_api()
        first=self.client.post("/api/fallback/sos",json={"incident_id":"INC-fallback-session-loss","alert_type":"other"})
        self.assertEqual(first.status_code,201,first.get_json())
        original=appmod.fallback_store.pending()[0]
        self.client.post("/api/logout",json={})
        retry=self.client.post("/api/fallback/sos",json={"incident_id":"INC-fallback-session-loss","alert_type":"other"})
        self.assertEqual(retry.status_code,200,retry.get_json())
        restored=appmod.fallback_store.pending()[0]
        self.assertEqual(restored["owner_identity_id"],original["owner_identity_id"])
        self.assertEqual(restored["ownership_assertion_digest"],original["ownership_assertion_digest"])

    def test_lost_ack_retry_does_not_duplicate_command_centre_incident(self):
        self.activate_fallback_api()
        self.client.post("/api/fallback/sos", json={"incident_id":"INC-fallback-ack", "alert_type":"other"})
        expected=sqlite3.connect(appmod.DB_PATH).execute("SELECT identity_id FROM users WHERE id=?",(self.client.get("/api/session").get_json()["user"]["id"],)).fetchone()[0]
        ctl = appmod.failover_controller
        ctl.set_simulation(primary_running=True, primary_ready=True, drop_heartbeats=False)
        ctl.tick(); ctl.tick()
        self.operator_client.post("/api/failover/simulate", json={"action":"lose_next_ack"})
        first = self.operator_client.post("/api/failover/simulate", json={"action":"sync"})
        self.assertEqual(first.get_json()["pending"], 1)
        self.assertEqual(appmod.fallback_store.pending_count(), 1)
        second = self.operator_client.post("/api/failover/simulate", json={"action":"sync"})
        # The first failed reconciliation leaves the controller degraded; a fresh
        # primary heartbeat/readiness cycle is required before retrying.
        self.assertEqual(second.status_code, 409)
        ctl.set_simulation(primary_running=True, primary_ready=True, drop_heartbeats=False)
        ctl.monitor.last_received_at -= 6
        ctl.tick(); ctl.tick()
        self.assertEqual(ctl.state.value, "SYNCING_INCIDENTS")
        retry = self.operator_client.post("/api/failover/simulate", json={"action": "sync"})
        self.assertEqual(retry.status_code, 200, retry.get_json())
        cc = __import__('sqlite3').connect(appmod.CC_DB_PATH)
        try:
            count = cc.execute("SELECT count(*) FROM command_centre_incidents WHERE incident_id=?", ("INC-fallback-ack",)).fetchone()[0]
            owner = cc.execute("SELECT owner_identity_id,ownership_state FROM command_centre_incidents WHERE incident_id=?",("INC-fallback-ack",)).fetchone()
        finally: cc.close()
        self.assertEqual(count, 1)
        self.assertEqual(owner,(expected,"verified"))
        self.assertEqual(appmod.fallback_store.pending_count(), 0)

    def test_public_registration_cannot_select_username_or_role(self):
        invalid = self.client.post("/api/register", json={"username":"operator-test","role":"operator"})
        self.assertEqual(invalid.status_code,400)
        self.assertEqual(self.client.get("/api/session").get_json()["user"]["role"],"citizen")
        another=appmod.app.test_client();another.get("/api/csrf")
        user=self.register("Test Citizen",another)
        self.assertNotEqual(user["id"],self.client.get("/api/session").get_json()["user"]["id"])

    def test_dashboard_broadcast_is_validated_persisted_and_local_only(self):
        forbidden=self.client.post("/api/announcements",json={"content":"Public spoof","category":"all_clear"})
        self.assertEqual(forbidden.status_code,403)
        invalid = self.operator_client.post("/api/announcements", json={"content": " ", "category": "broadcast"})
        self.assertEqual(invalid.status_code, 400)
        response = self.operator_client.post("/api/announcements", json={
            "content": "Flood alert", "category": "flood_alert", "actor_id":999999,
        })
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(body["delivery"], "local_application_only")
        rows = self.client.get("/api/announcements").get_json()
        self.assertEqual(rows[0]["content"], "Flood alert")
        operator=self.operator_client.get("/api/session").get_json()["user"]
        stored=self.db_rows("SELECT created_by FROM system_announcements WHERE id=?",(body["announcement"]["id"],))[0]
        self.assertEqual(stored["created_by"],operator["id"])

    def test_dashboard_status_reports_simulated_transport_and_real_summary(self):
        status = self.client.get("/api/status").get_json()
        self.assertEqual(status["transport"], "simulated")
        self.assertFalse(status["radio_verified"])
        summary = self.operator_client.get("/api/dashboard/summary").get_json()
        self.assertIn("online_participants", summary)
        self.assertIn("messages", summary)
        self.assertIn("sos_today", summary)
        self.assertGreaterEqual(summary["uptime_seconds"], 0)

    def test_citizen_and_dashboard_templates_expose_required_controls(self):
        citizen = self.client.get("/")
        self.assertEqual(citizen.status_code, 200)
        for marker in ("CONTINUE PRIVATELY", "CHAT", "CALL", "SOS", "MAP", "call-participant", "sos-grid"):
            self.assertIn(marker.encode(), citizen.data)
        self.assertEqual(self.client.get("/static/js/socket-client.js").status_code, 200)
        worker = self.client.get("/service-worker.js")
        self.assertEqual(worker.status_code, 200)
        self.assertEqual(worker.headers.get("Service-Worker-Allowed"), "/")
        self.assertEqual(self.client.get("/dashboard").status_code,302)
        dashboard = self.operator_client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        for marker in ("Incident Map", "SEND TO ALL", "CYCLONE WARNING", "activity-feed", "stat-uptime"):
            self.assertIn(marker.encode(), dashboard.data)

    def test_chat_post_is_saved_and_returned_by_real_message_api(self):
        recipient_client=appmod.app.test_client();recipient_client.get("/api/csrf")
        recipient=self.register("Recipient",recipient_client)
        response = self.client.post("/api/messages", json={"recipient":recipient["username"],"content":"Test message"})
        self.assertEqual(response.status_code, 201)
        message = response.get_json()["message"]
        rows = self.client.get("/api/messages?username=operator-test").get_json()
        self.assertTrue(any(row["id"] == message["id"] and row["content"] == "Test message" for row in rows))
        self.assertEqual(recipient_client.get("/api/messages?username=operator-test").status_code,200)
        self.assertTrue(any(row["id"]==message["id"] for row in recipient_client.get("/api/messages").get_json()))
        self.assertEqual(self.operator_client.get("/api/messages").status_code,403)

    def test_anonymous_and_citizen_private_endpoints_are_scoped(self):
        anonymous=appmod.app.test_client()
        self.assertEqual(anonymous.get("/api/incidents").status_code,401)
        self.assertEqual(anonymous.get("/api/users").status_code,401)
        self.assertEqual(self.client.get("/api/users").status_code,403)
        self.assertEqual(self.client.get("/api/alerts").status_code,403)

    def test_citizen_cannot_read_or_overwrite_another_citizens_incident(self):
        response=self.sos("INC-owned-by-first")
        self.assertEqual(response.status_code,201,response.get_json())
        owner=appmod.app.test_client();owner.get("/api/csrf")
        other=self.register("Other citizen",owner)
        self.assertEqual(owner.get("/api/incidents/INC-owned-by-first").status_code,404)
        self.assertEqual(owner.get("/api/incidents").get_json(),[])
        denied=owner.post("/api/incidents/INC-owned-by-first/updates",json={"description":"stolen"})
        self.assertEqual(denied.status_code,404)
        spoof=owner.post("/api/sos",json={"incident_id":"INC-forged-owner","user_id":self.client.get("/api/session").get_json()["user"]["id"],"description":"spoof"})
        self.assertEqual(spoof.status_code,400)
        saved=self.db_rows("SELECT description,user_id FROM alerts WHERE incident_id=?",("INC-owned-by-first",))[0]
        self.assertEqual(saved["description"],"Needs help")
        self.assertNotEqual(saved["user_id"],other["id"])

    def test_message_query_cannot_select_another_users_conversation(self):
        target=appmod.app.test_client();target.get("/api/csrf")
        target_user=self.register("Target",target)
        sent=self.client.post("/api/messages",json={"recipient":target_user["username"],"content":"private"})
        self.assertEqual(sent.status_code,201)
        unrelated=appmod.app.test_client();unrelated.get("/api/csrf");self.register("Unrelated",unrelated)
        self.assertEqual(unrelated.get("/api/messages?username="+target_user["username"]).get_json(),[])

    def test_private_message_socket_event_reaches_only_sender_and_recipient(self):
        recipient=appmod.app.test_client();recipient.get("/api/csrf")
        recipient_user=self.register("Socket recipient",recipient)
        bystander=appmod.app.test_client();bystander.get("/api/csrf");self.register("Socket bystander",bystander)
        sender_socket=appmod.socketio.test_client(appmod.app,flask_test_client=self.client)
        recipient_socket=appmod.socketio.test_client(appmod.app,flask_test_client=recipient)
        bystander_socket=appmod.socketio.test_client(appmod.app,flask_test_client=bystander)
        sent=self.client.post("/api/messages",json={"recipient":recipient_user["username"],"content":"private socket text"})
        self.assertEqual(sent.status_code,201)
        self.assertTrue(any(e["name"]=="new_message" for e in sender_socket.get_received()))
        self.assertTrue(any(e["name"]=="new_message" for e in recipient_socket.get_received()))
        self.assertFalse(any(e["name"]=="new_message" for e in bystander_socket.get_received()))
        sender_socket.disconnect();recipient_socket.disconnect();bystander_socket.disconnect()

    def test_citizens_cannot_change_incident_lifecycle_and_operator_actor_is_server_bound(self):
        response=self.sos("INC-audited-action")
        incident=response.get_json()["alert"]
        self.assertEqual(self.client.post("/api/incidents/INC-audited-action/assign",json={"responder":"Crew"}).status_code,403)
        self.assertEqual(self.client.post("/api/incidents/INC-audited-action/lifecycle",json={"status":"resolved"}).status_code,403)
        row=self.db_rows("SELECT id FROM alerts WHERE incident_id=?",("INC-audited-action",))[0]
        self.assertEqual(self.operator_client.post("/api/incidents/INC-audited-action/assign",json={"responder":"Crew","actor_id":999999}).status_code,200)
        action=self.db_rows("SELECT actor_id,action FROM operator_actions WHERE target_incident_id=? ORDER BY id DESC LIMIT 1",("INC-audited-action",))[0]
        operator=self.operator_client.get("/api/session").get_json()["user"]
        self.assertEqual(action["actor_id"],operator["id"])
        self.assertEqual(action["action"],"assign_responder")

    def test_socket_incident_events_are_limited_to_operator_audience(self):
        citizen_socket=appmod.socketio.test_client(appmod.app,flask_test_client=self.client)
        operator_socket=appmod.socketio.test_client(appmod.app,flask_test_client=self.operator_client)
        citizen_socket.emit("join",{"room":"operators"})
        for event in citizen_socket.get_received():
            self.assertNotEqual(event["name"],"new_alert")
        response=self.sos("INC-socket-private")
        self.assertEqual(response.status_code,201)
        citizen_events=citizen_socket.get_received()
        operator_events=operator_socket.get_received()
        self.assertFalse(any(e["name"]=="new_alert" for e in citizen_events))
        alert_event=next(e for e in operator_events if e["name"]=="new_alert")
        self.assertEqual(alert_event["args"][0]["incident_id"],"INC-socket-private")
        citizen_socket.disconnect();operator_socket.disconnect()

    def test_csrf_logout_revocation_and_cookie_policy(self):
        cookie=self.client.get_cookie("nexalert_session")
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site,"Lax")
        missing=self.client.post("/api/announcements",json={"content":"x"},without_csrf=True)
        self.assertEqual(missing.status_code,403)
        wrong=self.client.post("/api/logout",json={},headers={"X-CSRF-Token":"tampered"})
        self.assertEqual(wrong.status_code,403)
        stale=appmod.app.test_client()
        stale.set_cookie("nexalert_session",cookie.value)
        response=self.client.post("/api/logout",json={})
        self.assertEqual(response.status_code,200)
        self.assertEqual(stale.get("/api/session").status_code,401)

    def test_expired_session_cookie_cannot_access_authenticated_routes(self):
        cookie=self.client.get_cookie("nexalert_session")
        expired=appmod.app.test_client();expired.set_cookie("nexalert_session",cookie.value)
        old_lifetime=appmod.app.config["PERMANENT_SESSION_LIFETIME"]
        try:
            appmod.app.config["PERMANENT_SESSION_LIFETIME"]=timedelta(seconds=-1)
            self.assertEqual(expired.get("/api/session").status_code,401)
            self.assertEqual(expired.get("/api/incidents").status_code,401)
        finally:
            appmod.app.config["PERMANENT_SESSION_LIFETIME"]=old_lifetime

    def test_public_pages_health_and_announcement_reads_remain_available(self):
        public=appmod.app.test_client()
        self.assertEqual(public.get("/").status_code,200)
        self.assertEqual(public.get("/health").status_code,200)
        self.assertEqual(public.get("/api/announcements").status_code,200)

    def test_deployment_startup_fails_closed_without_a_strong_secret(self):
        base=dict(os.environ)
        for key in ("NEXALERT_SECRET","NEXALERT_HTTPS","NEXALERT_TRUST_PROXY"):
            base.pop(key,None)
        base.update({"NEXALERT_ENV":"production",
                     "NEXALERT_DB":os.path.join(self.tmp.name,"startup.db"),
                     "NEXALERT_CC_DB":os.path.join(self.tmp.name,"startup-cc.db"),
                     "NEXALERT_LOG_DIR":os.path.join(self.tmp.name,"logs")})
        for secret in (None,"too-short"):
            env=dict(base)
            if secret is not None:env["NEXALERT_SECRET"]=secret
            result=subprocess.run([sys.executable,"-c","import backend.app"],cwd=ROOT,env=env,
                                  text=True,capture_output=True,timeout=15)
            self.assertNotEqual(result.returncode,0)
            self.assertIn("NEXALERT_SECRET must contain at least 32 bytes",result.stderr)
        tls_env=dict(base);tls_env.update({"NEXALERT_SECRET":"x"*48,"NEXALERT_BACKUP_OWNERSHIP_KEY":"y"*48,
            "NEXALERT_TRUSTED_PROXY_ADDRESS":"127.0.0.1","NEXALERT_TLS_CERT":"/nonexistent/operator.crt",
            "NEXALERT_TLS_KEY":"/nonexistent/operator.key"})
        result=subprocess.run([sys.executable,"-c","import backend.app as m; m.validate_production_tls_config('127.0.0.1')"],
                              cwd=ROOT,env=tls_env,text=True,capture_output=True,timeout=15)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("TLS certificate or private key is invalid",result.stderr)

    def test_untrusted_forwarded_https_header_is_ignored(self):
        from unittest.mock import patch
        observed={}
        def app(environ,start_response):
            observed.update(environ)
            return []
        proxy=appmod.TrustedLocalProxy(app)
        environ={"REMOTE_ADDR":"192.0.2.55","wsgi.url_scheme":"http","HTTP_X_FORWARDED_PROTO":"https"}
        with patch.object(appmod,"TRUSTED_PROXY_ADDRESS","127.0.0.1"):
            proxy(environ,lambda *args:None)
        self.assertEqual(observed["wsgi.url_scheme"],"http")
        self.assertNotIn("HTTP_X_FORWARDED_PROTO",observed)

    def test_operator_cli_refuses_to_convert_existing_citizen(self):
        from unittest.mock import patch
        from backend import manage_operator
        module=manage_operator.app_module
        old=(module.DB_PATH,module.CC_DB_PATH,module.FALLBACK_DB_PATH)
        module.DB_PATH,module.CC_DB_PATH,module.FALLBACK_DB_PATH=appmod.DB_PATH,appmod.CC_DB_PATH,appmod.FALLBACK_DB_PATH
        try:
            args=["manage_operator","--username",self.client.get("/api/session").get_json()["user"]["username"]]
            with patch("sys.argv",args),patch.object(manage_operator.getpass,"getpass",side_effect=["test-password-long","test-password-long"]):
                with self.assertRaises(SystemExit):manage_operator.main()
            row=self.db_rows("SELECT role,password_hash FROM users WHERE username=?",(args[-1],))[0]
            self.assertEqual(tuple(row),("citizen",None))
        finally:module.DB_PATH,module.CC_DB_PATH,module.FALLBACK_DB_PATH=old

    def test_local_operator_recovery_rotates_password_and_revokes_old_session(self):
        from unittest.mock import patch
        from backend import manage_operator
        module=manage_operator.app_module
        old=(module.DB_PATH,module.CC_DB_PATH,module.FALLBACK_DB_PATH)
        module.DB_PATH,module.CC_DB_PATH,module.FALLBACK_DB_PATH=appmod.DB_PATH,appmod.CC_DB_PATH,appmod.FALLBACK_DB_PATH
        try:
            operator=self.operator_client.get("/api/session").get_json()["user"]
            previous_version=self.db_rows("SELECT session_version FROM users WHERE id=?",(operator["id"],))[0][0]
            args=["manage_operator","--username","operator-test"]
            with patch("sys.argv",args),patch.object(manage_operator.getpass,"getpass",side_effect=["recovered-password-long","recovered-password-long"]):
                manage_operator.main()
            self.assertEqual(self.operator_client.get("/api/session").status_code,401)
            row=self.db_rows("SELECT session_version,password_hash,role FROM users WHERE username='operator-test'")[0]
            self.assertGreater(row["session_version"],previous_version)
            self.assertEqual(row["role"],"operator")
            self.assertTrue(appmod.check_password_hash(row["password_hash"],"recovered-password-long"))
            with appmod.app.test_request_context("/api/operator/login",method="POST",json={"username":"operator-test","password":"recovered-password-long"}):
                response=appmod.app.make_response(appmod.app.view_functions["operator_login"]())
            self.assertEqual(response.status_code,200)
        finally:module.DB_PATH,module.CC_DB_PATH,module.FALLBACK_DB_PATH=old

    def test_contact_sync_never_discloses_phone_account_existence(self):
        other=appmod.app.test_client();other.get("/api/csrf")
        user=self.register("Phone user",other)
        db=sqlite3.connect(appmod.DB_PATH)
        db.execute("UPDATE users SET phone='555-0100' WHERE id=?",(user["id"],))
        db.execute("INSERT INTO contacts(owner_id,contact_name,contact_phone,on_network,network_user_id) VALUES(?,?,?,?,?)",
                   (self.client.get("/api/session").get_json()["user"]["id"],"Old match","555-0100",1,user["id"]))
        db.commit();db.close()
        response=self.client.post("/api/contacts",json={"contacts":[]})
        self.assertEqual(response.status_code,200,response.get_json())
        self.assertFalse(response.get_json()[0]["on_network"])

    def test_malformed_and_oversized_sos_do_not_create_incidents(self):
        before=self.db_rows("SELECT count(*) FROM alerts")[0][0]
        malformed=self.client.post("/api/sos",data="{",content_type="application/json")
        oversized=self.client.post("/api/sos",data='{"alert_type":"other","description":"'+("x"*5000)+'"}',content_type="application/json")
        self.assertEqual(malformed.status_code,400,malformed.get_json())
        self.assertEqual(oversized.status_code,413,oversized.get_json())
        self.assertEqual(self.db_rows("SELECT count(*) FROM alerts")[0][0],before)

    def test_operator_login_source_throttle_expires_without_account_lockout(self):
        old_clock=appmod.RATE_LIMIT_CLOCK
        now=[2_000_000]
        appmod.RATE_LIMIT_CLOCK=lambda:now[0]
        try:
            for _ in range(20):self.assertTrue(appmod._consume_rate_limit("operator-login","test-source",20,60)[0])
            self.assertFalse(appmod._consume_rate_limit("operator-login","test-source",20,60)[0])
            self.assertEqual(self.db_rows("SELECT auth_failed_attempts FROM users WHERE username='operator-test'")[0][0],0)
            now[0]+=60
            self.assertTrue(appmod._consume_rate_limit("operator-login","test-source",20,60)[0])
            for credentials,expected in (({"username":"operator-test","password":"wrong"},401),
                                         ({"username":"operator-test","password":TEST_OPERATOR_PASSWORD},200)):
                with appmod.app.test_request_context("/api/operator/login",method="POST",json=credentials):
                    response=appmod.app.view_functions["operator_login"]()
                    self.assertEqual(appmod.app.make_response(response).status_code,expected)
        finally:appmod.RATE_LIMIT_CLOCK=old_clock

    def test_sos_identity_rate_limit_recovers_at_window_boundary(self):
        old_clock=appmod.RATE_LIMIT_CLOCK
        now=[2_100_000]
        appmod.RATE_LIMIT_CLOCK=lambda:now[0]
        try:
            for i in range(20):
                response=self.client.post("/api/sos",json={"incident_id":f"INC-rate-{i}","alert_type":"other"})
                self.assertEqual(response.status_code,201,response.get_json())
            limited=self.client.post("/api/sos",json={"incident_id":"INC-rate-limited","alert_type":"other"})
            self.assertEqual(limited.status_code,429)
            now[0]+=60
            recovered=self.client.post("/api/sos",json={"incident_id":"INC-rate-recovered","alert_type":"other"})
            self.assertEqual(recovered.status_code,201,recovered.get_json())
        finally:appmod.RATE_LIMIT_CLOCK=old_clock

    def test_call_signalling_rejects_socket_without_session(self):
        socket_client = appmod.socketio.test_client(appmod.app)
        self.assertFalse(socket_client.is_connected())

    def test_session_bound_call_signalling_invite_accept_and_end(self):
        caller_http = appmod.app.test_client()
        callee_http = appmod.app.test_client()
        caller_user=self.register("Caller",caller_http)
        callee_user=self.register("Callee",callee_http)
        caller = appmod.socketio.test_client(appmod.app, flask_test_client=caller_http)
        callee = appmod.socketio.test_client(appmod.app, flask_test_client=callee_http)
        caller.emit("call:invite", {"to": callee_user["username"]})
        self.assertTrue(any(e["name"] == "call:invite" for e in callee.get_received()))
        callee.emit("call:accept", {"to": caller_user["username"]})
        self.assertTrue(any(e["name"] == "call:accepted" for e in caller.get_received()))
        caller.emit("call:signal", {"to": callee_user["username"], "kind": "offer", "data": {"type": "offer", "sdp": "test"}})
        self.assertTrue(any(e["name"] == "call:signal" for e in callee.get_received()))
        caller.emit("call:end", {"to": callee_user["username"]})
        self.assertTrue(any(e["name"] == "call:ended" for e in callee.get_received()))
        caller.disconnect()
        callee.disconnect()

    def test_invalid_input_is_rejected(self):
        self.assertEqual(self.sos(alert_type="not-real").status_code, 400)
        self.assertEqual(self.sos(latitude=100).status_code, 400)
        self.assertEqual(len(self.db_rows("SELECT * FROM alerts")), 0)

    def test_queueing_while_transport_is_disconnected(self):
        appmod.transport.configure(connected=False)
        self.sos()
        self.assertEqual(self.db_rows("SELECT state FROM outbound_queue")[0][0], "queued")
        self.assertEqual(self.db_rows("SELECT delivery_status FROM alerts")[0][0], "queued")

    def test_http_sos_returns_after_durable_queue_before_transport_worker(self):
        response = self.client.post("/api/sos", json={"incident_id": "INC-async-accept", "alert_type": "other"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["alert"]["delivery_status"], "queued")
        self.assertEqual(self.db_rows("SELECT state FROM outbound_queue WHERE incident_id='INC-async-accept'")[0][0], "queued")
        appmod.transport.process_queue(limit=1)
        self.assertEqual(self.db_rows("SELECT delivery_status FROM alerts WHERE incident_id='INC-async-accept'")[0][0], "received_by_command_centre")

    def test_outbox_selects_emergency_priority_before_routine(self):
        for incident, priority in (("INC-routine-first", "routine"), ("INC-critical-second", "critical")):
            response=self.client.post("/api/sos",json={"incident_id":incident,"alert_type":"other","priority":priority})
            self.assertEqual(response.status_code,201,response.get_json())
        appmod.transport.process_queue(limit=1)
        cc=sqlite3.connect(appmod.CC_DB_PATH)
        try:
            self.assertEqual(cc.execute("SELECT incident_id FROM command_centre_incidents").fetchall(),[("INC-critical-second",)])
        finally: cc.close()

    def test_delivery_after_transport_reconnection(self):
        appmod.transport.configure(connected=False)
        self.sos()
        appmod.transport.configure(connected=True)
        appmod.transport.process_queue()
        self.assertEqual(self.db_rows("SELECT state FROM outbound_queue")[0][0], "delivered")
        self.assertEqual(self.db_rows("SELECT delivery_status FROM alerts")[0][0], "received_by_command_centre")

    def test_incident_id_is_stable_across_retries(self):
        appmod.transport.configure(connected=False)
        first = self.sos("INC-stable-retry")
        second = self.sos("INC-stable-retry")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["alert"]["incident_id"], "INC-stable-retry")
        self.assertEqual(len(self.db_rows("SELECT * FROM alerts")), 1)

    def test_duplicate_delivery_is_suppressed(self):
        self.sos("INC-deduplicate")
        appmod.transport.process_queue()
        cc = __import__('sqlite3').connect(appmod.CC_DB_PATH)
        self.assertEqual(cc.execute("SELECT count(*) FROM command_centre_incidents WHERE incident_id=?", ("INC-deduplicate",)).fetchone()[0], 1)
        cc.close()
        self.assertEqual(len(self.db_rows("SELECT * FROM alerts WHERE incident_id=?", ("INC-deduplicate",))), 1)

    def test_no_false_receipt_before_command_centre_storage(self):
        appmod.transport.configure(lose_next_ack=True)
        response = self.sos("INC-lost-ack")
        self.assertEqual(response.get_json()["alert"]["delivery_status"], "queued")
        cc=__import__('sqlite3').connect(appmod.CC_DB_PATH)
        self.assertEqual(cc.execute("SELECT count(*) FROM command_centre_incidents WHERE incident_id=?", ("INC-lost-ack",)).fetchone()[0], 1); cc.close()
        self.assertEqual(self.db_rows("SELECT state FROM outbound_queue WHERE incident_id=?", ("INC-lost-ack",))[0][0], "queued")

    def test_retry_after_lost_acknowledgement(self):
        appmod.transport.configure(lose_next_ack=True)
        self.sos("INC-retry-ack")
        conn=sqlite3.connect(appmod.DB_PATH);conn.execute("UPDATE outbound_queue SET next_attempt_at='' WHERE incident_id='INC-retry-ack'");conn.commit();conn.close()
        appmod.transport.process_queue()
        self.assertEqual(self.db_rows("SELECT delivery_status FROM alerts WHERE incident_id=?", ("INC-retry-ack",))[0][0], "received_by_command_centre")
        cc=__import__('sqlite3').connect(appmod.CC_DB_PATH)
        self.assertEqual(cc.execute("SELECT count(*) FROM command_centre_incidents WHERE incident_id=?", ("INC-retry-ack",)).fetchone()[0], 1); cc.close()

    def test_queue_and_incident_survive_application_restart(self):
        appmod.transport.configure(connected=False)
        self.sos("INC-restart")
        # A fresh transport instance and init_db emulate a process restart.
        appmod.transport = appmod.SimulatedTransport()
        appmod.init_db()
        self.assertEqual(appmod.transport.connected,False)
        self.assertEqual(self.db_rows("SELECT state FROM outbound_queue WHERE incident_id=?", ("INC-restart",))[0][0],"queued")
        appmod.transport.configure(connected=True)
        appmod.transport.process_queue()
        self.assertEqual(self.db_rows("SELECT state FROM outbound_queue WHERE incident_id=?", ("INC-restart",))[0][0], "delivered")
        self.assertEqual(self.db_rows("SELECT delivery_status FROM alerts WHERE incident_id=?", ("INC-restart",))[0][0], "received_by_command_centre")

    def test_responder_assignment_is_distinct_from_receipt(self):
        self.sos("INC-assignment")
        before = self.db_rows("SELECT delivery_status FROM alerts WHERE incident_id=?", ("INC-assignment",))[0][0]
        self.assertEqual(before, "received_by_command_centre")
        self.assertEqual(self.client.post("/api/incidents/INC-assignment/assign",json={"responder":"Unit 7"}).status_code,403)
        response = self.operator_client.post("/api/incidents/INC-assignment/assign", json={"responder": "Unit 7"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db_rows("SELECT delivery_status,lifecycle_status FROM alerts WHERE incident_id=?", ("INC-assignment",))[0][:], ("received_by_command_centre","responder_assigned"))

    def test_anonymous_sos_without_location(self):
        response=self.client.post('/api/sos',json={'incident_id':'INC-anon','alert_type':'other'})
        self.assertEqual(response.status_code,201)
        self.assertIsNone(response.get_json()['alert']['latitude'])

    def test_partial_and_invalid_coordinates_rejected(self):
        self.assertEqual(self.sos('INC-partial',latitude=12.0,longitude=None).status_code,400)
        self.assertEqual(self.sos('INC-invalid',latitude='north',longitude=4).status_code,400)

    def test_idempotency_header_keeps_same_incident(self):
        one=self.client.post('/api/sos',json={'incident_id':'INC-key-one'},headers={'Idempotency-Key':'retry-key'})
        two=self.client.post('/api/sos',json={'incident_id':'INC-key-one'},headers={'Idempotency-Key':'retry-key'})
        self.assertEqual(one.get_json()['alert']['incident_id'],two.get_json()['alert']['incident_id'])
        self.assertEqual(len(self.db_rows('SELECT * FROM alerts')),1)

    def test_failed_transmission_keeps_queued_incident_and_retries(self):
        appmod.transport.configure(fail_next_transmission=True)
        response=self.sos('INC-failed-send')
        self.assertEqual(response.get_json()['alert']['delivery_status'],'queued')
        self.assertEqual(self.db_rows("SELECT outcome FROM delivery_attempts WHERE incident_id='INC-failed-send'")[0][0],'failed')
        conn=sqlite3.connect(appmod.DB_PATH);conn.execute("UPDATE outbound_queue SET next_attempt_at='2000-01-01 00:00:00' WHERE incident_id='INC-failed-send'");conn.commit();conn.close()
        appmod.transport.process_queue()
        self.assertEqual(self.db_rows("SELECT delivery_status FROM alerts WHERE incident_id='INC-failed-send'")[0][0],'received_by_command_centre')

    def test_followup_updates_same_incident(self):
        self.sos('INC-followup')
        response=self.client.post('/api/incidents/INC-followup/updates',json={'description':'two people trapped','landmark':'near bridge'})
        self.assertEqual(response.status_code,200)
        row=self.db_rows("SELECT description,landmark FROM alerts WHERE incident_id='INC-followup'")[0]
        self.assertEqual(tuple(row),('two people trapped','near bridge'))

    def test_lifecycle_requires_ordered_separate_events(self):
        self.sos('INC-lifecycle')
        for target in ['dispatched','arrived','resolved']:
            self.assertEqual(self.operator_client.post('/api/incidents/INC-lifecycle/lifecycle',json={'status':target}).status_code,409)
        self.operator_client.post('/api/incidents/INC-lifecycle/assign',json={'responder':'Unit 3'})
        for target in ['dispatched','arrived','resolved']:
            if target=='arrived':
                # Reassignment changes the unit but must not rewind lifecycle.
                self.operator_client.post('/api/incidents/INC-lifecycle/assign',json={'responder':'Unit 4'})
                self.assertEqual(self.db_rows("SELECT lifecycle_status FROM alerts WHERE incident_id='INC-lifecycle'")[0][0],'dispatched')
            response=self.operator_client.post('/api/incidents/INC-lifecycle/lifecycle',json={'status':target})
            self.assertEqual(response.status_code,200)
        self.assertEqual(self.db_rows("SELECT lifecycle_status FROM alerts WHERE incident_id='INC-lifecycle'")[0][0],'resolved')

    def test_dashboard_api_reflects_persisted_receipt_and_history(self):
        self.sos('INC-dashboard')
        response=self.operator_client.get('/api/incidents')
        self.assertEqual(response.status_code,200)
        incident=next(x for x in response.get_json() if x['incident_id']=='INC-dashboard')
        self.assertEqual(incident['delivery_status'],'received_by_command_centre')
        self.assertTrue(any(x['status']=='received_by_command_centre' for x in incident['history']))

    def test_genuine_receipt_id_is_returned_after_receiver_commit(self):
        response=self.sos('INC-receipt-proof')
        self.assertEqual(response.get_json()['alert']['receipt_id'],'RCP-INC-receipt-proof')
        cc=__import__('sqlite3').connect(appmod.CC_DB_PATH)
        saved=cc.execute("SELECT receipt_id FROM command_centre_receipts WHERE incident_id='INC-receipt-proof'").fetchone()[0]
        self.assertEqual(saved,response.get_json()['alert']['receipt_id'])
        cc.close()

    def test_repeated_acknowledgements_do_not_duplicate_history(self):
        self.sos('INC-repeat-ack')
        for _ in range(4): appmod.transport.process_queue()
        self.assertEqual(len(self.db_rows("SELECT * FROM incident_events WHERE incident_id='INC-repeat-ack' AND status='received_by_command_centre'")),1)

    def test_simulated_receiver_store_is_separate(self):
        self.sos('INC-separate-store')
        names=[r[0] for r in self.db_rows("SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertNotIn('command_centre_incidents',names)
        self.assertNotIn('command_centre_receipts',names)
        cc=__import__('sqlite3').connect(appmod.CC_DB_PATH)
        self.assertEqual(cc.execute("SELECT receipt_id FROM command_centre_receipts WHERE incident_id='INC-separate-store'").fetchone()[0],'RCP-INC-separate-store')
        cc.close()

    def test_repeated_retries_keep_one_stable_incident_and_history(self):
        appmod.transport.configure(connected=False)
        responses=[self.sos('INC-many-retries') for _ in range(8)]
        self.assertEqual(responses[-1].get_json()['alert']['incident_id'],'INC-many-retries')
        self.assertEqual(len(self.db_rows("SELECT * FROM alerts WHERE incident_id='INC-many-retries'")),1)
        self.assertEqual(len(self.db_rows("SELECT * FROM outbound_queue WHERE incident_id='INC-many-retries'")),1)

    def test_reinitialization_preserves_existing_records(self):
        self.sos('INC-preserve')
        before=len(self.db_rows('SELECT * FROM alerts'))
        appmod.transport.configure(connected=False)
        appmod.init_db()
        self.assertEqual(len(self.db_rows('SELECT * FROM alerts')),before)
        self.assertIsNotNone(self.db_rows("SELECT incident_id FROM alerts WHERE incident_id='INC-preserve'")[0][0])


if __name__ == "__main__":
    unittest.main()
