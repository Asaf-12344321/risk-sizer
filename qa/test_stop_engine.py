import unittest

import numpy as np
import pandas as pd

from stop_engine import build_policy_snapshot, har_parkinson_shadow, replay_post_close_stop


SETTINGS = {
    "initmult": 2.5, "trailmult": 3.5, "armpct": 15.0,
    "armatrmult": 3.0, "minstop": 8.0, "maxstop": 30.0,
}


class StopEngineTests(unittest.TestCase):
    def test_replay_arms_on_high_and_ratchets_from_close_only(self):
        policy = build_policy_snapshot(5.0, SETTINGS)
        result = replay_post_close_stop(
            entry_price=100.0, policy_snapshot=policy,
            bars=[
                {"session": "2026-01-02", "open": 100, "high": 116, "low": 99, "close": 110},
                {"session": "2026-01-05", "open": 110, "high": 130, "low": 108, "close": 120},
            ],
            previous_confirmed_stop_price=87.5,
        )
        # The first high arms the position, but the close (110), not 116, sets the trail.
        self.assertTrue(result["armed"])
        self.assertEqual(result["highest_close"], 120.0)
        self.assertEqual(result["current_stop_price"], 102.5)
        self.assertEqual(result["delta_ticks"], 1500)
        self.assertTrue(result["stop_moved_up"])
        self.assertTrue(result["actionable_alert_needed"])

    def test_gap_fill_stops_replay_and_suppresses_alert(self):
        policy = build_policy_snapshot(5.0, SETTINGS)
        result = replay_post_close_stop(
            entry_price=100.0, policy_snapshot=policy,
            bars=[{"session": "2026-01-02", "open": 80, "high": 90, "low": 78, "close": 88}],
            previous_confirmed_stop_price=87.5,
        )
        self.assertTrue(result["position_exit_detected"])
        self.assertEqual(result["exit_fill"], {"session": "2026-01-02", "price": 80.0, "gap": True})
        self.assertFalse(result["actionable_alert_needed"])

    def test_prior_broker_stop_cannot_be_lowered(self):
        policy = build_policy_snapshot(5.0, SETTINGS)
        result = replay_post_close_stop(
            entry_price=100.0, policy_snapshot=policy,
            bars=[{"session": "2026-01-02", "open": 110, "high": 112, "low": 109, "close": 110}],
            previous_confirmed_stop_price=108.0,
        )
        self.assertEqual(result["current_stop_price"], 108.0)
        self.assertFalse(result["stop_moved_up"])
        self.assertEqual(result["delta_ticks"], 0)

    def test_har_shadow_is_causal_with_respect_to_later_bars(self):
        index = pd.bdate_range("2023-01-02", periods=380)
        values = np.linspace(100, 180, len(index))
        frame = pd.DataFrame({"high": values * 1.02, "low": values * 0.98, "close": values}, index=index)
        as_of = index[340]
        first = har_parkinson_shadow(frame, as_of_session=as_of)
        changed = frame.copy()
        changed.loc[index[341]:, "high"] *= 20
        changed.loc[index[341]:, "low"] *= 0.1
        second = har_parkinson_shadow(changed, as_of_session=as_of)
        self.assertEqual(first, second)
        self.assertEqual(first["forecasts"]["21"]["status"], "available")
        self.assertEqual(first["forecasts"]["31"]["status"], "available")


if __name__ == "__main__":
    unittest.main()
