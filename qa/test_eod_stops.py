import unittest
from datetime import datetime

import numpy as np
import pandas as pd

import eod_stops as e
from database import DEFAULT_SETTINGS

NY = e.NY


def bars(n=300, drift=0.0015, vol=0.012, seed=3, start=100.0):
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(drift, vol, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.008, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.008, n))
    idx = pd.bdate_range("2025-06-02", periods=n)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                         "Volume": rng.uniform(0.8e6, 1.2e6, n)}, index=idx)


def position(df, gain_back=60):
    entry = float(df["Close"].iloc[-gain_back])
    return dict(entry_price=entry, atr=float(e.atr(df).iloc[-gain_back]),
                opened_at=str(df.index[-gain_back].date()), risk_status="RISK_ON", rung=0)


class IndicatorTests(unittest.TestCase):
    def test_ranges(self):
        df = bars()
        r, a = e.rsi(df["Close"]).dropna(), e.adx(df).dropna()
        self.assertTrue(((r >= 0) & (r <= 100)).all())
        self.assertTrue(((a["adx"] >= 0) & (a["adx"] <= 100)).all())

    def test_double_bottom_is_one_zone_with_two_touches(self):
        df = bars(200, drift=0.0, vol=0.004)
        df["Low"] = 100.0 + np.sin(np.arange(200) / 5) * 0  # flat base, then plant pivots
        df["High"], df["Close"], df["Open"] = 102.0, 101.0, 101.0
        for i in (60, 120):
            df.iloc[i, df.columns.get_loc("Low")] = 90.0
        zones = e.support_zones(df, atr_now=2.0, hvn=np.array([]))
        z = next(z for z in zones if z.lo == 90.0)
        self.assertEqual(z.touches, 2)

    def test_partial_day_dropped(self):
        df = bars(10)
        today = df.index[-1]
        mid = datetime(today.year, today.month, today.day, 11, 0, tzinfo=NY)
        aft = datetime(today.year, today.month, today.day, 16, 30, tzinfo=NY)
        self.assertEqual(len(e.completed_bars(df, mid)), 9)
        self.assertEqual(len(e.completed_bars(df, aft)), 10)


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.df = bars()
        self.pos = position(self.df)
        self.st = dict(DEFAULT_SETTINGS)

    def test_stop_never_moves_down(self):
        for cut in range(150, 300, 7):
            sub = self.df.iloc[:cut]
            pos = position(sub, 40)
            prev = e.ladder_stop(sub, pos, self.st)[0]
            for k in range(0, 20):
                d = e.decide("X", self.df.iloc[:cut + k], pos, prev, self.st)
                self.assertGreaterEqual(d.new_stop, prev)
                prev = d.new_stop

    def test_raise_stays_below_price(self):
        prev = e.ladder_stop(self.df, self.pos, self.st)[0]
        d = e.decide("X", self.df, self.pos, prev, self.st)
        self.assertLessEqual(d.new_stop, d.close - e.RULES["tight_clearance_atr"] * d.metrics["atr"] + 0.01 if d.moved else d.close)

    def test_sector_tightens_at_most_one_notch_and_never_loosens(self):
        prev = e.ladder_stop(self.df, self.pos, self.st)[0]
        base = e.decide("X", self.df, self.pos, prev, self.st, e.Context())
        weak = e.decide("X", self.df, self.pos, prev, self.st,
                        e.Context("XLK", True, "XLK weak", stock_vs_sector_10d=-0.05))
        self.assertIn(weak.tier - base.tier, (0, 1))
        self.assertGreaterEqual(weak.new_stop, base.new_stop)

    def test_idiosyncratic_strength_ignores_sector(self):
        df = bars(drift=0.004, seed=11)           # strong steady climber
        pos = position(df)
        prev = e.ladder_stop(df, pos, self.st)[0]
        base = e.decide("X", df, pos, prev, self.st, e.Context())
        weak = e.decide("X", df, pos, prev, self.st,
                        e.Context("XLK", True, "XLK weak", stock_vs_sector_10d=0.10))
        self.assertEqual(weak.tier, base.tier)

    def test_breach_reported_and_stop_kept(self):
        prev = float(self.df["Low"].iloc[-1]) + 1.0
        d = e.decide("X", self.df, self.pos, prev, self.st)
        self.assertEqual(d.action, "BREACH")
        self.assertEqual(d.new_stop, prev)

    def test_short_history_skipped(self):
        d = e.decide("X", self.df.iloc[:50], self.pos, 1.0, self.st)
        self.assertEqual(d.action, "SKIP")


class AlertTests(unittest.TestCase):
    @staticmethod
    def dec(tk, action, prev=40.0, new=45.0):
        return e.Decision(tk, 50.0, prev, new, e.NORMAL, action, ["trailing below support 44.00"])

    def test_only_actionable_in_one_message(self):
        ds = [self.dec("UP", "RAISE"), self.dec("SIT", "HOLD", new=40.0), self.dec("BAD", "BREACH", new=40.0)]
        msgs = e.format_alert(ds, "2026-10-16")
        self.assertEqual(len(msgs), 1)
        self.assertIn("UP", msgs[0])
        self.assertIn("BAD", msgs[0])
        self.assertNotIn("SIT", msgs[0])
        self.assertLess(msgs[0].index("BAD"), msgs[0].index("UP"))   # breach first
        self.assertIn("Prev $40.00", msgs[0])
        self.assertIn("New $45.00", msgs[0])

    def test_silent_or_quiet_line_when_nothing_to_do(self):
        ds = [self.dec("SIT", "HOLD", new=40.0)]
        self.assertEqual(e.format_alert(ds, "d"), [])
        self.assertEqual(e.format_alert(ds, "d", quiet_when_no_changes=False),
                         ["✅ EOD Check: No stop adjustments required today."])

    def test_skip_is_not_silent(self):
        d = e.Decision("NODATA", 0, 0, 0, e.NORMAL, "SKIP", ["no price data"])
        self.assertIn("NODATA", e.format_alert([d], "d")[0])

    def test_chunks_under_telegram_limit(self):
        ds = [self.dec(f"T{i}", "RAISE") for i in range(80)]
        msgs = e.format_alert(ds, "d")
        self.assertGreater(len(msgs), 1)
        self.assertTrue(all(len(m) < 4096 for m in msgs))

    def test_html_escaped(self):
        d = e.Decision("A&B", 1.0, 1.0, 2.0, e.NORMAL, "RAISE", ["-DI > +DI"])
        self.assertIn("&gt;", e.format_alert([d], "d")[0])


class InitialStopTests(unittest.TestCase):
    def test_structural_stop_below_entry_with_justification(self):
        df = bars(drift=0.002, seed=5)
        entry = float(df["Close"].iloc[-1])
        out = e.initial_stop("X", df, entry, dict(DEFAULT_SETTINGS))
        if out["stop"] is not None:
            self.assertLess(out["stop"], entry)
            self.assertGreaterEqual(entry - out["stop"], 1.5 * out["atr"] - 0.01)
            self.assertIn("support", out["justification"])

    def test_far_support_is_flagged_extended(self):
        df = bars(drift=0.002, seed=5)
        entry = float(df["Close"].iloc[-1]) * 2.0          # price doubled away from all structure
        out = e.initial_stop("X", df, entry, dict(DEFAULT_SETTINGS))
        self.assertTrue(out["extended"])

    def test_extended_limit_is_a_setting(self):
        df = bars(drift=0.002, seed=5)
        entry = float(df["Close"].iloc[-1]) * 1.3
        loose = e.initial_stop("X", df, entry, dict(DEFAULT_SETTINGS, extendedpct=90))
        tight = e.initial_stop("X", df, entry, dict(DEFAULT_SETTINGS, extendedpct=5))
        if loose["stop"] is not None:
            self.assertFalse(loose["extended"])
            self.assertTrue(tight["extended"])

    def test_short_history_has_no_stop(self):
        out = e.initial_stop("X", bars().iloc[:40], 100.0, dict(DEFAULT_SETTINGS))
        self.assertIsNone(out["stop"])
        self.assertTrue(out["no_support"])


class ThresholdTests(unittest.TestCase):
    def test_raise_needs_half_atr_and_one_and_half_percent(self):
        df = bars()
        pos = position(df)
        st = dict(DEFAULT_SETTINGS)
        prev = e.ladder_stop(df, pos, st)[0]
        for cut in range(150, 300, 5):
            d = e.decide("X", df.iloc[:cut], pos, prev, st)
            if d.action == "RAISE":
                step = d.new_stop - prev
                self.assertGreaterEqual(step + 0.01, 0.5 * d.metrics["atr"])
                self.assertGreaterEqual(step + 0.01, 0.015 * prev)
                prev = d.new_stop


if __name__ == "__main__":
    unittest.main()
