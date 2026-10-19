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
    def test_chunks_under_telegram_limit(self):
        ds = [e.Decision(f"T{i}", 50.0, 40.0, 45.0, e.NORMAL, "RAISE",
                         ["trailing below support 44.00 (3 touches)", "x" * 120]) for i in range(80)]
        msgs = e.format_alert(ds, "2026-10-16")
        self.assertGreater(len(msgs), 1)
        self.assertTrue(all(len(m) < 4096 for m in msgs))
        self.assertIn("Prev stop $40.00", msgs[0])
        self.assertIn("New $45.00", msgs[0])

    def test_html_escaped(self):
        d = e.Decision("A&B", 1.0, 1.0, 1.0, e.NORMAL, "HOLD", ["-DI > +DI"])
        self.assertIn("&gt;", e.format_alert([d], "d")[0])


if __name__ == "__main__":
    unittest.main()
