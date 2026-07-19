import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event, text
from sqlmodel import create_engine

import database


class DatabaseRuntimeTests(unittest.TestCase):
    def _engine(self, path: Path):
        engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 15})
        event.listen(engine, "connect", database._set_sqlite_connection_pragmas)
        return engine

    def test_file_sqlite_uses_wal_foreign_keys_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self._engine(Path(directory) / "runtime.db")
            try:
                with patch.object(database, "engine", engine):
                    database._enable_sqlite_wal()
                    with engine.connect() as connection:
                        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
                        foreign_keys = connection.execute(text("PRAGMA foreign_keys")).scalar_one()
                        busy_timeout = connection.execute(text("PRAGMA busy_timeout")).scalar_one()
                self.assertEqual(str(journal_mode).lower(), "wal")
                self.assertEqual(foreign_keys, 1)
                self.assertEqual(busy_timeout, 15000)
            finally:
                engine.dispose()

    def test_foreign_key_check_rejects_existing_orphan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orphan.db"
            raw = sqlite3.connect(path)
            raw.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            raw.execute(
                "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
            )
            raw.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")
            raw.commit()
            raw.close()

            engine = self._engine(path)
            try:
                with patch.object(database, "engine", engine):
                    with self.assertRaisesRegex(RuntimeError, "外部キー違反"):
                        database._validate_sqlite_foreign_keys()
            finally:
                engine.dispose()

    def test_create_db_and_tables_runs_all_sqlite_migrations(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = self._engine(Path(directory) / "schema.db")
            try:
                with patch.object(database, "engine", engine):
                    database.create_db_and_tables()
                    with engine.connect() as connection:
                        export_columns = {
                            row[1]
                            for row in connection.execute(
                                text("PRAGMA table_info(zengin_exports)")
                            )
                        }
                        profile_columns = {
                            row[1]
                            for row in connection.execute(
                                text("PRAGMA table_info(family_billing_profiles)")
                            )
                        }
                        meeting_note_columns = {
                            row[1]
                            for row in connection.execute(
                                text("PRAGMA table_info(meeting_notes)")
                            )
                        }
                self.assertIn("submitted_at", export_columns)
                self.assertIn("new_code_consumed_by_export_id", profile_columns)
                self.assertIn("search_text", meeting_note_columns)
            finally:
                engine.dispose()

    def test_health_profile_migration_adds_priority_items_and_backfills_equivalent_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-health.db"
            raw = sqlite3.connect(path)
            raw.execute(
                "CREATE TABLE child_health_profiles ("
                "id INTEGER PRIMARY KEY, child_id INTEGER NOT NULL, "
                "epipen_required BOOLEAN DEFAULT 0 NOT NULL)"
            )
            raw.execute(
                "CREATE TABLE child_allergies ("
                "id INTEGER PRIMARY KEY, child_id INTEGER NOT NULL, "
                "is_active BOOLEAN DEFAULT 1 NOT NULL)"
            )
            raw.execute(
                "INSERT INTO child_health_profiles (id, child_id, epipen_required) VALUES (1, 10, 1)"
            )
            raw.execute(
                "INSERT INTO child_allergies (id, child_id, is_active) VALUES (1, 10, 1)"
            )
            raw.commit()
            raw.close()

            engine = self._engine(path)
            try:
                with patch.object(database, "engine", engine):
                    database._migrate_add_child_health_profile_columns()
                    with engine.connect() as connection:
                        columns = {
                            row[1]
                            for row in connection.execute(
                                text("PRAGMA table_info(child_health_profiles)")
                            )
                        }
                        values = connection.execute(
                            text(
                                "SELECT has_allergy, has_epipen, has_anaphylaxis, "
                                "has_febrile_seizure, has_nursemaids_elbow, has_medication, "
                                "other_management_items FROM child_health_profiles WHERE id = 1"
                            )
                        ).one()
                self.assertTrue(
                    {
                        "has_allergy",
                        "has_epipen",
                        "has_anaphylaxis",
                        "has_febrile_seizure",
                        "has_nursemaids_elbow",
                        "has_medication",
                        "other_management_items",
                    }.issubset(columns)
                )
                self.assertEqual(tuple(values), (1, 1, 0, 0, 0, 0, None))
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
