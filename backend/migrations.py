"""Additive SQLite schema migration for the Phase 1 SOS workflow."""

import sqlite3
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
        }
        user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        for name, declaration in additions.items():
            if name not in user_cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {declaration}")
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
        migrated = conn.execute("SELECT 1 FROM security_migrations WHERE name='trusted-roles-v1'").fetchone()
        if not migrated:
            conn.execute("UPDATE users SET role='citizen'")
            conn.execute("INSERT INTO security_migrations(name) VALUES('trusted-roles-v1')")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
