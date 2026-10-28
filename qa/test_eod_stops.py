import io
import re
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import eod_stops as e
from database import DEFAULT_SETTINGS, Database

NY = e.NY


def bars(n=300, drift=0.0015, vol=0.012, seed=3, start=100.0, end=None):
    rng = np.random.default_rng(seed)
    close = start * np.cumprod(1 + rng.normal(drift, vol, n))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.008, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.008, n))
    idx = pd.bdate_range(end=end, periods=n) if end else pd.bdate_range("2025-06-02", periods=n)
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
        for i in (110, 170):
            df.iloc[i, df.columns.get_loc("Low")] = 90.0
        zones = e.support_zones(df, atr_now=2.0, hvn=np.array([]))
        z = next(z for z in zones if z.lo == 90.0)
        self.assertEqual(z.touches, 2)

    def test_same_day_bar_dropped_until_settled_but_next_morning_run_keeps_it(self):
        df = bars(10)
        d = df.index[-1]
        at = lambda day, h, m: datetime(day.year, day.month, day.day, h, m, tzinfo=NY)
        self.assertEqual(len(e.completed_bars(df, at(d, 11, 0))), 9)      # still trading
        self.assertEqual(len(e.completed_bars(df, at(d, 16, 30))), 9)     # volume still provisional
        self.assertEqual(len(e.completed_bars(df, at(d, 20, 30))), 10)    # settled
        nxt = d + pd.Timedelta(days=1)                                    # the scheduled 01:00 run
        self.assertEqual(len(e.completed_bars(df, at(nxt, 1, 0))), 10)

    def test_nan_volume_becomes_zero_and_never_poisons_the_profile(self):
        df = bars()
        df.iloc[-30, df.columns.get_loc("Volume")] = np.nan
        out = e.completed_bars(df, datetime(2030, 1, 1, tzinfo=NY))
        self.assertFalse(out["Volume"].isna().any())
        hvn, centres, poc = e.volume_profile(out, float(e.atr(out).iloc[-1]))
        self.assertFalse(np.isnan(hvn).any())
        self.assertTrue(np.isfinite(poc))

    def test_rsi_is_100_without_down_days_and_neutral_when_flat(self):
        up = pd.Series(np.arange(1.0, 80.0))
        self.assertEqual(float(e.rsi(up).iloc[-1]), 100.0)
        self.assertEqual(float(e.rsi(pd.Series(np.full(80, 5.0))).iloc[-1]), 50.0)
        self.assertTrue(e.rsi(up).iloc[:13].isna().all())          # warm-up stays missing


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.df = bars()
        self.pos = position(self.df)
        self.st = dict(DEFAULT_SETTINGS)

    def test_breach_reported_and_stop_kept(self):
        prev = float(self.df["Low"].iloc[-1]) + 1.0
        d = e.decide("X", self.df, self.pos, prev, self.st)
        self.assertEqual(d.action, "BREACH")
        self.assertEqual(d.new_stop, prev)

    def test_breach_on_a_missed_session_is_still_reported(self):
        df = self.df.copy()
        df.iloc[-3, df.columns.get_loc("Low")] = 40.0           # dipped through the stop 2 sessions ago
        d = e.decide("X", df, self.pos, 60.0, self.st, since=df.index[-6])
        self.assertEqual(d.action, "BREACH")
        self.assertIn(df.index[-3].strftime("%b %-d"), d.reasons[0])
        self.assertEqual(d.new_stop, 60.0)
        # a review already made after that dip must not report it again
        again = e.decide("X", df, self.pos, 60.0, self.st, since=df.index[-1])
        self.assertNotEqual(again.action, "BREACH")

    def test_stale_ticker_is_skipped_not_judged(self):
        d = e.decide("X", self.df.iloc[:-3], self.pos, 60.0, self.st, asof=self.df.index[-1])
        self.assertEqual(d.action, "SKIP")
        self.assertIn("older than the rest of the market", d.reasons[0])

    def test_last_bar_without_volume_is_skipped(self):
        df = self.df.copy()
        df.iloc[-1, df.columns.get_loc("Volume")] = 0
        self.assertEqual(e.decide("X", df, self.pos, 60.0, self.st).action, "SKIP")

    def test_split_since_entry_is_skipped_never_a_false_breach(self):
        df = self.df.copy()
        df["Stock Splits"] = 0.0
        df.iloc[-10, df.columns.get_loc("Stock Splits")] = 4.0
        pre = df.index < df.index[-10]
        df.loc[pre, ["Open", "High", "Low", "Close"]] /= 4      # Yahoo rewrites history on a split
        prev = float(self.df["Close"].iloc[-1]) * 0.9           # recorded in pre-split dollars
        d = e.decide("X", df, self.pos, prev, self.st)
        self.assertEqual(d.action, "SKIP")
        self.assertIn("stock split", d.reasons[0])

    def test_split_before_entry_is_ignored(self):
        df = self.df.copy()
        df["Stock Splits"] = 0.0
        df.iloc[5, df.columns.get_loc("Stock Splits")] = 2.0
        self.assertNotEqual(e.decide("X", df, self.pos, 1.0, self.st).action, "SKIP")

    def test_real_crash_is_not_mistaken_for_a_split(self):
        df = self.df.copy()
        df["Stock Splits"] = 0.0                                # Yahoo says: no split
        df.iloc[-2:, [df.columns.get_loc(c) for c in ("Open", "High", "Low", "Close")]] *= 0.4
        d = e.decide("X", df, self.pos, float(self.df["Close"].iloc[-1]) * 0.8, self.st)
        self.assertEqual(d.action, "BREACH")



def flat_range(n=200, dips=(), dip_low=99.2, floor_vol=1e6):
    """Quiet range 99.5-100.5 with optional single-bar dips down to `dip_low`."""
    idx = pd.bdate_range(end="2026-07-17", periods=n)
    df = pd.DataFrame({"Open": 100.0, "High": 100.5, "Low": 99.5, "Close": 100.0, "Volume": floor_vol}, index=idx)
    for i in dips:
        df.iloc[i, df.columns.get_loc("Low")] = dip_low
        df.iloc[i, df.columns.get_loc("Close")] = dip_low + 0.2
    return df


class SupportQualityTests(unittest.TestCase):
    """The stop must sit BELOW a real floor, and a floor must be real."""
    ST = dict(DEFAULT_SETTINGS)

    def zones(self, df):
        a = float(e.atr(df).iloc[-1])
        return [z for z in e.support_zones(df, a, np.array([])) if z.qualified]

    def test_two_real_bounces_make_a_qualified_floor(self):
        df = flat_range(dips=(100, 140), dip_low=90.0)
        z = self.zones(df)
        self.assertEqual(len(z), 1)
        self.assertEqual((z[0].lo, z[0].touches), (90.0, 2))

    def test_shallow_wiggles_are_not_support(self):
        df = flat_range(dips=(100, 140), dip_low=99.2)          # dips bounce back under 1.5 ATR
        self.assertEqual(self.zones(df), [])
        out = e.initial_stop("X", df, 100.0, self.ST)
        self.assertIsNone(out["stop"])
        self.assertTrue(out["no_support"] and out["extended"])

    def test_two_lows_a_few_bars_apart_are_one_touch_not_two(self):
        df = flat_range(dips=(100, 104), dip_low=97.0)
        a = float(e.atr(df).iloc[-1])
        zs = [z for z in e.support_zones(df, a, np.array([])) if z.lo == 97.0]
        self.assertEqual([z.touches for z in zs], [1])             # one wiggle, not two bounces
        self.assertEqual(self.zones(df), [])

    def test_one_modest_bounce_is_not_enough_but_a_major_swing_is(self):
        self.assertEqual(self.zones(flat_range(dips=(120,), dip_low=97.0)), [])      # ~3 ATR, no volume node
        major = self.zones(flat_range(dips=(120,), dip_low=90.0))                    # ~10 ATR swing low
        self.assertEqual([z.lo for z in major], [90.0])
        self.assertTrue(e.Zone(90.0, 90.0, 1, hvn=True, reaction=3.0).qualified)     # decent bounce ON a volume node
        self.assertFalse(e.Zone(90.0, 90.0, 1, hvn=True, reaction=2.0).qualified)
        self.assertFalse(e.Zone(90.0, 90.0, 1, hvn=False, reaction=3.9).qualified)
        self.assertTrue(e.Zone(90.0, 90.0, 1, hvn=False, reaction=4.0).qualified)

    def test_a_fresh_retest_that_holds_counts_as_a_second_touch(self):
        df = flat_range(dips=(100,), dip_low=97.0)                # one confirmed ~3 ATR swing: not enough alone
        self.assertEqual(self.zones(df), [])
        df.iloc[-2, df.columns.get_loc("Low")] = 97.3             # price comes back down to it and holds
        df.iloc[-2, df.columns.get_loc("Close")] = 99.0
        self.assertEqual([z.lo for z in self.zones(df)], [97.0])
        broke = df.copy()
        broke.iloc[-2, broke.columns.get_loc("Low")] = 95.0       # ...but if it breaks through, no support
        self.assertEqual(self.zones(broke), [])

    def test_old_lows_are_stale(self):
        df = flat_range(n=300, dips=(40, 70), dip_low=90.0)     # both older than the 140-bar window
        self.assertEqual(self.zones(df), [])

    def test_a_floor_price_already_broken_is_not_support(self):
        df = flat_range(dips=(100, 140), dip_low=90.0)
        out = e.initial_stop("X", df, 85.0, self.ST)            # entry below the old floor
        self.assertIsNone(out["stop"])

    def test_support_far_away_is_flagged_extended(self):
        near = e.initial_stop("X", flat_range(dips=(100, 140), dip_low=97.0), 100.0, self.ST)
        far = e.initial_stop("X", flat_range(dips=(100, 140), dip_low=70.0), 100.0, self.ST)
        self.assertIsNotNone(far["stop"])
        self.assertTrue(far["extended"])
        if near["stop"] is not None:
            self.assertFalse(near["extended"])

    def test_recent_touches_define_the_floor_not_one_old_deeper_wick(self):
        df = flat_range()
        for i, low in ((70, 96.6), (150, 97.0), (185, 96.9)):     # one old wick, two recent defences
            df.iloc[i, df.columns.get_loc("Low")] = low
            df.iloc[i, df.columns.get_loc("Close")] = low + 0.2
        z = self.zones(df)
        self.assertEqual(len(z), 1)
        self.assertEqual((z[0].lo, z[0].hi), (96.9, 97.0))        # NOT 96.6 from months ago
        self.assertEqual(z[0].touches, 3)                          # the old touch still counts as evidence
        stop = e.initial_stop("X", df, 100.0, self.ST)["stop"]
        self.assertTrue(stop is None or stop < 96.9)

    def test_floor_with_only_old_touches_keeps_its_lowest_low(self):
        df = flat_range()
        for i, low in ((70, 96.6), (100, 97.0)):
            df.iloc[i, df.columns.get_loc("Low")] = low
            df.iloc[i, df.columns.get_loc("Close")] = low + 0.2
        z = self.zones(df)
        self.assertEqual([x.lo for x in z], [96.6])

    def test_a_breach_message_contains_only_the_breach(self):
        df = flat_range(dips=(100, 140), dip_low=90.0)
        pos = dict(entry_price=100.0, atr=1.0, opened_at=str(df.index[-30].date()), risk_status="RISK_ON", rung=0)
        d = e.decide("X", df, pos, 99.7, self.ST, since=df.index[-3])
        self.assertEqual(d.action, "BREACH")
        self.assertEqual(len(d.reasons), 1)

    def test_stop_is_always_below_the_floor_with_a_real_buffer(self):
        found = 0
        for floor in (90.0, 96.0, 80.0):
            df = flat_range(dips=(100, 140), dip_low=floor)
            a = float(e.atr(df).iloc[-1])
            out = e.initial_stop("X", df, 100.0, self.ST)
            if out["stop"] is None:
                continue
            found += 1
            lo = out["support_lo"]
            buffer = max(0.3 * a, 0.005 * lo)
            self.assertLessEqual(out["stop"], lo - buffer + 1e-9, f"floor {floor}")
            self.assertLess(out["stop"], lo)
        self.assertGreaterEqual(found, 2)

    def test_stop_below_floor_on_random_markets(self):
        found = 0
        for seed in range(40):
            df = bars(drift=0.0005, vol=0.02, seed=seed, end="2026-07-17")
            out = e.initial_stop("X", df, float(df["Close"].iloc[-1]), self.ST)
            if out["stop"] is None:
                continue
            found += 1
            a = out["atr"]
            self.assertLessEqual(out["stop"], out["support_lo"] - max(0.3 * a, 0.005 * out["support_lo"]) + 1e-9)
        self.assertGreater(found, 3)

    def test_penny_stock_buffer_is_still_below_the_floor(self):
        z = e.Zone(0.2800, 0.2850, 3)
        s = e.stop_under(z, 0.0004, 0.30)
        self.assertLess(s, 0.2800)
        self.assertLessEqual(s, 0.28 - 0.005 * 0.28 + 1e-9)

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
        self.assertIn("$40.00 → <b>$45.00</b>", msgs[0])
        self.assertIn("🟢⬆️ <b>UP</b>", msgs[0])                    # a raise is flagged with a green arrow
        self.assertNotIn("🟢⬆️ <b>BAD</b>", msgs[0])

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
        self.assertIsNotNone(out["stop"], "fixture must yield a structural stop or this test proves nothing")
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
        entry = float(df["Close"].iloc[-1])
        loose = e.initial_stop("X", df, entry, dict(DEFAULT_SETTINGS, extendedpct=90))
        tight = e.initial_stop("X", df, entry, dict(DEFAULT_SETTINGS, extendedpct=0.5))
        self.assertIsNotNone(loose["stop"])
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
        prev = float(df["Close"].iloc[150]) * 0.6
        raises = holds = 0
        for cut in range(150, 300, 5):
            d = e.decide("X", df.iloc[:cut], pos, prev, st)
            if d.action == "RAISE":
                step = d.new_stop - prev
                self.assertGreaterEqual(step + 0.01, 0.5 * d.metrics["atr"])
                self.assertGreaterEqual(step + 0.01, 0.015 * prev)
                prev, raises = d.new_stop, raises + 1
            elif d.action == "HOLD":
                self.assertEqual(d.new_stop, prev)             # sub-threshold steps are never saved
                holds += 1
        self.assertGreater(raises, 0)
        self.assertGreater(holds, 0)


# ------------------------------------------------------------------ pipeline (real DB, fake network)
END = "2026-07-17"
NIGHT_AFTER = datetime(2026, 7, 21, 1, 0, tzinfo=NY)       # the scheduled morning-after run


def make_downloader(frames):
    """yfinance-shaped download: ticker -> frame, wide MultiIndex, missing rows are NaN."""
    def fetch(symbols, **kw):
        assert kw.get("actions") is True and kw.get("auto_adjust") is False
        parts = {}
        for sym in symbols:
            f = frames.get(sym, frames["*"]).copy()
            f["Stock Splits"] = f.get("Stock Splits", 0.0)
            parts[sym] = f
        return pd.concat(parts, axis=1)
    return fetch


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "p.db"
        self.db = Database(self.path)
        self.db.initialize()
        self.climber = bars(drift=0.004, seed=11, end=END)
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, ticker="ZZZ", df=None, back=60):
        df = self.climber if df is None else df
        return self.db.create_active(dict(
            ticker=ticker, entry_price=float(df["Close"].iloc[-back]), atr=3.0, quantity=1, value_ils=100,
            currency="USD", fx_to_ils=1, fx_ticker=None, display_name=None, legacy=0,
            risk_status="RISK_ON", rung=0, opened_at=str(df.index[-back].date())))

    def go(self, frames=None, send=None, **kw):
        kw.setdefault("dry_run", False)
        return e.run(self.path, downloader=make_downloader(frames or {"*": self.climber}),
                     sector_lookup=lambda t: None, send=send or self.sent.extend, now=NIGHT_AFTER, **kw)

    def state(self):
        with sqlite3.connect(self.path) as c:
            return c.execute("SELECT position_id, stop, tier, asof FROM eod_stop_state ORDER BY 1").fetchall()

    def last_asof(self):
        with sqlite3.connect(self.path) as c:
            return c.execute("SELECT value FROM eod_run_state WHERE key='last_asof'").fetchone()

    def seed_low(self, p, frac=0.5):
        e.seed_stop(self.path, p["id"], p["ticker"], float(self.climber["Close"].iloc[-1]) * frac, "STRUCTURAL")

    # --- red 1: a failed Telegram send must not consume the alert
    def test_failed_send_leaves_state_untouched_and_next_run_resends(self):
        p = self.add()
        self.seed_low(p)
        before = self.state()

        def boom(msgs):
            raise e.TelegramError("telegram HTTP 429 after 4 attempts")
        with self.assertRaises(e.TelegramError):
            self.go(send=boom)
        self.assertEqual(self.state(), before, "stop was ratcheted although the alert was never delivered")
        self.assertIsNone(self.last_asof())

        decisions, _ = self.go()                                    # retry succeeds
        self.assertEqual([d.action for d in decisions], ["RAISE"])
        self.assertIn("Move your stop up: $", self.sent[0])
        self.assertGreater(self.state()[0][1], before[0][1])
        self.sent.clear()
        again, _ = self.go(force=True)                              # alert delivered once, now quiet
        self.assertEqual([d.action for d in again], ["HOLD"])
        self.assertEqual(self.sent, [])

    def test_dry_run_saves_nothing_and_sends_nothing(self):
        p = self.add()
        self.seed_low(p)
        before = self.state()
        self.go(dry_run=True)
        self.assertEqual((self.state(), self.sent, self.last_asof()), (before, [], None))

    # --- holidays / duplicate runs
    def test_run_is_skipped_when_no_new_session_completed(self):
        p = self.add()
        self.seed_low(p)
        self.go()
        ev = e.evaluate(self.path, downloader=make_downloader({"*": self.climber}),
                        sector_lookup=lambda t: None, now=NIGHT_AFTER)
        self.assertEqual(ev.decisions, [])
        self.assertIn("no new completed session", ev.note)
        self.sent.clear()
        self.assertEqual(self.go()[0], [])
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.go(force=True)[0]), 1)             # --force overrides the gate

    # --- red 3: --set-stop used to open a second writer and die with "database is locked"
    def test_set_stop_on_several_positions_uses_one_connection(self):
        self.add("AAA")
        self.add("BBB")
        t0 = time.time()
        self.go(overrides_stop={"AAA": 80.0, "BBB": 90.0}, force=True)
        self.assertLess(time.time() - t0, 3, "second writer waited on the first")
        stops = {r[0]: r[1] for r in self.state()}
        self.assertGreaterEqual(stops[1], 80.0)
        self.assertGreaterEqual(stops[2], 90.0)

    def test_open_state_waits_for_a_busy_writer_instead_of_failing_at_5s(self):
        c = e.open_state(self.path)
        self.assertEqual(c.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
        c.close()

    def test_persist_is_all_or_nothing(self):
        self.add("AAA")
        self.add("BBB")
        ev = e.evaluate(self.path, downloader=make_downloader({"*": self.climber}),
                        sector_lookup=lambda t: None, now=NIGHT_AFTER)
        ev.pending[1] = (ev.pending[1][0], None)                    # second record explodes mid-transaction
        with self.assertRaises(Exception):
            e.persist(self.path, ev)
        self.assertEqual(self.state(), [])
        self.assertIsNone(self.last_asof())

    # --- stale / partial data
    def test_stale_ticker_skipped_others_evaluated_and_run_stays_retryable(self):
        a, b = self.add("AAA"), self.add("BBB")
        self.seed_low(a), self.seed_low(b)
        frames = {"*": self.climber, "BBB": self.climber.iloc[:-3]}
        decisions, _ = self.go(frames)
        by = {d.ticker: d for d in decisions}
        self.assertEqual(by["BBB"].action, "SKIP")
        self.assertIn("older than the rest of the market", by["BBB"].reasons[0])
        self.assertEqual(by["AAA"].action, "RAISE")
        self.assertIn("Skipped: BBB", self.sent[0])
        self.assertIsNone(self.last_asof(), "a data gap must not close the day")

    def test_ticker_missing_from_download_is_reported_not_silent(self):
        self.add("AAA")
        self.add("GONE")
        frames = {"*": self.climber, "GONE": self.climber.iloc[0:0].assign()}
        decisions, _ = self.go(frames)
        self.assertEqual({d.ticker: d.action for d in decisions}["GONE"], "SKIP")

    # --- short history: protected by the ladder, told once
    # --- unseeded positions
    # --- adoption never invents a breach, and is one compact line
    # --- Tel Aviv tickers are out of scope
    def test_tase_tickers_are_not_evaluated_alerted_or_stored(self):
        self.add("AAA")
        self.add("IMCO.TA")
        self.add("lumi.ta")
        decisions, _ = self.go()
        self.assertEqual([d.ticker for d in decisions], ["AAA"])
        self.assertFalse(any(".TA" in m.upper() for m in self.sent))
        self.assertEqual([r[0] for r in self.state()], [1])

    def test_only_tase_positions_means_nothing_to_do(self):
        self.add("IMCO.TA")
        ev = e.evaluate(self.path, downloader=make_downloader({"*": self.climber}),
                        sector_lookup=lambda t: None, now=NIGHT_AFTER)
        self.assertEqual(ev.decisions, [])

    def test_tase_stop_records_are_never_seeded_or_shown(self):
        with sqlite3.connect(self.path) as c:
            e.ensure_schema(c)
            e.seed_stop_conn(c, 7, "IMCO.TA", 90.0, "MANUAL")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM eod_stop_state").fetchone()[0], 0)
            c.execute("INSERT INTO eod_stop_state VALUES (8, 'ARYT.TA', 50, 'NORMAL', '2026-10-16', 'x')")
        self.assertEqual(e.stop_info(self.path), {})

    # --- plain-language explanations
    JARGON = r"\b(ADX|RSI|ATR|DI|EMA|SMA|HVN|POC|volume node|bars?|ladder|tier|notch|distribution|exhaustion|divergence|chandelier)\b"

    # --- breach window
    def test_breach_in_a_session_the_last_run_missed_is_alerted(self):
        df = self.climber.copy()
        df.iloc[-3, df.columns.get_loc("Low")] = 10.0
        p = self.add("ZZZ", df)
        with sqlite3.connect(self.path) as c:
            e.ensure_schema(c)
            c.execute("INSERT INTO eod_stop_state VALUES (?, 'ZZZ', 60.0, 'CHART', ?, 'x')",
                      (p["id"], str(df.index[-6].date())))
        decisions, _ = self.go({"*": df})
        self.assertEqual(decisions[0].action, "BREACH")
        self.assertIn("🚨", self.sent[0])

    # --- split
    def test_split_is_reported_as_skip_end_to_end(self):
        df = self.climber.copy()
        df["Stock Splits"] = 0.0
        df.iloc[-10, df.columns.get_loc("Stock Splits")] = 4.0
        df.loc[df.index < df.index[-10], ["Open", "High", "Low", "Close"]] /= 4
        p = self.add("ZZZ", self.climber)
        e.seed_stop(self.path, p["id"], "ZZZ", float(self.climber["Close"].iloc[-1]) * 0.9, "STRUCTURAL")
        before = self.state()
        decisions, _ = self.go({"*": df})
        self.assertEqual(decisions[0].action, "SKIP")
        self.assertIn("stock split", self.sent[0])
        self.assertEqual(self.state(), before)

    # --- seeding semantics
    def test_seed_marks_the_last_completed_session_not_today(self):
        monday_morning = datetime(2026, 7, 20, 9, 0, tzinfo=NY)
        self.assertEqual(e.last_session_date(monday_morning).isoformat(), "2026-07-17")   # Friday
        self.assertEqual(e.last_session_date(datetime(2026, 7, 21, 17, 0, tzinfo=NY)).isoformat(), "2026-07-21")
        self.assertEqual(e.last_session_date(datetime(2026, 7, 21, 1, 0, tzinfo=NY)).isoformat(), "2026-07-20")

    def test_stop_info_reports_previous_stop_only_after_a_raise(self):
        p = self.add()
        self.seed_low(p)
        before = self.state()[0][1]
        self.go()
        info = e.stop_info(self.path)[p["id"]]
        self.assertEqual(info["stop_last_action"], "RAISE")
        self.assertAlmostEqual(info["stop_prev"], before)
        self.assertGreater(info["stop"], info["stop_prev"])
        self.assertNotIn(";", info["stop_why"])
        self.go(force=True)                                         # next review: no change
        self.assertIsNone(e.stop_info(self.path)[p["id"]]["stop_prev"])

    def test_log_index_exists(self):
        e.open_state(self.path).close()
        with sqlite3.connect(self.path) as c:
            names = [r[1] for r in c.execute("PRAGMA index_list('eod_stop_log')")]
        self.assertIn("idx_eod_stop_log_position", names)


# --------------------------------------------------------------------------- telegram transport
class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, retry_after=None, body=b"{}"):
    hdrs = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("https://api.telegram.org/botSECRET/sendMessage", code, "x", hdrs, io.BytesIO(body))


class Opener:
    def __init__(self, *outcomes):
        self.outcomes, self.bodies = list(outcomes), []

    def __call__(self, req, timeout=None):
        self.bodies.append(req.data.decode())
        out = self.outcomes.pop(0) if self.outcomes else FakeResponse()
        if isinstance(out, Exception):
            raise out
        return out


class TelegramTests(unittest.TestCase):
    def send(self, opener, msgs=("hi",), **kw):
        self.sleeps = []
        e.send_telegram(list(msgs), "SECRET", "1", opener=opener, sleep=self.sleeps.append, **kw)

    def test_429_honours_retry_after(self):
        op = Opener(http_error(429, retry_after=7), FakeResponse())
        self.send(op)
        self.assertEqual(self.sleeps, [7.0])
        self.assertEqual(len(op.bodies), 2)

    def test_429_retry_after_in_json_body(self):
        op = Opener(http_error(429, body=b'{"parameters": {"retry_after": 11}}'), FakeResponse())
        self.send(op)
        self.assertEqual(self.sleeps, [11.0])

    def test_5xx_and_network_errors_back_off_exponentially(self):
        op = Opener(http_error(502), urllib.error.URLError("blip"), FakeResponse())
        self.send(op)
        self.assertEqual(self.sleeps, [2.0, 4.0])

    def test_gives_up_after_the_attempt_budget_without_leaking_the_token(self):
        op = Opener(*[urllib.error.URLError("down")] * 4)
        with self.assertRaises(e.TelegramError) as cm:
            self.send(op)
        self.assertNotIn("SECRET", str(cm.exception))
        self.assertEqual(len(op.bodies), 4)

    def test_markup_rejected_falls_back_to_plain_text(self):
        op = Opener(http_error(400), FakeResponse())
        self.send(op, msgs=["<b>A&amp;B&lt;</b> up"])
        self.assertIn("parse_mode=HTML", op.bodies[0])
        self.assertNotIn("parse_mode", op.bodies[1])
        self.assertIn("A%26B%3C+up", op.bodies[1])

    def test_other_4xx_is_not_retried(self):
        op = Opener(http_error(403))
        with self.assertRaises(e.TelegramError):
            self.send(op)
        self.assertEqual(len(op.bodies), 1)

    def test_second_message_failing_raises_after_the_first_was_sent(self):
        op = Opener(FakeResponse(), *[http_error(500)] * 4)
        with self.assertRaises(e.TelegramError):
            self.send(op, msgs=["one", "two"])
        self.assertIn("text=one", op.bodies[0])

    def test_hostile_ticker_is_escaped_in_the_alert(self):
        d = e.Decision("A<B>&", 1.0, 1.0, 2.0, e.NORMAL, "RAISE", ["x < y"])
        text = e.format_alert([d], "d")[0]
        self.assertIn("A&lt;B&gt;&amp;", text)
        self.assertNotIn("<B>", text)



# ============================================================== chart-only engine
def series(closes, spread=0.5, overrides=None, volume=1e6):
    """Daily bars from a list of closes: High/Low = close +- spread, with per-bar overrides {i: (high, low)}."""
    n = len(closes)
    idx = pd.bdate_range(end="2026-07-17", periods=n)
    df = pd.DataFrame({"Open": closes, "High": [c + spread for c in closes], "Low": [c - spread for c in closes],
                       "Close": closes, "Volume": volume}, index=idx, dtype=float)
    for i, (h, l) in (overrides or {}).items():
        df.iloc[i, df.columns.get_loc("High")] = h
        df.iloc[i, df.columns.get_loc("Low")] = l
    return df


def pos_for(df, entry=None, back=30):
    return dict(entry_price=float(df["Close"].iloc[-back]) if entry is None else entry, atr=1.0,
                opened_at=str(df.index[-back].date()), risk_status="RISK_ON", rung=0)


def two_floors(upper=96.0, lower=88.0, n=220):
    """Quiet range around 100 with two defended floors: `upper` (dips at 100/150) and `lower` (dips at 120/170)."""
    df = flat_range(n=n)
    for i, low in ((100, upper), (150, upper), (120, lower), (170, lower)):
        df.iloc[i, df.columns.get_loc("Low")] = low
        df.iloc[i, df.columns.get_loc("Close")] = low + 0.6
    return df


class VolatilityTests(unittest.TestCase):
    def test_a_fresh_volatility_shock_widens_the_cushion_at_once(self):
        calm = flat_range(n=200)
        shock = calm.copy()
        shock.iloc[-5:, shock.columns.get_loc("High")] = 106.0
        shock.iloc[-5:, shock.columns.get_loc("Low")] = 94.0
        a_calm, a_shock = float(e.atr(calm).iloc[-1]), float(e.atr(shock).iloc[-1])
        self.assertAlmostEqual(e.true_range_size(calm, a_calm), a_calm, delta=0.2)
        self.assertGreater(e.true_range_size(shock, a_shock), 1.5 * a_shock)      # not hidden by the slow ATR

    def test_high_low_estimate_catches_wide_ranges_atr_smooths_over(self):
        df = flat_range(n=200)
        df.iloc[-15:, df.columns.get_loc("High")] = 103.0
        df.iloc[-15:, df.columns.get_loc("Low")] = 97.0
        a = float(e.atr(df).iloc[-1])
        self.assertGreaterEqual(e.true_range_size(df, a), a)


class ProvenSupportTests(unittest.TestCase):
    def test_volume_node_needs_two_defended_visits(self):
        df = flat_range(n=200)
        for i in (100, 150):
            df.iloc[i, df.columns.get_loc("Low")] = 96.8
            df.iloc[i, df.columns.get_loc("Close")] = 97.5
        a = float(e.atr(df).iloc[-1])
        zones = e.node_supports(df, a, np.array([97.0]), 100.0)
        self.assertEqual(len(zones), 1)
        self.assertEqual((zones[0].kinds, zones[0].touches, zones[0].proven), (("volume",), 2, True))
        one = flat_range(n=200)
        one.iloc[100, one.columns.get_loc("Low")] = 96.8
        one.iloc[100, one.columns.get_loc("Close")] = 97.5
        self.assertEqual(e.node_supports(one, a, np.array([97.0]), 100.0), [])      # a busy price nobody defended
        self.assertEqual(e.node_supports(df, a, np.array([103.0]), 100.0), [])      # nodes above the price are not support

    def test_volume_node_merges_with_the_swing_zone_it_sits_on(self):
        df = flat_range(n=200)
        for i in (100, 150):
            df.iloc[i, df.columns.get_loc("Low")] = 96.8
            df.iloc[i, df.columns.get_loc("Close")] = 97.5
        a = float(e.atr(df).iloc[-1])
        zs = e.find_supports(df, a, np.array([97.0]), 100.0)
        self.assertEqual(len(zs), 1)
        self.assertIn("volume", zs[0].kinds)
        self.assertIn("swing", zs[0].kinds)

    def flip_df(self, retest=True, fail=False):
        closes = [90.0] * 140
        for i, c in zip(range(58, 65), (90.5, 92, 94, 95, 93, 91, 90)):
            closes[i] = c
        for i in range(100, 140):
            closes[i] = 97.0 if i < 103 else 99.0
        ov = {61: (95.5, 94.0)}
        if retest:
            ov[112] = (99.5, 95.6)                         # price comes back to the old high and holds
        if fail:
            for i in range(120, 126):
                closes[i] = 93.0                           # ...but later falls back under it: breakout failed
        return series(closes, overrides=ov)

    def test_resistance_broken_and_retested_becomes_support(self):
        df = self.flip_df()
        a = float(e.atr(df).iloc[-1])
        z = e.flip_supports(df, a, np.array([]), float(df["Close"].iloc[-1]))
        self.assertEqual(len(z), 1)
        self.assertEqual(z[0].kinds, ("flip",))
        self.assertAlmostEqual(z[0].hi, 95.5)
        self.assertLessEqual(z[0].lo, z[0].hi)

    def test_flip_needs_a_holding_retest_or_a_volume_node(self):
        df = self.flip_df(retest=False)
        a = float(e.atr(df).iloc[-1])
        self.assertEqual(e.flip_supports(df, a, np.array([]), 99.0), [])
        self.assertEqual(len(e.flip_supports(df, a, np.array([95.4]), 99.0)), 1)    # volume proves it instead

    def test_failed_breakout_is_not_support(self):
        df = self.flip_df(fail=True)
        a = float(e.atr(df).iloc[-1])
        self.assertEqual(e.flip_supports(df, a, np.array([]), float(df["Close"].iloc[-1])), [])

    def test_a_high_price_never_rejected_is_not_resistance(self):
        closes = [90.0] * 140
        for i in range(100, 140):
            closes[i] = 99.0                               # a plain jump: no swing high with a rejection behind it
        df = series(closes)
        a = float(e.atr(df).iloc[-1])
        self.assertEqual(e.flip_supports(df, a, np.array([]), 99.0), [])


class ChartStopSelectionTests(unittest.TestCase):
    ST = dict(DEFAULT_SETTINGS)

    def decide(self, df, prev=0.0, ctx=None, since=None):
        return e.decide("X", df, pos_for(df), prev, self.ST, ctx, since=since)

    def test_stop_goes_under_the_nearest_proven_support_that_clears_the_cushion(self):
        df = two_floors()
        d = self.decide(df)
        self.assertEqual(d.action, "ENTRY")
        a = float(e.atr(df).iloc[-1])
        hvn, _, _ = e.volume_profile(df, a)
        close = float(df["Close"].iloc[-1])
        sup = [z for z in e.find_supports(df, a, hvn, close) if z.hi < close]
        eligible = [z for z in sup if close - e.stop_under(z, a, close) >= d.metrics["cushion"] * d.metrics["vol"]]
        self.assertTrue(eligible)
        best = max(eligible, key=lambda z: z.hi)
        self.assertAlmostEqual(d.new_stop, e.stop_under(best, a, close))
        self.assertLess(d.new_stop, best.lo)

    def test_every_stop_sits_below_a_proven_support_and_a_cushion_under_the_price(self):
        checked = 0
        for seed in range(30):
            df = bars(drift=0.0005, vol=0.02, seed=seed, end="2026-07-17")
            d = e.decide("X", df, pos_for(df), 0.0, self.ST)
            if d.action != "ENTRY":
                continue
            checked += 1
            a = float(e.atr(df).iloc[-1])
            hvn, _, _ = e.volume_profile(df, a)
            sup = e.find_supports(df, a, hvn, d.close)
            self.assertTrue(any(d.new_stop < z.lo for z in sup), seed)
            self.assertGreaterEqual(d.close - d.new_stop, e.RULES["cushion_min"] * d.metrics["vol"] - 1e-6, seed)
        self.assertGreater(checked, 5)

    @staticmethod
    def jumpy(floors):
        """Wide daily range (about 2 points), so the cushion is about 3.5-4 points."""
        df = flat_range(n=220)
        df["High"], df["Low"] = 101.0, 99.0
        for i, low in floors:
            df.iloc[i, df.columns.get_loc("Low")] = low
            df.iloc[i, df.columns.get_loc("Close")] = low + 1.5
        return df

    def test_support_inside_normal_daily_swings_is_skipped_for_the_next_one_down(self):
        df = self.jumpy([(100, 97.9), (150, 97.9), (120, 88.0), (170, 88.0)])
        d = self.decide(df)
        self.assertEqual(d.action, "ENTRY")
        self.assertLess(d.new_stop, 88.0)                                    # the 97 floor is inside the cushion
        self.assertIn("around $88.00", " ".join(d.reasons))

    def test_weak_sector_picks_a_nearer_support_but_a_strong_stock_ignores_it(self):
        df = two_floors(upper=97.4, lower=88.0)
        base = self.decide(df)
        weak = self.decide(df, ctx=e.Context("XLK", True, "x", stock_vs_sector_10d=-0.05))
        self.assertGreaterEqual(weak.new_stop, base.new_stop)                  # never a deeper (looser) level
        strong = self.decide(df, ctx=e.Context("XLK", True, "x", stock_vs_sector_10d=0.10))
        self.assertEqual(strong.tier, base.tier)

    def test_no_proven_support_means_no_stop_and_a_flag(self):
        df = flat_range(dips=(100, 140), dip_low=99.2)                         # only shallow wiggles
        d = self.decide(df)
        self.assertEqual((d.action, d.new_stop, d.source), ("NO_SUPPORT", 0.0, "NONE"))
        self.assertIn("No chart support below, manual decision required.", d.reasons)

    def test_price_at_multi_month_lows_says_so(self):
        falling = bars(drift=-0.01, vol=0.01, seed=4, end="2026-07-17")
        d = e.decide("X", falling, pos_for(falling), 0.0, self.ST)
        self.assertEqual(d.action, "NO_SUPPORT")
        self.assertIn("lowest level in about 7 months", d.reasons[0])

    def test_short_history_has_no_chart_support_and_no_distance_fallback(self):
        df = flat_range(n=60)
        d = self.decide(df)
        self.assertEqual((d.action, d.new_stop), ("NO_SUPPORT", 0.0))
        self.assertIn("Too new to read chart levels", d.reasons[0])
        keep = self.decide(df, prev=95.0)
        self.assertEqual((keep.action, keep.new_stop), ("HOLD", 95.0))

    def test_support_too_close_is_explained(self):
        df = self.jumpy([(100, 97.9), (150, 97.9)])
        d = self.decide(df)
        self.assertEqual(d.action, "NO_SUPPORT")
        self.assertIn("too close", d.reasons[0])

    def test_chart_stop_only_rises(self):
        df = two_floors()
        first = self.decide(df)
        up = self.decide(df, prev=first.new_stop - 5.0)
        self.assertEqual(up.action, "RAISE")
        self.assertAlmostEqual(up.new_stop, first.new_stop)
        down = self.decide(df, prev=first.new_stop + 0.5)
        self.assertEqual(down.action, "HOLD")
        self.assertEqual(down.new_stop, first.new_stop + 0.5)

    def test_tiny_steps_are_not_alerted(self):
        df = two_floors()
        first = self.decide(df)
        d = self.decide(df, prev=first.new_stop - 0.05)
        self.assertEqual(d.action, "HOLD")

    def test_breach_means_the_support_broke_and_keeps_the_stop(self):
        df = two_floors()
        stop = self.decide(df).new_stop
        broken = df.copy()
        broken.iloc[-1, broken.columns.get_loc("Low")] = stop - 1.0
        d = e.decide("X", broken, pos_for(broken), stop, self.ST, since=broken.index[-3])
        self.assertEqual((d.action, d.new_stop), ("BREACH", stop))
        self.assertEqual(len(d.reasons), 1)

    def test_explanation_names_the_chart_level_in_plain_words(self):
        d = self.decide(two_floors())
        text = " ".join(d.reasons)
        self.assertIn("just under a support level around", text)
        self.assertNotRegex(text, r"days' worth|normal price swings")

    JARGON = r"\b(ADX|RSI|ATR|DI|EMA|SMA|HVN|POC|VRVP|bars?|ladder|tier|notch|distribution|exhaustion|divergence|chandelier|ceiling)\b"

    def test_every_explanation_is_plain_language(self):
        seen = []
        for df in (two_floors(), two_floors(upper=97.4), flat_range(dips=(100, 140), dip_low=99.2), flat_range(n=60),
                   bars(drift=0.004, seed=11, end="2026-07-17"), bars(drift=-0.01, vol=0.01, seed=4, end="2026-07-17")):
            for ctx in (e.Context(), e.Context("XLK", True, "x", stock_vs_sector_10d=-0.05)):
                for prev in (0.0, 50.0):
                    seen += e.decide("X", df, pos_for(df), prev, self.ST, ctx, since=df.index[-6]).reasons
        seen += e.initial_stop("X", two_floors(), 100.0, self.ST)["reasons"]
        self.assertGreater(len(set(seen)), 8)
        for text in set(seen):
            self.assertIsNone(re.search(self.JARGON, text, re.I), text)
            self.assertNotRegex(text, r"\\d+\\.\\d{3,}|[<>]=?\\s*\\d", text)
            self.assertTrue(text.rstrip().endswith((".", ")")), text)

    def test_initial_stop_uses_the_same_proven_supports(self):
        df = two_floors()
        out = e.initial_stop("X", df, 100.0, self.ST)
        self.assertIsNotNone(out["stop"])
        self.assertLess(out["stop"], out["support_lo"])
        self.assertIn("swing", out["kinds"])
        none = e.initial_stop("X", flat_range(dips=(100, 140), dip_low=99.2), 100.0, self.ST)
        self.assertIsNone(none["stop"])
        self.assertTrue(none["no_support"] and none["extended"])


class AlertFormatTests(unittest.TestCase):
    def dec(self, action, t="AAA", **kw):
        base = dict(ticker=t, close=50.0, prev_stop=40.0, new_stop=45.0, tier=e.NORMAL, action=action,
                    reasons=["The stop now sits just under a support level around $44.00."])
        base.update(kw)
        return e.Decision(**base)

    def test_new_chart_stops_replacing_old_ones_are_explained(self):
        d = self.dec("ENTRY", prev_stop=0.0, notice="replaces the earlier distance-based stop of $41.20")
        text = e.format_alert([d], "d")[0]
        self.assertIn("🆕 <b>AAA</b>", text)
        self.assertIn("Chart-based stop: <b>$45.00</b> (replaces the earlier distance-based stop of $41.20)", text)
        self.assertIn("1 new chart stop", text)

    def test_positions_without_support_are_grouped_by_cause_in_one_block_each(self):
        cause = "The price is at or near its lowest level in about 7 months, so there is no proven support underneath to anchor a stop to."
        ds = [self.dec("NO_SUPPORT", t, new_stop=0.0, prev_stop=0.0, reasons=[cause, e.NO_SUPPORT_SENTENCE])
              for t in ("BBB", "AAA", "CCC")]
        ds.append(self.dec("NO_SUPPORT", "DDD", new_stop=0.0, prev_stop=0.0,
                           reasons=["There is no proven support level underneath the price.", e.NO_SUPPORT_SENTENCE]))
        text = e.format_alert(ds, "d")[0]
        self.assertEqual(text.count("No chart support below, manual decision required"), 2)
        self.assertIn("AAA $50.00 · BBB $50.00 · CCC $50.00", text)
        self.assertIn("4 without chart support", text)

    def test_breach_alert_points_to_the_hold_decision(self):
        d = self.dec("BREACH", reasons=["On Oct 16 it traded down to $39.00, at or below your stop of $40.00."])
        text = e.format_alert([d], "d")[0]
        self.assertIn("Your chart-based stop of $40.00 was reached", text)
        self.assertIn("Holding on purpose", text)

    def test_scorecard_appears_only_next_to_other_news(self):
        holds = [dict(ticker="ZZZ", since="2026-10-28", price_then=38.10, price_now=39.50)]
        quiet = e.format_alert([self.dec("HOLD")], "d", True, holds)
        self.assertEqual(quiet, [])
        text = e.format_alert([self.dec("RAISE")], "d", True, holds)[0]
        self.assertIn("📌 Holding on purpose: ZZZ since Oct 28 ($38.10 → $39.50, +3.7%)", text)


class ChartPipeline(Pipeline):
    """Pipeline behaviour of the chart-only engine (reuses Pipeline's DB/fake-network helpers)."""
    def setUp(self):
        super().setUp()
        self.floors = two_floors()
        self.falling = bars(drift=-0.01, vol=0.01, seed=4, end=END)

    def seed(self, p, stop, tier="CHART"):
        with sqlite3.connect(self.path) as c:
            e.ensure_schema(c)
            c.execute("INSERT OR REPLACE INTO eod_stop_state VALUES (?, ?, ?, ?, ?, 'x')",
                      (p["id"], p["ticker"], stop, tier, str(self.floors.index[-6].date())))

    def log_actions(self):
        with sqlite3.connect(self.path) as c:
            return [r[0] for r in c.execute("SELECT action FROM eod_stop_log ORDER BY id")]

    # --- first chart stop
    def test_first_run_records_a_chart_stop_with_its_reasons(self):
        self.add("ZZZ", self.floors)
        decisions, _ = self.go({"*": self.floors})
        self.assertEqual(decisions[0].action, "ENTRY")
        self.assertEqual(self.state()[0][2], "CHART")
        self.assertIn("🆕 <b>ZZZ</b>", self.sent[0])
        self.assertIn("just under a support level around", self.sent[0])
        self.sent.clear()
        again, _ = self.go({"*": self.floors}, force=True)
        self.assertEqual(again[0].action, "HOLD")
        self.assertEqual(self.sent, [])

    # --- the one-time reset of earlier distance-based stops
    def test_earlier_distance_based_stops_are_replaced_by_chart_stops_once(self):
        p = self.add("ZZZ", self.floors)
        self.seed(p, 97.31, tier="TIGHT")
        decisions, _ = self.go({"*": self.floors})
        self.assertEqual(decisions[0].action, "ENTRY")
        self.assertIn("replaces the earlier distance-based stop of $97.31", self.sent[0])
        self.assertEqual(self.state()[0][2], "CHART")
        self.assertLess(self.state()[0][1], 97.31)                       # it may go down: the old number was not chart-based
        self.sent.clear()
        self.go({"*": self.floors}, force=True)
        self.assertEqual(self.sent, [])

    def test_a_user_set_or_calculator_stop_is_kept_as_a_chart_anchor(self):
        for tier in ("MANUAL", "STRUCTURAL", "CHART"):
            self.tearDown(); self.setUp()
            p = self.add("ZZZ", self.floors)
            self.seed(p, 80.0, tier=tier)
            decisions, _ = self.go({"*": self.floors})
            self.assertEqual(decisions[0].action, "RAISE", tier)

    # --- no support
    def test_no_support_is_flagged_once_then_reminded_weekly(self):
        self.add("DOWN", self.falling)
        decisions, _ = self.go({"*": self.falling})
        self.assertEqual(decisions[0].action, "NO_SUPPORT")
        self.assertIn("No chart support below, manual decision required", self.sent[0])
        self.assertEqual(self.state()[0][1:3], (0.0, "NONE"))
        self.assertIsNone(e.stop_info(self.path)[1]["stop"])
        self.sent.clear()
        self.go({"*": self.falling}, force=True)                          # next day: already flagged
        self.assertEqual(self.sent, [])
        with sqlite3.connect(self.path) as c:                             # a week later it reminds again
            c.execute("UPDATE eod_stop_log SET asof = '2026-07-01'")
        self.go({"*": self.falling}, force=True)
        self.assertIn("No chart support below", self.sent[0])

    def test_no_support_never_invents_a_stop_even_for_a_position_far_in_profit(self):
        df = series([90.0] * 250)
        p = self.add("FLAT", df, back=100)
        decisions, _ = self.go({"*": df})
        self.assertEqual((decisions[0].action, decisions[0].new_stop), ("NO_SUPPORT", 0.0))

    def test_support_appearing_later_turns_the_flag_into_a_chart_stop(self):
        p = self.add("ZZZ", self.floors)
        self.seed(p, 0.0, tier="NONE")
        decisions, _ = self.go({"*": self.floors})
        self.assertEqual(decisions[0].action, "ENTRY")
        self.assertEqual(self.state()[0][2], "CHART")

    # --- holding on purpose
    def breach_setup(self):
        p = self.add("ZZZ", self.floors)
        stop = e.decide("ZZZ", self.floors, pos_for(self.floors), 0.0, dict(DEFAULT_SETTINGS)).new_stop
        broken = self.floors.copy()
        broken.iloc[-1, broken.columns.get_loc("Low")] = stop - 1.0
        broken.iloc[-1, broken.columns.get_loc("Close")] = stop + 1.0
        self.seed(p, stop)
        return p, stop, broken

    def preview(self, p, df):
        pos = self.db.get_active(p["id"])
        return e.hold_preview(pos, dict(DEFAULT_SETTINGS), make_downloader({"*": df}))

    def test_hold_preview_changes_nothing(self):
        p, stop, broken = self.breach_setup()
        before = self.state()
        v = self.preview(p, broken)
        self.assertIn("new_stop", v)
        self.assertEqual(self.state(), before)

    def test_holding_on_purpose_rebases_the_stop_and_stops_the_breach_alerts(self):
        p, stop, broken = self.breach_setup()
        decisions, _ = self.go({"*": broken})
        self.assertEqual(decisions[0].action, "BREACH")
        v = self.preview(p, broken)
        res = e.record_hold(self.path, self.db.get_active(p["id"]), "thesis", "earnings soon", v)
        self.assertEqual(res["hold_count"], 1)
        self.assertEqual(self.log_actions()[-1], "OVERRIDE")
        self.sent.clear()
        after, _ = self.go({"*": broken}, force=True)
        self.assertNotEqual(after[0].action, "BREACH")
        self.assertFalse(any("🚨" in m for m in self.sent))
        info = e.stop_info(self.path)[p["id"]]
        self.assertEqual(info["hold_count"], 1)
        self.assertIsNotNone(info["held_since"])

    def test_holding_with_no_support_leaves_no_stop_and_silences_the_flag(self):
        self.add("DOWN", self.falling)
        self.go({"*": self.falling})
        p = self.db.list_active()[0]
        v = e.hold_preview(p, dict(DEFAULT_SETTINGS), make_downloader({"*": self.falling}))
        self.assertIsNone(v["new_stop"])
        e.record_hold(self.path, p, "liquidity", None, v)
        with sqlite3.connect(self.path) as c:
            c.execute("UPDATE eod_stop_log SET asof = '2026-07-01'")      # even a week later: decided, not forgotten
        self.sent.clear()
        self.go({"*": self.falling}, force=True)
        self.assertEqual(self.sent, [])
        info = e.stop_info(self.path)[p["id"]]
        self.assertEqual((info["held_reason"], info["hold_count"], info["stop"]), ("liquidity", 1, None))

    def test_a_second_hold_needs_the_ticker_typed(self):
        p, stop, broken = self.breach_setup()
        pos = self.db.get_active(p["id"])
        v = self.preview(p, broken)
        e.record_hold(self.path, pos, "thesis", None, v)
        with self.assertRaises(PermissionError):
            e.record_hold(self.path, pos, "thesis", None, v)
        with self.assertRaises(PermissionError):
            e.record_hold(self.path, pos, "thesis", None, v, "WRONG")
        self.assertEqual(e.record_hold(self.path, pos, "thesis", None, v, "zzz")["hold_count"], 2)

    def test_hold_reason_must_be_one_of_the_choices(self):
        p, stop, broken = self.breach_setup()
        with self.assertRaises(ValueError):
            e.record_hold(self.path, self.db.get_active(p["id"]), "because", None, self.preview(p, broken))

    def test_the_scorecard_shows_what_happened_since_the_hold(self):
        p, stop, broken = self.breach_setup()
        e.record_hold(self.path, self.db.get_active(p["id"]), "earnings", None, self.preview(p, broken))
        better = self.floors.copy()
        better.iloc[-1, better.columns.get_loc("Close")] = 103.0
        better.iloc[-1, better.columns.get_loc("High")] = 103.5
        self.sent.clear()
        other = self.add("OTHER", self.falling)
        self.go({"*": better, "OTHER": self.falling}, force=True)
        self.assertTrue(any("📌 Holding on purpose: ZZZ since" in m for m in self.sent), self.sent)

    def test_deleting_a_position_removes_its_holds(self):
        p, stop, broken = self.breach_setup()
        e.record_hold(self.path, self.db.get_active(p["id"]), "thesis", None, self.preview(p, broken))
        with sqlite3.connect(self.path) as c:
            e.purge_position(c, p["id"])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM eod_holds").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
