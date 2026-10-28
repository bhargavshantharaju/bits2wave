"""Hardware-independent transport adapter tests; fake links never access RF."""

import sqlite3
import unittest

from backend.radio_protocol import Frame, MessageType, encode
from backend.radio_transport import (RadioSosTransport, RadioTxResult, sos_frame,
                                     stable_message_id)


NODE_A = b"NODE0001"
NODE_B = b"NODE0002"


class FakeLink:
    def __init__(self):
        self.connected = True
        self.peer = None
        self.on_packet = None
        self.incoming = []
        self.sent = []
        self.drop_next = None

    def send(self, packet):
        if not self.connected:
            raise ConnectionError("fake device disconnected")
        self.sent.append(packet)
        if self.drop_next and self.drop_next(packet):
            self.drop_next = None
            return RadioTxResult(True, None)
        if self.peer is not None:
            if self.peer.on_packet:
                self.peer.on_packet(packet)
            else:
                self.peer.incoming.append(packet)
        return RadioTxResult(True, None)

    def receive(self, timeout_s):
        return self.incoming.pop(0) if self.incoming else None


class RadioTransportTests(unittest.TestCase):
    def setUp(self):
        self.a = FakeLink()
        self.b = FakeLink()
        self.a.peer, self.b.peer = self.b, self.a
        self.pending = [{"incident_id": "INC-radio-001", "alert_type": "medical",
                         "description": "help", "latitude": None, "longitude": None,
                         "landmark": "", "timestamp": "2026-10-28T00:00:00Z"}]
        self.attempts = []
        self.sender_receipts = []
        self.receiver_calls = []
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE incidents(incident_id TEXT PRIMARY KEY, receipt TEXT UNIQUE)")
        self.db.commit()
        self.sender = RadioSosTransport(
            self.a, NODE_A, NODE_B, lambda: list(self.pending),
            lambda report, event, detail: self.attempts.append((event, detail)),
            lambda report, receipt: self.sender_receipts.append((report["incident_id"], receipt)),
            lambda frame: self.persist(frame), lambda: True,
            max_attempts=3, ack_timeout_s=0.01,
            base_backoff_s=0, max_backoff_s=0)
        self.receiver = RadioSosTransport(
            self.b, NODE_B, NODE_A, lambda: [], lambda *args: None,
            lambda *args: None, lambda frame: self.persist(frame), lambda: True,
            ack_timeout_s=0.01)
        self.b.on_packet = self.receiver.handle_packet

    def tearDown(self):
        self.db.close()

    def persist(self, frame):
        self.receiver_calls.append(frame.message_id)
        receipt = "RCP-" + frame.incident_id
        self.db.execute("INSERT OR IGNORE INTO incidents VALUES(?,?)",
                        (frame.incident_id, receipt))
        self.db.commit()
        return self.db.execute("SELECT receipt FROM incidents WHERE incident_id=?",
                               (frame.incident_id,)).fetchone()[0]

    def test_frame_identity_and_payload_are_stable(self):
        first = sos_frame(self.pending[0], NODE_A, NODE_B)
        second = sos_frame(self.pending[0], NODE_A, NODE_B)
        self.assertEqual(first.message_id, second.message_id)
        self.assertEqual(first.message_id, stable_message_id("INC-radio-001"))
        self.assertEqual(encode(first), encode(second))

    def test_persistent_receipt_follows_receiver_commit(self):
        events = []
        original_send = self.b.send

        def check_commit(packet):
            response = __import__("backend.radio_protocol", fromlist=["decode"]).decode(packet)
            if response.message_type == MessageType.RECEIPT:
                events.append(self.db.execute("SELECT count(*) FROM incidents").fetchone()[0])
            return original_send(packet)

        self.b.send = check_commit
        self.assertEqual(self.sender.process_queue(), 1)
        self.assertEqual(events, [1])
        self.assertEqual(self.sender_receipts, [("INC-radio-001", "RCP-INC-radio-001")])
        self.assertIn("radio_tx_done", [event for event, _ in self.attempts])
        self.assertIn("application_ack", [event for event, _ in self.attempts])
        self.assertIn("command_centre_receipt", [event for event, _ in self.attempts])

    def test_lost_receipt_retries_same_frame_and_destination_deduplicates(self):
        from backend.radio_protocol import decode
        self.b.drop_next = lambda packet: decode(packet).message_type == MessageType.RECEIPT
        self.assertEqual(self.sender.process_queue(), 1)
        sent_sos = [packet for packet in self.a.sent
                    if decode(packet).message_type == MessageType.SOS]
        self.assertGreaterEqual(len(sent_sos), 2)
        self.assertEqual(len(set(sent_sos)), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM incidents").fetchone()[0], 1)
        self.assertEqual(len(self.sender_receipts), 1)

    def test_ack_without_persistent_receipt_never_marks_delivered(self):
        from backend.radio_protocol import decode
        self.b.drop_next = lambda packet: decode(packet).message_type == MessageType.RECEIPT
        # Drop every receipt for this run; the ACK is not enough to complete delivery.
        original_send = self.b.send

        def drop_receipts(packet):
            if decode(packet).message_type == MessageType.RECEIPT:
                return RadioTxResult(True, None)
            return original_send(packet)

        self.b.send = drop_receipts
        self.assertEqual(self.sender.process_queue(), 0)
        self.assertEqual(self.sender_receipts, [])
        self.assertIn("application_ack", [event for event, _ in self.attempts])
        self.assertEqual(self.db.execute("SELECT count(*) FROM incidents").fetchone()[0], 1)

    def test_unverified_tx_gate_and_disconnect_do_not_send(self):
        blocked = RadioSosTransport(self.a, NODE_A, NODE_B, lambda: self.pending,
                                    lambda *args: None, lambda *args: None,
                                    lambda frame: "RCP-x")
        self.assertEqual(blocked.process_queue(), 0)
        self.assertEqual(blocked.status()["state"], "transmit_blocked")
        self.assertEqual(self.a.sent, [])
        self.a.connected = False
        self.assertEqual(self.sender.process_queue(), 0)
        self.assertEqual(self.sender.status()["state"], "disconnected")

    def test_oversized_application_payload_remains_untransmitted(self):
        self.pending[0]["description"] = "x" * 200
        self.assertEqual(self.sender.process_queue(), 0)
        self.assertEqual(self.a.sent, [])
        self.assertEqual(self.sender.last_event, "invalid_outbox_record")
        self.assertTrue(any(event == "failed" for event, _ in self.attempts))

    def test_duplicate_after_receiver_restart_uses_durable_idempotent_store(self):
        packet = encode(sos_frame(self.pending[0], NODE_A, NODE_B))
        self.assertTrue(self.receiver.handle_packet(packet))
        self.receiver = RadioSosTransport(
            self.b, NODE_B, NODE_A, lambda: [], lambda *args: None,
            lambda *args: None, lambda frame: self.persist(frame), lambda: True)
        self.b.on_packet = self.receiver.handle_packet
        self.assertTrue(self.receiver.handle_packet(packet))
        self.assertEqual(self.db.execute("SELECT count(*) FROM incidents").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT receipt FROM incidents").fetchone()[0],
                         "RCP-INC-radio-001")

    def test_malformed_or_wrong_peer_frame_is_not_persisted(self):
        self.assertFalse(self.receiver.handle_packet(b"not a frame"))
        wrong_peer = Frame(MessageType.SOS, b"BADNODE1", NODE_B, b"M" * 16,
                           1, b"{}", "INC-wrong-peer")
        self.assertFalse(self.receiver.handle_packet(encode(wrong_peer)))
        self.assertEqual(self.receiver_calls, [])

    def test_local_receipt_cannot_be_promoted_to_command_centre_receipt(self):
        rejected = RadioSosTransport(
            self.b, NODE_B, NODE_A, lambda: [], lambda *args: None,
            lambda *args: None, lambda frame: "LOCAL-" + frame.incident_id,
            lambda: True)
        packet = encode(sos_frame(self.pending[0], NODE_A, NODE_B))
        self.assertFalse(rejected.handle_packet(packet))
        self.assertEqual(rejected.status()["state"], "ready")
        self.assertEqual(rejected.last_event, "persistence_failed")

    def test_wrong_incident_stale_or_uncorrelated_receipt_never_completes_outbox(self):
        from backend.radio_protocol import decode

        original = self.b.on_packet

        def stale_response(packet):
            request = decode(packet)
            if request.message_type != MessageType.SOS:
                return original(packet)
            # A different incident and a different ack_for are both rejected.
            wrong = Frame(MessageType.RECEIPT, NODE_B, NODE_A, b"R" * 16,
                          request.sequence, b"RCP-wrong", "INC-other", b"X" * 16)
            self.a.incoming.append(encode(wrong))

        self.b.on_packet = stale_response
        self.assertEqual(self.sender.process_queue(), 0)
        self.assertEqual(self.sender_receipts, [])
        self.assertIn("retry_pending", [event for event, _ in self.attempts])

    def test_store_callback_failures_leave_outbox_pending(self):
        self.sender.pending_reports = lambda: (_ for _ in ()).throw(OSError("outbox unavailable"))
        self.assertEqual(self.sender.process_queue(), 0)
        self.assertEqual(self.sender.last_event, "outbox_error")
        self.assertIn("outbox unavailable", self.sender.last_error)

        self.sender.pending_reports = lambda: list(self.pending)
        self.sender.record_receipt = lambda *args: (_ for _ in ()).throw(OSError("sender DB busy"))
        self.assertEqual(self.sender.process_queue(), 0)
        self.assertIn("sender receipt persistence failed", self.sender.last_error)
        self.assertEqual(self.sender_receipts, [])

    def test_link_exception_and_malformed_reply_are_diagnosable_and_bounded(self):
        self.a.send = lambda packet: (_ for _ in ()).throw(OSError("serial endpoint vanished"))
        self.assertEqual(self.sender.process_queue(), 0)
        self.assertEqual(self.sender.last_event, "send_failed")
        self.assertIn("serial endpoint vanished", self.sender.last_error)
        failed = [event for event, _ in self.attempts].count("failed")
        self.assertEqual(failed, self.sender.max_attempts)

        def malformed_send(packet):
            self.a.incoming.append(b"not-a-protocol-frame")
            return RadioTxResult(tx_done=True)

        self.a.send = malformed_send
        self.assertEqual(self.sender.process_queue(), 0)
        self.assertIn("malformed response", self.sender.last_error)
        self.assertEqual(self.sender_receipts, [])


if __name__ == "__main__":
    unittest.main()
