import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
from database import Database
from qa.test_quant_risk_engine import synthetic_download


class RiskApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "api.db")
        self.database.initialize()
        self.db_patch = patch.object(app_module, "db", self.database)
        self.data_patch = patch.object(app_module, "market_data", synthetic_download)
        self.env_patch = patch.dict(os.environ, {"RISK_SIZER_API_KEY": "test-secret"})
        self.db_patch.start()
        self.data_patch.start()
        self.env_patch.start()
        self.client = TestClient(app_module.app)
        self.headers = {"X-API-Key": "test-secret"}

    def tearDown(self):
        self.client.close()
        self.env_patch.stop()
        self.data_patch.stop()
        self.db_patch.stop()
        self.temporary.cleanup()

    def test_authentication_and_health(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        self.assertEqual(self.client.get("/api/core").status_code, 401)

    def test_cors_preflight_allows_configured_mobile_origin_contract(self):
        response = self.client.options(
            "/api/positions",
            headers={
                "Origin": "http://localhost:8000",
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "content-type,x-api-key",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:8000")
        self.assertIn("X-API-Key", response.headers["access-control-allow-headers"])

    def test_core_and_active_crud(self):
        core = self.client.post("/api/core", headers=self.headers, json={
            "ticker": "qqq", "display_name": "Nasdaq anchor",
            "value_ils": 100_000, "currency": "USD",
        })
        self.assertEqual(core.status_code, 201, core.text)
        core_id = core.json()["id"]
        changed = self.client.put(f"/api/core/{core_id}", headers=self.headers, json={"value_ils": 110_000})
        self.assertEqual(changed.json()["value_ils"], 110_000)

        position = self.client.post("/api/positions", headers=self.headers, json={
            "ticker": "COREA", "entry_price": 100, "atr": 3, "quantity": 100,
            "value_ils": 10_000, "currency": "ILS", "risk_status": "RISK_ON",
            "display_name": "Core A momentum", "legacy": True,
            "opened_at": "2026-08-21",
        })
        self.assertEqual(position.status_code, 201, position.text)
        self.assertTrue(position.json()["legacy"])
        position_id = position.json()["id"]
        changed = self.client.put(
            f"/api/positions/{position_id}", headers=self.headers,
            json={"risk_status": "ARMED_ZERO_RISK"},
        )
        self.assertEqual(changed.json()["risk_status"], "ARMED_ZERO_RISK")
        self.assertEqual(self.client.delete(f"/api/positions/{position_id}", headers=self.headers).status_code, 204)
        self.assertEqual(self.client.delete(f"/api/core/{core_id}", headers=self.headers).status_code, 204)

    def test_new_position_seeds_eod_stop_state(self):
        import sqlite3
        created = self.client.post("/api/positions", headers=self.headers, json={
            "ticker": "abc", "entry_price": 100, "atr": 4, "quantity": 10, "value_ils": 3650,
            "currency": "USD", "initial_stop": 91.5, "stop_source": "manual",
        })
        self.assertEqual(created.status_code, 201, created.text)
        with sqlite3.connect(self.database.path) as conn:
            row = conn.execute("SELECT ticker, stop, tier FROM eod_stop_state WHERE position_id = ?",
                               (created.json()["id"],)).fetchone()
        self.assertEqual(row, ("ABC", 91.5, "MANUAL"))

    def test_positions_list_returns_recorded_stop_and_reason(self):
        self.client.post("/api/positions", headers=self.headers, json={
            "ticker": "abc", "entry_price": 100, "atr": 4, "quantity": 10, "value_ils": 3650,
            "initial_stop": 91.5, "stop_source": "structural", "stop_note": "below support 92"})
        self.client.post("/api/positions", headers=self.headers, json={
            "ticker": "old", "entry_price": 50, "atr": 2, "quantity": 1, "value_ils": 100})
        rows = {r["ticker"]: r for r in self.client.get("/api/positions", headers=self.headers).json()}
        self.assertEqual(rows["ABC"]["stop"], 91.5)
        self.assertEqual(rows["ABC"]["stop_source"], "STRUCTURAL")
        self.assertEqual(rows["ABC"]["stop_why"], "below support 92")
        self.assertIsNone(rows["OLD"]["stop"])

    def test_position_without_stop_is_not_seeded_and_bad_stop_rejected(self):
        ok = self.client.post("/api/positions", headers=self.headers, json={
            "ticker": "OLD", "entry_price": 100, "atr": 4, "quantity": 1, "value_ils": 100})
        self.assertEqual(ok.status_code, 201)
        bad = self.client.post("/api/positions", headers=self.headers, json={
            "ticker": "BAD", "entry_price": 100, "atr": 4, "quantity": 1, "value_ils": 100,
            "initial_stop": 100})
        self.assertEqual(bad.status_code, 422)

    def test_stop_suggest_endpoint(self):
        import numpy as np, pandas as pd
        dates = pd.bdate_range("2025-06-02", periods=300)
        rng = np.random.default_rng(2)
        close = 100 * np.cumprod(1 + rng.normal(0.002, 0.012, 300))
        cols = {("ZZZ", "Open"): close, ("ZZZ", "High"): close * 1.01, ("ZZZ", "Low"): close * 0.99,
                ("ZZZ", "Close"): close, ("ZZZ", "Volume"): np.full(300, 1e6)}
        frame = pd.DataFrame(cols, index=dates)
        with patch.object(app_module, "market_data", lambda *a, **k: frame):
            r = self.client.get("/api/stop/suggest", headers=self.headers,
                                params={"ticker": "zzz", "entry_price": float(close[-1])})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("extended", body)
        self.assertTrue(body["stop"] is None or body["stop"] < close[-1])

    def test_risk_evaluation_reads_portfolio_from_database(self):
        self.database.update_settings({"maxdailyvar": 1_000_000, "maxriskonr": 5})
        self.database.create_core({
            "ticker": "COREB", "value_ils": 100_000, "currency": "ILS", "fx_ticker": None,
        })
        self.database.create_active({
            "ticker": "COREA", "entry_price": 100, "atr": 3, "quantity": 100,
            "value_ils": 100_000, "currency": "ILS", "fx_to_ils": 1,
            "legacy": True, "risk_status": "RISK_ON", "rung": 0, "opened_at": "2026-08-21",
        })
        response = self.client.post(
            "/api/risk/evaluate", headers=self.headers,
            json={
                "new_ticker": "DIVERSE", "new_ticker_currency": "ILS",
                "proposed_entry_size_ils": 10_000,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["is_trade_approved"])
        self.assertEqual(body["portfolio"]["core_positions"], 1)
        self.assertEqual(body["portfolio"]["active_positions"], 1)
        self.assertEqual(body["portfolio"]["combined_instruments"], 2)
        self.assertEqual(body["risk_metrics"]["portfolio_heat"]["current_r"], 0)
        self.assertEqual(body["risk_metrics"]["portfolio_heat"]["legacy_risk_on_excluded"], 1)

    def test_risk_evaluation_uses_server_settings_and_enforces_heat(self):
        self.database.update_settings({"maxdailyvar": 1_000_000, "maxriskonr": 1})
        self.database.create_active({
            "ticker": "COREA", "entry_price": 100, "atr": 3, "quantity": 100,
            "value_ils": 100_000, "currency": "ILS", "fx_to_ils": 1,
            "legacy": False, "risk_status": "RISK_ON", "rung": 0,
            "opened_at": "2026-08-21",
        })
        response = self.client.post(
            "/api/risk/evaluate", headers=self.headers,
            json={"new_ticker": "DIVERSE", "new_ticker_currency": "ILS", "proposed_entry_size_ils": 5_000},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["is_trade_approved"])
        self.assertTrue(body["risk_metrics"]["portfolio_heat"]["budget_breach"])
        self.assertEqual(body["risk_metrics"]["historical_var_99"]["max_daily_risk_budget_ils"], 1_000_000)

        forbidden = self.client.post(
            "/api/risk/evaluate", headers=self.headers,
            json={
                "new_ticker": "DIVERSE", "new_ticker_currency": "ILS",
                "proposed_entry_size_ils": 5_000, "max_daily_var_ils": 9_999_999,
            },
        )
        self.assertEqual(forbidden.status_code, 422)

    def test_settings_are_server_persisted(self):
        changed = self.client.put("/api/settings", headers=self.headers, json={
            "settings": {"capital": 750_000, "maxriskonr": 7}, "setup_complete": True,
        })
        self.assertEqual(changed.status_code, 200)
        loaded = self.client.get("/api/settings", headers=self.headers).json()
        self.assertEqual(loaded["settings"]["capital"], 750_000)
        self.assertEqual(loaded["settings"]["maxriskonr"], 7)
        self.assertTrue(loaded["setup_complete"])
        invalid = self.client.put("/api/settings", headers=self.headers, json={
            "settings": {"maxriskonr": 0},
        })
        self.assertEqual(invalid.status_code, 422)


if __name__ == "__main__":
    unittest.main()
