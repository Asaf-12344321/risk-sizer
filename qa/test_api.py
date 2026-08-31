import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
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

    def test_native_stop_alert_subscription_and_test_delivery(self):
        # The browser only receives the public VAPID key. The private key stays in the
        # process environment and is consumed solely by the server-side sender.
        configured = {
            "RISK_SIZER_VAPID_PRIVATE_KEY": "private-test-key",
            "RISK_SIZER_VAPID_PUBLIC_KEY": "public-test-key",
            "RISK_SIZER_VAPID_SUBJECT": "https://risk-sizer.test",
        }
        with patch.dict(os.environ, configured, clear=False):
            config = self.client.get("/api/notifications/config", headers=self.headers)
            self.assertEqual(config.status_code, 200, config.text)
            self.assertEqual(config.json(), {"enabled": True, "public_key": "public-test-key"})
            invalid = self.client.post("/api/notifications/subscribe", headers=self.headers, json={
                "endpoint": "http://not-secure.test/push", "keys": {"p256dh": "a" * 20, "auth": "b" * 12},
            })
            self.assertEqual(invalid.status_code, 422)
            subscription = {
                "endpoint": "https://push.example.test/subscription-1",
                "keys": {"p256dh": "a" * 20, "auth": "b" * 12},
            }
            created = self.client.post("/api/notifications/subscribe", headers=self.headers, json=subscription)
            self.assertEqual(created.status_code, 201, created.text)
            self.assertEqual(len(self.database.list_push_subscriptions()), 1)
            with patch.object(app_module, "_deliver_web_push", return_value={"sent": 1, "expired": 0, "failed": 0}) as send:
                test = self.client.post("/api/notifications/test", headers=self.headers)
            self.assertEqual(test.status_code, 200, test.text)
            self.assertEqual(test.json()["sent"], 1)
            self.assertEqual(send.call_args.args[0]["title"], "Risk Sizer")
            deleted = self.client.request(
                "DELETE", "/api/notifications/subscribe", headers=self.headers,
                json={"endpoint": subscription["endpoint"]},
            )
            self.assertEqual(deleted.status_code, 204, deleted.text)
            self.assertEqual(self.database.list_push_subscriptions(), [])

    def test_stop_move_alert_only_sends_for_actionable_instruction(self):
        position = {"id": 7, "ticker": "ASTS", "display_name": "AST SpaceMobile"}
        with patch.object(app_module, "_deliver_web_push", return_value={"sent": 1, "expired": 0, "failed": 0}) as send:
            quiet = app_module._send_stop_move_alert(position, {
                "actionable_alert_needed": False, "current_stop_price": 42.04,
            })
            moved = app_module._send_stop_move_alert(position, {
                "actionable_alert_needed": True, "current_stop_price": 44.25,
            })
        self.assertEqual(quiet["sent"], 0)
        self.assertEqual(moved["sent"], 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0]["body"], "AST SpaceMobile: move stop to 44.25")

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
        self.assertEqual(position.json()["policy_snapshot"]["entry_atr"], 3.0)
        self.assertEqual(position.json()["policy_snapshot"]["trailmult"], 3.5)
        position_id = position.json()["id"]
        immutable = self.client.put(f"/api/positions/{position_id}", headers=self.headers, json={"atr": 4})
        self.assertEqual(immutable.status_code, 409)
        changed = self.client.put(
            f"/api/positions/{position_id}", headers=self.headers,
            json={"risk_status": "ARMED_ZERO_RISK"},
        )
        self.assertEqual(changed.json()["risk_status"], "ARMED_ZERO_RISK")
        self.assertEqual(self.client.delete(f"/api/positions/{position_id}", headers=self.headers).status_code, 204)
        self.assertEqual(self.client.delete(f"/api/core/{core_id}", headers=self.headers).status_code, 204)

    def test_post_close_endpoint_logs_stop_and_reference_only_har_shadow(self):
        position = self.client.post("/api/positions", headers=self.headers, json={
            "ticker": "COREA", "entry_price": 100, "atr": 3, "quantity": 100,
            "value_ils": 10_000, "currency": "ILS", "opened_at": "2025-01-02",
        }).json()
        dates = pd.bdate_range("2025-01-02", periods=380)
        prices = np.linspace(100, 150, len(dates))
        bars = pd.DataFrame({"open": prices, "high": prices * 1.02,
                             "low": prices * 0.98, "close": prices}, index=dates)
        with patch.object(app_module, "_daily_ohlc", return_value=bars):
            response = self.client.post(
                f"/api/positions/{position['id']}/post-close", headers=self.headers,
                json={"as_of_session": dates[-1].date().isoformat()},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["shadow_reference_only"])
        self.assertFalse(body["sizing_or_var_inputs_changed"])
        self.assertIn("current_stop_price", body["stop_update"])
        self.assertIn("delta_ticks", body["stop_update"])
        self.assertIn("21", body["har_parkinson_shadow"]["forecasts"])
        shadows = self.client.get("/api/positions/shadow", headers=self.headers)
        self.assertIn(str(position["id"]), shadows.json())
        latest_stop = self.client.get("/api/positions/stops", headers=self.headers)
        self.assertEqual(latest_stop.status_code, 200, latest_stop.text)
        self.assertEqual(latest_stop.json()[str(position["id"])]["current_stop_price"], body["stop_update"]["current_stop_price"])

        # The scheduler calls the batch endpoint.  Replaying the same finalized session
        # is idempotent and exposes a position-level result for operational logging.
        with patch.object(app_module, "_daily_ohlc", return_value=bars):
            batch = self.client.post(
                "/api/positions/post-close", headers=self.headers,
                json={"as_of_session": dates[-1].date().isoformat()},
            )
        self.assertEqual(batch.status_code, 200, batch.text)
        self.assertEqual(batch.json()["processed"][0]["position_id"], position["id"])
        self.assertFalse(batch.json()["sizing_or_var_inputs_changed"])

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
