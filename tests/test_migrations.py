"""Migration tests against explicit historical schemas in disposable databases."""

import os
import sqlite3
import tempfile
import unittest
import uuid

from backend.migrations import migrate_sos_schema, migrate_security_schema


LEGACY_SCHEMA_V1 = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    latitude REAL DEFAULT 0.0,
    longitude REAL DEFAULT 0.0,
    is_online INTEGER DEFAULT 0,
    socket_id TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now'))
);
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    alert_type TEXT NOT NULL,
    description TEXT DEFAULT '',
    latitude REAL DEFAULT 0.0,
    longitude REAL DEFAULT 0.0,
    status TEXT DEFAULT 'active',
    timestamp TEXT DEFAULT (datetime('now')),
    resolved_at TEXT DEFAULT NULL
);
"""


class SosMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nexalert-migration-")
        self.db_path = os.path.join(self.tmp.name, "legacy-v1.sqlite")
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(LEGACY_SCHEMA_V1)
        self.conn.execute(
            "INSERT INTO users(id,username,full_name,phone) VALUES(1,'legacy','Legacy User','000')"
        )
        self.conn.execute(
            "INSERT INTO alerts(id,user_id,alert_type,description,latitude,longitude) "
            "VALUES(1,1,'medical','Historical report',12.9,77.5)"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def migrate(self):
        migrate_sos_schema(self.conn, lambda: "INC-" + str(uuid.uuid4()))

    def test_legacy_records_survive_v1_to_current_migration(self):
        legacy_columns = [row[1] for row in self.conn.execute("PRAGMA table_info(alerts)")]
        before_alerts = self.conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()
        before_users = self.conn.execute("SELECT * FROM users ORDER BY id").fetchall()

        self.migrate()

        selected = ",".join(legacy_columns)
        self.assertEqual(
            self.conn.execute(f"SELECT {selected} FROM alerts ORDER BY id").fetchall(),
            before_alerts,
        )
        self.assertEqual(self.conn.execute("SELECT * FROM users ORDER BY id").fetchall(), before_users)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts WHERE incident_id LIKE 'INC-%'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT lifecycle_status FROM alerts WHERE id=1").fetchone()[0], "saved_locally")
        self.assertIn("incident_capability_hash",{row[1] for row in self.conn.execute("PRAGMA table_info(alerts)")})
        self.assertIsNone(self.conn.execute("SELECT incident_capability_hash FROM alerts WHERE id=1").fetchone()[0])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_migration_is_repeatable_and_adds_constraints(self):
        self.migrate()
        ids_before = self.conn.execute("SELECT incident_id FROM alerts ORDER BY id").fetchall()
        self.migrate()
        self.assertEqual(self.conn.execute("SELECT incident_id FROM alerts ORDER BY id").fetchall(), ids_before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE alerts SET incident_id=? WHERE id=1", ("duplicate",))
            self.conn.execute("INSERT INTO alerts(user_id,alert_type,incident_id) VALUES(1,'fire',?)", ("duplicate",))
        self.conn.rollback()
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_duplicate_preexisting_incident_ids_roll_back_migration(self):
        """A malformed partially migrated DB must retain its pre-migration schema/data."""
        self.conn.execute("ALTER TABLE alerts ADD COLUMN incident_id TEXT")
        self.conn.execute("UPDATE alerts SET incident_id='DUPLICATE' WHERE id=1")
        self.conn.execute(
            "INSERT INTO alerts(user_id,alert_type,incident_id) VALUES(1,'fire','DUPLICATE')"
        )
        self.conn.commit()
        before_columns = [row[1] for row in self.conn.execute("PRAGMA table_info(alerts)")]
        before_rows = self.conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()

        with self.assertRaises(sqlite3.IntegrityError):
            self.migrate()

        self.assertEqual([row[1] for row in self.conn.execute("PRAGMA table_info(alerts)")], before_columns)
        self.assertEqual(self.conn.execute("SELECT * FROM alerts ORDER BY id").fetchall(), before_rows)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='incident_events'"
        ).fetchone())
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_security_migration_preserves_user_and_incident_ids_but_demotes_untrusted_roles(self):
        self.conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'citizen'")
        self.conn.execute("UPDATE users SET role='operator' WHERE id=1")
        self.conn.execute("CREATE TABLE system_announcements(id INTEGER PRIMARY KEY, category TEXT, content TEXT, created_at TEXT)")
        self.conn.commit()
        migrate_security_schema(self.conn)
        user=self.conn.execute("SELECT id,role,session_version,password_hash FROM users WHERE id=1").fetchone()
        self.assertEqual(tuple(user),(1,"citizen",0,None))
        migrate_security_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT id,user_id FROM alerts WHERE id=1").fetchone(),(1,1))
        self.assertIn("created_by",{row[1] for row in self.conn.execute("PRAGMA table_info(system_announcements)")})
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_actions'").fetchone())
        identity=self.conn.execute("SELECT identity_id FROM users WHERE id=1").fetchone()[0]
        self.assertTrue(identity)
        self.assertEqual(self.conn.execute("SELECT ownership_state,owner_identity_id FROM alerts WHERE id=1").fetchone(),("legacy_unverified",None))


if __name__ == "__main__":
    unittest.main()
