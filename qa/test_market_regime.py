import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import market_regime as m

NY = m.NY


def synthetic(start="1985-10-01", end="2026-10-30", seed=1, with_extras=True):
    """Random-walk NDX with a volatility-linked VIX; VIX3M/QQQ/QQEW only from 2006, like reality."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, end)
    vol = np.where(rng.random(len(idx)) < 0.02, 0.03, 0.011)
    ret = rng.normal(0.0004, vol)
    ndx = pd.Series(100 * np.cumprod(1 + ret), index=idx)
    realized = pd.Series(np.abs(ret), index=idx).rolling(10, min_periods=1).mean()
    vix = (12 + 900 * realized).clip(9, 80)
    out = {"ndx": ndx, "vix": vix[vix.index >= "1990-01-02"]}
    if with_extras:
        late = idx >= "2006-07-17"
        out["vix3m"] = (vix * 0.93)[late]
        late2 = idx >= "2006-05-02"
        qqq = pd.Series(50 * np.cumprod(1 + rng.normal(0.0004, 0.011, late2.sum())), index=idx[late2])
        out["qqq"] = qqq
        out["qqew"] = qqq * pd.Series(np.cumprod(1 + rng.normal(0.0, 0.002, late2.sum())), index=idx[late2])
    return out


def row(**kw):
    base = dict(dist_sma=0.05, slope=0.01, dd=-0.02, fall20=-0.01, vix=15.0, term=0.9, narrow=0.0)
    base.update(kw)
    return pd.Series(base)


class FeatureTests(unittest.TestCase):
    def test_features_never_use_the_future(self):
        s = synthetic(end="2012-12-31")
        full = m.compute_features(m.build_frame(s))
        cut_date = pd.Timestamp("2009-03-09")
        part = m.compute_features(m.build_frame(m.cut_asof(s, cut_date)))
        a, b = full.loc[:cut_date], part.loc[:cut_date]
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-12, atol=1e-12)

    def test_drawdown_is_measured_from_the_52_week_high(self):
        idx = pd.bdate_range("2020-01-01", periods=400)
        c = pd.Series(np.linspace(100, 200, 400), index=idx)
        c.iloc[-1] = 150.0
        df = pd.DataFrame({"close": c, "vix": 20.0, "vix3m": np.nan, "qqq": np.nan, "qqew": np.nan})
        f = m.compute_features(df)
        self.assertAlmostEqual(f["dd"].iloc[-1], 150.0 / c.iloc[-252:-1].max() - 1, places=9)

    def test_shock_is_scaled_by_yesterdays_volatility_not_todays(self):
        rng = np.random.default_rng(0)
        idx = pd.bdate_range("2010-01-01", periods=400)
        ret = rng.normal(0, 0.01, 400)
        ret[-1] = -0.05                                            # a 5-sigma day after a calm period
        c = pd.Series(100 * np.cumprod(1 + ret), index=idx)
        df = pd.DataFrame({"close": c, "vix": 20.0, "vix3m": np.nan, "qqq": np.nan, "qqew": np.nan})
        f = m.compute_features(df)
        self.assertLess(f["z"].iloc[-1], -4.0)
        self.assertTrue(np.isnan(f["z"].iloc[10]))                 # warm-up days have no z

    def test_same_drop_is_rarer_in_a_calm_market_than_a_stormy_one(self):
        idx = pd.bdate_range("2010-01-01", periods=300)
        def z_for(vol):
            rng = np.random.default_rng(3)
            r = rng.normal(0, vol, 300)
            r[-1] = -0.035
            c = pd.Series(100 * np.cumprod(1 + r), index=idx)
            return m.compute_features(pd.DataFrame({"close": c, "vix": 20.0, "vix3m": np.nan, "qqq": np.nan,
                                                   "qqew": np.nan}))["z"].iloc[-1]
        self.assertLess(z_for(0.005), z_for(0.02) - 1.0)

    def test_term_structure_and_breadth_flags(self):
        flags = {f.key: f.on for f in m.red_flags(row(term=1.05, narrow=1.0))}
        self.assertTrue(flags["inverted"] and flags["narrow"])
        flags = {f.key: f.on for f in m.red_flags(row(term=0.9, narrow=0.0))}
        self.assertFalse(flags["inverted"] or flags["narrow"])
        flags = {f.key: f.on for f in m.red_flags(row(term=np.nan, narrow=np.nan))}
        self.assertIsNone(flags["inverted"])
        self.assertIsNone(flags["narrow"])


class ForwardOutcomeTests(unittest.TestCase):
    def test_forward_return_and_max_further_drawdown(self):
        c = pd.Series([100, 90, 95, 120, 80, 110.0], index=pd.bdate_range("2020-01-01", periods=6))
        fwd, mdd = m.forward_outcomes(c, 3)
        self.assertAlmostEqual(fwd.iloc[0], 120 / 100 - 1)
        self.assertAlmostEqual(mdd.iloc[0], 90 / 100 - 1)                   # lowest of the next 3 closes
        self.assertAlmostEqual(fwd.iloc[1], 80 / 90 - 1)
        self.assertAlmostEqual(mdd.iloc[1], 80 / 90 - 1)
        self.assertTrue(fwd.iloc[3:].isna().all() and mdd.iloc[3:].isna().all())   # horizon not complete yet

    def test_a_rising_market_has_zero_further_drawdown(self):
        c = pd.Series(np.arange(100.0, 120.0), index=pd.bdate_range("2020-01-01", periods=20))
        _, mdd = m.forward_outcomes(c, 5)
        self.assertEqual(float(mdd.dropna().max()), 0.0)


class ClassifyTests(unittest.TestCase):
    def label(self, **kw):
        r = row(**kw)
        return m.classify(r, m.red_flags(r))

    def test_calm(self):
        self.assertEqual(self.label(), m.LABEL_CALM)

    def test_normal_pullback(self):
        self.assertEqual(self.label(dd=-0.06, vix=19.0), m.LABEL_PULLBACK)

    def test_elevated_by_drawdown_or_vix(self):
        self.assertEqual(self.label(dd=-0.12), m.LABEL_ELEVATED)
        self.assertEqual(self.label(dd=-0.05, vix=27.0), m.LABEL_ELEVATED)

    def test_bear_needs_a_broken_trend_or_a_deep_stressed_drop(self):
        self.assertEqual(self.label(dist_sma=-0.08, slope=-0.02, dd=-0.18, vix=28.0), m.LABEL_BEAR)
        self.assertEqual(self.label(dd=-0.24, vix=34.0, term=1.1, fall20=-0.15), m.LABEL_BEAR)
        self.assertEqual(self.label(dd=-0.32), m.LABEL_BEAR)
        self.assertEqual(self.label(dist_sma=-0.02, slope=0.01, dd=-0.13, vix=22.0), m.LABEL_ELEVATED)


class AnalogTests(unittest.TestCase):
    @staticmethod
    def frame(inv_before=np.nan, inv_after=0.0, cur_inv=0.0, n_start="1995-01-02", n_end="2010-12-31"):
        idx = pd.bdate_range(n_start, n_end)
        f = pd.DataFrame(index=idx)
        f["close"] = np.linspace(100, 300, len(idx))
        f["trend"], f["vix_bin"] = 0.0, 1.0
        block = (np.arange(len(idx)) // 60) % 3 == 0              # matching state comes in separate 60-day episodes
        f["dd_bin"] = np.where(block, 4.0, 1.0)
        f.iloc[-1, f.columns.get_loc("dd_bin")] = 4.0
        f["inv"] = np.where(idx < "2006-07-17", inv_before, inv_after)
        f["narrow"] = np.where(idx < "2006-07-17", np.nan, 0.0)
        f.iloc[-1, f.columns.get_loc("inv")] = cur_inv
        return f

    def test_days_before_2006_without_term_structure_still_match(self):
        f = self.frame()
        mask, level, low = m.select_analogs(f)
        self.assertTrue(level.startswith("exact"))
        self.assertFalse(low)
        early = mask & (f.index < "2006-01-01")
        self.assertGreater(early.sum(), 500)                       # the 1995-2005 history is not thrown away
        self.assertFalse(mask[-1])                                  # today never matches itself

    def test_term_structure_refines_only_days_that_have_it(self):
        f = self.frame(cur_inv=1.0)                                 # today inverted; every post-2006 day is not
        mask, level, _ = m.select_analogs(f)
        self.assertTrue(level.startswith("exact"))
        self.assertFalse(mask[f.index >= "2006-07-17"].any())
        self.assertTrue(mask[f.index < "2006-07-17"].any())

    def test_too_few_exact_days_relaxes_step_by_step_and_says_so(self):
        f = self.frame()
        f["vix_bin"] = 3.0
        f.iloc[-300:, f.columns.get_loc("vix_bin")] = 1.0           # only the last 300 days share today's VIX bucket
        mask, level, low = m.select_analogs(f, min_days=500)
        self.assertTrue(level.startswith("relaxed") or level.startswith("broad") or level.startswith("core"))
        self.assertGreaterEqual(mask.sum(), 0)
        mask2, level2, low2 = m.select_analogs(f, min_days=10 ** 6)
        self.assertTrue(low2)                                       # nothing is big enough: flagged, not hidden

    def test_episode_grouping(self):
        pos = np.array([1, 2, 3, 40, 41, 100])
        self.assertEqual(m._episodes(pos, gap=20), [(1, 3), (40, 41), (100, 100)])
        self.assertEqual(m._episodes(np.array([]), gap=20), [])

    def test_stats_exclude_days_whose_horizon_is_incomplete(self):
        f = self.frame(n_start="2015-01-02", n_end="2015-12-31")
        mask = np.zeros(len(f), dtype=bool)
        mask[[10, 11, 12, len(f) - 5]] = True
        res = m.analog_stats(f, mask, "test", False)
        by = {h.horizon: h for h in res.horizons}
        self.assertEqual(by[20].n, 3)                               # the day 5 bars from the end has no 20d outcome yet
        self.assertEqual(by[120].n, 3)

    def test_probabilities_and_percentiles(self):
        idx = pd.bdate_range("2015-01-02", periods=400)
        c = pd.Series(100.0, index=idx)
        c.iloc[160:260] = 70.0                                      # a crash, then back to 100
        f = pd.DataFrame({"close": c, "inv": np.nan, "narrow": np.nan}, index=idx)
        mask = np.zeros(400, dtype=bool)
        mask[[100, 120, 140, 300]] = True                           # three days before the crash, one calm day after it
        res = m.analog_stats(f, mask, "t", False)
        h60 = next(h for h in res.horizons if h.horizon == 60)
        self.assertEqual(h60.n, 4)
        self.assertAlmostEqual(h60.p_crash20, 0.75)                 # days 100, 120 and 140 see -30% within 60 days
        self.assertAlmostEqual(h60.dd_p10, np.percentile([-0.3, -0.3, -0.3, 0.0], 10))
        self.assertAlmostEqual(h60.p_negative, 0.75)


class LeakAndDataTests(unittest.TestCase):
    def test_analysis_as_of_a_past_date_ignores_everything_after_it(self):
        s = synthetic(seed=5)
        asof = "2015-08-24"
        trimmed = {k: v[v.index <= asof] for k, v in s.items()}
        a = m.analyze(m.cut_asof(s, asof))
        b = m.analyze(trimmed)
        self.assertEqual(a.snapshot, b.snapshot)
        self.assertEqual(a.analogs.horizons, b.analogs.horizons)
        self.assertEqual(a.analogs.days, b.analogs.days)

    def test_partial_todays_bar_is_dropped_until_after_the_close(self):
        s = synthetic(end="2026-10-30")
        during = datetime(2026, 10, 30, 11, 0, tzinfo=NY)
        after = datetime(2026, 10, 30, 17, 0, tzinfo=NY)
        self.assertEqual(m.cut_asof(s, None, now=during)["ndx"].index[-1], pd.Timestamp("2026-10-29"))
        self.assertEqual(m.cut_asof(s, None, now=after)["ndx"].index[-1], pd.Timestamp("2026-10-30"))
        self.assertEqual(m.cut_asof(s, None, now=during, include_partial=True)["ndx"].index[-1],
                         pd.Timestamp("2026-10-30"))

    def test_missing_optional_series_disable_their_flags_but_not_the_report(self):
        s = synthetic(seed=2, with_extras=False)
        rep = m.analyze(s)
        keys = {f.key: f.on for f in rep.flags}
        self.assertIsNone(keys["inverted"])
        self.assertIsNone(keys["narrow"])
        self.assertEqual(rep.flags_known, 5)
        self.assertIsNone(rep.snapshot.term)
        self.assertTrue(rep.analogs.days > 0)

    def test_download_failure_falls_back_to_the_cache_and_says_so(self):
        s = synthetic(end="2026-10-29")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            by_symbol = {m.SYMBOLS[k]: v for k, v in s.items()}
            loaded, notes = m.load_series(fetch=lambda sym: by_symbol[sym], cache_dir=tmp, refresh=True)
            self.assertEqual(notes, [])

            def broken(sym):
                raise ConnectionError("offline")
            loaded, notes = m.load_series(fetch=broken, cache_dir=tmp, refresh=True)
            self.assertEqual(len(loaded), 5)
            self.assertTrue(any("using cached data" in n for n in notes))

    def test_missing_ndx_is_fatal_but_missing_vix3m_is_not(self):
        s = synthetic(end="2026-10-29")
        by_symbol = {m.SYMBOLS[k]: v for k, v in s.items()}
        with tempfile.TemporaryDirectory() as tmp:
            def no_vix3m(sym):
                if sym == "^VIX3M":
                    raise ConnectionError("x")
                return by_symbol[sym]
            loaded, notes = m.load_series(fetch=no_vix3m, cache_dir=Path(tmp), refresh=True)
            self.assertNotIn("vix3m", loaded)
            self.assertTrue(any("^VIX3M" in n for n in notes))
        with tempfile.TemporaryDirectory() as tmp:
            def no_ndx(sym):
                if sym == "^NDX":
                    raise ConnectionError("x")
                return by_symbol[sym]
            with self.assertRaises(RuntimeError):
                m.load_series(fetch=no_ndx, cache_dir=Path(tmp), refresh=True)


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.rep = m.analyze(synthetic(seed=7))

    def test_cli_report_has_every_requested_section(self):
        text = m.render_text(self.rep, episodes=True)
        for needle in ("CURRENT SNAPSHOT", "drawdown from 52w high", "200d SMA", "200d EMA", "shock z", "VIX",
                       "VIX/VIX3M", "Breadth QQEW/QQQ", "RED FLAGS", "HISTORICAL ANALOGS", "Forward return",
                       "Maximum further drawdown", "further -20%", "EPISODES", "Regime:"):
            self.assertIn(needle, text)
        for horizon in ("20 trading days", "60 trading days", "120 trading days"):
            self.assertIn(horizon, text)

    def test_telegram_version_is_concise_and_gives_no_advice(self):
        text = m.render_telegram(self.rep)
        self.assertLess(len(text), 900)
        self.assertLessEqual(len(text.splitlines()), 14)
        for banned in ("buy", "sell", "position size", "should", "recommend", "add to"):
            self.assertNotIn(banned, text.lower())
        self.assertNotIn("buy", m.render_text(self.rep).lower().replace("buy-and-hold", ""))

    def test_json_round_trips(self):
        import json
        data = json.loads(m.to_json(self.rep))
        self.assertIn("label", data)
        self.assertEqual(len(data["analogs"]["horizons"]), 3)

    def test_send_requires_credentials_and_never_leaks_the_token(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                m.send_telegram("hi")
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "SECRETTOKEN", "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("boom https://api.telegram.org/botSECRETTOKEN")):
            with self.assertRaises(RuntimeError) as cm:
                m.send_telegram("hi")
        self.assertNotIn("SECRETTOKEN", str(cm.exception))



class ViewModelTests(unittest.TestCase):
    @staticmethod
    def snap(**kw):
        base = dict(date="2026-10-30", close=28385.0, dd=-0.091, high_date="2026-10-06", dist_sma=0.016, dist_ema=0.008,
                    slope=0.0163, fall20=-0.091, ret=-0.0346, z=-2.83, shock_pct=0.009, shock_one_in=115.0, raw_one_in=40.0,
                    vix=19.4, term=0.93, inverted=False, br_change=-0.017, narrow=False)
        base.update(kw)
        return m.Snapshot(**base)

    def rep(self, snap, on):
        """A Report with the seven flags set from `on` (key -> True/False/None)."""
        keys = ["below_sma", "falling_sma", "deep_dd", "fast_fall", "high_vix", "inverted", "narrow"]
        flags = [m.Flag(k, k, on.get(k, False)) for k in keys]
        known = [f for f in flags if f.on is not None]
        from types import SimpleNamespace
        analogs = m.AnalogResult("exact match", 146, 32, False, 0.65, "1990-03-16", "2026-10-23", horizons=[
            m.HorizonStats(20, 145, -0.032, 0.034, 0.088, -0.076, -0.021, 0.0, 0.03, 0.0, 0.26, 0.014, 0.034),
            m.HorizonStats(60, 145, -0.052, 0.074, 0.175, -0.088, -0.037, 0.0, 0.06, 0.01, 0.20, 0.066, 0.07),
            m.HorizonStats(120, 139, -0.036, 0.100, 0.326, -0.101, -0.041, 0.0, 0.11, 0.03, 0.19, 0.113, 0.10)])
        return m.Report(label=m.LABEL_PULLBACK, snapshot=snap, flags=flags, flags_on=sum(1 for f in flags if f.on),
                        flags_known=len(known), trend_state="above a rising 200d average", dd_bucket="-12% to -7%",
                        vix_bucket="VIX 15-20", analogs=analogs, notes=[])

    def test_checks_are_positive_when_fine_and_a_red_flag_floats_to_the_top(self):
        rep = self.rep(self.snap(dist_sma=-0.06, slope=-0.01, vix=34.0, term=1.1, dd=-0.22, fall20=-0.12),
                       {"below_sma": True, "falling_sma": True, "deep_dd": True, "fast_fall": True,
                        "high_vix": True, "inverted": True, "narrow": False})
        checks = m._checks(rep)
        self.assertEqual([c["ok"] for c in checks], [False] * 6 + [True])      # reds first, the one OK last
        self.assertTrue(all(c["badge"] == "RED FLAG" for c in checks[:6]))
        self.assertEqual(checks[-1]["badge"], "OK")
        trend = next(c for c in checks if c["key"] == "below_sma")
        self.assertIn("below the 200-day average", trend["status"])

    def test_an_ok_check_never_describes_a_problem(self):
        checks = m._checks(self.rep(self.snap(), {}))
        self.assertTrue(all(c["badge"] == "OK" for c in checks))
        for c in checks:
            for bad in ("below the 200", "falling", "inverted", "panic-speed", "past the", "only a few"):
                self.assertNotIn(bad, c["status"])
        self.assertIn("above the 200-day average", next(c for c in checks if c["key"] == "below_sma")["status"])

    def test_unavailable_checks_say_no_data_not_ok(self):
        rep = self.rep(self.snap(term=None, inverted=None, br_change=None, narrow=None), {"inverted": None, "narrow": None})
        by = {c["key"]: c for c in m._checks(rep)}
        self.assertEqual(by["inverted"]["badge"], "NO DATA")
        self.assertIsNone(by["narrow"]["ok"])

    def test_four_kpi_cards_with_scales(self):
        k = {c["key"]: c for c in m._kpis(self.rep(self.snap(), {}))}
        self.assertEqual(list(k), ["drawdown", "trend", "vix", "today"])
        self.assertEqual(k["drawdown"]["value"], "-9.1%")
        self.assertEqual(k["drawdown"]["scale"], "Normal pullback: 5–10% | Bear market: 20%+")
        self.assertEqual(k["trend"]["value"], "+1.6% Above & Rising")
        self.assertEqual(k["trend"]["scale"], "Bullish above 200d SMA | Bearish below")
        self.assertEqual(k["vix"]["value"], "19.4 — Mild Caution")
        self.assertEqual(k["vix"]["scale"], "<18 Calm | 18–25 Caution | 25–30 High Stress | 30+ Panic")
        self.assertEqual(k["today"]["value"], "-3.46%")
        self.assertEqual(k["today"]["scale"], "Shock rarity: ~1 in 115 trading days")

    def test_kpi_tones(self):
        tone = lambda **kw: {c["key"]: c["tone"] for c in m._kpis(self.rep(self.snap(**kw), {}))}
        self.assertEqual(tone(dd=-0.03)["drawdown"], "good")
        self.assertEqual(tone(dd=-0.15)["drawdown"], "warn")
        self.assertEqual(tone(dd=-0.25)["drawdown"], "bad")
        self.assertEqual(tone(dist_sma=-0.05, slope=-0.02)["trend"], "bad")
        self.assertEqual(tone(dist_sma=-0.02, slope=0.01)["trend"], "warn")
        self.assertEqual(tone(vix=35.0)["vix"], "bad")
        self.assertEqual(tone(z=-3.0)["today"], "bad")

    def test_vix_bands_follow_the_published_scale(self):
        self.assertEqual([m.vix_band(v)[0] for v in (12, 17.9, 18, 21.9, 22, 24.9, 25, 29.9, 30, 60)],
                         ["Calm", "Calm", "Mild Caution", "Mild Caution", "Caution", "Caution", "High Stress",
                          "High Stress", "Panic", "Panic"])

    def test_analog_rows_are_simple_and_win_rate_is_the_complement_of_ended_lower(self):
        vm = m.view_model(self.rep(self.snap(), {}))
        rows = vm["analogs"]["rows"]
        self.assertEqual([r["timeframe"] for r in rows], ["1 Month (20d)", "3 Months (60d)", "6 Months (120d)"])
        self.assertAlmostEqual(rows[1]["win_rate"], 0.80)
        self.assertEqual((rows[1]["typical"], rows[1]["worst_dip"], rows[1]["crash_risk"]), (0.074, -0.088, 0.01))
        self.assertEqual(vm["title"], "Nasdaq-100 (NDX / QQQ) Market Regime")
        self.assertEqual(len(vm["raw"]["horizons"]), 3)
        import json
        json.dumps(vm)                                                          # the web API serialises it as is

    def test_telegram_summary_is_six_plain_english_lines(self):
        text = m.render_telegram(self.rep(self.snap(), {}))
        lines = text.splitlines()
        self.assertEqual(len(lines), 6)
        self.assertIn("Nasdaq-100 Market Regime", lines[0])
        self.assertIn("0 of 7 red flags", lines[1])
        self.assertIn("-9.1% from 52-week high", lines[2])
        self.assertIn("rising 200-day line", lines[2])
        self.assertIn("VIX 19.4 (Mild Caution)", lines[3])
        self.assertIn("1-in-115-day shock", lines[4])
        self.assertIn("higher 80% of the time", lines[5])
        self.assertNotIn("p10", text)
        self.assertNotIn("|", text)                                             # no ASCII tables

    def test_dashboard_url_and_the_send_button(self):
        import os, urllib.parse
        from unittest import mock
        with mock.patch.dict(os.environ, {"RISK_SIZER_PUBLIC_URL": "https://example.test/"}, clear=True):
            self.assertEqual(m.dashboard_url(), "https://example.test/regime")
        with mock.patch.dict(os.environ, {"RISK_SIZER_PUBLIC_URL": "http://insecure"}, clear=True):
            self.assertIsNone(m.dashboard_url())
        captured = {}

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout=None):
            captured["body"] = urllib.parse.parse_qs(req.data.decode())
            return Resp()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch("urllib.request.urlopen", side_effect=fake):
            m.send_telegram("hello", "https://example.test/regime")
        markup = json.loads(captured["body"]["reply_markup"][0])
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "🔗 Open Full Market Regime Dashboard")
        self.assertEqual(markup["inline_keyboard"][0][0]["url"], "https://example.test/regime")

    def test_cli_has_no_html_export_any_more(self):
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m.main(["--html", "x.html"])
        self.assertFalse(hasattr(m, "render_html"))

    def test_compute_view_returns_the_page_model(self):
        from unittest import mock
        with mock.patch.object(m, "load_series", return_value=(synthetic(seed=4), [])):
            vm = m.compute_view()
        self.assertEqual(len(vm["kpis"]), 4)
        self.assertEqual(len(vm["checks"]), 7)

if __name__ == "__main__":
    unittest.main()
