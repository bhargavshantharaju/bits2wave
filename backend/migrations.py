"""Additive SQLite schema migration for the Phase 1 SOS workflow."""

import sqlite3
import uuid
from typing import Callable


def migrate_sos_schema(conn: sqlite3.Connection, new_incident_id: Callable[[], str]) -> None:
    """Migrate existing alerts without deleting or rebuilding user data.

    Caller creates the legacy application tables first. This function owns a
    single SQLite transaction for SOS columns, indexes, supporting tables and
    legacy incident identifiers.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
        additions = {
            "incident_id": "TEXT",
            "delivery_status": "TEXT DEFAULT 'saved_locally'",
            "responder_assignment": "TEXT DEFAULT NULL",
            "lifecycle_status": "TEXT NOT NULL DEFAULT 'saved_locally'",
            "landmark": "TEXT NOT NULL DEFAULT ''",
            "idempotency_key": "TEXT",
            "received_at": "TEXT DEFAULT NULL",
            "receipt_id": "TEXT DEFAULT NULL",
            "source_node_id": "TEXT DEFAULT NULL",
            "origin_mode": "TEXT NOT NULL DEFAULT 'primary'",
        }
        for name, declaration in additions.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {declaration}")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_incident_id ON alerts(incident_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_idempotency ON alerts(idempotency_key) WHERE idempotency_key IS NOT NULL")
        alert_cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
        if "priority" not in alert_cols:
            conn.execute("ALTER TABLE alerts ADD COLUMN priority TEXT NOT NULL DEFAULT 'high'")
        if "incident_capability_hash" not in alert_cols:
            conn.execute("ALTER TABLE alerts ADD COLUMN incident_capability_hash TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_capability ON alerts(incident_capability_hash)")
        conn.execute("CREATE TABLE IF NOT EXISTS outbound_queue ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,incident_id TEXT NOT NULL UNIQUE,"
            "state TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,"
            "last_attempt_at TEXT DEFAULT NULL,delivered_at TEXT DEFAULT NULL,"
            "created_at TEXT DEFAULT(datetime('now')))")
        queue_cols = {row[1] for row in conn.execute("PRAGMA table_info(outbound_queue)")}
        queue_additions = {
            "next_attempt_at": "TEXT NOT NULL DEFAULT ''",
            "max_attempts": "INTEGER NOT NULL DEFAULT 8",
            "failure_reason": "TEXT DEFAULT NULL",
            "priority": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, declaration in queue_additions.items():
            if name not in queue_cols:
                conn.execute(f"ALTER TABLE outbound_queue ADD COLUMN {name} {declaration}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_queue_ready ON outbound_queue(state, priority DESC, next_attempt_at, id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS incident_events(
            id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL, status TEXT NOT NULL,
            detail TEXT DEFAULT '', created_at TEXT DEFAULT(datetime('now')))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS delivery_attempts(
            id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL,
            attempted_at TEXT NOT NULL DEFAULT(datetime('now')), outcome TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '')""")
        conn.execute("""CREATE TABLE IF NOT EXISTS responder_assignments(
            id INTEGER PRIMARY KEY, incident_id TEXT NOT NULL UNIQUE, responder TEXT NOT NULL,
            assigned_at TEXT NOT NULL DEFAULT(datetime('now')))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS simulated_transport_config(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), connected INTEGER NOT NULL,
            lose_next_ack INTEGER NOT NULL DEFAULT 0, fail_next_transmission INTEGER NOT NULL DEFAULT 0)""")
        rows = conn.execute("SELECT id FROM alerts WHERE incident_id IS NULL OR incident_id='' ").fetchall()
        for row in rows:
            conn.execute("UPDATE alerts SET incident_id=? WHERE id=?", (new_incident_id(), row[0]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_security_schema(conn: sqlite3.Connection) -> None:
    """Add server-side authentication state without changing incident ownership.

    Existing user roles are untrusted because public registration previously
    controlled them. The one-time marker demotes historical roles; operator
    accounts are provisioned explicitly after this migration.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS security_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT(datetime('now')))")
        additions = {
            "session_version": "INTEGER NOT NULL DEFAULT 0",
            "password_hash": "TEXT DEFAULT NULL",
            "auth_failed_attempts": "INTEGER NOT NULL DEFAULT 0",
            "auth_locked_until": "TEXT DEFAULT NULL",
            "identity_id": "TEXT",
        }
        user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        for name, declaration in additions.items():
            if name not in user_cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")
        for row in conn.execute("SELECT id FROM users WHERE identity_id IS NULL OR identity_id='' ").fetchall():
            conn.execute("UPDATE users SET identity_id=? WHERE id=?", (str(uuid.uuid4()), row[0]))
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_identity_id ON users(identity_id)")
        alert_cols={row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
        ownership_additions={
            "ownership_state": "TEXT NOT NULL DEFAULT 'legacy_unverified'",
            "owner_identity_id": "TEXT",
            "ownership_issuer": "TEXT",
            "ownership_assertion_digest": "TEXT",
        }
        for name,declaration in ownership_additions.items():
            if name not in alert_cols:
                conn.execute(f"ALTER TABLE alerts ADD COLUMN {name} {declaration}")
        announcement_cols = {row[1] for row in conn.execute("PRAGMA table_info(system_announcements)")}
        if "created_by" not in announcement_cols:
            conn.execute("ALTER TABLE system_announcements ADD COLUMN created_by INTEGER REFERENCES users(id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS operator_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER NOT NULL REFERENCES users(id),
            action TEXT NOT NULL,
            target_incident_id TEXT,
            from_state TEXT,
            to_state TEXT,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT(datetime('now'))
        )""")
        conn.execute("CREATE TABLE IF NOT EXISTS security_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,actor_id INTEGER,"
            "event TEXT NOT NULL,outcome TEXT NOT NULL,source_hash TEXT,"
            "detail TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT(datetime('now')))")
        conn.execute("CREATE TABLE IF NOT EXISTS request_rate_limits ("
            "scope TEXT NOT NULL,key_hash TEXT NOT NULL,window_start INTEGER NOT NULL,hits INTEGER NOT NULL,"
            "PRIMARY KEY(scope,key_hash,window_start))")
        migrated = conn.execute("SELECT 1 FROM security_migrations WHERE name='trusted-roles-v1'").fetchone()
        if not migrated:
            conn.execute("UPDATE users SET role='citizen'")
            conn.execute("INSERT INTO security_migrations(name) VALUES('trusted-roles-v1')")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
