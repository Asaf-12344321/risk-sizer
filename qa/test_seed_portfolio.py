import tempfile
import unittest
from pathlib import Path

from database import Database
from scripts.seed_portfolio import load_snapshot, seed_database


class SeedPortfolioTests(unittest.TestCase):
    def test_seed_is_complete_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "risk.db")
            snapshot = {
                "core": [
                    {"ticker": "INDEX", "display_name": "Index anchor", "value_ils": 100_000, "currency": "ILS"},
                    {"ticker": "ANCHOR", "display_name": "Long-term anchor", "value_ils": 50_000, "currency": "USD"},
                ],
                "active": [
                    {"ticker": "MOMO1", "display_name": "Momentum one", "entry_price": 100, "quantity": 50, "value_ils": 20_000, "currency": "USD", "fx_to_ils": 4},
                    {"ticker": "MOMO2", "display_name": "Momentum two", "entry_price": 200, "quantity": 100, "value_ils": 30_000, "currency": "ILS", "fx_to_ils": 1},
                ],
            }
            atr = {"MOMO1": 2.5, "MOMO2": 4.0}
            first = seed_database(
                database, snapshot, atr, opened_at="2026-08-21", replace=True
            )
            second = seed_database(
                database, snapshot, atr, opened_at="2026-08-21", replace=False
            )

            self.assertEqual(first["core_created"], 2)
            self.assertEqual(first["active_created"], 2)
            self.assertEqual(second["core_updated"], 2)
            self.assertEqual(second["active_updated"], 2)
            self.assertEqual(len(database.list_core()), 2)
            self.assertEqual(len(database.list_active()), 2)
            self.assertAlmostEqual(
                sum(row["value_ils"] for row in database.list_core() + database.list_active()),
                200_000,
                places=2,
            )
            self.assertTrue(
                all(row["risk_status"] == "RISK_ON" for row in database.list_active())
            )
            self.assertTrue(all(row["legacy"] == 1 for row in database.list_active()))
            self.assertTrue(all(row["display_name"] for row in database.list_core()))
            self.assertTrue(all(row["display_name"] for row in database.list_active()))

    def test_snapshot_loader_rejects_empty_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.json"
            path.write_text('{"core": [], "active": []}', encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_snapshot(path)


if __name__ == "__main__":
    unittest.main()
