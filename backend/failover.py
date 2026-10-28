"""Hardware-independent failover state, heartbeat checks, and fallback outbox.

This module has no GPIO/radio dependencies. SQLite is the host simulation of a
portable backup store; target firmware will need an ESP32-S3 storage adapter.
"""

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from typing import Callable


class FailoverError(ValueError):
    pass


class NodeRole(str, Enum):
    PRIMARY = "primary"
    BACKUP = "backup"
    UNKNOWN = "unknown"


class FailoverState(str, Enum):
    PRIMARY_ACTIVE = "PRIMARY_ACTIVE"
    BACKUP_STANDBY = "BACKUP_STANDBY"
    FALLBACK_ACTIVATING = "FALLBACK_ACTIVATING"
    FALLBACK_ACTIVE = "FALLBACK_ACTIVE"
    PRIMARY_RECOVERING = "PRIMARY_RECOVERING"
    SYNCING_INCIDENTS = "SYNCING_INCIDENTS"
    DEGRADED = "DEGRADED"


ALLOWED_TRANSITIONS = {
    FailoverState.PRIMARY_ACTIVE: {FailoverState.BACKUP_STANDBY, FailoverState.DEGRADED},
    FailoverState.BACKUP_STANDBY: {FailoverState.FALLBACK_ACTIVATING, FailoverState.DEGRADED},
    FailoverState.FALLBACK_ACTIVATING: {FailoverState.FALLBACK_ACTIVE, FailoverState.DEGRADED},
    FailoverState.FALLBACK_ACTIVE: {FailoverState.PRIMARY_RECOVERING, FailoverState.DEGRADED},
    FailoverState.PRIMARY_RECOVERING: {FailoverState.SYNCING_INCIDENTS, FailoverState.FALLBACK_ACTIVE, FailoverState.DEGRADED},
    FailoverState.SYNCING_INCIDENTS: {FailoverState.BACKUP_STANDBY, FailoverState.DEGRADED},
    FailoverState.DEGRADED: {FailoverState.PRIMARY_RECOVERING, FailoverState.BACKUP_STANDBY},
}


@dataclass(frozen=True)
class Heartbeat:
    protocol_version: int
    node_id: str
    master_id: str
    sequence: int
    timestamp: float
    auth_tag: str

    def signing_bytes(self) -> bytes:
        fields = [self.protocol_version, self.node_id, self.master_id, self.sequence, self.timestamp]
        return json.dumps(fields, separators=(",", ":"), ensure_ascii=True).encode("ascii")

    @classmethod
    def signed(cls, node_id: str, master_id: str, sequence: int, timestamp: float,
               key: bytes, protocol_version: int = 1) -> "Heartbeat":
        unsigned = cls(protocol_version, node_id, master_id, sequence, timestamp, "")
        tag = hmac.new(key, unsigned.signing_bytes(), hashlib.sha256).hexdigest()
        return cls(protocol_version, node_id, master_id, sequence, timestamp, tag)

    @classmethod
    def from_value(cls, value) -> "Heartbeat":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise FailoverError("heartbeat must be an object")
        try:
            beat = cls(value["protocol_version"], value["node_id"], value["master_id"],
                       value["sequence"], value["timestamp"], value["auth_tag"])
        except (KeyError, TypeError) as exc:
            raise FailoverError("heartbeat is missing required fields") from exc
        return beat


class HeartbeatMonitor:
    """Validates authenticated monotonic heartbeats with injectable clocks."""

    def __init__(self, master_id: str, key: bytes | None, interval_s: float = 5.0,
                 timeout_s: float = 25.0, clock: Callable[[], float] | None = None,
                 wall_clock: Callable[[], float] | None = None, max_clock_skew_s: float = 120.0):
        if not master_id or not isinstance(master_id, str):
            raise FailoverError("master_id is required")
        if interval_s <= 0 or timeout_s <= interval_s or max_clock_skew_s < 0:
            raise FailoverError("heartbeat timeout must exceed a positive heartbeat interval")
        self.master_id = master_id
        self.key = key
        self.interval_s = float(interval_s)
        self.timeout_s = float(timeout_s)
        self.clock = clock or __import__("time").monotonic
        self.wall_clock = wall_clock or __import__("time").time
        self.max_clock_skew_s = float(max_clock_skew_s)
        self.last_sequence = -1
        self.last_received_at: float | None = None
        self.last_timestamp: float | None = None
        self.last_rejection: str | None = None

    def receive(self, value) -> bool:
        try:
            beat = Heartbeat.from_value(value)
            if beat.protocol_version != 1:
                raise FailoverError("unsupported heartbeat protocol version")
            if beat.node_id != self.master_id or beat.master_id != self.master_id:
                raise FailoverError("heartbeat master identity mismatch")
            if isinstance(beat.sequence, bool) or not isinstance(beat.sequence, int) or beat.sequence < 0:
                raise FailoverError("heartbeat sequence must be a non-negative integer")
            if beat.sequence <= self.last_sequence:
                raise FailoverError("repeated or out-of-order heartbeat sequence")
            if not isinstance(beat.timestamp, (int, float)) or not math.isfinite(beat.timestamp):
                raise FailoverError("heartbeat timestamp must be finite")
            age = self.wall_clock() - float(beat.timestamp)
            if age < -self.max_clock_skew_s:
                raise FailoverError("heartbeat timestamp is too far in the future")
            if age > self.timeout_s:
                raise FailoverError("stale heartbeat packet")
            if self.key is None:
                raise FailoverError("heartbeat authentication is not configured")
            expected = hmac.new(self.key, Heartbeat(beat.protocol_version, beat.node_id,
                beat.master_id, beat.sequence, float(beat.timestamp), "").signing_bytes(), hashlib.sha256).hexdigest()
            if not isinstance(beat.auth_tag, str) or not hmac.compare_digest(expected, beat.auth_tag):
                raise FailoverError("heartbeat authentication failed")
            self.last_sequence = beat.sequence
            self.last_timestamp = float(beat.timestamp)
            self.last_received_at = self.clock()
            self.last_rejection = None
            return True
        except FailoverError as exc:
            self.last_rejection = str(exc)
            return False

    def restore(self, sequence: int, received_at: float | None, timestamp: float | None) -> None:
        self.last_sequence = int(sequence)
        self.last_received_at = received_at
        self.last_timestamp = timestamp

    @property
    def fresh(self) -> bool:
        return self.last_received_at is not None and self.clock() - self.last_received_at < self.timeout_s

    @property
    def age_s(self) -> float | None:
        return None if self.last_received_at is None else max(0.0, self.clock() - self.last_received_at)


class FallbackStore:
    """Independent host SQLite simulation for backup-local SOS and failover journal."""

    ALERT_TYPES = {"medical", "fire", "flood", "earthquake", "accident", "violence",
                   "natural_disaster", "power_outage", "gas_leak", "missing_person",
                   "animal_attack", "other"}

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS fallback_incidents(
                    incident_id TEXT PRIMARY KEY, source_node_id TEXT NOT NULL,
                    alert_type TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    latitude REAL, longitude REAL, landmark TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, local_receipt_id TEXT NOT NULL UNIQUE,
                    citizen_user_id INTEGER);
                CREATE TABLE IF NOT EXISTS fallback_outbox(
                    incident_id TEXT PRIMARY KEY REFERENCES fallback_incidents(incident_id),
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at REAL, command_receipt_id TEXT);
                CREATE TABLE IF NOT EXISTS failover_runtime(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), state TEXT NOT NULL,
                    primary_running INTEGER NOT NULL, link_available INTEGER NOT NULL,
                    drop_heartbeats INTEGER NOT NULL, primary_ready INTEGER NOT NULL,
                    fallback_service_ready INTEGER NOT NULL, last_sequence INTEGER NOT NULL,
                    last_heartbeat_received_at REAL, last_heartbeat_timestamp REAL,
                    failover_started_at REAL, activation_started_at REAL,
                    reason TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS failover_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL, reason TEXT NOT NULL, changed_at REAL NOT NULL);
            """)
            columns={row[1] for row in db.execute("PRAGMA table_info(fallback_incidents)")}
            if "citizen_user_id" not in columns:
                db.execute("ALTER TABLE fallback_incidents ADD COLUMN citizen_user_id INTEGER")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @contextmanager
    def connection(self):
        db = self.connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def accept(self, report: dict, source_node_id: str, now: float,
               created_at: str | None = None) -> tuple[dict, bool]:
        if not isinstance(report, dict):
            raise FailoverError("SOS report must be an object")
        incident_id = report.get("incident_id")
        if not isinstance(incident_id, str) or not re.fullmatch(r"INC-[A-Za-z0-9._-]{1,75}", incident_id):
            raise FailoverError("invalid incident_id")
        alert_type = report.get("alert_type", "other")
        if alert_type not in self.ALERT_TYPES:
            raise FailoverError("invalid alert_type")
        description = report.get("description", "")
        landmark = report.get("landmark", "")
        if not isinstance(description, str) or len(description) > 1000:
            raise FailoverError("description must be at most 1000 characters")
        if not isinstance(landmark, str) or len(landmark) > 250:
            raise FailoverError("landmark must be at most 250 characters")
        lat, lon = report.get("latitude"), report.get("longitude")
        if (lat is None) != (lon is None):
            raise FailoverError("latitude and longitude must be provided together")
        if lat is not None:
            if isinstance(lat, bool) or isinstance(lon, bool):
                raise FailoverError("coordinates must be numeric")
            try:
                lat, lon = float(lat), float(lon)
            except (TypeError, ValueError) as exc:
                raise FailoverError("coordinates must be numeric") from exc
            if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
                raise FailoverError("coordinates are outside valid ranges")
        if not isinstance(source_node_id, str) or not source_node_id or len(source_node_id) > 80:
            raise FailoverError("source_node_id is required")
        citizen_user_id=report.get("citizen_user_id")
        if citizen_user_id is not None and (isinstance(citizen_user_id,bool) or not isinstance(citizen_user_id,int) or citizen_user_id<1):
            raise FailoverError("citizen identity is invalid")
        created_at = created_at or __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        local_receipt = "LOCAL-" + incident_id
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            before = db.execute("SELECT * FROM fallback_incidents WHERE incident_id=?", (incident_id,)).fetchone()
            if before:
                if before["source_node_id"] != source_node_id:
                    raise FailoverError("incident_id already belongs to another source node")
                if before["citizen_user_id"] != citizen_user_id:
                    raise FailoverError("incident_id already belongs to another citizen")
                db.commit()
                status = db.execute("SELECT state,command_receipt_id FROM fallback_outbox WHERE incident_id=?", (incident_id,)).fetchone()
                return self._local_response(before, status["state"], status["command_receipt_id"]), False
            db.execute("""INSERT INTO fallback_incidents
                (incident_id,source_node_id,alert_type,description,latitude,longitude,landmark,created_at,local_receipt_id,citizen_user_id)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (incident_id, source_node_id, alert_type, description.strip(), lat, lon,
                 landmark.strip(), created_at, local_receipt, citizen_user_id))
            db.execute("INSERT INTO fallback_outbox(incident_id,state,last_attempt_at) VALUES(?,'pending',NULL)", (incident_id,))
            row = db.execute("SELECT * FROM fallback_incidents WHERE incident_id=?", (incident_id,)).fetchone()
            db.commit()
            return self._local_response(row, "pending"), True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _local_response(row, state, command_receipt_id=None):
        return {"incident_id": row["incident_id"], "source_node_id": row["source_node_id"],
                "alert_type": row["alert_type"], "description": row["description"],
                "latitude": row["latitude"], "longitude": row["longitude"],
                "landmark": row["landmark"], "created_at": row["created_at"],
                "local_receipt_id": row["local_receipt_id"], "outbox_state": state,
                "command_receipt_id": command_receipt_id}

    def pending(self):
        with self.connection() as db:
            rows = db.execute("SELECT i.*,o.attempts FROM fallback_incidents i JOIN fallback_outbox o USING(incident_id) WHERE o.state='pending' ORDER BY i.created_at,i.incident_id").fetchall()
            return [dict(row) for row in rows]

    def incidents_for_user(self, citizen_user_id: int):
        with self.connection() as db:
            rows=db.execute("""SELECT i.*,o.state,o.command_receipt_id FROM fallback_incidents i
                JOIN fallback_outbox o USING(incident_id) WHERE i.citizen_user_id=?
                ORDER BY i.created_at DESC""",(citizen_user_id,)).fetchall()
            return [{"incident_id":r["incident_id"],"source_node_id":r["source_node_id"],
                     "alert_type":r["alert_type"],"description":r["description"],
                     "latitude":r["latitude"],"longitude":r["longitude"],"landmark":r["landmark"],
                     "created_at":r["created_at"],"delivery_status":"received_by_command_centre" if r["state"]=="received" else "queued_on_backup",
                     "receipt_id":r["command_receipt_id"]} for r in rows]

    def pending_count(self) -> int:
        with self.connection() as db:
            return int(db.execute("SELECT count(*) FROM fallback_outbox WHERE state='pending'").fetchone()[0])

    def received_count(self) -> int:
        with self.connection() as db:
            return int(db.execute("SELECT count(*) FROM fallback_outbox WHERE state='received'").fetchone()[0])

    def record_attempt(self, incident_id: str, now: float):
        with self.connection() as db:
            db.execute("UPDATE fallback_outbox SET attempts=attempts+1,last_attempt_at=? WHERE incident_id=? AND state='pending'", (now, incident_id))

    def mark_received(self, incident_id: str, receipt_id: str):
        if not receipt_id or receipt_id.startswith("LOCAL-"):
            raise FailoverError("a persistent command-centre receipt is required")
        with self.connection() as db:
            db.execute("UPDATE fallback_outbox SET state='received',command_receipt_id=? WHERE incident_id=?", (receipt_id, incident_id))

    def get_runtime(self):
        with self.connection() as db:
            row = db.execute("SELECT * FROM failover_runtime WHERE singleton=1").fetchone()
            return dict(row) if row else None

    def save_runtime(self, values: dict, transition: tuple[str, str, str] | None = None):
        with self.connection() as db:
            db.execute("""INSERT INTO failover_runtime(singleton,state,primary_running,link_available,
                drop_heartbeats,primary_ready,fallback_service_ready,last_sequence,
                last_heartbeat_received_at,last_heartbeat_timestamp,failover_started_at,
                activation_started_at,reason,updated_at) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(singleton) DO UPDATE SET state=excluded.state,
                primary_running=excluded.primary_running,link_available=excluded.link_available,
                drop_heartbeats=excluded.drop_heartbeats,primary_ready=excluded.primary_ready,
                fallback_service_ready=excluded.fallback_service_ready,last_sequence=excluded.last_sequence,
                last_heartbeat_received_at=excluded.last_heartbeat_received_at,
                last_heartbeat_timestamp=excluded.last_heartbeat_timestamp,
                failover_started_at=excluded.failover_started_at,
                activation_started_at=excluded.activation_started_at,reason=excluded.reason,
                updated_at=excluded.updated_at""", (
                values["state"], int(values["primary_running"]), int(values["link_available"]),
                int(values["drop_heartbeats"]), int(values["primary_ready"]),
                int(values["fallback_service_ready"]), values["last_sequence"],
                values["last_heartbeat_received_at"], values["last_heartbeat_timestamp"],
                values["failover_started_at"], values["activation_started_at"],
                values["reason"], values["updated_at"]))
            if transition:
                db.execute("INSERT INTO failover_events(from_state,to_state,reason,changed_at) VALUES(?,?,?,?)",
                           (*transition, values["updated_at"]))

    def recent_events(self, limit=50):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM failover_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


@dataclass(frozen=True)
class ForwardResult:
    persisted: bool
    command_receipt_id: str | None = None
    detail: str = ""


class FallbackSynchronizer:
    """Forwards durable fallback rows; only a destination receipt clears an outbox row."""

    def __init__(self, store: FallbackStore, forwarder: Callable[[dict], ForwardResult], clock: Callable[[], float]):
        self.store, self.forwarder, self.clock = store, forwarder, clock

    def sync_once(self) -> dict:
        sent = received = failed = 0
        details = []
        for report in self.store.pending():
            sent += 1
            self.store.record_attempt(report["incident_id"], self.clock())
            try:
                result = self.forwarder(report)
            except Exception as exc:
                failed += 1
                details.append({"incident_id": report["incident_id"], "state": "pending", "error": str(exc)})
                continue
            if result.persisted and result.command_receipt_id and not result.command_receipt_id.startswith("LOCAL-"):
                self.store.mark_received(report["incident_id"], result.command_receipt_id)
                received += 1
                details.append({"incident_id": report["incident_id"], "state": "received", "receipt_id": result.command_receipt_id})
            else:
                failed += 1
                details.append({"incident_id": report["incident_id"], "state": "pending", "error": result.detail or "command-centre receipt not returned"})
        return {"attempted": sent, "received": received, "pending": self.store.pending_count(), "failed": failed, "details": details}


class FailoverController:
    """Persistent state machine. A backup promotes only with explicit power-off proof."""

    def __init__(self, store: FallbackStore, master_id: str, heartbeat_key: bytes | None,
                 interval_s: float = 5.0, timeout_s: float = 25.0, activation_s: float = 2.0,
                 target_s: float = 90.0, clock: Callable[[], float] | None = None,
                 wall_clock: Callable[[], float] | None = None, simulate_primary: bool = True):
        if activation_s < 0 or timeout_s + activation_s >= target_s:
            raise FailoverError("heartbeat timeout plus activation must remain below failover target")
        self.store = store
        self.clock = clock or __import__("time").monotonic
        self.wall_clock = wall_clock or __import__("time").time
        self.interval_s, self.timeout_s = float(interval_s), float(timeout_s)
        self.activation_s, self.target_s = float(activation_s), float(target_s)
        self.master_id, self.heartbeat_key = master_id, heartbeat_key
        self.simulate_primary = simulate_primary
        self.monitor = HeartbeatMonitor(master_id, heartbeat_key, interval_s, timeout_s,
                                        self.clock, self.wall_clock)
        saved = store.get_runtime()
        if saved:
            self.state = FailoverState(saved["state"])
            self.primary_running = bool(saved["primary_running"])
            self.link_available = bool(saved["link_available"])
            self.drop_heartbeats = bool(saved["drop_heartbeats"])
            self.primary_ready = bool(saved["primary_ready"])
            self.fallback_service_ready = bool(saved["fallback_service_ready"])
            self.failover_started_at = saved["failover_started_at"]
            self.activation_started_at = saved["activation_started_at"]
            self.reason = saved["reason"]
            self.next_sequence = int(saved["last_sequence"]) + 1
            # Monotonic values are process/boot-relative. Never treat a value
            # persisted by a previous process as fresh in this process.
            self.monitor.restore(saved["last_sequence"], None, saved["last_heartbeat_timestamp"])
            if self.state in {FailoverState.BACKUP_STANDBY, FailoverState.PRIMARY_RECOVERING,
                              FailoverState.SYNCING_INCIDENTS, FailoverState.FALLBACK_ACTIVATING}:
                previous = self.state
                self.state = FailoverState.DEGRADED
                self.fallback_service_ready = False if previous == FailoverState.FALLBACK_ACTIVATING else self.fallback_service_ready
                self.reason = "Backup restarted; prior-process heartbeat freshness is unknown"
                self._persist((previous.value, self.state.value, self.reason))
        else:
            self.state = FailoverState.PRIMARY_ACTIVE
            self.primary_running = True
            self.link_available = True
            self.drop_heartbeats = False
            self.primary_ready = True
            self.fallback_service_ready = False
            self.failover_started_at = None
            self.activation_started_at = None
            self.reason = "Initial simulated primary active"
            self.next_sequence = 0
            self._persist()
            self.transition(FailoverState.BACKUP_STANDBY, "Backup heartbeat monitoring started")
            if simulate_primary:
                self.tick()

    @property
    def node_role(self) -> NodeRole:
        # This controller models the backup endpoint. A degraded state does
        # not change its configured node role into an unknown/primary role.
        return NodeRole.BACKUP

    def transition(self, target: FailoverState, reason: str):
        target = FailoverState(target)
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise FailoverError(f"invalid transition {self.state.value} -> {target.value}")
        old = self.state
        self.state = target
        self.reason = reason
        now = self.clock()
        if target == FailoverState.FALLBACK_ACTIVATING:
            self.activation_started_at = now
            self.failover_started_at = self.monitor.last_received_at
        elif target == FailoverState.FALLBACK_ACTIVE:
            self.fallback_service_ready = True
        elif target == FailoverState.BACKUP_STANDBY:
            self.fallback_service_ready = False
            self.failover_started_at = None
            self.activation_started_at = None
        self._persist((old.value, target.value, reason))

    def _persist(self, transition=None):
        self.store.save_runtime({
            "state": self.state.value, "primary_running": self.primary_running,
            "link_available": self.link_available, "drop_heartbeats": self.drop_heartbeats,
            "primary_ready": self.primary_ready, "fallback_service_ready": self.fallback_service_ready,
            "last_sequence": self.monitor.last_sequence, "last_heartbeat_received_at": self.monitor.last_received_at,
            "last_heartbeat_timestamp": self.monitor.last_timestamp,
            "failover_started_at": self.failover_started_at,
            "activation_started_at": self.activation_started_at, "reason": self.reason,
            "updated_at": self.clock()}, transition)

    def receive(self, value) -> bool:
        accepted = self.monitor.receive(value)
        if not accepted:
            return False
        if self.state in {FailoverState.FALLBACK_ACTIVE, FailoverState.DEGRADED}:
            self.transition(FailoverState.PRIMARY_RECOVERING, "Fresh authenticated primary heartbeat received")
        else:
            self._persist()
        return True

    def set_simulation(self, *, primary_running=None, link_available=None,
                       drop_heartbeats=None, primary_ready=None):
        if not self.simulate_primary:
            raise FailoverError("simulation controls are disabled")
        if primary_running is not None: self.primary_running = bool(primary_running)
        if link_available is not None: self.link_available = bool(link_available)
        if drop_heartbeats is not None: self.drop_heartbeats = bool(drop_heartbeats)
        if primary_ready is not None: self.primary_ready = bool(primary_ready)
        self._persist()

    def _send_simulated_heartbeat_if_due(self):
        if not (self.simulate_primary and self.primary_running and self.link_available and not self.drop_heartbeats and self.heartbeat_key):
            return
        last = self.monitor.last_received_at
        if last is None or self.clock() - last >= self.interval_s:
            beat = Heartbeat.signed(self.master_id, self.master_id, self.next_sequence,
                                    self.wall_clock(), self.heartbeat_key)
            self.next_sequence += 1
            self.receive(beat)

    def tick(self):
        self._send_simulated_heartbeat_if_due()
        now = self.clock()
        if self.state == FailoverState.BACKUP_STANDBY and not self.monitor.fresh and self.monitor.age_s is not None and self.monitor.age_s >= self.timeout_s:
            if self.simulate_primary and not self.primary_running:
                self.transition(FailoverState.FALLBACK_ACTIVATING, "Primary process power-off confirmed by simulator")
            else:
                self.transition(FailoverState.DEGRADED, "Heartbeat timeout is ambiguous; backup promotion withheld")
        elif self.state == FailoverState.FALLBACK_ACTIVATING and self.activation_started_at is not None and now - self.activation_started_at >= self.activation_s:
            self.transition(FailoverState.FALLBACK_ACTIVE, "Fallback emergency service passed simulated readiness check")
        elif self.state == FailoverState.PRIMARY_RECOVERING:
            if not self.monitor.fresh:
                self.transition(FailoverState.DEGRADED, "Primary heartbeat became stale during recovery")
            elif self.primary_ready:
                self.transition(FailoverState.SYNCING_INCIDENTS, "Primary readiness check passed; reconciliation started")
        elif self.state == FailoverState.SYNCING_INCIDENTS and not self.monitor.fresh:
            self.transition(FailoverState.DEGRADED, "Primary link lost during incident reconciliation")
        else:
            self._persist()
        return self.snapshot()

    def sync_complete(self):
        if self.state != FailoverState.SYNCING_INCIDENTS:
            raise FailoverError("incident sync can complete only in SYNCING_INCIDENTS")
        pending = self.store.pending_count()
        if pending:
            raise FailoverError(f"{pending} fallback incident(s) remain pending")
        self.transition(FailoverState.BACKUP_STANDBY, "All fallback incidents have persistent command-centre receipts")

    def sync_failed(self, reason: str):
        if self.state == FailoverState.SYNCING_INCIDENTS:
            self.transition(FailoverState.DEGRADED, reason)

    def snapshot(self) -> dict:
        now = self.clock()
        failover_elapsed = None if self.failover_started_at is None else max(0.0, now - self.failover_started_at)
        return {"node_role": self.node_role.value, "state": self.state.value,
                "master_id": self.master_id, "heartbeat_authenticated": bool(self.heartbeat_key),
                "heartbeat_interval_s": self.interval_s, "heartbeat_timeout_s": self.timeout_s,
                "failover_target_s": self.target_s, "last_heartbeat_sequence": self.monitor.last_sequence,
                "last_heartbeat_timestamp": self.monitor.last_timestamp,
                "heartbeat_age_s": self.monitor.age_s, "heartbeat_fresh": self.monitor.fresh,
                "fallback_service_ready": self.fallback_service_ready,
                "pending_fallback_incidents": self.store.pending_count(),
                "received_fallback_incidents": self.store.received_count(),
                "failover_started_at": self.failover_started_at,
                "failover_elapsed_s": failover_elapsed, "reason": self.reason,
                "primary_running_simulated": self.primary_running,
                "link_available_simulated": self.link_available,
                "recent_events": self.store.recent_events(20)}
