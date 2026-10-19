import io
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
        for i in (60, 120):
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

    def test_stop_never_moves_down(self):
        for cut in range(150, 300, 7):
            sub = self.df.iloc[:cut]
            pos = position(sub, 40)
            prev = e.ladder_stop(sub, pos, self.st)[0]
            for k in range(0, 20):
                d = e.decide("X", self.df.iloc[:cut + k], pos, prev, self.st)
                self.assertGreaterEqual(d.new_stop, prev)
                prev = d.new_stop

    def test_raise_stays_below_price_even_after_tick_rounding(self):
        raises = 0
        for cut in range(150, 300, 10):
            sub = self.df.iloc[:cut]
            prev = float(sub["Close"].iloc[-1]) * 0.5            # far below: forces a RAISE
            d = e.decide("X", sub, position(sub, 40), prev, self.st)
            self.assertEqual(d.action, "RAISE", f"cut {cut}")
            raises += 1
            cap = e._floor_tick(d.close - e.RULES["tight_clearance_atr"] * d.metrics["atr"], e._tick(d.close))
            self.assertLessEqual(d.new_stop, cap + 1e-9)
        self.assertGreater(raises, 0)

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

    def test_short_history_falls_back_to_the_atr_ladder(self):
        sub = self.df.iloc[:60]
        pos = position(sub, 30)
        d = e.decide("X", sub, pos, float(sub["Close"].iloc[-1]) * 0.5, self.st)
        self.assertEqual(d.source, "LADDER")
        self.assertIn(d.action, ("RAISE", "HOLD"))
        self.assertGreaterEqual(d.new_stop, d.prev_stop)
        self.assertLess(d.new_stop, d.close)

    def test_breach_on_a_missed_session_is_still_reported(self):
        df = self.df.copy()
        df.iloc[-3, df.columns.get_loc("Low")] = 40.0           # dipped through the stop 2 sessions ago
        d = e.decide("X", df, self.pos, 60.0, self.st, since=df.index[-6])
        self.assertEqual(d.action, "BREACH")
        self.assertIn(str(df.index[-3].date()), d.reasons[0])
        self.assertEqual(d.new_stop, 60.0)
        # a review already made after that dip must not report it again
        again = e.decide("X", df, self.pos, 60.0, self.st, since=df.index[-1])
        self.assertNotEqual(again.action, "BREACH")

    def test_stale_ticker_is_skipped_not_judged(self):
        d = e.decide("X", self.df.iloc[:-3], self.pos, 60.0, self.st, asof=self.df.index[-1])
        self.assertEqual(d.action, "SKIP")
        self.assertIn("stale", d.reasons[0])

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
        self.assertIn("split detected", d.reasons[0])

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

    def test_all_time_high_with_no_support_below_does_not_crash(self):
        df = bars(drift=0.01, vol=0.004, seed=9)                # relentless climb, price far above every node
        pos = position(df, 60)
        d = e.decide("X", df, pos, float(df["Close"].iloc[-60]) * 0.8, self.st)
        self.assertIn(d.action, ("RAISE", "HOLD"))
        self.assertLess(d.new_stop, d.close)


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
        entry = float(df["Close"].iloc[-1]) * 1.3
        loose = e.initial_stop("X", df, entry, dict(DEFAULT_SETTINGS, extendedpct=90))
        tight = e.initial_stop("X", df, entry, dict(DEFAULT_SETTINGS, extendedpct=5))
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
        self.assertIn("New $", self.sent[0])
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
        self.assertIn("stale", by["BBB"].reasons[0])
        self.assertEqual(by["AAA"].action, "RAISE")
        self.assertIn("Not evaluated: BBB", self.sent[0])
        self.assertIsNone(self.last_asof(), "a data gap must not close the day")

    def test_ticker_missing_from_download_is_reported_not_silent(self):
        self.add("AAA")
        self.add("GONE")
        frames = {"*": self.climber, "GONE": self.climber.iloc[0:0].assign()}
        decisions, _ = self.go(frames)
        self.assertEqual({d.ticker: d.action for d in decisions}["GONE"], "SKIP")

    # --- short history: protected by the ladder, told once
    def test_ipo_uses_ladder_and_is_announced_only_once(self):
        ipo = self.climber.iloc[-60:]
        p = self.add("NEW", ipo, back=30)
        e.seed_stop(self.path, p["id"], "NEW", float(ipo["Close"].iloc[-1]) * 0.5, "STRUCTURAL")
        frames = {"*": self.climber, "NEW": ipo}
        self.go(frames)
        self.assertIn("ATR ladder only: NEW (60 bars)", self.sent[0])
        self.assertEqual(self.state()[0][2], "LADDER")
        self.sent.clear()
        self.go(frames, force=True)
        self.assertFalse(any("NEW (60 bars)" in m for m in self.sent))

    # --- unseeded positions
    def test_first_run_announces_the_adopted_ladder_stop_once(self):
        self.add()
        self.go()
        self.assertIn("Starting stops, confirm the broker order matches: ZZZ $", self.sent[0])
        self.assertEqual(len(self.state()), 1)
        self.sent.clear()
        self.go(force=True)
        self.assertFalse(any("Starting stops" in m for m in self.sent))


    # --- adoption never invents a breach, and is one compact line
    def test_adopting_a_ladder_stop_already_above_the_price_starts_from_todays_price(self):
        falling = bars(drift=-0.01, vol=0.01, seed=4, end=END)
        p = self.add("DOWN", falling, back=60)
        entry_ladder = e.ladder_stop(falling, p, dict(DEFAULT_SETTINGS))[0]
        close = float(falling["Close"].iloc[-1])
        self.assertGreater(entry_ladder, close, "fixture must start with a ladder stop above the market")
        decisions, _ = self.go({"*": falling})
        d = decisions[0]
        self.assertNotEqual(d.action, "BREACH")
        self.assertLess(d.new_stop, close)
        self.assertGreater(d.new_stop, 0)
        self.assertEqual(self.state()[0][2], "BASELINE")
        self.assertIn(f"DOWN ${d.new_stop:,.2f}*", self.sent[0])
        self.assertNotIn("BREACH", self.sent[0])
        self.sent.clear()
        again, _ = self.go({"*": falling}, force=True)           # nothing new: quiet, still no breach
        self.assertEqual(again[0].action, "HOLD")
        self.assertFalse(any("Starting stops" in m for m in self.sent))

    def test_adoption_never_reports_a_breach_even_when_the_last_low_is_under_the_ladder(self):
        df = self.climber.copy()
        p = self.add("ZZZ", df)
        ladder = e.ladder_stop(df, p, dict(DEFAULT_SETTINGS))[0]
        df.iloc[-1, df.columns.get_loc("Low")] = ladder * 0.99
        decisions, _ = self.go({"*": df})
        self.assertNotEqual(decisions[0].action, "BREACH")

    def test_many_adopted_positions_share_one_notice_line(self):
        for t in ("AAA", "BBB", "CCC"):
            self.add(t)
        self.go()
        text = self.sent[0]
        self.assertEqual(text.count("ℹ️"), 1)
        for t in ("AAA", "BBB", "CCC"):
            self.assertIn(f"{t} $", text)
        self.assertLess(len(text), 400)

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

    # --- breach window
    def test_breach_in_a_session_the_last_run_missed_is_alerted(self):
        df = self.climber.copy()
        df.iloc[-3, df.columns.get_loc("Low")] = 10.0
        p = self.add("ZZZ", df)
        with sqlite3.connect(self.path) as c:
            e.ensure_schema(c)
            c.execute("INSERT INTO eod_stop_state VALUES (?, 'ZZZ', 60.0, 'NORMAL', ?, 'x')",
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
        self.assertIn("split detected", self.sent[0])
        self.assertEqual(self.state(), before)

    # --- seeding semantics
    def test_seed_marks_the_last_completed_session_not_today(self):
        monday_morning = datetime(2026, 7, 20, 9, 0, tzinfo=NY)
        self.assertEqual(e.last_session_date(monday_morning).isoformat(), "2026-07-17")   # Friday
        self.assertEqual(e.last_session_date(datetime(2026, 7, 21, 17, 0, tzinfo=NY)).isoformat(), "2026-07-21")
        self.assertEqual(e.last_session_date(datetime(2026, 7, 21, 1, 0, tzinfo=NY)).isoformat(), "2026-07-20")

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


if __name__ == "__main__":
    unittest.main()
