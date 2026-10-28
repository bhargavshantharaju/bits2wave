"""
NexAlert v3.0 - Fixed Backend
Fixes:
  1. SocketIO async_mode='threading' (eventlet breaks on Pi without sudo tricks)
  2. DB init uses check_same_thread=False + WAL mode + connection pooling via g
  3. All routes return proper JSON errors (no bare 500s)
  4. /api/sos validates lat/lon, stores as REAL not TEXT
  5. /api/contacts deduplicates before insert
  6. /api/messages handles missing recipient gracefully
  7. Socket auth uses session cookie, not raw username string
  8. Environmental data endpoint returns 404 gracefully when no rows
  9. CORS restricted to the hotspot subnet (not *)
 10. DB schema uses IF NOT EXISTS everywhere
 11. Broadcast groups: missing member check before insert
 12. deploy.sh: wlan1 check before activating hotspot
"""

import os
import sqlite3
import time
import logging
import re
import uuid
from datetime import datetime
from time import monotonic
from flask import Flask, request, jsonify, render_template, g, session, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS
try:
    from backend.migrations import migrate_sos_schema
    from backend.failover import FailoverController, FailoverError, FailoverState, FallbackStore, FallbackSynchronizer, ForwardResult, Heartbeat
    from backend.transport import SosTransport
except ModuleNotFoundError:  # Running as `python backend/app.py`
    from migrations import migrate_sos_schema
    from failover import FailoverController, FailoverError, FailoverState, FallbackStore, FallbackSynchronizer, ForwardResult, Heartbeat
    from transport import SosTransport

# ── App setup ─────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.environ.get("NEXALERT_DB", os.path.join(BASE_DIR, "..", "database", "nexalert.db"))
CC_DB_PATH = os.environ.get("NEXALERT_CC_DB", os.path.join(BASE_DIR, "..", "database", "command-centre.db"))
FALLBACK_DB_PATH = os.environ.get("NEXALERT_FALLBACK_DB", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "nexalert-fallback.db"))
LOG_DIR  = os.path.join(BASE_DIR, "..", "logs")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
os.makedirs(os.path.dirname(os.path.abspath(CC_DB_PATH)), exist_ok=True)
if os.path.realpath(DB_PATH) == os.path.realpath(CC_DB_PATH):
    raise RuntimeError("NEXALERT_DB and NEXALERT_CC_DB must be separate SQLite files")

# ── Logging (use BASE_DIR-relative path, not hardcoded /home/pi) ──────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "nexalert.log")),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__,
            template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))
APP_STARTED_MONOTONIC = monotonic()
app.secret_key = os.environ.get("NEXALERT_SECRET", "nexalert-change-me-in-prod")
INTERNAL_FORWARD_TOKEN = uuid.uuid4().hex

# BUG FIX #9: Only allow hotspot subnet + localhost; not "*"
CORS(app, resources={r"/api/*": {"origins": ["http://10.42.0.*", "http://localhost"]}},
     supports_credentials=True)

# BUG FIX #1: Use threading mode — eventlet requires monkey-patching at module
# top-level which breaks Pi GPIO libs and conflicts with BalenaOS containers.
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*",
                    logger=False, engineio_logger=False)

# ── Alert type registry ───────────────────────────────────────────────────────
ALERT_TYPES = {
    "medical":          {"color": "#e74c3c", "icon": "🏥", "label": "Medical Emergency"},
    "fire":             {"color": "#e67e22", "icon": "🔥", "label": "Fire"},
    "flood":            {"color": "#3498db", "icon": "🌊", "label": "Flood"},
    "earthquake":       {"color": "#795548", "icon": "🌍", "label": "Earthquake"},
    "accident":         {"color": "#f39c12", "icon": "🚗", "label": "Accident"},
    "violence":         {"color": "#9b59b6", "icon": "⚠️", "label": "Violence"},
    "natural_disaster": {"color": "#1abc9c", "icon": "🌪️", "label": "Natural Disaster"},
    "power_outage":     {"color": "#95a5a6", "icon": "⚡", "label": "Power Outage"},
    "gas_leak":         {"color": "#f1c40f", "icon": "💨", "label": "Gas Leak"},
    "missing_person":   {"color": "#e91e63", "icon": "👤", "label": "Missing Person"},
    "animal_attack":    {"color": "#4caf50", "icon": "🐾", "label": "Animal Attack"},
    "other":            {"color": "#607d8b", "icon": "❗", "label": "Other"},
}

DELIVERY_STATUSES = {
    "saved_locally", "queued", "relayed", "received_by_command_centre"
}


def new_incident_id():
    """Return a public, retry-safe identifier that is independent of SQLite row IDs."""
    return f"INC-{uuid.uuid4()}"


def open_db_connection():
    """Open a short-lived connection for background/simulated transport work."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def record_incident_event(conn, incident_id, status, detail=""):
    conn.execute(
        "INSERT INTO incident_events (incident_id, status, detail) VALUES (?, ?, ?)",
        (incident_id, status, detail),
    )


def persist_command_centre_incident(receiver, report):
    """Commit one destination incident and return its durable receipt ID.

    The caller supplies an already initialized command-centre connection. The
    same helper is used by the simulator and the physical-adapter integration
    boundary so both preserve insert-before-receipt ordering and deduplication.
    """
    incident_id = report["incident_id"]
    receipt_id = "RCP-" + incident_id
    receiver.execute("BEGIN IMMEDIATE")
    receiver.execute(
        "INSERT OR IGNORE INTO command_centre_incidents "
        "(incident_id,user_id,alert_type,description,latitude,longitude,reported_at,"
        "landmark,source_node_id,origin_mode) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (incident_id, report.get("user_id"), report["alert_type"],
         report.get("description", ""), report.get("latitude"), report.get("longitude"),
         report.get("timestamp") or report.get("reported_at") or datetime.now().isoformat(),
         report.get("landmark", ""), report.get("source_node_id"),
         report.get("origin_mode", "primary")),
    )
    receiver.execute(
        "INSERT OR IGNORE INTO command_centre_receipts(incident_id,receipt_id) VALUES(?,?)",
        (incident_id, receipt_id),
    )
    receiver.commit()
    row = receiver.execute(
        "SELECT receipt_id FROM command_centre_receipts WHERE incident_id=?",
        (incident_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("destination commit did not produce a persistent receipt")
    return row[0]


def record_command_centre_receipt(sender, receiver, incident_id, receipt_id,
                                  detail="persistent command-centre receipt"):
    """Mark sender delivered only when the destination has the matching receipt."""
    durable = receiver.execute(
        "SELECT receipt_id FROM command_centre_receipts WHERE incident_id=?",
        (incident_id,),
    ).fetchone()
    if not receipt_id or receipt_id.startswith("LOCAL-") or not durable or durable[0] != receipt_id:
        return False
    current = sender.execute(
        "SELECT delivery_status,receipt_id FROM alerts WHERE incident_id=?",
        (incident_id,),
    ).fetchone()
    if not current:
        return False
    if current["delivery_status"] == "received_by_command_centre" and current["receipt_id"] not in (None, receipt_id):
        return False
    sender.execute(
        "UPDATE alerts SET delivery_status='received_by_command_centre', receipt_id=?, "
        "received_at=COALESCE(received_at,datetime('now')), "
        "lifecycle_status=CASE WHEN lifecycle_status='saved_locally' "
        "THEN 'received_by_command_centre' ELSE lifecycle_status END WHERE incident_id=?",
        (receipt_id, incident_id),
    )
    sender.execute(
        "UPDATE outbound_queue SET state='delivered', delivered_at=COALESCE(delivered_at,datetime('now')) "
        "WHERE incident_id=?",
        (incident_id,),
    )
    if not sender.execute(
            "SELECT 1 FROM incident_events WHERE incident_id=? AND status='received_by_command_centre'",
            (incident_id,)).fetchone():
        record_incident_event(sender, incident_id, "received_by_command_centre", detail)
    sender.commit()
    return True


class SimulatedTransport(SosTransport):
    """A local test transport; it never accesses a physical radio or its settings."""

    def __init__(self):
        self.connected = True
        self.lose_next_ack = False
        self.fail_next_transmission = False

    def status(self):
        return {"kind": "simulated", "connected": bool(self.connected),
                "radio_verified": False}

    def configure(self, connected=None, lose_next_ack=None, fail_next_transmission=None):
        if connected is not None:
            self.connected = bool(connected)
        if lose_next_ack is not None:
            self.lose_next_ack = bool(lose_next_ack)
        if fail_next_transmission is not None:
            self.fail_next_transmission = bool(fail_next_transmission)
        conn=open_db_connection()
        conn.execute("INSERT INTO simulated_transport_config(singleton,connected,lose_next_ack,fail_next_transmission) VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET connected=excluded.connected,lose_next_ack=excluded.lose_next_ack,fail_next_transmission=excluded.fail_next_transmission",(int(self.connected),int(self.lose_next_ack),int(self.fail_next_transmission)))
        conn.commit(); conn.close()
        if self.connected:
            self.process_queue()

    def restore_controls(self):
        conn=open_db_connection()
        row=conn.execute("SELECT connected,lose_next_ack,fail_next_transmission FROM simulated_transport_config WHERE singleton=1").fetchone()
        conn.close()
        if row:
            self.connected=bool(row[0]); self.lose_next_ack=bool(row[1]); self.fail_next_transmission=bool(row[2])

    def process_queue(self):
        conn = open_db_connection()
        receiver = sqlite3.connect(CC_DB_PATH, check_same_thread=False)
        receiver.row_factory = sqlite3.Row
        receiver.execute("PRAGMA journal_mode=WAL")
        delivered = 0
        try:
            receiver.executescript("""CREATE TABLE IF NOT EXISTS command_centre_incidents (
                incident_id TEXT PRIMARY KEY, user_id INTEGER, alert_type TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', latitude REAL, longitude REAL,
                landmark TEXT NOT NULL DEFAULT '', reported_at TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT (datetime('now')));
                CREATE TABLE IF NOT EXISTS command_centre_receipts (
                incident_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));""")
            receiver_cols={r[1] for r in receiver.execute("PRAGMA table_info(command_centre_incidents)")}
            if "landmark" not in receiver_cols:
                receiver.execute("ALTER TABLE command_centre_incidents ADD COLUMN landmark TEXT NOT NULL DEFAULT ''")
            for column, declaration in (("source_node_id", "TEXT"), ("origin_mode", "TEXT NOT NULL DEFAULT 'primary'")):
                if column not in receiver_cols:
                    receiver.execute(f"ALTER TABLE command_centre_incidents ADD COLUMN {column} {declaration}")
            # Earlier Phase 1 code kept a simulated destination table inside the
            # sender DB. Copy those already-persisted destination records once to
            # the separate receiver store and preserve the original receipt fact.
            sender_tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            legacy=(conn.execute("SELECT incident_id,user_id,alert_type,description,latitude,longitude,reported_at FROM command_centre_incidents").fetchall()
                    if "command_centre_incidents" in sender_tables else [])
            for old in legacy:
                receiver.execute("INSERT OR IGNORE INTO command_centre_incidents(incident_id,user_id,alert_type,description,latitude,longitude,reported_at) VALUES(?,?,?,?,?,?,?)",tuple(old))
                receiver.execute("INSERT OR IGNORE INTO command_centre_receipts(incident_id,receipt_id) VALUES(?,?)",(old[0],"RCP-"+old[0]))
                conn.execute("UPDATE alerts SET receipt_id=COALESCE(receipt_id,?),delivery_status='received_by_command_centre',lifecycle_status=CASE WHEN lifecycle_status='saved_locally' THEN 'received_by_command_centre' ELSE lifecycle_status END,received_at=COALESCE(received_at,datetime('now')) WHERE incident_id=?",("RCP-"+old[0],old[0]))
                if not conn.execute("SELECT 1 FROM incident_events WHERE incident_id=? AND status='received_by_command_centre'",(old[0],)).fetchone():
                    record_incident_event(conn,old[0],"received_by_command_centre","legacy persistent command-centre record and receipt migrated")
            receiver.commit()
            conn.commit()
            # A legacy sender-side success without any persisted receiver record
            # is not evidence of delivery. Downgrade it and safely requeue.
            claimed=conn.execute("SELECT incident_id FROM alerts WHERE delivery_status='received_by_command_centre'").fetchall()
            for claim in claimed:
                incident_id=claim[0]
                if receiver.execute("SELECT 1 FROM command_centre_receipts WHERE incident_id=?",(incident_id,)).fetchone():
                    continue
                conn.execute("UPDATE alerts SET delivery_status='queued',lifecycle_status='saved_locally',received_at=NULL,receipt_id=NULL WHERE incident_id=?",(incident_id,))
                conn.execute("INSERT INTO outbound_queue(incident_id,state) VALUES(?,'queued') ON CONFLICT(incident_id) DO UPDATE SET state='queued',delivered_at=NULL",(incident_id,))
                record_incident_event(conn,incident_id,"delivery_reconciled","unverified legacy receipt reset; incident queued for verified delivery")
            conn.commit()
            if not self.connected:
                return 0
            pending = conn.execute(
                "SELECT incident_id FROM outbound_queue WHERE state != 'delivered' ORDER BY id"
            ).fetchall()
            for row in pending:
                incident_id = row["incident_id"]
                if self.fail_next_transmission:
                    self.fail_next_transmission = False
                    conn.execute("UPDATE outbound_queue SET attempts=attempts+1,last_attempt_at=datetime('now') WHERE incident_id=?",(incident_id,))
                    conn.execute("INSERT INTO delivery_attempts(incident_id,outcome,detail) VALUES(?,'failed','simulated transmission failure')",(incident_id,))
                    conn.execute("UPDATE simulated_transport_config SET fail_next_transmission=0 WHERE singleton=1")
                    conn.commit()
                    continue
                conn.execute(
                    "UPDATE outbound_queue SET state='queued', attempts=attempts+1, "
                    "last_attempt_at=datetime('now') WHERE incident_id=?",
                    (incident_id,),
                )
                conn.execute("INSERT INTO delivery_attempts(incident_id,outcome,detail) VALUES(?,'attempted','simulated transport send')", (incident_id,))
                alert = conn.execute(
                    "SELECT incident_id, user_id, alert_type, description, latitude, longitude, timestamp, landmark, source_node_id, origin_mode "
                    "FROM alerts WHERE incident_id=?", (incident_id,)
                ).fetchone()
                if not alert:
                    continue
                # Persistent command-centre storage happens before any acknowledgement.
                persist_command_centre_incident(receiver, dict(alert))
                # Destination persistence precedes any application receipt.
                conn.commit()
                if self.lose_next_ack:
                    self.lose_next_ack = False
                    conn.execute("UPDATE delivery_attempts SET outcome='ack_lost',detail='destination committed; sender did not observe receipt' WHERE id=last_insert_rowid()")
                    conn.execute("UPDATE simulated_transport_config SET lose_next_ack=0 WHERE singleton=1")
                    conn.commit()
                    # Destination has the record, but sender cannot claim receipt.
                    continue
                receipt=receiver.execute("SELECT receipt_id FROM command_centre_receipts WHERE incident_id=?",(incident_id,)).fetchone()
                if not receipt:
                    conn.execute("UPDATE delivery_attempts SET outcome='failed',detail='destination receipt missing' WHERE id=last_insert_rowid()")
                    conn.commit()
                    continue
                if not record_command_centre_receipt(
                        conn, receiver, incident_id, receipt[0],
                        "simulated persistent command-centre receipt"):
                    conn.execute("UPDATE delivery_attempts SET outcome='failed',detail='receipt did not match durable destination record' WHERE id=last_insert_rowid()")
                    conn.commit()
                    continue
                conn.execute("UPDATE delivery_attempts SET outcome='acknowledged',detail='persistent command-centre receipt returned' WHERE id=last_insert_rowid()")
                conn.commit()
                delivered += 1
        finally:
            conn.close()
            receiver.close()
        return delivered


transport = SimulatedTransport()
fallback_store = None
failover_controller = None
failover_simulation_enabled = False
failover_sync_fault = None
ACTIVE_CALLS = {}


def _call_key(first, second):
    return tuple(sorted((first, second)))

# ── Database helpers ──────────────────────────────────────────────────────────
def get_db():
    """Return a per-request SQLite connection with WAL mode enabled."""
    # BUG FIX #2: check_same_thread=False is required when Flask handles
    # requests across threads. We store one connection per Flask request
    # context via g.
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    """Create tables. BUG FIX #10: all use IF NOT EXISTS."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT    UNIQUE NOT NULL,
            full_name   TEXT    NOT NULL,
            phone       TEXT    NOT NULL,
            latitude    REAL    DEFAULT 0.0,
            longitude   REAL    DEFAULT 0.0,
            is_online   INTEGER DEFAULT 0,
            socket_id   TEXT    DEFAULT '',
            role        TEXT    NOT NULL DEFAULT 'civilian',
            created_at  TEXT    DEFAULT (datetime('now')),
            last_seen   TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS contacts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            contact_name TEXT   NOT NULL,
            contact_phone TEXT  NOT NULL,
            on_network  INTEGER DEFAULT 0,
            network_user_id INTEGER DEFAULT NULL,
            UNIQUE(owner_id, contact_phone)          -- BUG FIX #5: dedup constraint
        );

        CREATE TABLE IF NOT EXISTS messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            recipient_id INTEGER DEFAULT NULL REFERENCES users(id) ON DELETE SET NULL,
            content      TEXT    NOT NULL,
            is_broadcast INTEGER DEFAULT 0,
            group_id     INTEGER DEFAULT NULL,
            timestamp    TEXT    DEFAULT (datetime('now')),
            is_read      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS system_announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'broadcast',
            content TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            alert_type   TEXT    NOT NULL,
            description  TEXT    DEFAULT '',
            latitude     REAL    DEFAULT 0.0,   -- BUG FIX #4: REAL not TEXT
            longitude    REAL    DEFAULT 0.0,
            status       TEXT    DEFAULT 'active',
            timestamp    TEXT    DEFAULT (datetime('now')),
            resolved_at  TEXT    DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS environmental_data (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            temperature  REAL    DEFAULT 0.0,
            humidity     REAL    DEFAULT 0.0,
            air_quality  REAL    DEFAULT 0.0,
            battery_v    REAL    DEFAULT 0.0,
            solar_v      REAL    DEFAULT 0.0,
            timestamp    TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS broadcast_groups (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT    UNIQUE NOT NULL,
            creator_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at   TEXT    DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS group_members (
            group_id     INTEGER NOT NULL REFERENCES broadcast_groups(id) ON DELETE CASCADE,
            user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY (group_id, user_id)      -- BUG FIX #11: prevents duplicate membership
        );

        CREATE TABLE IF NOT EXISTS outbound_queue (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id     TEXT NOT NULL UNIQUE,
            state           TEXT NOT NULL DEFAULT 'queued',
            attempts        INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT DEFAULT NULL,
            delivered_at    TEXT DEFAULT NULL,
            created_at      TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS incident_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id TEXT NOT NULL,
            status      TEXT NOT NULL,
            detail      TEXT DEFAULT '',
            created_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "role" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'civilian'")
    migrate_sos_schema(conn, new_incident_id)
    conn.commit()
    conn.close()
    logger.info("Database initialised at %s", DB_PATH)
    # On process start, reconcile receiver records even when disconnected and
    # only drain queued work when the persisted link setting is connected.
    restore_transport = getattr(transport, "restore_controls", None)
    if callable(restore_transport):
        restore_transport()
    # This also migrates any legacy command-centre rows while disconnected;
    # the simulated transport does not drain the queue unless connected.
    transport.process_queue()
    global fallback_store, failover_controller, failover_simulation_enabled
    fallback_store = FallbackStore(FALLBACK_DB_PATH)
    failover_simulation_enabled = bool(app.testing or (
        os.environ.get("NEXALERT_ENV", "").lower() in {"development", "test"}
        and os.environ.get("NEXALERT_ENABLE_FAILOVER_SIM") == "1"))
    if failover_simulation_enabled:
        key = os.environ.get("NEXALERT_FAILOVER_SECRET", "nexalert-test-only-heartbeat-key").encode()
        failover_controller = FailoverController(fallback_store, "pi5-primary", key,
            interval_s=5, timeout_s=25, activation_s=2, target_s=90,
            simulate_primary=True)
    else:
        failover_controller = None

# ── REST API ──────────────────────────────────────────────────────────────────

# --- Registration ---
@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    if not isinstance(data,dict): return jsonify({"error":"request body must be a JSON object"}),400
    if any(data.get(k) is not None and not isinstance(data.get(k),str) for k in ("username","full_name","phone","role")):
        return jsonify({"error":"registration fields must be text"}),400
    username  = (data.get("username") or "").strip()
    full_name = (data.get("full_name") or "").strip()
    phone     = (data.get("phone") or "").strip()
    role = (data.get("role") or "civilian").strip().lower()
    if role not in {"civilian", "responder", "operator"}:
        return jsonify({"error":"role must be civilian, responder, or operator"}),400

    if not username:
        username = "anon-" + str(uuid.uuid4())
    full_name = full_name or "Anonymous"
    phone = phone or ""
    if len(username)>100 or len(full_name)>200 or len(phone)>40:
        return jsonify({"error":"registration fields exceed allowed lengths"}),400

    db = get_db()
    try:
        db.execute(
            "INSERT INTO users (username, full_name, phone, role) VALUES (?, ?, ?, ?)",
            (username, full_name, phone, role)
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        session["username"] = username
        session["user_id"]  = user["id"]
        return jsonify({"status": "ok", "user": dict(user)}), 201
    except sqlite3.IntegrityError:
        # Username already taken — return the existing user so the client
        # can resume rather than getting a cryptic 500.
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user:
            session["username"] = username
            session["user_id"]  = user["id"]
            return jsonify({"status": "existing", "user": dict(user)}), 200
        return jsonify({"error": "Username taken"}), 409
    except Exception as e:
        logger.exception("register failed")
        return jsonify({"error": str(e)}), 500


# --- User list ---
@app.route("/api/users", methods=["GET"])
def get_users():
    db = get_db()
    rows = db.execute("SELECT id, username, full_name, role, latitude, longitude, is_online, last_seen FROM users").fetchall()
    return jsonify([dict(r) for r in rows])


# --- Location update ---
@app.route("/api/location", methods=["POST"])
def update_location():
    data = request.get_json(silent=True) or {}
    username = data.get("username") or session.get("username")
    try:
        lat = float(data.get("latitude", 0))
        lon = float(data.get("longitude", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "latitude and longitude must be numbers"}), 400

    db = get_db()
    db.execute(
        "UPDATE users SET latitude=?, longitude=?, last_seen=datetime('now') WHERE username=?",
        (lat, lon, username)
    )
    db.commit()
    socketio.emit("location_update", {"username": username, "latitude": lat, "longitude": lon})
    return jsonify({"status": "ok"})


# --- SOS ---
@app.route("/api/sos", methods=["POST"])
def create_sos():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict): return jsonify({"error":"request body must be a JSON object"}),400
    username    = data.get("username") or session.get("username")
    alert_type  = data.get("alert_type", "other")
    raw_description=data.get("description", "")
    if raw_description is not None and not isinstance(raw_description,str): return jsonify({"error":"description must be text"}),400
    description = (raw_description or "").strip()
    raw_incident_id=data.get("incident_id", "")
    if raw_incident_id and not isinstance(raw_incident_id,str): return jsonify({"error":"incident_id must be text"}),400
    incident_id = (raw_incident_id or "").strip() or new_incident_id()
    raw_key=request.headers.get("Idempotency-Key") or data.get("idempotency_key") or incident_id
    if not isinstance(raw_key,str): return jsonify({"error":"idempotency_key must be text"}),400
    idempotency_key = raw_key.strip()
    landmark = data.get("landmark", "")
    source_node_id = None
    origin_mode = "primary"
    if data.get("origin_mode") == "fallback" and request.headers.get("X-NexAlert-Internal") == INTERNAL_FORWARD_TOKEN:
        source_node_id = data.get("source_node_id")
        origin_mode = "fallback"
        if not isinstance(source_node_id, str) or not source_node_id or len(source_node_id) > 80:
            return jsonify({"error": "valid fallback source_node_id required"}), 400

    # BUG FIX #4: validate and coerce coordinates
    try:
        if isinstance(data.get("latitude"),bool) or isinstance(data.get("longitude"),bool): raise ValueError()
        lat = None if data.get("latitude") is None and data.get("longitude") is None else float(data.get("latitude"))
        lon = None if data.get("latitude") is None and data.get("longitude") is None else float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"error": "latitude and longitude must be numbers"}), 400

    if username is not None and not isinstance(username,str): return jsonify({"error":"username must be text"}),400
    if not isinstance(alert_type,str) or alert_type not in ALERT_TYPES:
        return jsonify({"error": f"Unknown alert_type. Valid: {list(ALERT_TYPES.keys())}"}), 400
    if not re.fullmatch(r"INC-[A-Za-z0-9._-]{1,75}", incident_id):
        return jsonify({"error": "incident_id must be a valid client-generated incident identifier"}), 400
    if len(description) > 1000:
        return jsonify({"error": "description must be at most 1000 characters"}), 400
    if not isinstance(landmark, str) or len(landmark) > 250:
        return jsonify({"error": "landmark must be a string up to 250 characters"}), 400
    if len(idempotency_key) > 200 or not idempotency_key:
        return jsonify({"error": "valid idempotency_key required"}), 400
    if lat is not None and (not (-90 <= lat <= 90 and -180 <= lon <= 180)):
        return jsonify({"error": "latitude or longitude is out of range"}), 400

    db = get_db()
    user = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone() if username else None
    if not user:
        # Anonymous, durable identity derived from the retry key; no personal details required.
        anon = username if isinstance(username,str) and 0<len(username)<=100 else "anon-" + str(uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key))
        db.execute("INSERT OR IGNORE INTO users(username,full_name,phone) VALUES(?,?,?)", (anon, "Anonymous", ""))
        db.commit()
        username = anon
        user = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    existing = db.execute("SELECT * FROM alerts WHERE idempotency_key=? OR incident_id=?", (idempotency_key, incident_id)).fetchone()
    if existing:
        if existing["user_id"] != user["id"]:
            return jsonify({"error": "incident_id belongs to another user"}), 409
        alert_id = existing["id"]
        incident_id = existing["incident_id"]
        alert_row = existing
    else:
        cur = db.execute(
            "INSERT INTO alerts (user_id, alert_type, description, latitude, longitude, incident_id, delivery_status, lifecycle_status, landmark, idempotency_key, source_node_id, origin_mode) "
            "VALUES (?,?,?,?,?,?, 'saved_locally','saved_locally',?,?,?,?)",
            (user["id"], alert_type, description, lat, lon, incident_id, landmark, idempotency_key, source_node_id, origin_mode)
        )
        alert_id = cur.lastrowid
        record_incident_event(db, incident_id, "saved_locally", "validated SOS persisted locally")
        db.execute("INSERT INTO outbound_queue (incident_id, state) VALUES (?, 'queued')", (incident_id,))
        db.execute("UPDATE alerts SET delivery_status='queued' WHERE id=?", (alert_id,))
        record_incident_event(db, incident_id, "queued", "queued for transport")
        db.commit()
        alert_row = db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()

    # The transaction above completes before forwarding is attempted.
    if transport.status().get("connected"):
        transport.process_queue()
    alert_row = db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()

    alert_data = {
        "id":          alert_id,
        "incident_id": alert_row["incident_id"],
        "username":    username,
        "alert_type":  alert_row["alert_type"],
        "label":       ALERT_TYPES[alert_row["alert_type"]]["label"],
        "icon":        ALERT_TYPES[alert_row["alert_type"]]["icon"],
        "color":       ALERT_TYPES[alert_row["alert_type"]]["color"],
        "description": alert_row["description"],
        "latitude":    alert_row["latitude"],
        "longitude":   alert_row["longitude"],
        "landmark":    alert_row["landmark"],
        "timestamp":   alert_row["timestamp"],
        "delivery_status": alert_row["delivery_status"],
        "lifecycle_status": alert_row["lifecycle_status"],
        "receipt_id": alert_row["receipt_id"],
        "source_node_id": alert_row["source_node_id"],
        "origin_mode": alert_row["origin_mode"],
    }
    socketio.emit("new_alert", alert_data)
    logger.info("SOS persisted: incident=%s type=%s", incident_id, alert_type)
    return jsonify({"status": "ok", "created": existing is None, "alert": alert_data}), 201 if existing is None else 200


# --- Active alerts ---
@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    db = get_db()
    rows = db.execute("""
        SELECT a.id, a.incident_id, u.username, a.alert_type, a.description,
               a.latitude, a.longitude, a.status, a.delivery_status, a.responder_assignment, a.timestamp
        FROM alerts a JOIN users u ON a.user_id = u.id
        WHERE a.status = 'active'
        ORDER BY a.timestamp DESC
    """).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d.update(ALERT_TYPES.get(d["alert_type"], {}))
        result.append(d)
    return jsonify(result)


@app.route("/api/alerts/<int:alert_id>/resolve", methods=["POST"])
def resolve_alert(alert_id):
    db = get_db()
    row=db.execute("SELECT incident_id,lifecycle_status,delivery_status FROM alerts WHERE id=?",(alert_id,)).fetchone()
    if not row:return jsonify({"error":"Incident not found"}),404
    if row["delivery_status"]!="received_by_command_centre" or row["lifecycle_status"]!="arrived":
        return jsonify({"error":"incident must be received and arrived before resolution"}),409
    db.execute("UPDATE alerts SET lifecycle_status='resolved',status='resolved',resolved_at=datetime('now') WHERE id=?",(alert_id,))
    record_incident_event(db,row["incident_id"],"resolved","operator recorded lifecycle event")
    db.commit()
    socketio.emit("alert_resolved", {"id": alert_id})
    return jsonify({"status": "ok"})


@app.route("/api/incidents/<incident_id>/assign", methods=["POST"])
def assign_responder(incident_id):
    """Record an operator assignment; it is intentionally separate from receipt."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data,dict): return jsonify({"error":"request body must be a JSON object"}),400
    responder = data.get("responder")
    responder = responder.strip() if isinstance(responder,str) else ""
    if not responder or len(responder) > 200:
        return jsonify({"error": "a responder name up to 200 characters is required"}), 400
    db = get_db()
    alert = db.execute("SELECT id,delivery_status,lifecycle_status FROM alerts WHERE incident_id=?", (incident_id,)).fetchone()
    if not alert:
        return jsonify({"error": "Incident not found"}), 404
    if alert["delivery_status"] != "received_by_command_centre":
        return jsonify({"error":"incident has not been received by command centre"}),409
    if alert["lifecycle_status"] == "resolved":
        return jsonify({"error":"resolved incidents cannot be reassigned"}),409
    next_lifecycle="responder_assigned" if alert["lifecycle_status"]=="received_by_command_centre" else alert["lifecycle_status"]
    event="responder_assigned" if next_lifecycle=="responder_assigned" else "responder_reassigned"
    db.execute("UPDATE alerts SET lifecycle_status=?, responder_assignment=? WHERE incident_id=?",
               (next_lifecycle,responder, incident_id))
    record_incident_event(db, incident_id, event, "operator assignment recorded")
    db.execute("INSERT INTO responder_assignments(incident_id,responder) VALUES(?,?) ON CONFLICT(incident_id) DO UPDATE SET responder=excluded.responder,assigned_at=datetime('now')", (incident_id, responder))
    db.commit()
    socketio.emit("incident_updated", {"incident_id": incident_id})
    return jsonify({"status": "ok", "incident_id": incident_id, "lifecycle_status": next_lifecycle})


LIFECYCLE_TRANSITIONS = {
    "received_by_command_centre": {"responder_assigned"},
    "responder_assigned": {"dispatched"}, "dispatched": {"arrived"},
    "arrived": {"resolved"},
}

@app.route("/api/incidents", methods=["GET"])
def list_incidents():
    db = get_db()
    filters=["a.delivery_status='received_by_command_centre'"]; params=[]
    category=request.args.get("alert_type")
    lifecycle=request.args.get("lifecycle_status")
    if category:
        if category not in ALERT_TYPES:return jsonify({"error":"unknown alert_type"}),400
        filters.append("a.alert_type=?"); params.append(category)
    if lifecycle:
        if lifecycle not in {"saved_locally","queued","relayed","received_by_command_centre","responder_assigned","dispatched","arrived","resolved"}:
            return jsonify({"error":"unknown lifecycle_status"}),400
        filters.append("a.lifecycle_status=?"); params.append(lifecycle)
    rows = db.execute("""SELECT a.id,a.incident_id,u.username,a.alert_type,a.description,a.latitude,a.longitude,a.landmark,
        a.timestamp,a.received_at,a.receipt_id,a.delivery_status,a.lifecycle_status,a.responder_assignment,a.status,a.source_node_id,a.origin_mode
        FROM alerts a JOIN users u ON u.id=a.user_id WHERE """+" AND ".join(filters)+" ORDER BY a.timestamp DESC",params).fetchall()
    out=[]
    for row in rows:
        item=dict(row); item.update(ALERT_TYPES.get(item['alert_type'], {}))
        item['history']=[dict(e) for e in db.execute("SELECT status,detail,created_at FROM incident_events WHERE incident_id=? ORDER BY id",(item['incident_id'],))]
        out.append(item)
    return jsonify(out)

@app.route("/api/incidents/<incident_id>", methods=["GET"])
def incident_detail(incident_id):
    row=get_db().execute("SELECT * FROM alerts WHERE incident_id=?",(incident_id,)).fetchone()
    if not row: return jsonify({"error":"Incident not found"}),404
    return jsonify({"incident":dict(row),"history":[dict(e) for e in get_db().execute("SELECT * FROM incident_events WHERE incident_id=? ORDER BY id",(incident_id,))]})

@app.route("/api/incidents/<incident_id>/updates", methods=["POST"])
def update_incident(incident_id):
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict): return jsonify({"error":"request body must be a JSON object"}),400
    description=data.get('description')
    landmark=data.get('landmark')
    if description is not None and (not isinstance(description,str) or len(description)>1000): return jsonify({"error":"description must be up to 1000 characters"}),400
    if landmark is not None and (not isinstance(landmark,str) or len(landmark)>250): return jsonify({"error":"landmark must be up to 250 characters"}),400
    db=get_db(); row=db.execute("SELECT id FROM alerts WHERE incident_id=?",(incident_id,)).fetchone()
    if not row: return jsonify({"error":"Incident not found"}),404
    if description is not None: db.execute("UPDATE alerts SET description=? WHERE incident_id=?",(description.strip(),incident_id))
    if landmark is not None: db.execute("UPDATE alerts SET landmark=? WHERE incident_id=?",(landmark.strip(),incident_id))
    # Mirror follow-up data only when the command centre already owns the incident.
    if db.execute("SELECT 1 FROM alerts WHERE incident_id=? AND delivery_status='received_by_command_centre'",(incident_id,)).fetchone():
        cc=sqlite3.connect(CC_DB_PATH)
        if description is not None: cc.execute("UPDATE command_centre_incidents SET description=? WHERE incident_id=?",(description.strip(),incident_id))
        if landmark is not None: cc.execute("UPDATE command_centre_incidents SET landmark=? WHERE incident_id=?",(landmark.strip(),incident_id))
        cc.commit(); cc.close()
    record_incident_event(db,incident_id,"details_updated","citizen follow-up details updated"); db.commit()
    return jsonify({"status":"ok","incident_id":incident_id})

@app.route("/api/incidents/<incident_id>/lifecycle", methods=["POST"])
def advance_lifecycle(incident_id):
    data=request.get_json(silent=True) or {}; target=data.get('status')
    db=get_db(); row=db.execute("SELECT lifecycle_status,delivery_status FROM alerts WHERE incident_id=?",(incident_id,)).fetchone()
    if not row:return jsonify({"error":"Incident not found"}),404
    if row['delivery_status']!='received_by_command_centre': return jsonify({"error":"incident has not been received by command centre"}),409
    if not isinstance(target,str) or target not in LIFECYCLE_TRANSITIONS.get(row['lifecycle_status'],set()): return jsonify({"error":"invalid lifecycle transition"}),409
    db.execute("UPDATE alerts SET lifecycle_status=?,status=?,resolved_at=CASE WHEN ?='resolved' THEN datetime('now') ELSE resolved_at END WHERE incident_id=?",(target,'resolved' if target=='resolved' else 'active',target,incident_id))
    record_incident_event(db,incident_id,target,"operator recorded lifecycle event"); db.commit()
    socketio.emit("incident_updated",{"incident_id":incident_id})
    return jsonify({"status":"ok","incident_id":incident_id,"lifecycle_status":target})


@app.route("/api/transport/simulated", methods=["POST"])
def configure_simulated_transport():
    """Prototype/test control for the simulated link; it cannot operate hardware."""
    dev_mode=os.environ.get("NEXALERT_ENV", "production").lower() in {"dev","development","test"}
    if not (app.testing or app.debug or (dev_mode and os.environ.get("NEXALERT_ENABLE_SIM_CONTROLS") == "1")):
        return jsonify({"error":"simulated transport controls are disabled"}), 404
    data = request.get_json(silent=True) or {}
    if not isinstance(data,dict): return jsonify({"error":"request body must be a JSON object"}),400
    if "connected" in data and not isinstance(data["connected"], bool):
        return jsonify({"error": "connected must be boolean"}), 400
    if "lose_next_ack" in data and not isinstance(data["lose_next_ack"], bool):
        return jsonify({"error": "lose_next_ack must be boolean"}), 400
    if "fail_next_transmission" in data and not isinstance(data["fail_next_transmission"], bool):
        return jsonify({"error":"fail_next_transmission must be boolean"}),400
    transport.configure(data.get("connected"), data.get("lose_next_ack"), data.get("fail_next_transmission"))
    return jsonify({"transport": "simulated", "connected": transport.connected,
                    "lose_next_ack": transport.lose_next_ack, "fail_next_transmission":transport.fail_next_transmission})


# --- Messages ---
@app.route("/api/messages", methods=["POST"])
def send_message():
    data      = request.get_json(silent=True) or {}
    sender_un = data.get("sender") or session.get("username")
    content   = (data.get("content") or "").strip()

    if not content:
        return jsonify({"error": "content required"}), 400

    db = get_db()
    sender = db.execute("SELECT id FROM users WHERE username=?", (sender_un,)).fetchone()
    if not sender:
        return jsonify({"error": "Sender not registered"}), 404

    recipient_un = data.get("recipient")
    is_broadcast = int(data.get("is_broadcast", 0))
    group_id     = data.get("group_id")

    recipient_id = None
    if recipient_un:
        # BUG FIX #6: gracefully handle unknown recipient
        rec = db.execute("SELECT id FROM users WHERE username=?", (recipient_un,)).fetchone()
        if not rec:
            return jsonify({"error": f"Recipient '{recipient_un}' not found"}), 404
        recipient_id = rec["id"]

    cur = db.execute(
        "INSERT INTO messages (sender_id, recipient_id, content, is_broadcast, group_id) VALUES (?,?,?,?,?)",
        (sender["id"], recipient_id, content, is_broadcast, group_id)
    )
    db.commit()

    msg = {
        "id":          cur.lastrowid,
        "sender":      sender_un,
        "recipient":   recipient_un,
        "content":     content,
        "is_broadcast": is_broadcast,
        "timestamp":   datetime.now().isoformat(),
    }

    if is_broadcast:
        socketio.emit("new_message", msg)
    elif recipient_un:
        socketio.emit("new_message", msg, room=recipient_un)
        socketio.emit("new_message", msg, room=sender_un)

    return jsonify({"status": "ok", "message": msg}), 201


@app.route("/api/messages", methods=["GET"])
def get_messages():
    username = request.args.get("username") or session.get("username")
    db = get_db()
    user = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not user:
        return jsonify([])
    rows = db.execute("""
        SELECT m.id, su.username as sender, ru.username as recipient,
               m.content, m.is_broadcast, m.timestamp, m.is_read
        FROM messages m
        JOIN users su ON m.sender_id = su.id
        LEFT JOIN users ru ON m.recipient_id = ru.id
        WHERE m.is_broadcast = 1
           OR m.sender_id = ? OR m.recipient_id = ?
        ORDER BY m.timestamp DESC LIMIT 100
    """, (user["id"], user["id"])).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/announcements", methods=["GET"])
def get_announcements():
    rows = get_db().execute(
        "SELECT id,category,content,created_at FROM system_announcements ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/announcements", methods=["POST"])
def create_announcement():
    """Persist a local command-centre broadcast before notifying connected clients."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    content = data.get("content")
    category = data.get("category", "broadcast")
    if not isinstance(content, str) or not content.strip() or len(content.strip()) > 500:
        return jsonify({"error": "content must be between 1 and 500 characters"}), 400
    allowed = {"broadcast", "cyclone_warning", "flood_alert", "all_clear", "rescue_en_route"}
    if not isinstance(category, str) or category not in allowed:
        return jsonify({"error": "unknown announcement category"}), 400
    db = get_db()
    cur = db.execute("INSERT INTO system_announcements(category,content) VALUES(?,?)",
                     (category, content.strip()))
    db.commit()
    announcement = dict(db.execute(
        "SELECT id,category,content,created_at FROM system_announcements WHERE id=?",
        (cur.lastrowid,),
    ).fetchone())
    socketio.emit("system_announcement", announcement)
    return jsonify({"status": "persisted_local_broadcast", "announcement": announcement,
                    "delivery": "local_application_only"}), 201


@app.route("/api/dashboard/summary", methods=["GET"])
def dashboard_summary():
    db = get_db()
    online = db.execute("SELECT count(*) FROM users WHERE is_online=1").fetchone()[0]
    messages = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    announcements = db.execute("SELECT count(*) FROM system_announcements").fetchone()[0]
    today = db.execute("SELECT count(*) FROM alerts WHERE date(timestamp)=date('now')").fetchone()[0]
    return jsonify({"online_participants": online, "messages": messages + announcements,
                    "sos_today": today, "uptime_seconds": int(monotonic() - APP_STARTED_MONOTONIC)})


@app.route("/api/status", methods=["GET"])
def application_status():
    state = transport.status()
    return jsonify({"application": "connected", "transport": state.get("kind", "unknown"),
                    "transport_state": state.get("state", "connected" if state.get("connected") else "disconnected"),
                    "simulated_transport_connected": bool(state.get("connected")) if state.get("kind") == "simulated" else False,
                    "physical_radio": "verified" if state.get("radio_verified") else "not_configured",
                    "radio_verified": bool(state.get("radio_verified", False))})


def _failover_snapshot():
    if failover_controller is None:
        return {"enabled": False, "node_role": "primary", "state": "PRIMARY_ACTIVE",
                "reason": "Failover monitor is not configured on this node", "fallback_service_ready": False,
                "pending_fallback_incidents": fallback_store.pending_count() if fallback_store else 0,
                "received_fallback_incidents": fallback_store.received_count() if fallback_store else 0,
                "physical_hardware_verified": False}
    state = failover_controller.tick()
    state["enabled"] = True
    state["physical_hardware_verified"] = False
    state["transport"] = "simulated" if failover_simulation_enabled else "unverified"
    return state


@app.route("/api/failover/status", methods=["GET"])
def failover_status():
    return jsonify(_failover_snapshot())


@app.route("/api/failover/heartbeat", methods=["POST"])
def receive_failover_heartbeat():
    if failover_controller is None:
        return jsonify({"error": "backup heartbeat service is not configured"}), 503
    data = request.get_json(silent=True)
    if not failover_controller.receive(data):
        return jsonify({"accepted": False, "error": failover_controller.monitor.last_rejection}), 400
    return jsonify({"accepted": True, "state": failover_controller.state.value}), 202


@app.route("/api/fallback/sos", methods=["POST"])
def fallback_sos():
    if not failover_controller or not failover_controller.fallback_service_ready:
        return jsonify({"error": "local fallback service is not active"}), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400
    report = dict(data)
    report.setdefault("incident_id", new_incident_id())
    try:
        accepted, created = fallback_store.accept(report, "esp32-backup-sim", time.time())
    except FailoverError as exc:
        return jsonify({"error": str(exc)}), 400
    socketio.emit("fallback_sos_accepted", {"incident_id": accepted["incident_id"], "origin_mode": "fallback"})
    receipt = accepted.get("command_receipt_id")
    delivery = "received_by_command_centre" if receipt else "queued_on_backup"
    return jsonify({"status": "accepted_locally", "created": created,
                    "delivery_status": delivery, "command_centre_receipt": receipt,
                    "incident": accepted}), 201 if created else 200


def _forward_fallback_report(report):
    global failover_sync_fault
    if failover_sync_fault == "fail_transfer":
        failover_sync_fault = None
        return ForwardResult(False, detail="simulated transfer failure")
    if not transport.connected:
        return ForwardResult(False, detail="simulated transport is disconnected")
    if failover_sync_fault == "lose_ack":
        failover_sync_fault = None
        transport.lose_next_ack = True
        conn = open_db_connection()
        conn.execute("UPDATE simulated_transport_config SET lose_next_ack=1 WHERE singleton=1")
        conn.commit(); conn.close()
    username = "fallback-" + report["source_node_id"]
    payload = {"username": username, "incident_id": report["incident_id"],
        "idempotency_key": report["incident_id"], "alert_type": report["alert_type"],
        "description": report["description"], "latitude": report["latitude"],
        "longitude": report["longitude"], "landmark": report["landmark"],
        "origin_mode": "fallback", "source_node_id": report["source_node_id"]}
    with app.test_client() as client:
        response = client.post("/api/sos", json=payload, headers={"X-NexAlert-Internal": INTERNAL_FORWARD_TOKEN})
        body = response.get_json(silent=True) or {}
    if response.status_code not in (200, 201):
        return ForwardResult(False, detail=body.get("error", "primary rejected fallback report"))
    alert = body.get("alert", {})
    if alert.get("delivery_status") == "received_by_command_centre" and alert.get("receipt_id"):
        return ForwardResult(True, alert["receipt_id"])
    return ForwardResult(False, detail="primary saved report but has no persistent command-centre receipt")


def _sync_fallback():
    if not failover_controller or failover_controller.state != FailoverState.SYNCING_INCIDENTS:
        return {"error": "primary is not in incident-sync state"}, 409
    syncer = FallbackSynchronizer(fallback_store, _forward_fallback_report, time.time)
    result = syncer.sync_once()
    if result["pending"] == 0:
        failover_controller.sync_complete()
    elif result["failed"]:
        failover_controller.sync_failed("One or more fallback incidents remain unacknowledged; durable outbox retained")
    return result, 200


@app.route("/api/failover/simulate", methods=["POST"])
def simulate_failover():
    global failover_controller, failover_sync_fault
    if not failover_simulation_enabled or not failover_controller:
        return jsonify({"error": "development failover controls are disabled"}), 404
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    try:
        if action == "heartbeat":
            failover_controller.set_simulation(drop_heartbeats=False, link_available=True)
        elif action == "drop_heartbeats":
            failover_controller.set_simulation(drop_heartbeats=True)
        elif action == "power_off":
            failover_controller.set_simulation(primary_running=False, drop_heartbeats=True)
        elif action == "partition":
            failover_controller.set_simulation(link_available=False)
        elif action == "restore_network":
            failover_controller.set_simulation(link_available=True, drop_heartbeats=False)
        elif action == "restore_primary":
            failover_controller.set_simulation(primary_running=True, primary_ready=bool(data.get("ready", True)),
                                               link_available=bool(data.get("link", True)), drop_heartbeats=False)
        elif action == "restart_backup":
            key = os.environ.get("NEXALERT_FAILOVER_SECRET", "nexalert-test-only-heartbeat-key").encode()
            failover_controller = FailoverController(fallback_store, "pi5-primary", key, simulate_primary=True)
        elif action == "fail_next_transfer":
            failover_sync_fault = "fail_transfer"
        elif action == "lose_next_ack":
            failover_sync_fault = "lose_ack"
        elif action == "sync":
            result, status = _sync_fallback()
            return jsonify(result), status
        elif action != "tick":
            return jsonify({"error": "unknown simulation action"}), 400
        return jsonify(_failover_snapshot())
    except FailoverError as exc:
        return jsonify({"error": str(exc)}), 409


# --- Contacts ---
@app.route("/api/contacts", methods=["POST"])
def sync_contacts():
    data     = request.get_json(silent=True) or {}
    username = data.get("username") or session.get("username")
    contacts = data.get("contacts", [])

    db = get_db()
    user = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not user:
        return jsonify({"error": "User not registered"}), 404

    # BUG FIX #5: use INSERT OR IGNORE to avoid duplicate inserts
    for c in contacts:
        name  = (c.get("name") or "").strip()
        phone = (c.get("phone") or "").strip()
        if not name or not phone:
            continue
        # check if this contact is on the network
        net_user = db.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone()
        on_net   = 1 if net_user else 0
        net_uid  = net_user["id"] if net_user else None
        db.execute(
            """INSERT OR IGNORE INTO contacts (owner_id, contact_name, contact_phone, on_network, network_user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (user["id"], name, phone, on_net, net_uid)
        )
    db.commit()

    rows = db.execute(
        "SELECT * FROM contacts WHERE owner_id=?", (user["id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# --- Environmental data ---
@app.route("/api/environmental", methods=["GET"])
def get_environmental():
    db = get_db()
    row = db.execute(
        "SELECT * FROM environmental_data ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    # BUG FIX #8: return 404 + message instead of crashing on None
    if not row:
        return jsonify({"error": "No sensor data yet"}), 404
    return jsonify(dict(row))


@app.route("/api/environmental", methods=["POST"])
def post_environmental():
    """Endpoint for the ESP32/sensor to push readings."""
    data = request.get_json(silent=True) or {}
    try:
        temp  = float(data.get("temperature", 0))
        hum   = float(data.get("humidity", 0))
        aq    = float(data.get("air_quality", 0))
        batt  = float(data.get("battery_v", 0))
        solar = float(data.get("solar_v", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "All fields must be numeric"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO environmental_data (temperature, humidity, air_quality, battery_v, solar_v) VALUES (?,?,?,?,?)",
        (temp, hum, aq, batt, solar)
    )
    db.commit()
    socketio.emit("env_update", {"temperature": temp, "humidity": hum,
                                  "air_quality": aq, "battery_v": batt, "solar_v": solar})
    return jsonify({"status": "ok"}), 201


# --- Groups ---
@app.route("/api/groups", methods=["POST"])
def create_group():
    data     = request.get_json(silent=True) or {}
    name     = (data.get("name") or "").strip()
    username = data.get("username") or session.get("username")
    members  = data.get("members", [])  # list of usernames

    if not name:
        return jsonify({"error": "group name required"}), 400

    db = get_db()
    creator = db.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not creator:
        return jsonify({"error": "Creator not registered"}), 404

    try:
        cur = db.execute(
            "INSERT INTO broadcast_groups (name, creator_id) VALUES (?, ?)",
            (name, creator["id"])
        )
        group_id = cur.lastrowid
        # always add creator
        db.execute("INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?,?)",
                   (group_id, creator["id"]))
        for mu in members:
            mu_row = db.execute("SELECT id FROM users WHERE username=?", (mu,)).fetchone()
            if mu_row:
                # BUG FIX #11: INSERT OR IGNORE respects the PK constraint
                db.execute("INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?,?)",
                           (group_id, mu_row["id"]))
        db.commit()
        return jsonify({"status": "ok", "group_id": group_id}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Group name already exists"}), 409


# --- Pages ---
@app.route("/")
@app.route("/phone")
def phone_ui():
    return render_template("phone.html", alert_types=ALERT_TYPES)

@app.route("/dashboard")
def dashboard_ui():
    return render_template("dashboard.html", alert_types=ALERT_TYPES)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now().isoformat()})


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(app.static_folder, "service-worker.js",
                                   mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response

# ── WebSocket events ──────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    # Socket identity is tied to the Flask session established by registration.
    username = session.get("username")
    if username:
        join_room(username)
        try:
            db = sqlite3.connect(DB_PATH, check_same_thread=False)
            db.execute("UPDATE users SET is_online=1, socket_id=?, last_seen=datetime('now') WHERE username=?",
                       (request.sid, username))
            db.commit()
            db.close()
        except Exception:
            pass
        socketio.emit("user_online", {"username": username})
        logger.info("Connected: %s (%s)", username, request.sid)


@socketio.on("disconnect")
def on_disconnect():
    username = session.get("username")
    if username:
        for key, call in list(ACTIVE_CALLS.items()):
            if username in key:
                other = call["callee"] if call["caller"] == username else call["caller"]
                ACTIVE_CALLS.pop(key, None)
                socketio.emit("call:ended", {"from": username}, room=other)
        leave_room(username)
        try:
            db = sqlite3.connect(DB_PATH, check_same_thread=False)
            db.execute("UPDATE users SET is_online=0, socket_id='' WHERE username=?", (username,))
            db.commit()
            db.close()
        except Exception:
            pass
        socketio.emit("user_offline", {"username": username})
        logger.info("Disconnected: %s", username)


@socketio.on("join")
def on_join(data):
    """Rejoin only the room associated with this Flask session."""
    username = session.get("username")
    if username:
        join_room(username)


@socketio.on("typing")
def on_typing(data):
    recipient = data.get("recipient")
    if recipient:
        emit("typing", {"from": data.get("sender")}, room=recipient)


def _call_identity(data):
    """Resolve a call peer only when both socket and target are session-backed users."""
    sender = session.get("username")
    target = data.get("to") if isinstance(data, dict) else None
    if not sender or not isinstance(target, str) or not target or target == sender:
        return None, None
    db = sqlite3.connect(DB_PATH)
    try:
        peer = db.execute("SELECT username FROM users WHERE username=? AND is_online=1", (target,)).fetchone()
    finally:
        db.close()
    return (sender, target) if peer else (sender, None)


@socketio.on("call:invite")
def on_call_invite(data):
    sender, target = _call_identity(data)
    if not sender or not target:
        emit("call:error", {"message": "Call requires a registered session and an online participant"})
        return
    key = _call_key(sender, target)
    if key in ACTIVE_CALLS:
        emit("call:error", {"message": "Participant is already in a call"})
        return
    ACTIVE_CALLS[key] = {"caller": sender, "callee": target, "state": "ringing"}
    socketio.emit("call:invite", {"from": sender}, room=target)


@socketio.on("call:accept")
def on_call_accept(data):
    sender, target = _call_identity(data)
    call = ACTIVE_CALLS.get(_call_key(sender, target)) if sender and target else None
    if not call or call["caller"] != target or call["callee"] != sender or call["state"] != "ringing":
        emit("call:error", {"message": "Call participant is unavailable"})
        return
    call["state"] = "active"
    socketio.emit("call:accepted", {"from": sender}, room=target)


@socketio.on("call:decline")
def on_call_decline(data):
    sender, target = _call_identity(data)
    key = _call_key(sender, target) if sender and target else None
    call = ACTIVE_CALLS.get(key) if key else None
    if call and call["caller"] == target and call["callee"] == sender and call["state"] == "ringing":
        ACTIVE_CALLS.pop(key, None)
        socketio.emit("call:declined", {"from": sender}, room=target)


@socketio.on("call:signal")
def on_call_signal(data):
    sender, target = _call_identity(data)
    kind = data.get("kind") if isinstance(data, dict) else None
    signal = data.get("data") if isinstance(data, dict) else None
    call = ACTIVE_CALLS.get(_call_key(sender, target)) if sender and target else None
    if not call or call["state"] != "active" or kind not in {"offer", "answer", "ice"} or not isinstance(signal, dict):
        emit("call:error", {"message": "Invalid call signal or unavailable participant"})
        return
    socketio.emit("call:signal", {"from": sender, "kind": kind, "data": signal}, room=target)


@socketio.on("call:end")
def on_call_end(data):
    sender, target = _call_identity(data)
    key = _call_key(sender, target) if sender and target else None
    if key and key in ACTIVE_CALLS:
        ACTIVE_CALLS.pop(key, None)
        socketio.emit("call:ended", {"from": sender}, room=target)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    host=os.environ.get("NEXALERT_HOST","0.0.0.0")
    port=int(os.environ.get("NEXALERT_PORT","5000"))
    logger.info("NexAlert v3.0 starting on %s:%s",host,port)
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
