"""Hardware-independent acknowledged radio adapter boundary.

This module has no GPIO, SPI, serial-driver or RF configuration. A future driver
must implement RadioLink and provide durable database callbacks. TX is disabled
unless the caller supplies an explicit authorization predicate.
"""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
from itertools import islice
import json
import time
from typing import Callable, Iterable, Protocol

from backend.radio_protocol import Frame, FrameError, MessageType, decode, encode
from backend.transport import SosTransport


class LinkDisconnected(ConnectionError):
    """The underlying serial/device link is unavailable."""


@dataclass(frozen=True)
class RadioTxResult:
    """Driver-level result; neither field is a command-centre receipt."""

    tx_done: bool
    hop_ack: bool | None = None


class RadioLink(Protocol):
    connected: bool

    def send(self, packet: bytes) -> RadioTxResult: ...

    def receive(self, timeout_s: float) -> bytes | None: ...


def stable_message_id(incident_id: str) -> bytes:
    """Derive a retry-stable 128-bit ID from the unchanged incident ID."""
    if not isinstance(incident_id, str) or not incident_id:
        raise ValueError("incident_id is required")
    return hashlib.sha256(b"NexAlert-radio-v1\0" + incident_id.encode("utf-8")).digest()[:16]


def sos_frame(report: dict, source: bytes, destination: bytes) -> Frame:
    """Build the same compact payload on every retry; fail closed at codec limits."""
    incident_id = report.get("incident_id")
    message_id = stable_message_id(incident_id)
    payload = {
        "c": report.get("alert_type", "other"),
        "d": report.get("description", ""),
    }
    for key, short in (("latitude", "a"), ("longitude", "o"),
                       ("landmark", "l"), ("timestamp", "t")):
        if report.get(key) is not None and report.get(key) != "":
            payload[short] = report[key]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    sequence = int.from_bytes(message_id[:4], "big")
    return Frame(MessageType.SOS, source, destination, message_id, sequence,
                 encoded, incident_id)


class RadioSosTransport(SosTransport):
    """Retry-safe adapter over an injected radio/serial link and durable stores.

    `pending_reports` must read the existing durable outbox. `persist_received`
    must commit the destination incident and receipt in one durable transaction
    before returning its receipt ID. `record_receipt` updates the sender only
    after a validated RECEIPT frame. These callbacks are intentionally injected;
    this module does not open or migrate any SQLite database.
    """

    def __init__(self, link: RadioLink, node_id: bytes, peer_id: bytes,
                 pending_reports: Callable[[], Iterable[dict]],
                 record_attempt: Callable[[dict, str, str], None],
                 record_receipt: Callable[[dict, str], None],
                 persist_received: Callable[[Frame], str],
                 tx_permitted: Callable[[], bool] | None = None,
                 max_attempts: int = 3, ack_timeout_s: float = 0.25,
                 base_backoff_s: float = 0.05, max_backoff_s: float = 0.5,
                 max_batch: int = 8, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 duplicate_cache_size: int = 256):
        if len(node_id) != 8 or len(peer_id) != 8:
            raise ValueError("node and peer IDs must be exactly 8 bytes")
        if not 1 <= max_attempts <= 8 or not 1 <= max_batch <= 64:
            raise ValueError("retry and batch limits are outside safe bounds")
        if ack_timeout_s <= 0 or base_backoff_s < 0 or max_backoff_s < 0:
            raise ValueError("timeouts must be positive and backoff non-negative")
        self.link = link
        self.node_id = node_id
        self.peer_id = peer_id
        self.pending_reports = pending_reports
        self.record_attempt = record_attempt
        self.record_receipt = record_receipt
        self.persist_received = persist_received
        self.tx_permitted = tx_permitted or (lambda: False)
        self.max_attempts = max_attempts
        self.ack_timeout_s = ack_timeout_s
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self.max_batch = max_batch
        self.monotonic = monotonic
        self.sleep = sleep
        self.duplicate_cache_size = max(1, duplicate_cache_size)
        self._receipt_cache: OrderedDict[bytes, str] = OrderedDict()
        self.last_error = ""
        self.last_event = "idle"

    def status(self):
        connected = bool(getattr(self.link, "connected", False))
        permitted = bool(self.tx_permitted())
        return {"kind": "physical_radio_adapter", "connected": connected,
                "radio_verified": False, "transmission_permitted": permitted,
                "state": "ready" if connected and permitted else
                         "transmit_blocked" if connected else "disconnected",
                "last_event": self.last_event, "last_error": self.last_error}

    @property
    def connected(self) -> bool:
        """Compatibility property for the existing application status surface."""
        return bool(getattr(self.link, "connected", False))

    def _transmit(self, packet: bytes) -> RadioTxResult:
        if not bool(self.tx_permitted()):
            raise PermissionError("radio transmission is not authorized by the hardware gate")
        if not bool(getattr(self.link, "connected", False)):
            raise LinkDisconnected("radio device/serial link disconnected")
        result = self.link.send(packet)
        if not isinstance(result, RadioTxResult) or not result.tx_done:
            raise IOError("radio driver did not confirm TX completion")
        return result

    def _record_attempt(self, report: dict, event: str, detail: str) -> None:
        try:
            self.record_attempt(report, event, detail)
        except Exception as exc:
            # Diagnostics must not turn a durable queued report into an HTTP 500.
            self.last_error = f"attempt journal callback failed: {exc}"

    def process_queue(self, limit: int | None = None) -> int:
        """Send pending records; return count with persistent destination receipts."""
        self.last_error = ""
        if not bool(getattr(self.link, "connected", False)):
            self.last_event = "disconnected"
            return 0
        if not bool(self.tx_permitted()):
            self.last_event = "transmit_blocked"
            return 0
        delivered = 0
        try:
            queued = self.pending_reports()
        except Exception as exc:
            self.last_error = f"outbox read failed: {exc}"
            self.last_event = "outbox_error"
            return 0
        batch_limit = self.max_batch if limit is None else max(1, min(int(limit), self.max_batch))
        for report in islice(queued, batch_limit):
            try:
                frame = sos_frame(report, self.node_id, self.peer_id)
                packet = encode(frame)  # Keep these exact bytes and IDs on every retry.
            except (FrameError, TypeError, ValueError) as exc:
                self.last_error = f"outbox report cannot be framed: {exc}"
                self.last_event = "invalid_outbox_record"
                self._record_attempt(report, "failed", self.last_error)
                continue
            complete = False
            for attempt in range(self.max_attempts):
                try:
                    tx = self._transmit(packet)
                    self._record_attempt(report, "radio_tx_done",
                                         f"attempt={attempt + 1}; hop_ack={tx.hop_ack}")
                    self.last_event = "radio_tx_done"
                    if tx.hop_ack is not None:
                        self._record_attempt(report, "hop_ack",
                                             "acknowledged" if tx.hop_ack else "not acknowledged")
                    deadline = self.monotonic() + self.ack_timeout_s
                    while self.monotonic() < deadline:
                        remaining = max(0.0, deadline - self.monotonic())
                        incoming = self.link.receive(remaining)
                        if incoming is None:
                            break
                        try:
                            response = decode(incoming)
                        except FrameError:
                            self.last_error = "discarded malformed response frame"
                            continue
                        if response.message_type == MessageType.SOS:
                            self.handle_packet(incoming)
                            continue
                        if (response.source != self.peer_id or response.destination != self.node_id or
                                response.ack_for != frame.message_id):
                            continue
                        if response.message_type == MessageType.ACK:
                            self._record_attempt(report, "application_ack",
                                                 "correlated ACK received; awaiting persistent receipt")
                            self.last_event = "application_ack"
                            continue
                        if response.message_type != MessageType.RECEIPT:
                            continue
                        if response.incident_id != frame.incident_id:
                            continue
                        try:
                            receipt_id = response.payload.decode("utf-8").strip()
                        except UnicodeDecodeError:
                            continue
                        if not receipt_id or receipt_id.startswith("LOCAL-"):
                            continue
                        try:
                            self.record_receipt(report, receipt_id)
                        except Exception as exc:
                            self.last_error = f"sender receipt persistence failed: {exc}"
                            continue
                        self._record_attempt(report, "command_centre_receipt", receipt_id)
                        self.last_event = "command_centre_receipt"
                        delivered += 1
                        complete = True
                        break
                except Exception as exc:
                    self.last_error = str(exc)
                    self.last_event = "disconnected" if isinstance(exc, LinkDisconnected) else "send_failed"
                    self._record_attempt(report, "failed", str(exc))
                if complete:
                    break
                self._record_attempt(report, "retry_pending", f"attempt={attempt + 1} did not yield persistent receipt")
                if attempt + 1 < self.max_attempts:
                    delay = min(self.base_backoff_s * (2 ** attempt), self.max_backoff_s)
                    if delay:
                        self.sleep(delay)
            if not complete and not self.last_error:
                self.last_event = "receipt_pending"
        return delivered

    def handle_packet(self, packet: bytes) -> bool:
        """Persist incoming SOS before returning correlated ACK and receipt."""
        try:
            frame = decode(packet)
        except FrameError as exc:
            self.last_error = str(exc)
            self.last_event = "malformed_frame"
            return False
        if (frame.message_type != MessageType.SOS or frame.destination != self.node_id or
                frame.source != self.peer_id):
            return False
        receipt_id = self._receipt_cache.get(frame.message_id)
        if receipt_id is None:
            try:
                receipt_id = self.persist_received(frame)
            except Exception as exc:
                self.last_error = f"destination persistence failed: {exc}"
                self.last_event = "persistence_failed"
                return False
            if not isinstance(receipt_id, str) or not receipt_id.strip() or receipt_id.startswith("LOCAL-"):
                self.last_error = "destination callback did not return a persistent command-centre receipt"
                self.last_event = "persistence_failed"
                return False
            self._receipt_cache[frame.message_id] = receipt_id
            self._receipt_cache.move_to_end(frame.message_id)
            while len(self._receipt_cache) > self.duplicate_cache_size:
                self._receipt_cache.popitem(last=False)
        self.last_event = "destination_persisted"
        ack = self._control_frame(frame, MessageType.ACK, b"")
        receipt = self._control_frame(frame, MessageType.RECEIPT, receipt_id.encode("utf-8"))
        try:
            self._transmit(encode(ack))
            self._transmit(encode(receipt))
        except Exception as exc:
            self.last_error = str(exc)
            self.last_event = "receipt_response_pending"
            return False
        self.last_event = "receipt_sent_after_persistence"
        return True

    def _control_frame(self, original: Frame, message_type: MessageType,
                       payload: bytes) -> Frame:
        prefix = b"ACK" if message_type == MessageType.ACK else b"RECEIPT"
        control_id = hashlib.sha256(prefix + original.message_id).digest()[:16]
        return Frame(message_type, self.node_id, original.source, control_id,
                     original.sequence, payload, original.incident_id,
                     original.message_id)
