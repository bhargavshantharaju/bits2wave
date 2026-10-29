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
  9. Socket.IO accepts same-origin browser connections only
 10. DB schema uses IF NOT EXISTS everywhere
 11. Broadcast groups: missing member check before insert
 12. deploy.sh: wlan1 check before activating hotspot
"""

import os
import sqlite3
import time
import logging
import hashlib
import hmac
import ipaddress
import ssl
import re
import uuid
import secrets
from functools import wraps
from datetime import datetime, timedelta
from time import monotonic
from flask import Flask, request, jsonify, render_template, g, session, send_from_directory, redirect
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import check_password_hash, generate_password_hash
try:
    from backend.migrations import migrate_sos_schema, migrate_security_schema
    from backend.failover import FailoverController, FailoverError, FailoverState, FallbackStore, FallbackSynchronizer, ForwardResult, Heartbeat, sign_ownership_assertion, verify_ownership_assertion
    from backend.transport import SosTransport
except ModuleNotFoundError:  # Running as `python backend/app.py`
    from migrations import migrate_sos_schema, migrate_security_schema
    from failover import FailoverController, FailoverError, FailoverState, FallbackStore, FallbackSynchronizer, ForwardResult, Heartbeat, sign_ownership_assertion, verify_ownership_assertion
    from transport import SosTransport

# ── App setup ─────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.environ.get("NEXALERT_DB", os.path.join(BASE_DIR, "..", "database", "nexalert.db"))
CC_DB_PATH = os.environ.get("NEXALERT_CC_DB", os.path.join(BASE_DIR, "..", "database", "command-centre.db"))
FALLBACK_DB_PATH = os.environ.get("NEXALERT_FALLBACK_DB", os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "nexalert-fallback.db"))
LOG_DIR  = os.environ.get("NEXALERT_LOG_DIR", os.path.join(BASE_DIR, "..", "logs"))
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


@app.errorhandler(RequestEntityTooLarge)
def request_too_large(_error):
    return jsonify({"error": "request body is too large"}), 413


@app.errorhandler(BadRequest)
def malformed_request(_error):
    return jsonify({"error": "malformed request"}), 400
APP_STARTED_MONOTONIC = monotonic()
APP_ENV = os.environ.get("NEXALERT_ENV", "production").lower()
IS_DEVELOPMENT = APP_ENV in {"dev", "development", "test"}
configured_secret = os.environ.get("NEXALERT_SECRET")
if not IS_DEVELOPMENT and (not configured_secret or len(configured_secret.encode("utf-8")) < 32):
    raise RuntimeError("NEXALERT_SECRET must contain at least 32 bytes in deployment mode")
if not configured_secret:
    configured_secret = secrets.token_urlsafe(48)
    if APP_ENV != "test":
        logger.warning("NEXALERT_SECRET is ephemeral; signed sessions will not survive restart")
app.secret_key = configured_secret
app.config.update(
    SESSION_COOKIE_NAME="nexalert_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=not IS_DEVELOPMENT,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SESSION_REFRESH_EACH_REQUEST=False,
    MAX_CONTENT_LENGTH=64 * 1024,
)
app.config["TESTING"] = APP_ENV == "test"
app.config["ENV"] = APP_ENV
if os.environ.get("NEXALERT_TRUST_PROXY"):
    raise RuntimeError("NEXALERT_TRUST_PROXY is unsafe and unsupported; configure the explicit trusted-proxy settings")
OPERATOR_HTTPS_ENABLED = IS_DEVELOPMENT or bool(os.environ.get("NEXALERT_TLS_CERT") and os.environ.get("NEXALERT_TLS_KEY"))
TRUSTED_PROXY_ADDRESS = os.environ.get("NEXALERT_TRUSTED_PROXY_ADDRESS", "")
TLS_CERT_PATH = os.environ.get("NEXALERT_TLS_CERT", "")
TLS_KEY_PATH = os.environ.get("NEXALERT_TLS_KEY", "")
INTERNAL_FORWARD_TOKEN = uuid.uuid4().hex
PRIMARY_NODE_ID = os.environ.get("NEXALERT_PRIMARY_NODE_ID","pi5-primary")
FALLBACK_NODE_ID = os.environ.get("NEXALERT_FALLBACK_NODE_ID","esp32-backup-sim")
# One key per configured fallback node, installed on that backup and this primary.
# It is distinct from Flask's session signing secret.
OWNERSHIP_ASSERTION_KEY = os.environ.get("NEXALERT_BACKUP_OWNERSHIP_KEY",
    "").encode()
if not IS_DEVELOPMENT and len(OWNERSHIP_ASSERTION_KEY) < 32:
    raise RuntimeError("production requires a unique NEXALERT_BACKUP_OWNERSHIP_KEY of at least 32 bytes")
if IS_DEVELOPMENT and not OWNERSHIP_ASSERTION_KEY:
    OWNERSHIP_ASSERTION_KEY = secrets.token_bytes(32)
DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32), method="scrypt")
ACTIVE_SOCKETS = {}
ACTIVE_SOCKET_IDENTITIES = {}
SOCKET_REAPER_STARTED = False
SOCKET_CLOCK = time.time
RATE_LIMIT_CLOCK = time.time

# BUG FIX #1: Use threading mode — eventlet requires monkey-patching at module
# top-level which breaks Pi GPIO libs and conflicts with BalenaOS containers.
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=None,
                    logger=False, engineio_logger=False)


class TrustedLocalProxy:
    """Honor proxy scheme/client IP only from the configured loopback proxy."""
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        remote = environ.get("REMOTE_ADDR", "")
        if TRUSTED_PROXY_ADDRESS and remote == TRUSTED_PROXY_ADDRESS:
            proto = environ.pop("HTTP_X_FORWARDED_PROTO", "")
            client = environ.pop("HTTP_X_REAL_IP", "")
            if proto == "https":
                environ["wsgi.url_scheme"] = "https"
            try:
                ipaddress.ip_address(client)
                environ["REMOTE_ADDR"] = client
            except ValueError:
                pass
        else:
            environ.pop("HTTP_X_FORWARDED_PROTO", None)
            environ.pop("HTTP_X_REAL_IP", None)
        return self.wsgi_app(environ, start_response)


app.wsgi_app = TrustedLocalProxy(app.wsgi_app)


def validate_production_tls_config(host=None):
    if IS_DEVELOPMENT:
        return
    if not TLS_CERT_PATH or not TLS_KEY_PATH:
        raise RuntimeError("production requires NEXALERT_TLS_CERT and NEXALERT_TLS_KEY")
    if not TRUSTED_PROXY_ADDRESS:
        raise RuntimeError("production requires NEXALERT_TRUSTED_PROXY_ADDRESS for the local TLS proxy")
    try:
        address = ipaddress.ip_address(TRUSTED_PROXY_ADDRESS)
    except ValueError as exc:
        raise RuntimeError("trusted TLS proxy must be a literal loopback IP address") from exc
    if not address.is_loopback:
        raise RuntimeError("trusted TLS proxy address must be loopback; remote proxy trust is unsupported")
    if host is not None:
        try:
            bind_address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise RuntimeError("production app bind host must be a literal loopback IP behind the TLS proxy") from exc
        if not bind_address.is_loopback:
            raise RuntimeError("production app must bind loopback behind the TLS proxy")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(TLS_CERT_PATH, TLS_KEY_PATH)
    except (OSError, ssl.SSLError) as exc:
        raise RuntimeError("production TLS certificate or private key is invalid") from exc

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


def incident_capability_hash(value):
    """Return a digest only for a syntactically valid 256-bit bearer capability."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
        return None
    try:
        decoded = __import__("base64").urlsafe_b64decode(value + "=")
    except Exception:
        return None
    if len(decoded) != 32:
        return None
    return hashlib.sha256(decoded).hexdigest()


def _source_hash():
    address = request.remote_addr or "unknown"
    return hmac.new(app.secret_key.encode(), address.encode(), hashlib.sha256).hexdigest()


def record_security_event(event, outcome, actor_id=None, detail=""):
    try:
        db = open_db_connection()
        db.execute("INSERT INTO security_events(actor_id,event,outcome,source_hash,detail) VALUES(?,?,?,?,?)",
                   (actor_id, event, outcome, _source_hash(), str(detail)[:200]))
        db.commit(); db.close()
    except sqlite3.Error:
        logger.warning("security event persistence failed event=%s", event)


def _consume_rate_limit(scope, key, limit, window_seconds):
    now = int(RATE_LIMIT_CLOCK())
    bucket = now - (now % window_seconds)
    key_hash = hmac.new(app.secret_key.encode(), str(key).encode(), hashlib.sha256).hexdigest()
    db = open_db_connection()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO request_rate_limits(scope,key_hash,window_start,hits) VALUES(?,?,?,1) "
                   "ON CONFLICT(scope,key_hash,window_start) DO UPDATE SET hits=hits+1",
                   (scope, key_hash, bucket))
        hits = db.execute("SELECT hits FROM request_rate_limits WHERE scope=? AND key_hash=? AND window_start=?",
                          (scope, key_hash, bucket)).fetchone()[0]
        if now % 100 < 2:
            db.execute("DELETE FROM request_rate_limits WHERE window_start<?", (now - 7 * 86400,))
        db.commit()
        return hits <= limit, max(1, bucket + window_seconds - now)
    finally:
        db.close()


def _rate_limit_request():
    endpoint = request.endpoint
    policy = {
        "register": (("registration", 30, 3600, "ip"),),
        "create_sos": (("sos-ip", 300, 60, "ip"), ("sos-identity", 20, 60, "identity")),
        "fallback_sos": (("fallback-sos-ip", 300, 60, "ip"), ("fallback-sos-identity", 20, 60, "identity")),
        "operator_login": (("operator-login", 20, 60, "ip"),),
        "create_announcement": (("announcement-ip", 20, 300, "ip"), ("announcement-identity", 10, 300, "identity")),
        "send_message": (("message", 60, 60, "identity"),),
        "create_group": (("group", 10, 60, "identity"),),
        "sync_contacts": (("contacts", 30, 60, "identity"),),
        "post_environmental": (("environmental", 120, 60, "identity"),),
    }.get(endpoint, ())
    identity = authenticated_user() if any(item[3] == "identity" for item in policy) else None
    for scope, limit, window, basis in policy:
        key = (request.remote_addr or "unknown") if basis == "ip" else (
            str(identity["identity_id"]) if identity else (request.remote_addr or "unknown"))
        allowed, retry = _consume_rate_limit(scope, key, limit, window)
        if not allowed:
            response = jsonify({"error": "request rate limit exceeded; retry later"})
            response.status_code = 429
            response.headers["Retry-After"] = str(retry)
            return response
    return None


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
        "landmark,source_node_id,origin_mode,owner_identity_id,ownership_state,ownership_issuer,ownership_assertion_digest,priority,incident_capability_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (incident_id, report.get("user_id"), report["alert_type"],
         report.get("description", ""), report.get("latitude"), report.get("longitude"),
         report.get("timestamp") or report.get("reported_at") or datetime.now().isoformat(),
         report.get("landmark", ""), report.get("source_node_id"),
         report.get("origin_mode", "primary"),report.get("owner_identity_id"),
         report.get("ownership_state","legacy_unverified"),report.get("ownership_issuer"),
         report.get("ownership_assertion_digest"),report.get("priority","high"),report.get("incident_capability_hash")),
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

    def restore_controls(self):
        conn=open_db_connection()
        row=conn.execute("SELECT connected,lose_next_ack,fail_next_transmission FROM simulated_transport_config WHERE singleton=1").fetchone()
        conn.close()
        if row:
            self.connected=bool(row[0]); self.lose_next_ack=bool(row[1]); self.fail_next_transmission=bool(row[2])

    def process_queue(self, limit=100):
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
            for column,declaration in (("owner_identity_id","TEXT"),("ownership_state","TEXT NOT NULL DEFAULT 'legacy_unverified'"),("ownership_issuer","TEXT"),("ownership_assertion_digest","TEXT")):
                if column not in receiver_cols:
                    receiver.execute(f"ALTER TABLE command_centre_incidents ADD COLUMN {column} {declaration}")
            if "priority" not in receiver_cols:
                receiver.execute("ALTER TABLE command_centre_incidents ADD COLUMN priority TEXT NOT NULL DEFAULT 'high'")
            if "incident_capability_hash" not in receiver_cols:
                receiver.execute("ALTER TABLE command_centre_incidents ADD COLUMN incident_capability_hash TEXT")
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
                "SELECT incident_id FROM outbound_queue WHERE state IN ('queued','retry') "
                "AND (next_attempt_at='' OR next_attempt_at<=datetime('now')) "
                "ORDER BY priority DESC,id LIMIT ?", (max(1, min(int(limit), 500)),)
            ).fetchall()
            for row in pending:
                incident_id = row["incident_id"]
                if self.fail_next_transmission:
                    self.fail_next_transmission = False
                    conn.execute("UPDATE outbound_queue SET attempts=attempts+1,last_attempt_at=datetime('now'),state=CASE WHEN attempts+1>=max_attempts THEN 'failed' ELSE 'retry' END,next_attempt_at=datetime('now','+' || min(300,(1 << min(attempts+1,8))) || ' seconds'),failure_reason=CASE WHEN attempts+1>=max_attempts THEN 'retry limit reached' ELSE NULL END WHERE incident_id=?",(incident_id,))
                    conn.execute("INSERT INTO delivery_attempts(incident_id,outcome,detail) VALUES(?,'failed','simulated transmission failure')",(incident_id,))
                    conn.execute("UPDATE simulated_transport_config SET fail_next_transmission=0 WHERE singleton=1")
                    conn.commit()
                    continue
                conn.execute(
                    "UPDATE outbound_queue SET state='queued', attempts=attempts+1, "
                    "last_attempt_at=datetime('now'),next_attempt_at=datetime('now','+' || min(300,(1 << min(attempts+1,8))) || ' seconds'), "
                    "failure_reason=CASE WHEN attempts+1>=max_attempts THEN 'retry limit reached' ELSE NULL END WHERE incident_id=?",
                    (incident_id,),
                )
                conn.execute("INSERT INTO delivery_attempts(incident_id,outcome,detail) VALUES(?,'attempted','simulated transport send')", (incident_id,))
                alert = conn.execute(
                    "SELECT incident_id, user_id, alert_type, description, latitude, longitude, timestamp, landmark, source_node_id, origin_mode,owner_identity_id,ownership_state,ownership_issuer,ownership_assertion_digest,priority,incident_capability_hash "
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


def authenticated_user():
    """Return the database-backed identity; signed cookie claims are not roles."""
    if hasattr(g, "security_identity"):
        return g.security_identity
    user_id = session.get("user_id")
    session_version = session.get("session_version")
    expires_at = session.get("session_expires_at")
    if (isinstance(user_id, bool) or not isinstance(user_id, int) or
        not isinstance(session_version, int) or not isinstance(expires_at, (int, float)) or
        expires_at <= time.time()):
        g.security_identity = None
        return None
    user = get_db().execute(
        "SELECT id,identity_id,username,full_name,role,session_version FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if user is None or user["session_version"] != session_version:
        session.clear()
        g.security_identity = None
        return None
    g.security_identity = dict(user)
    return g.security_identity


def require_auth(*roles):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "operator" in roles and not IS_DEVELOPMENT and not request.is_secure:
                return jsonify({"error": "operator access requires the configured HTTPS proxy"}), 503
            identity = authenticated_user()
            if identity is None:
                return jsonify({"error": "authentication required"}), 401
            if roles and identity["role"] not in roles:
                return jsonify({"error": "forbidden"}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorate


@app.before_request
def enforce_csrf_for_mutations():
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if request.content_length is not None:
        path_limits = {"/api/sos": 4096, "/api/fallback/sos": 4096,
                       "/api/operator/login": 4096, "/api/announcements": 4096,
                       "/api/messages": 4096, "/api/contacts": 16384}
        if request.content_length > path_limits.get(request.path, 16384):
            return jsonify({"error": "request body is too large"}), 413
    if request.path.startswith("/api/") and request.get_data(cache=True):
        if not request.is_json:
            return jsonify({"error": "application/json content type required"}), 415
        try:
            parsed=request.get_json(silent=False)
        except RequestEntityTooLarge:
            return jsonify({"error":"request body is too large"}),413
        except BadRequest:
            return jsonify({"error": "malformed JSON request body"}), 400
        except Exception:
            return jsonify({"error": "malformed JSON request body"}), 400
        if request.path in {"/api/sos", "/api/fallback/sos"} and not isinstance(parsed,dict):
            return jsonify({"error":"request body must be a JSON object"}),400
    limited = _rate_limit_request()
    if limited is not None:
        return limited
    endpoint = request.endpoint
    if endpoint == "receive_failover_heartbeat":
        return None  # The failover controller validates its signed heartbeat.
    if endpoint == "create_sos" and secrets.compare_digest(
        request.headers.get("X-NexAlert-Internal", ""), INTERNAL_FORWARD_TOKEN
    ):
        return None
    if endpoint in {"create_sos", "fallback_sos", "incident_detail", "update_incident"} and incident_capability_hash(
        request.headers.get("X-Incident-Capability", "")
    ):
        return None
    identity = authenticated_user()
    token = session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not isinstance(token, str) or not supplied or not secrets.compare_digest(token, supplied):
        return jsonify({"error": "CSRF token missing or invalid"}), 403
    return None


def issue_session(identity):
    session.clear()
    session.permanent = True
    session["user_id"] = identity["id"]
    session["session_version"] = identity["session_version"]
    session["session_expires_at"] = time.time() + app.permanent_session_lifetime.total_seconds()
    session["csrf_token"] = secrets.token_urlsafe(32)


def public_identity(identity):
    return {"id": identity["id"], "username": identity["username"],
            "full_name": identity["full_name"], "role": identity["role"]}


def write_operator_action(db, actor_id, action, incident_id=None, from_state=None,
                          to_state=None, outcome="success"):
    db.execute("""INSERT INTO operator_actions
        (actor_id,action,target_incident_id,from_state,to_state,outcome)
        VALUES(?,?,?,?,?,?)""", (actor_id, action, incident_id, from_state, to_state, outcome))


def record_operator_rejection(db, action, incident_id=None, from_state=None, to_state=None):
    """Record a rejected consequential action by an authenticated operator."""
    actor=authenticated_user()
    if actor is not None and actor["role"]=="operator":
        write_operator_action(db,actor["id"],action,incident_id,from_state,to_state,"rejected")
        db.commit()

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
            role        TEXT    NOT NULL DEFAULT 'citizen',
            session_version INTEGER NOT NULL DEFAULT 0,
            password_hash TEXT DEFAULT NULL,
            auth_failed_attempts INTEGER NOT NULL DEFAULT 0,
            auth_locked_until TEXT DEFAULT NULL,
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
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'citizen'")
    migrate_sos_schema(conn, new_incident_id)
    migrate_security_schema(conn)
    conn.commit()
    conn.close()
    # Keep the command-centre store independently initialised. It is not a
    # view over the sender database and remains readable while the sender is
    # stopped or unavailable.
    receiver = sqlite3.connect(CC_DB_PATH)
    receiver.execute("PRAGMA journal_mode=WAL")
    receiver.executescript("""CREATE TABLE IF NOT EXISTS command_centre_incidents (
        incident_id TEXT PRIMARY KEY,user_id INTEGER,alert_type TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',latitude REAL,longitude REAL,
        landmark TEXT NOT NULL DEFAULT '',reported_at TEXT NOT NULL,
        received_at TEXT NOT NULL DEFAULT(datetime('now')),
        source_node_id TEXT,origin_mode TEXT NOT NULL DEFAULT 'primary',
        owner_identity_id TEXT,ownership_state TEXT NOT NULL DEFAULT 'legacy_unverified',
        ownership_issuer TEXT,ownership_assertion_digest TEXT,
        priority TEXT NOT NULL DEFAULT 'high',incident_capability_hash TEXT);
        CREATE TABLE IF NOT EXISTS command_centre_receipts (
        incident_id TEXT PRIMARY KEY,receipt_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS command_centre_events (
        id INTEGER PRIMARY KEY,incident_id TEXT NOT NULL,status TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT(datetime('now')));
        CREATE TABLE IF NOT EXISTS command_centre_operator_actions (
        id INTEGER PRIMARY KEY,actor_id INTEGER NOT NULL,action TEXT NOT NULL,
        incident_id TEXT,from_state TEXT,to_state TEXT,outcome TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT(datetime('now')));""")
    receiver.close()
    logger.info("Database initialised at %s", DB_PATH)
    # On process start, reconcile receiver records even when disconnected and
    # only drain queued work when the persisted link setting is connected.
    restore_transport = getattr(transport, "restore_controls", None)
    if callable(restore_transport):
        restore_transport()
    # This also migrates any legacy command-centre rows while disconnected;
    # the simulated transport does not drain the queue unless connected.
    # Delivery is performed by the separately supervised outbox worker. Startup
    # only restores transport controls; HTTP startup never drains the queue.
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
    # The host simulation provisions identities only when the primary is known
    # available. A separate physical node needs an authenticated transfer.
    if failover_controller is None or failover_controller.state in {FailoverState.PRIMARY_ACTIVE,FailoverState.BACKUP_STANDBY}:
        provision_db=sqlite3.connect(DB_PATH)
        try:
            for row in provision_db.execute("SELECT identity_id FROM users WHERE identity_id IS NOT NULL"):
                fallback_store.provision_citizen(row[0])
        finally:
            provision_db.close()

# ── REST API ──────────────────────────────────────────────────────────────────

# --- Registration ---
@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    if not isinstance(data,dict): return jsonify({"error":"request body must be a JSON object"}),400
    if "role" in data or "username" in data or "user_id" in data:
        return jsonify({"error":"identity and role are assigned by the server"}),400
    identity = authenticated_user()
    if identity is not None:
        return jsonify({"status":"existing_session","user":public_identity(identity),
                        "csrf_token":session["csrf_token"]}),200
    full_name = data.get("display_name", data.get("full_name", ""))
    if not isinstance(full_name,str): return jsonify({"error":"display_name must be text"}),400
    full_name = full_name.strip() or "Anonymous"
    if len(full_name)>100:
        return jsonify({"error":"display_name must be at most 100 characters"}),400
    username = "citizen-" + secrets.token_urlsafe(18)
    db = get_db()
    try:
        db.execute(
            "INSERT INTO users (identity_id,username, full_name, phone, role) VALUES (?, ?, ?, '', 'citizen')",
            (str(uuid.uuid4()),username, full_name)
        )
        db.commit()
        user = dict(db.execute("SELECT id,identity_id,username,full_name,role,session_version FROM users WHERE username=?", (username,)).fetchone())
        issue_session(user)
        provisioning_state={FailoverState.PRIMARY_ACTIVE,FailoverState.BACKUP_STANDBY} if failover_controller else set()
        if fallback_store is not None and (failover_controller is None or failover_controller.state in provisioning_state):
            fallback_store.provision_citizen(user["identity_id"])
        record_security_event("citizen_registration", "success", user["id"])
        return jsonify({"status":"ok","user":public_identity(user),"csrf_token":session["csrf_token"]}),201
    except Exception as e:
        logger.error("citizen onboarding failed (%s)",type(e).__name__)
        return jsonify({"error":"could not start a citizen session"}),500


@app.route("/api/csrf", methods=["GET"])
def csrf_bootstrap():
    token=session.get("csrf_token")
    if not isinstance(token,str):
        token=secrets.token_urlsafe(32);session["csrf_token"]=token
    return jsonify({"csrf_token":token})


@app.route("/api/session", methods=["GET"])
@require_auth()
def session_info():
    return jsonify({"user":public_identity(authenticated_user()),"csrf_token":session["csrf_token"]})


@app.route("/api/operator/login", methods=["POST"])
def operator_login():
    if not OPERATOR_HTTPS_ENABLED or (not IS_DEVELOPMENT and not request.is_secure):
        return jsonify({"error":"operator login requires the configured HTTPS listener"}),503
    data=request.get_json(silent=True)
    if not isinstance(data,dict):
        record_security_event("operator_login", "malformed")
        return jsonify({"error":"request body must be an object"}),400
    username=data.get("username"); password=data.get("password")
    if not isinstance(username,str) or not isinstance(password,str) or len(password)>1024:
        record_security_event("operator_login", "failed")
        return jsonify({"error":"invalid credentials"}),401
    db=get_db(); user=db.execute("SELECT * FROM users WHERE username=? AND role='operator'",(username,)).fetchone()
    encoded=user["password_hash"] if user and user["password_hash"] else DUMMY_PASSWORD_HASH
    valid=check_password_hash(encoded,password)
    if not user or not user["password_hash"] or not valid:
        record_security_event("operator_login", "failed", user["id"] if user else None)
        return jsonify({"error":"invalid credentials"}),401
    db.execute("UPDATE users SET auth_failed_attempts=0,auth_locked_until=NULL,session_version=session_version+1 WHERE id=?",(user["id"],));db.commit()
    updated=dict(db.execute("SELECT id,username,full_name,role,session_version FROM users WHERE id=?",(user["id"],)).fetchone())
    issue_session(updated)
    record_security_event("operator_login", "success", user["id"])
    return jsonify({"status":"ok","user":public_identity(updated),"csrf_token":session["csrf_token"]})


def disconnect_user_sockets(user_id):
    for sid in tuple(ACTIVE_SOCKETS.get(user_id,set())):
        try: socketio.server.disconnect(sid,namespace="/")
        except Exception: pass
        ACTIVE_SOCKET_IDENTITIES.pop(sid,None)
    ACTIVE_SOCKETS.pop(user_id,None)


def _socket_identity():
    # Socket.IO may retain Flask's request context between events. Do not trust
    # a g-cached identity: reload the user and session version for every event.
    g.pop("security_identity",None)
    return authenticated_user()


def emit_to_user(user_id,event,payload):
    """Deliver only after revalidating every recipient's current DB session."""
    _emit_to_authorized(event, payload, allowed_roles=None, user_ids={user_id})


def emit_to_operators(event,payload):
    _emit_to_authorized(event, payload, allowed_roles={"operator"})


def _disconnect_socket(sid, user_id):
    try:
        socketio.server.disconnect(sid, namespace="/")
    except Exception:
        pass
    ACTIVE_SOCKET_IDENTITIES.pop(sid, None)
    sockets = ACTIVE_SOCKETS.get(user_id)
    if sockets is not None:
        sockets.discard(sid)
        if not sockets:
            ACTIVE_SOCKETS.pop(user_id, None)


def socket_binding_is_current(sid, now=None, allowed_roles=None):
    """Check expiry, database role/version, and the socket's immutable binding."""
    binding = ACTIVE_SOCKET_IDENTITIES.get(sid)
    if not binding:
        return False
    now = SOCKET_CLOCK() if now is None else now
    if binding["expires_at"] <= now:
        _disconnect_socket(sid, binding["user_id"])
        return False
    db = sqlite3.connect(DB_PATH); db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT role,session_version FROM users WHERE id=?",(binding["user_id"],)).fetchone()
    finally:
        db.close()
    if (not row or row["role"] != binding["role"] or
        row["session_version"] != binding["session_version"]):
        _disconnect_socket(sid, binding["user_id"])
        return False
    return True


def _emit_to_authorized(event, payload, allowed_roles=None, user_ids=None):
    """Never use a room broadcast for private, operator, or public push events."""
    for user_id, sids in list(ACTIVE_SOCKETS.items()):
        if user_ids is not None and user_id not in user_ids:
            continue
        for sid in tuple(sids):
            if socket_binding_is_current(sid):
                binding=ACTIVE_SOCKET_IDENTITIES.get(sid)
                if allowed_roles is not None and (not binding or binding["role"] not in allowed_roles):
                    continue
                socketio.emit(event, payload, to=sid)


def _reap_expired_sockets(now=None):
    now = SOCKET_CLOCK() if now is None else now
    for user_id, sids in list(ACTIVE_SOCKETS.items()):
        for sid in tuple(sids):
            socket_binding_is_current(sid, now=now)


def _socket_reaper():
    while True:
        _reap_expired_sockets()
        socketio.sleep(5)


@app.route("/api/logout", methods=["POST"])
@require_auth()
def logout():
    identity=authenticated_user(); db=get_db()
    db.execute("UPDATE users SET session_version=session_version+1 WHERE id=?",(identity["id"],));db.commit()
    disconnect_user_sockets(identity["id"])
    if identity["role"] == "operator":
        record_security_event("operator_logout", "success", identity["id"])
    session.clear();g.security_identity=None
    return jsonify({"status":"logged_out"})


# --- User list ---
@app.route("/api/users", methods=["GET"])
@require_auth("operator")
def get_users():
    db = get_db()
    rows = db.execute("SELECT id, username, full_name, role, is_online,last_seen FROM users").fetchall()
    return jsonify([dict(r) for r in rows])


# --- Location update ---
@app.route("/api/location", methods=["POST"])
@require_auth("citizen")
def update_location():
    data = request.get_json(silent=True) or {}
    if any(k in data for k in ("username","user_id")):
        return jsonify({"error":"location owner comes from the authenticated session"}),400
    try:
        lat = float(data.get("latitude", 0))
        lon = float(data.get("longitude", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "latitude and longitude must be numbers"}), 400

    db = get_db()
    if not (-90<=lat<=90 and -180<=lon<=180):return jsonify({"error":"coordinates out of range"}),400
    db.execute("UPDATE users SET latitude=?, longitude=?, last_seen=datetime('now') WHERE id=?",
               (lat,lon,authenticated_user()["id"]))
    db.commit()
    return jsonify({"status": "ok"})


# --- SOS ---
@app.route("/api/sos", methods=["POST"])
def create_sos():
    data = request.get_json(silent=True)
    if not isinstance(data, dict): return jsonify({"error":"request body must be a JSON object"}),400
    internal = secrets.compare_digest(request.headers.get("X-NexAlert-Internal", ""), INTERNAL_FORWARD_TOKEN)
    identity = authenticated_user()
    if not internal and identity is not None and identity["role"]!="citizen":return jsonify({"error":"citizen identity required"}),403
    capability_hash=(data.get("incident_capability_hash") if internal else
                     incident_capability_hash(request.headers.get("X-Incident-Capability", "")))
    if capability_hash is not None and (not isinstance(capability_hash,str) or not re.fullmatch(r"[0-9a-f]{64}",capability_hash)):
        return jsonify({"error":"invalid incident capability commitment"}),400
    if not internal and identity is None and capability_hash is None:return jsonify({"error":"authentication or incident capability required"}),401
    if capability_hash is None:return jsonify({"error":"incident capability is required"}),400
    if any(k in data for k in ("user_id","owner_id")) or (not internal and any(k in data for k in ("owner_identity_id","ownership_state","ownership_issuer","ownership_assertion","ownership_assertion_digest","incident_capability_hash"))):
        return jsonify({"error":"incident owner comes from the authenticated session"}),400
    is_fallback=bool(internal and data.get("origin_mode")=="fallback")
    owner_identity_id=identity["identity_id"] if identity and not internal else None
    ownership_state="verified" if identity and not internal else "unclaimed"
    ownership_issuer=None
    ownership_digest=None
    if is_fallback:
        owner_identity_id=data.get("owner_identity_id")
        assertion=data.get("ownership_assertion")
        ownership_issuer=data.get("ownership_issuer")
        ownership_state="unclaimed"
        if owner_identity_id or assertion or ownership_issuer:
            if not verify_ownership_assertion(
            OWNERSHIP_ASSERTION_KEY,assertion,data.get("incident_id"),owner_identity_id,
            ownership_issuer,PRIMARY_NODE_ID,data.get("source_node_id"),capability_hash):
                return jsonify({"error":"fallback ownership assertion is invalid or out of scope"}),403
            trusted=get_db().execute("SELECT id FROM users WHERE identity_id=? AND role='citizen'",(owner_identity_id,)).fetchone()
            if trusted:
                ownership_state="verified"
                ownership_digest=__import__("hashlib").sha256(assertion.encode()).hexdigest()
            else:
                owner_identity_id=None
    username = identity["username"] if identity else ("fallback-" + str(data.get("source_node_id") or "unclaimed"))
    alert_type  = data.get("alert_type", "other")
    raw_description=data.get("description", "")
    if raw_description is not None and not isinstance(raw_description,str): return jsonify({"error":"description must be text"}),400
    description = (raw_description or "").strip()
    priority = data.get("priority", "critical")
    priority_order = {"critical": 4, "high": 3, "moderate": 2, "routine": 1}
    if not isinstance(priority, str) or priority not in priority_order:
        return jsonify({"error": "priority must be critical, high, moderate or routine"}), 400
    raw_incident_id=data.get("incident_id", "")
    if raw_incident_id and not isinstance(raw_incident_id,str): return jsonify({"error":"incident_id must be text"}),400
    incident_id = (raw_incident_id or "").strip() or new_incident_id()
    raw_key=request.headers.get("Idempotency-Key") or data.get("idempotency_key") or incident_id
    if not isinstance(raw_key,str): return jsonify({"error":"idempotency_key must be text"}),400
    idempotency_key = raw_key.strip()
    landmark = data.get("landmark", "")
    source_node_id = None
    origin_mode = "primary"
    if data.get("origin_mode") == "fallback" and internal:
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
    by_id=db.execute("SELECT * FROM alerts WHERE incident_id=?",(incident_id,)).fetchone()
    by_key=db.execute("SELECT * FROM alerts WHERE idempotency_key=?",(idempotency_key,)).fetchone()
    if by_id and by_key and by_id["incident_id"]!=by_key["incident_id"]:
        return jsonify({"error":"idempotency key and incident ID refer to different reports"}),409
    existing=by_id or by_key
    user = None
    if identity and not internal:
        user=db.execute("SELECT id,username FROM users WHERE id=?",(identity["id"],)).fetchone()
    elif ownership_state=="verified" and owner_identity_id:
        user=db.execute("SELECT id,username FROM users WHERE identity_id=? AND role='citizen'",(owner_identity_id,)).fetchone()
    if user is None and not existing:
        unclaimed_name="nexalert-unclaimed"
        db.execute("INSERT OR IGNORE INTO users(username,full_name,phone,role) VALUES(?,?,?,'citizen')",
                   (unclaimed_name,"Unclaimed reports",""))
        user=db.execute("SELECT id,username FROM users WHERE username=?",(unclaimed_name,)).fetchone()
    elif user is None and existing:
        user=db.execute("SELECT id,username FROM users WHERE id=?",(existing["user_id"],)).fetchone()
    if existing:
        if existing["incident_capability_hash"] != capability_hash:
            record_security_event("incident_capability_rejected","conflict",identity["id"] if identity else None,incident_id)
            return jsonify({"error":"incident capability does not match this report"}),404
        if (is_fallback and ownership_state=="verified" and existing["ownership_state"]=="verified" and
            existing["owner_identity_id"]!=owner_identity_id):
            return jsonify({"error":"incident_id has conflicting verified ownership"}),409
        if is_fallback and existing["ownership_state"]=="verified" and ownership_state!="verified":
            return jsonify({"error":"unverified retry cannot replace established ownership"}),409
        if is_fallback and existing["ownership_state"]=="unclaimed" and ownership_state=="verified":
            db.execute("UPDATE alerts SET user_id=?,owner_identity_id=?,ownership_state='verified',ownership_issuer=?,ownership_assertion_digest=? WHERE incident_id=?",
                       (user["id"],owner_identity_id,ownership_issuer,ownership_digest,incident_id));db.commit()
            existing=db.execute("SELECT * FROM alerts WHERE incident_id=?",(incident_id,)).fetchone()
            alert_row=existing;alert_id=existing["id"]
        alert_id = existing["id"]
        incident_id = existing["incident_id"]
        alert_row = existing
    else:
        queued = db.execute("SELECT count(*) FROM outbound_queue WHERE state IN ('queued','retry')").fetchone()[0]
        if queued >= 5000:
            return jsonify({"error": "SOS queue is full; keep this report on this device and retry"}), 503
        cur = db.execute(
            "INSERT INTO alerts (user_id, alert_type, description, latitude, longitude, incident_id, delivery_status, lifecycle_status, landmark, idempotency_key, source_node_id, origin_mode,ownership_state,owner_identity_id,ownership_issuer,ownership_assertion_digest,priority,incident_capability_hash) "
            "VALUES (?,?,?,?,?,?, 'saved_locally','saved_locally',?,?,?,?,?,?,?,?,?,?)",
            (user["id"], alert_type, description, lat, lon, incident_id, landmark, idempotency_key, source_node_id, origin_mode,ownership_state,owner_identity_id,ownership_issuer,ownership_digest,priority,capability_hash)
        )
        alert_id = cur.lastrowid
        record_incident_event(db, incident_id, "saved_locally", "validated SOS persisted locally")
        db.execute("INSERT INTO outbound_queue (incident_id, state,priority) VALUES (?, 'queued',?)", (incident_id,priority_order[priority]))
        db.execute("UPDATE alerts SET delivery_status='queued' WHERE id=?", (alert_id,))
        record_incident_event(db, incident_id, "queued", "queued for transport")
        db.commit()
        alert_row = db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()

    # The transaction above completes before forwarding is attempted.
    alert_row = db.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()

    alert_data = {
        "id":          alert_id,
        "incident_id": alert_row["incident_id"],
        "username":    user["username"] if user else username,
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
        "priority": alert_row["priority"],
    }
    # Full SOS details are restricted to authenticated operator sockets; the
    # submitting citizen receives only status for their own incident.
    emit_to_operators("new_alert", alert_data)
    if not internal and identity:
        emit_to_user(identity["id"],"sos_status", {"incident_id":incident_id,
                                      "delivery_status":alert_row["delivery_status"],
                                      "lifecycle_status":alert_row["lifecycle_status"]})
    logger.info("SOS persisted: incident=%s type=%s", incident_id, alert_type)
    return jsonify({"status": "ok", "created": existing is None, "alert": alert_data}), 201 if existing is None else 200


# --- Active alerts ---
@app.route("/api/alerts", methods=["GET"])
@require_auth("operator")
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
@require_auth("operator")
def resolve_alert(alert_id):
    db = get_db()
    row=db.execute("SELECT incident_id,lifecycle_status,delivery_status FROM alerts WHERE id=?",(alert_id,)).fetchone()
    if not row:
        record_operator_rejection(db,"resolve")
        return jsonify({"error":"Incident not found"}),404
    if row["delivery_status"]!="received_by_command_centre" or row["lifecycle_status"]!="arrived":
        record_operator_rejection(db,"resolve",row["incident_id"],row["lifecycle_status"],"resolved")
        return jsonify({"error":"incident must be received and arrived before resolution"}),409
    db.execute("UPDATE alerts SET lifecycle_status='resolved',status='resolved',resolved_at=datetime('now') WHERE id=?",(alert_id,))
    record_incident_event(db,row["incident_id"],"resolved","operator recorded lifecycle event")
    write_operator_action(db,authenticated_user()["id"],"resolve",row["incident_id"],row["lifecycle_status"],"resolved")
    db.commit()
    emit_to_operators("alert_resolved", {"incident_id":row["incident_id"]})
    return jsonify({"status": "ok"})


@app.route("/api/incidents/<incident_id>/assign", methods=["POST"])
@require_auth("operator")
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
        record_operator_rejection(db,"assign_responder",incident_id)
        return jsonify({"error": "Incident not found"}), 404
    if alert["delivery_status"] != "received_by_command_centre":
        record_operator_rejection(db,"assign_responder",incident_id,alert["lifecycle_status"],"responder_assigned")
        return jsonify({"error":"incident has not been received by command centre"}),409
    if alert["lifecycle_status"] == "resolved":
        record_operator_rejection(db,"assign_responder",incident_id,"resolved","responder_assigned")
        return jsonify({"error":"resolved incidents cannot be reassigned"}),409
    next_lifecycle="responder_assigned" if alert["lifecycle_status"]=="received_by_command_centre" else alert["lifecycle_status"]
    event="responder_assigned" if next_lifecycle=="responder_assigned" else "responder_reassigned"
    db.execute("UPDATE alerts SET lifecycle_status=?, responder_assignment=? WHERE incident_id=?",
               (next_lifecycle,responder, incident_id))
    record_incident_event(db, incident_id, event, "operator assignment recorded")
    write_operator_action(db,authenticated_user()["id"],"assign_responder",incident_id,
                          alert["lifecycle_status"],next_lifecycle)
    db.execute("INSERT INTO responder_assignments(incident_id,responder) VALUES(?,?) ON CONFLICT(incident_id) DO UPDATE SET responder=excluded.responder,assigned_at=datetime('now')", (incident_id, responder))
    db.commit()
    emit_to_operators("incident_updated", {"incident_id": incident_id})
    return jsonify({"status": "ok", "incident_id": incident_id, "lifecycle_status": next_lifecycle})


LIFECYCLE_TRANSITIONS = {
    "received_by_command_centre": {"responder_assigned"},
    "responder_assigned": {"dispatched"}, "dispatched": {"arrived"},
    "arrived": {"resolved"},
}

@app.route("/api/incidents", methods=["GET"])
@require_auth()
def list_incidents():
    db = get_db()
    identity=authenticated_user()
    filters=[]; params=[]
    if identity["role"]!="operator":
        filters.append("a.user_id=?");params.append(identity["id"])
    else:
        filters.append("a.delivery_status='received_by_command_centre'")
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
    identity=authenticated_user()
    capability_hash=incident_capability_hash(request.headers.get("X-Incident-Capability", ""))
    if identity is None and capability_hash is None:return jsonify({"error":"authentication or incident capability required"}),401
    query="SELECT * FROM alerts WHERE incident_id=?";params=[incident_id]
    if not identity or identity["role"]!="operator":
        query+=" AND incident_capability_hash=?";params.append(capability_hash or "")
        if identity:
            query=query.replace("incident_capability_hash=?", "(incident_capability_hash=? OR user_id=?")
            params.append(identity["id"]);query+=")"
    row=get_db().execute(query,params).fetchone()
    if not row: return jsonify({"error":"Incident not found"}),404
    incident=dict(row);incident.pop("incident_capability_hash",None)
    return jsonify({"incident":incident,"history":[dict(e) for e in get_db().execute("SELECT status,detail,created_at FROM incident_events WHERE incident_id=? ORDER BY id",(incident_id,))]})

@app.route("/api/incidents/<incident_id>/updates", methods=["POST"])
def update_incident(incident_id):
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict): return jsonify({"error":"request body must be a JSON object"}),400
    description=data.get('description')
    landmark=data.get('landmark')
    if description is not None and (not isinstance(description,str) or len(description)>1000): return jsonify({"error":"description must be up to 1000 characters"}),400
    if landmark is not None and (not isinstance(landmark,str) or len(landmark)>250): return jsonify({"error":"landmark must be up to 250 characters"}),400
    identity=authenticated_user()
    capability_hash=incident_capability_hash(request.headers.get("X-Incident-Capability", ""))
    if identity is None and capability_hash is None:return jsonify({"error":"authentication or incident capability required"}),401
    if identity is not None and identity["role"]!="citizen":return jsonify({"error":"citizen identity required"}),403
    db=get_db(); row=db.execute("SELECT id FROM alerts WHERE incident_id=? AND (incident_capability_hash=? OR user_id=?)",
                                (incident_id,capability_hash or "",identity["id"] if identity else -1)).fetchone()
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
@require_auth("operator")
def advance_lifecycle(incident_id):
    data=request.get_json(silent=True) or {}; target=data.get('status')
    db=get_db(); row=db.execute("SELECT lifecycle_status,delivery_status FROM alerts WHERE incident_id=?",(incident_id,)).fetchone()
    if not row:
        record_operator_rejection(db,"lifecycle",incident_id,None,target if isinstance(target,str) else None)
        return jsonify({"error":"Incident not found"}),404
    if row['delivery_status']!='received_by_command_centre':
        record_operator_rejection(db,"lifecycle",incident_id,row['lifecycle_status'],target if isinstance(target,str) else None)
        return jsonify({"error":"incident has not been received by command centre"}),409
    if not isinstance(target,str) or target not in LIFECYCLE_TRANSITIONS.get(row['lifecycle_status'],set()):
        record_operator_rejection(db,"lifecycle",incident_id,row['lifecycle_status'],target if isinstance(target,str) else None)
        return jsonify({"error":"invalid lifecycle transition"}),409
    db.execute("UPDATE alerts SET lifecycle_status=?,status=?,resolved_at=CASE WHEN ?='resolved' THEN datetime('now') ELSE resolved_at END WHERE incident_id=?",(target,'resolved' if target=='resolved' else 'active',target,incident_id))
    record_incident_event(db,incident_id,target,"operator recorded lifecycle event")
    write_operator_action(db,authenticated_user()["id"],"lifecycle",incident_id,row["lifecycle_status"],target)
    db.commit()
    emit_to_operators("incident_updated",{"incident_id":incident_id})
    return jsonify({"status":"ok","incident_id":incident_id,"lifecycle_status":target})


@app.route("/api/transport/simulated", methods=["POST"])
@require_auth("operator")
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
@require_auth("citizen")
def send_message():
    data=request.get_json(silent=True)
    if not isinstance(data,dict):return jsonify({"error":"request body must be an object"}),400
    if any(k in data for k in ("sender","sender_id","user_id","group_id")) or data.get("is_broadcast"):
        return jsonify({"error":"sender identity is session-bound and citizens cannot broadcast"}),400
    recipient_un=data.get("recipient");content=data.get("content")
    if not isinstance(recipient_un,str) or not recipient_un or not isinstance(content,str) or not content.strip() or len(content)>500:
        return jsonify({"error":"recipient and message content are required"}),400
    db=get_db();identity=authenticated_user()
    recipient=db.execute("SELECT id,username FROM users WHERE username=? AND role='citizen'",(recipient_un,)).fetchone()
    if not recipient:return jsonify({"error":"recipient unavailable"}),404
    if recipient["id"]==identity["id"]:return jsonify({"error":"cannot message this recipient"}),400
    cur = db.execute(
        "INSERT INTO messages (sender_id, recipient_id, content, is_broadcast, group_id) VALUES (?,?,?,?,?)",
        (identity["id"],recipient["id"],content.strip(),0,None)
    )
    db.commit()
    msg = {
        "id":cur.lastrowid,"sender":identity["username"],"recipient":recipient["username"],
        "content":content.strip(),"is_broadcast":False,
        "timestamp":   datetime.now().isoformat(),
    }
    emit_to_user(recipient["id"],"new_message",msg)
    emit_to_user(identity["id"],"new_message",msg)
    return jsonify({"status": "ok", "message": msg}), 201


@app.route("/api/messages", methods=["GET"])
@require_auth("citizen")
def get_messages():
    db=get_db();user_id=authenticated_user()["id"]
    rows = db.execute("""
        SELECT m.id, su.username as sender, ru.username as recipient,
               m.content, m.is_broadcast, m.timestamp, m.is_read
        FROM messages m
        JOIN users su ON m.sender_id = su.id
        LEFT JOIN users ru ON m.recipient_id = ru.id
        WHERE m.is_broadcast = 0 AND (m.sender_id = ? OR m.recipient_id = ?)
        ORDER BY m.timestamp DESC LIMIT 100
    """, (user_id,user_id)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/announcements", methods=["GET"])
def get_announcements():
    rows = get_db().execute(
        "SELECT id,category,content,created_at FROM system_announcements ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.route("/api/announcements", methods=["POST"])
@require_auth("operator")
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
    actor=authenticated_user()
    cur = db.execute("INSERT INTO system_announcements(category,content,created_by) VALUES(?,?,?)",
                     (category, content.strip(),actor["id"]))
    write_operator_action(db,actor["id"],"publish_announcement",None,None,category)
    db.commit()
    announcement = dict(db.execute(
        "SELECT id,category,content,created_at FROM system_announcements WHERE id=?",
        (cur.lastrowid,),
    ).fetchone())
    _emit_to_authorized("system_announcement", announcement, allowed_roles={"citizen"})
    return jsonify({"status": "persisted_local_broadcast", "announcement": announcement,
                    "delivery": "local_application_only"}), 201


@app.route("/api/dashboard/summary", methods=["GET"])
@require_auth("operator")
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
@require_auth("operator")
def failover_status():
    return jsonify(_failover_snapshot())


@app.route("/api/operator/actions", methods=["GET"])
@require_auth("operator")
def operator_actions():
    rows=get_db().execute("""SELECT id,actor_id,action,target_incident_id,from_state,to_state,outcome,created_at
        FROM operator_actions ORDER BY id DESC LIMIT 200""").fetchall()
    return jsonify([dict(row) for row in rows])


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
    if any(key in report for key in ("user_id","owner_id","username","role","owner_identity_id","ownership_state","ownership_issuer","ownership_assertion","incident_capability_hash")):
        return jsonify({"error":"fallback SOS identity comes from the authenticated session"}),400
    report.setdefault("incident_id", new_incident_id())
    capability_hash=incident_capability_hash(request.headers.get("X-Incident-Capability", ""))
    if capability_hash is None:
        return jsonify({"error":"a valid incident capability is required"}),400
    report["incident_capability_hash"]=capability_hash
    citizen=authenticated_user()
    identity_id=citizen["identity_id"] if citizen else None
    existing=None
    with fallback_store.connection() as fallback_db:
        existing=fallback_db.execute("SELECT * FROM fallback_incidents WHERE incident_id=?",(report["incident_id"],)).fetchone()
    capability_matches=bool(existing and existing["incident_capability_hash"] and
        hmac.compare_digest(existing["incident_capability_hash"],capability_hash))
    if capability_matches:
        # A retry with the incident capability reuses the stored trust decision;
        # session loss or a new login must not downgrade or reassign ownership.
        for field in ("owner_identity_id","ownership_state","ownership_issuer","ownership_assertion","ownership_assertion_digest"):
            report[field]=existing[field]
    else:
        report.update({"owner_identity_id":None,"ownership_state":"unclaimed",
                       "ownership_issuer":None,"ownership_assertion":None,
                       "ownership_assertion_digest":None})
    provisioned=bool(identity_id and fallback_store.trusts_citizen(identity_id))
    if not capability_matches and provisioned:
        report.update({"owner_identity_id":identity_id,"ownership_state":"verified",
            "ownership_issuer":FALLBACK_NODE_ID})
        report["ownership_assertion"]=sign_ownership_assertion(
            OWNERSHIP_ASSERTION_KEY,report["incident_id"],identity_id,FALLBACK_NODE_ID,
            PRIMARY_NODE_ID,FALLBACK_NODE_ID,capability_hash)
        report["ownership_assertion_digest"]=__import__("hashlib").sha256(report["ownership_assertion"].encode()).hexdigest()
    elif not capability_matches:
        report.update({"owner_identity_id":None,"ownership_state":"unclaimed",
                       "ownership_issuer":None,"ownership_assertion":None,
                       "ownership_assertion_digest":None})
    # Node-local IDs remain local metadata and are never included in the sync payload.
    report["citizen_user_id"]=(existing["citizen_user_id"] if capability_matches else (citizen["id"] if citizen else None))
    try:
        accepted, created = fallback_store.accept(report, FALLBACK_NODE_ID, time.time())
    except FailoverError as exc:
        return jsonify({"error": str(exc)}), 400
    emit_to_operators("fallback_sos_accepted", {"incident_id": accepted["incident_id"], "origin_mode": "fallback"})
    receipt = accepted.get("command_receipt_id")
    delivery = "received_by_command_centre" if receipt else "queued_on_backup"
    public_incident={key:accepted.get(key) for key in ("incident_id","source_node_id","alert_type","created_at","local_receipt_id","outbox_state","command_receipt_id")}
    return jsonify({"status": "accepted_locally", "created": created,
                    "delivery_status": delivery, "command_centre_receipt": receipt,
                    "incident": public_incident}), 201 if created else 200


@app.route("/api/fallback/incidents", methods=["GET"])
@require_auth("citizen")
def fallback_incidents_for_citizen():
    if fallback_store is None:
        return jsonify([])
    identity=authenticated_user()
    capability_hash=incident_capability_hash(request.headers.get("X-Incident-Capability", ""))
    if identity is None and capability_hash is None:return jsonify({"error":"authentication or incident capability required"}),401
    rows=fallback_store.incidents_for_user(identity["identity_id"]) if identity else []
    return jsonify(rows)


@app.route("/api/fallback/incidents/<incident_id>", methods=["GET"])
def fallback_incident_detail(incident_id):
    if fallback_store is None:return jsonify({"error":"Incident not found"}),404
    capability_hash=incident_capability_hash(request.headers.get("X-Incident-Capability", ""))
    identity=authenticated_user()
    with fallback_store.connection() as db:
        row=db.execute("SELECT * FROM fallback_incidents WHERE incident_id=?",(incident_id,)).fetchone()
        if not row:return jsonify({"error":"Incident not found"}),404
        allowed=(capability_hash is not None and row["incident_capability_hash"]==capability_hash) or (
            identity is not None and identity["identity_id"]==row["owner_identity_id"] and row["ownership_state"]=="verified")
        if not allowed:return jsonify({"error":"Incident not found"}),404
        outbox=db.execute("SELECT state,command_receipt_id FROM fallback_outbox WHERE incident_id=?",(incident_id,)).fetchone()
        return jsonify({"incident_id":incident_id,"alert_type":row["alert_type"],"description":row["description"],
                        "latitude":row["latitude"],"longitude":row["longitude"],"landmark":row["landmark"],
                        "delivery_status":"received_by_command_centre" if outbox["state"]=="received" else "queued_on_backup",
                        "receipt_id":outbox["command_receipt_id"]})


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
    payload = {"incident_id": report["incident_id"],
        "idempotency_key": report["incident_id"], "alert_type": report["alert_type"],
        "description": report["description"], "latitude": report["latitude"],
        "longitude": report["longitude"], "landmark": report["landmark"],
        "origin_mode": "fallback", "source_node_id": report["source_node_id"],
        "owner_identity_id":report.get("owner_identity_id"),
        "ownership_state":report.get("ownership_state","unclaimed"),
        "ownership_issuer":report.get("ownership_issuer"),
        "ownership_assertion":report.get("ownership_assertion"),
        "incident_capability_hash":report.get("incident_capability_hash")}
    with app.test_client() as client:
        response = client.post("/api/sos", json=payload, headers={"X-NexAlert-Internal": INTERNAL_FORWARD_TOKEN})
        body = response.get_json(silent=True) or {}
    if response.status_code not in (200, 201):
        return ForwardResult(False, detail=body.get("error", "primary rejected fallback report"))
    # Failover synchronisation is an operator-controlled recovery operation.
    # After the primary has durably accepted the report, its worker step commits
    # the independent command-centre record before the fallback outbox is acked.
    retry_db = open_db_connection()
    retry_db.execute("UPDATE outbound_queue SET next_attempt_at='' WHERE incident_id=? AND state IN ('queued','retry')",
                     (report["incident_id"],))
    retry_db.commit()
    retry_db.close()
    transport.process_queue(limit=1)
    sender = open_db_connection()
    try:
        saved = sender.execute("SELECT delivery_status,receipt_id FROM alerts WHERE incident_id=?",
                               (report["incident_id"],)).fetchone()
        if saved:
            body.setdefault("alert", {}).update(dict(saved))
    finally:
        sender.close()
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
@require_auth("operator")
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
@require_auth("citizen")
def sync_contacts():
    data     = request.get_json(silent=True) or {}
    contacts = data.get("contacts", [])
    if "username" in data or "user_id" in data:return jsonify({"error":"contact owner comes from the authenticated session"}),400
    if not isinstance(contacts,list) or len(contacts)>200:return jsonify({"error":"contacts must be a list of at most 200 entries"}),400

    db = get_db()
    user_id=authenticated_user()["id"]

    # BUG FIX #5: use INSERT OR IGNORE to avoid duplicate inserts
    for c in contacts:
        if not isinstance(c,dict):continue
        name  = (c.get("name") or "").strip()
        phone = (c.get("phone") or "").strip()
        if not name or not phone:
            continue
        # Phone-number matching disclosed account existence to callers. Keep
        # address-book data private and never resolve it against user records.
        on_net = 0
        net_uid = None
        db.execute(
            """INSERT OR IGNORE INTO contacts (owner_id, contact_name, contact_phone, on_network, network_user_id)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, name, phone, on_net, net_uid)
        )
    db.commit()

    rows = db.execute(
        "SELECT id,contact_name,contact_phone,0 AS on_network FROM contacts WHERE owner_id=?", (user_id,)
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
@require_auth("operator")
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
    emit_to_operators("env_update", {"temperature": temp, "humidity": hum,
                                  "air_quality": aq, "battery_v": batt, "solar_v": solar})
    return jsonify({"status": "ok"}), 201


# --- Groups ---
@app.route("/api/groups", methods=["POST"])
@require_auth("citizen")
def create_group():
    data     = request.get_json(silent=True) or {}
    name     = (data.get("name") or "").strip()
    members  = data.get("members", [])
    if "username" in data or "user_id" in data:return jsonify({"error":"group owner comes from the authenticated session"}),400

    if not name:
        return jsonify({"error": "group name required"}), 400
    if len(name)>100 or not isinstance(members,list) or members:
        return jsonify({"error":"group member invitations are not enabled; groups start with their creator only"}),400

    db = get_db()
    creator_id=authenticated_user()["id"]

    try:
        cur = db.execute(
            "INSERT INTO broadcast_groups (name, creator_id) VALUES (?, ?)",
            (name, creator_id)
        )
        group_id = cur.lastrowid
        # always add creator
        db.execute("INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?,?)",
                   (group_id, creator_id))
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
    identity=authenticated_user()
    if identity is None or identity["role"]!="operator":return redirect("/operator/login",code=302)
    return render_template("dashboard.html", alert_types=ALERT_TYPES)

@app.route("/operator/login")
def operator_login_page():
    identity=authenticated_user()
    if identity and identity["role"]=="operator":return redirect("/dashboard",code=302)
    return render_template("operator_login.html")

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


@app.after_request
def apply_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options","nosniff")
    response.headers.setdefault("Referrer-Policy","no-referrer")
    response.headers.setdefault("X-Frame-Options","DENY")
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"]="no-store"
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security","max-age=31536000")
    return response

# ── WebSocket events ──────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    global SOCKET_REAPER_STARTED
    identity=_socket_identity()
    if identity is None:return False
    expires_at=session.get("session_expires_at")
    if not isinstance(expires_at,(int,float)) or expires_at<=time.time():return False
    user_id=identity["id"];sid=request.sid
    join_room(f"user:{user_id}")
    if identity["role"]=="operator":join_room("operators")
    ACTIVE_SOCKETS.setdefault(user_id,set()).add(sid)
    ACTIVE_SOCKET_IDENTITIES[sid]={"user_id":user_id,"session_version":identity["session_version"],
                                  "role":identity["role"],"expires_at":float(expires_at)}
    if not SOCKET_REAPER_STARTED and not app.testing:
        socketio.start_background_task(_socket_reaper)
        SOCKET_REAPER_STARTED=True
    db=get_db();db.execute("UPDATE users SET is_online=1,last_seen=datetime('now') WHERE id=?",(user_id,));db.commit()
    emit_to_operators("user_online",{"username":identity["username"]})
    logger.info("Authenticated socket connected: user_id=%s",user_id)


@socketio.on("disconnect")
def on_disconnect():
    user_id=session.get("user_id")
    ACTIVE_SOCKET_IDENTITIES.pop(request.sid,None)
    if isinstance(user_id,int):
        ACTIVE_SOCKETS.get(user_id,set()).discard(request.sid)
        if not ACTIVE_SOCKETS.get(user_id):
            ACTIVE_SOCKETS.pop(user_id,None)
            db=get_db();db.execute("UPDATE users SET is_online=0 WHERE id=?",(user_id,));db.commit()
            for key,call in list(ACTIVE_CALLS.items()):
                if user_id in {call["caller_id"],call["callee_id"]}:
                    ACTIVE_CALLS.pop(key,None)
                    other=call["callee_id"] if call["caller_id"]==user_id else call["caller_id"]
                    emit_to_user(other,"call:ended",{"from":str(user_id)})
            emit_to_operators("user_offline",{"user_id":user_id})


@socketio.on("join")
def on_join(data):
    """Rooms are assigned at connect and cannot be selected by clients."""
    if _socket_identity() is None:disconnect()


@socketio.on("typing")
def on_typing(data):
    sender=_socket_identity();recipient=data.get("recipient") if isinstance(data,dict) else None
    if sender is None:disconnect();return
    if not isinstance(recipient,str):return
    peer=get_db().execute("SELECT id FROM users WHERE username=? AND role='citizen'",(recipient,)).fetchone()
    if peer and peer["id"] in ACTIVE_SOCKETS:
        emit_to_user(peer["id"],"typing",{"from":sender["username"]})


def _call_identity(data):
    """Resolve call participants from live, authenticated server identities."""
    sender=_socket_identity()
    target = data.get("to") if isinstance(data, dict) else None
    if sender is None or sender["role"]!="citizen" or not isinstance(target,str) or not target or target==sender["username"]:
        return None,None
    peer=get_db().execute("SELECT id,username FROM users WHERE username=? AND role='citizen'",(target,)).fetchone()
    if peer is None or peer["id"] not in ACTIVE_SOCKETS:return sender,None
    return sender,dict(peer)


@socketio.on("call:invite")
def on_call_invite(data):
    sender, target = _call_identity(data)
    if not sender or not target:
        emit("call:error", {"message": "Call requires a registered session and an online participant"})
        return
    key = _call_key(sender["username"], target["username"])
    if key in ACTIVE_CALLS:
        emit("call:error", {"message": "Participant is already in a call"})
        return
    ACTIVE_CALLS[key] = {"caller":sender["username"],"callee":target["username"],"caller_id":sender["id"],"callee_id":target["id"],"state":"ringing"}
    emit_to_user(target["id"],"call:invite", {"from": sender["username"]})


@socketio.on("call:accept")
def on_call_accept(data):
    sender, target = _call_identity(data)
    call = ACTIVE_CALLS.get(_call_key(sender["username"], target["username"])) if sender and target else None
    if not call or call["caller"] != target["username"] or call["callee"] != sender["username"] or call["state"] != "ringing":
        emit("call:error", {"message": "Call participant is unavailable"})
        return
    call["state"] = "active"
    emit_to_user(target["id"],"call:accepted", {"from": sender["username"]})


@socketio.on("call:decline")
def on_call_decline(data):
    sender, target = _call_identity(data)
    key = _call_key(sender["username"], target["username"]) if sender and target else None
    call = ACTIVE_CALLS.get(key) if key else None
    if call and call["caller"] == target["username"] and call["callee"] == sender["username"] and call["state"] == "ringing":
        ACTIVE_CALLS.pop(key, None)
        emit_to_user(target["id"],"call:declined", {"from": sender["username"]})


@socketio.on("call:signal")
def on_call_signal(data):
    sender, target = _call_identity(data)
    kind = data.get("kind") if isinstance(data, dict) else None
    signal = data.get("data") if isinstance(data, dict) else None
    call = ACTIVE_CALLS.get(_call_key(sender["username"], target["username"])) if sender and target else None
    if not call or call["state"] != "active" or kind not in {"offer", "answer", "ice"} or not isinstance(signal, dict):
        emit("call:error", {"message": "Invalid call signal or unavailable participant"})
        return
    emit_to_user(target["id"],"call:signal", {"from": sender["username"], "kind": kind, "data": signal})


@socketio.on("call:end")
def on_call_end(data):
    sender, target = _call_identity(data)
    key = _call_key(sender["username"], target["username"]) if sender and target else None
    if key and key in ACTIVE_CALLS:
        ACTIVE_CALLS.pop(key, None)
        emit_to_user(target["id"],"call:ended", {"from": sender["username"]})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Socket identity/session state is process local. Enforce the supported
    # one-web-process topology for this SQLite deployment rather than allowing
    # accidental multi-worker execution on the same database.
    app_lock_path = os.environ.get("NEXALERT_APP_LOCK", DB_PATH + ".app.lock")
    app_lock = open(app_lock_path, "a+")
    if os.name == "nt":
        import msvcrt
        app_lock.seek(0, os.SEEK_END)
        if app_lock.tell() == 0:
            app_lock.write("0"); app_lock.flush()
        app_lock.seek(0)
        try:
            msvcrt.locking(app_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise SystemExit("another NexAlert web process holds the database lock") from exc
    else:
        import fcntl
        try:
            fcntl.flock(app_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("another NexAlert web process holds the database lock") from exc
    init_db()
    host=os.environ.get("NEXALERT_HOST", "127.0.0.1" if not IS_DEVELOPMENT else "0.0.0.0")
    port=int(os.environ.get("NEXALERT_PORT","5000"))
    validate_production_tls_config(host)
    if not IS_DEVELOPMENT and port != 5000:
        raise SystemExit("production reverse-proxy integration expects the loopback application port 5000")
    logger.info("NexAlert v3.0 starting on %s:%s",host,port)
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
