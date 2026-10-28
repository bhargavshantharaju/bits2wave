"""SQLite-only migration checks; these do not import Flask or modify project DBs."""

import os
import shutil
import sqlite3
import tempfile
import unittest
import uuid

from backend.migrations import migrate_sos_schema


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY_DB = os.path.join(ROOT, "database", "nexalert-pre-sos-20261026-180302.db")


class SosMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "migration-copy.db")
        shutil.copy2(LEGACY_DB, self.db_path)
        self.conn = sqlite3.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def migrate(self):
        migrate_sos_schema(self.conn, lambda: "INC-" + str(uuid.uuid4()))

    def test_legacy_records_survive_migration_with_incident_ids(self):
        legacy_columns = [row[1] for row in self.conn.execute("PRAGMA table_info(alerts)")]
        before_alerts = self.conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()
        before_users = self.conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        self.migrate()
        selected = ",".join(legacy_columns)
        self.assertEqual(self.conn.execute(f"SELECT {selected} FROM alerts ORDER BY id").fetchall(), before_alerts)
        self.assertEqual(self.conn.execute("SELECT * FROM users ORDER BY id").fetchall(), before_users)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM alerts WHERE incident_id LIKE 'INC-%'").fetchone()[0], len(before_alerts))
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_migration_is_repeatable_and_adds_constraints(self):
        self.migrate()
        ids_before = self.conn.execute("SELECT incident_id FROM alerts ORDER BY id").fetchall()
        self.migrate()
        self.assertEqual(self.conn.execute("SELECT incident_id FROM alerts ORDER BY id").fetchall(), ids_before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE alerts SET incident_id=? WHERE id=2", (ids_before[0][0],))
        self.conn.rollback()
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
