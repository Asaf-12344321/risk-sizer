import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "risk.db"
        self.db = Database(self.path)
        self.db.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def test_schema_crud_and_combined_portfolio(self):
        core = self.db.create_core({
            "ticker": "MSFT", "value_ils": 100_000, "currency": "USD",
            "fx_ticker": None, "display_name": "Microsoft",
        })
        active = self.db.create_active({
            "ticker": "MSFT", "entry_price": 100, "atr": 3, "quantity": 100,
            "value_ils": 25_000, "currency": "USD", "fx_to_ils": 3.5,
            "display_name": "Microsoft momentum", "legacy": True,
            "risk_status": "RISK_ON", "rung": 0, "opened_at": "2026-08-21",
        })
        self.assertEqual(core["display_name"], "Microsoft")
        self.assertEqual(active["legacy"], 1)
        self.assertEqual(
            self.db.combined_portfolio(),
            [{"ticker": "MSFT", "value_ils": 125_000, "currency": "USD", "fx_ticker": None}],
        )
        changed = self.db.update_active(active["id"], {"risk_status": "ARMED_ZERO_RISK"})
        self.assertEqual(changed["risk_status"], "ARMED_ZERO_RISK")
        self.assertTrue(self.db.delete_active(active["id"]))
        self.assertTrue(self.db.delete_core(core["id"]))

    def test_initialize_adds_new_columns_to_an_existing_database(self):
        old_path = Path(self.temporary.name) / "old.db"
        with sqlite3.connect(old_path) as connection:
            connection.executescript(
                """
                CREATE TABLE core_portfolio (
                    id INTEGER PRIMARY KEY, ticker TEXT, value_ils REAL, currency TEXT,
                    fx_ticker TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE active_positions (
                    id INTEGER PRIMARY KEY, ticker TEXT, entry_price REAL, atr REAL,
                    quantity REAL, value_ils REAL, currency TEXT, fx_to_ils REAL,
                    fx_ticker TEXT, risk_status TEXT, rung INTEGER, opened_at TEXT,
                    created_at TEXT, updated_at TEXT
                );
                CREATE TABLE app_settings (
                    id INTEGER PRIMARY KEY, settings_json TEXT,
                    setup_complete INTEGER, updated_at TEXT
                );
                """
            )
        migrated = Database(old_path)
        migrated.initialize()
        with migrated.connect() as connection:
            core_columns = {row["name"] for row in connection.execute("PRAGMA table_info(core_portfolio)")}
            active_columns = {row["name"] for row in connection.execute("PRAGMA table_info(active_positions)")}
        self.assertIn("display_name", core_columns)
        self.assertTrue({"display_name", "legacy"}.issubset(active_columns))

    def test_constraints_fail_closed(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.create_core({
                "ticker": "QQQ", "value_ils": 0, "currency": "USD", "fx_ticker": None,
            })

    def test_settings_and_consistent_backup(self):
        state = self.db.update_settings({"capital": 500_000}, setup_complete=True)
        self.assertEqual(state["settings"]["capital"], 500_000)
        self.assertTrue(state["setup_complete"])
        backup = Path(self.temporary.name) / "backup.db"
        self.db.backup_to(backup)
        restored = Database(backup)
        self.assertEqual(restored.get_settings()["settings"]["capital"], 500_000)


if __name__ == "__main__":
    unittest.main()
