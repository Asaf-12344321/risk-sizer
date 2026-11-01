import json
import os
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


def knn_frame(n=3200, seed=5, today=None):
    """A feature frame for matching tests: random history, with `today` as the last row."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("1995-01-02", periods=n)
    f = pd.DataFrame(index=idx)
    f["close"] = 100 * np.cumprod(1 + rng.normal(0.0003, 0.011, n))
    f["dist_sma"] = rng.uniform(-0.2, 0.2, n)
    f["slope"] = rng.uniform(-0.02, 0.03, n)
    f["dd"] = -rng.uniform(0.0, 0.35, n)
    f["fall20"] = -rng.uniform(0.0, 0.2, n)
    f["vix"] = rng.uniform(10, 50, n)
    f["proxy"] = rng.uniform(0.7, 1.8, n)
    f["term"] = rng.uniform(0.7, 1.3, n)
    f["br_change"] = rng.uniform(-0.08, 0.08, n)
    cur = dict(dist_sma=0.016, slope=0.0163, dd=-0.091, fall20=-0.091, vix=19.4, proxy=1.21, term=0.93, br_change=-0.017)
    cur.update(today or {})
    for k, v in cur.items():
        f.iloc[-1, f.columns.get_loc(k)] = v
    return with_logs(f)


def with_logs(f):
    f["lvix"], f["lproxy"] = np.log(f["vix"]), np.log(f["proxy"])
    return f


def plant_twins(f, dates, **offsets):
    """Overwrite given rows so they sit very close to today's values."""
    base = f.iloc[-1]
    for d in dates:
        i = f.index.get_loc(pd.Timestamp(d))
        for col in ("dist_sma", "dd", "vix", "proxy"):
            f.iloc[i, f.columns.get_loc(col)] = base[col] + offsets.get(col, 0.0)
    return with_logs(f)


class AnalogTests(unittest.TestCase):
    def test_the_closest_days_are_picked_and_no_more_than_k(self):
        f = knn_frame()
        pick = m.knn_episodes(f, 60)
        self.assertEqual(len(pick["days"]), m.KNN["k"])
        self.assertNotIn(len(f) - 1, pick["days"])                   # today never matches itself
        mask = np.zeros(len(f), bool)
        mask[pick["days"]] = True
        far = f[~mask & (f.index >= m.SAMPLE_START)]
        self.assertLess(abs(f[mask]["dd"] - f["dd"].iloc[-1]).mean(), abs(far["dd"] - f["dd"].iloc[-1]).mean())

    def test_only_the_four_deduplicated_dimensions_decide_the_match(self):
        self.assertEqual(m.MATCH_COLUMNS, ("dd", "dist_sma", "lvix", "lproxy"))
        f = knn_frame()
        f = plant_twins(f, ["2001-03-05"])
        before = m._rank_distance(f, m.SAMPLE_START)[0].copy()
        i = f.index.get_loc(pd.Timestamp("2001-03-05"))
        for col, v in (("slope", -0.02), ("fall20", -0.3), ("term", 1.4), ("br_change", -0.08)):
            f.iloc[i, f.columns.get_loc(col)] = v                    # wildly different on everything we do NOT match on
        self.assertEqual(m._rank_distance(f, m.SAMPLE_START)[0][i], before[i])

    def test_no_hard_boundary_at_the_200d_line_and_no_caps(self):
        f = plant_twins(knn_frame(), ["2001-03-05"], dist_sma=-0.033)   # identical except 1.7% BELOW the line
        pick = m.knn_episodes(f, 60)
        self.assertIn(f.index.get_loc(pd.Timestamp("2001-03-05")), pick["days"])      # close in rank: it matches

    def test_an_extreme_day_still_finds_a_real_pool_of_analogs(self):
        f = knn_frame(today=dict(vix=82.7, proxy=2.7, dd=-0.278, dist_sma=-0.142))     # nothing in history is this extreme
        pick = m.knn_episodes(f, 60)
        self.assertIsNotNone(pick)
        self.assertEqual(len(pick["days"]), m.KNN["k"])
        self.assertGreaterEqual(len(pick["reps"]), 4)                # the old caps returned 1 day here
        res = m.analog_stats(f)
        self.assertGreaterEqual(res.episodes, 4)

    def test_only_days_whose_outcome_has_happened_can_match(self):
        f = knn_frame()
        last = len(f) - 1
        f = plant_twins(f, [f.index[last - 10], f.index[last - 30], f.index[last - 90]])
        for h in (20, 60, 120):
            self.assertLessEqual(m.knn_episodes(f, h)["days"].max(), last - h)
        self.assertIn(last - 90, m.knn_episodes(f, 60)["days"])      # finished at 60 days ...
        self.assertNotIn(last - 30, m.knn_episodes(f, 60)["days"])   # ... but not these (unfinished: cannot crowd out)
        self.assertIn(last - 30, m.knn_episodes(f, 20)["days"])

    @staticmethod
    def planted(f, n_episodes, start=200, step=130, **offsets):
        """Make n separate 3-day episodes that sit close to today; the middle day is the closest."""
        rows = []
        for k in range(n_episodes):
            rows += [start + k * step, start + k * step + 1, start + k * step + 2]
        f = plant_twins(f, [f.index[i] for i in rows], **offsets)
        for k in range(n_episodes):                                   # nudge the outer days away so the middle one wins
            for j, nudge in ((0, 0.004), (2, -0.004)):
                f.iloc[start + k * step + j, f.columns.get_loc("dd")] += nudge
        return f

    def far_history(self, n=3200, seed=4):
        f = knn_frame(n=n, seed=seed)
        f["dd"] = -0.33
        f.iloc[-1, f.columns.get_loc("dd")] = -0.091
        return f

    def test_each_episode_counts_once_through_its_closest_day(self):
        f = self.planted(self.far_history(), 20)
        pick = m.knn_episodes(f, 60, k=60)
        self.assertEqual(len(pick["spans"]), len(pick["reps"]))
        self.assertGreaterEqual(len(pick["reps"]), 15)
        for (a, b, nd), rep in zip(pick["spans"], pick["reps"]):
            self.assertTrue(a <= rep <= b)
            self.assertEqual(rep, a + 1)                              # the middle (closest) day of each 3-day episode

    def test_a_single_long_episode_is_one_observation_and_raises_the_low_sample_warning(self):
        f = self.far_history()
        f = plant_twins(f, [f.index[i] for i in range(300, 440)])     # 140 consecutive near-identical days
        res = m.analog_stats(f)
        self.assertEqual(res.episodes, 1)
        self.assertTrue(res.low_confidence)
        self.assertTrue(all(h.low_sample for h in res.horizons))

    def test_enough_separate_episodes_do_not_warn(self):
        f = self.planted(self.far_history(), 20)
        res = m.analog_stats(f)
        self.assertGreaterEqual(res.episodes, m.KNN["warn_episodes"])
        self.assertFalse(res.low_confidence)

    def test_shrinkage_pulls_small_samples_toward_the_base_rate(self):
        self.assertAlmostEqual(m.shrink(0.50, 10, 0.20, m=10), 0.35)
        self.assertAlmostEqual(m.shrink(0.50, 0, 0.20), 0.20)         # no episodes: pure base rate
        self.assertGreater(m.shrink(0.50, 1000, 0.20), 0.49)          # lots of episodes: follows the data
        self.assertEqual(m.KNN["shrink_m"], 12.0)

    def test_stats_are_per_episode_and_probabilities_sit_between_raw_and_base(self):
        rep = m.analyze(synthetic(seed=12))
        for h in rep.analogs.horizons:
            self.assertLessEqual(h.n, m.KNN["k"])
            self.assertEqual(h.low_sample, h.n < m.KNN["warn_episodes"])
            for raw, shrunk, base in ((h.win_raw, h.win, h.base_win), (h.down10_raw, h.down10, h.base_down10),
                                      (h.crash20_raw, h.crash20, h.base_crash20)):
                self.assertGreaterEqual(shrunk, min(raw, base) - 1e-12)
                self.assertLessEqual(shrunk, max(raw, base) + 1e-12)
                self.assertAlmostEqual(shrunk, (h.n * raw + m.KNN["shrink_m"] * base) / (h.n + m.KNN["shrink_m"]))

    def test_base_rates_are_the_unconditional_rates_over_finished_days(self):
        s = synthetic(seed=12)
        f = m.compute_features(m.build_frame(s))
        f = f[f.index >= pd.Timestamp("1986-01-01")]
        rep = m.analyze(s)
        fwd, mdd = m.forward_outcomes(f["close"], 60)
        ok = (f.index >= m.SAMPLE_START) & fwd.notna().to_numpy() & mdd.notna().to_numpy()
        h60 = next(h for h in rep.analogs.horizons if h.horizon == 60)
        self.assertAlmostEqual(h60.base_win, float((fwd[ok] > 0).mean()))
        self.assertAlmostEqual(h60.base_crash20, float((mdd[ok] <= -0.2).mean()))
        self.assertAlmostEqual(h60.base_dd_p10, float(np.percentile(mdd[ok], 10)))

    def test_without_todays_vix_there_is_no_match(self):
        f = knn_frame(today=dict(vix=np.nan, proxy=np.nan))
        f = with_logs(f)
        self.assertIsNone(m.find_analogs(f))

    def test_spread_text_shows_the_real_range_of_the_matched_days(self):
        f = self.planted(self.far_history(), 24)
        res = m.find_analogs(f)
        self.assertRegex(res.spread, r"^Matched \d+ episodes \(\d+ days\): drawdown [-+][\d.]+% to [-+][\d.]+%, "
                                     r"200d cushion [-+][\d.]+% to [-+][\d.]+%, VIX [\d.]+ to [\d.]+, "
                                     r"VIX vs 60d average [\d.]+ to [\d.]+$")
        for gone in ("20d speed", "VIX/VIX3M", "breadth", "slope"):
            self.assertNotIn(gone, res.spread)                        # not matching dimensions any more
        self.assertIn(f"Matched {res.episodes} episodes ({res.days} days)", res.spread)

    def test_episode_grouping(self):
        pos = np.array([1, 2, 3, 40, 41, 100])
        self.assertEqual(m._episodes(pos, gap=20), [(1, 3), (40, 41), (100, 100)])
        self.assertEqual(m._episodes(np.array([]), gap=20), [])

    def test_the_fear_proxy_exists_in_every_year_after_the_first_60_days(self):
        s = synthetic(seed=9)
        feats = m.compute_features(m.build_frame(s))
        self.assertTrue(feats.loc["1992-01-01":, "proxy"].notna().all())
        self.assertTrue(feats.loc[:"2006-01-01", "term"].isna().all())   # the real curve starts in 2006
        self.assertTrue(feats.loc["2007-01-01":, "term"].notna().all())

    def test_end_to_end_one_unified_1990_set(self):
        rep = m.analyze(synthetic(seed=12))
        self.assertEqual(rep.analogs.key, "full")
        self.assertFalse(hasattr(rep, "modern"))
        self.assertEqual(rep.analogs.days, m.KNN["k"])
        self.assertTrue(rep.analogs.first_date < "2006-09-01")        # the 1990s stay in the pool
        self.assertEqual([h.horizon for h in rep.analogs.horizons], [20, 60, 120])


class HistoricalRegressionTests(unittest.TestCase):
    """Real NDX history from the local cache (skipped on machines without it, e.g. CI): the stress days where the
    old capped matcher collapsed to 1-3 days must now find a real pool, and as-of discipline must hold."""
    STRESS = ("2008-10-10", "2020-03-16", "2022-06-16")

    @classmethod
    def setUpClass(cls):
        try:
            cls.series, _ = m.load_series(fetch=lambda s: (_ for _ in ()).throw(OSError("offline")), max_age_h=1e12)
        except Exception:
            cls.series = None

    def setUp(self):
        if self.series is None or len(self.series["ndx"]) < 9000:
            self.skipTest("no cached NDX history on this machine")

    def test_stress_days_no_longer_collapse_to_one_episode(self):
        for d in self.STRESS:
            rep = m.analyze(m.cut_asof(self.series, d, include_partial=True))
            self.assertEqual(rep.snapshot.date, d)
            self.assertGreaterEqual(rep.analogs.episodes, m.KNN["warn_episodes"], d)
            self.assertEqual(rep.analogs.days, m.KNN["k"], d)
            self.assertFalse(any("LOW SAMPLE (60" in n for n in rep.notes), d)

    def test_march_2020_is_still_called_systemic_stress(self):
        rep = m.analyze(m.cut_asof(self.series, "2020-03-16", include_partial=True))
        self.assertEqual(rep.label, m.LABEL_BEAR)

    def test_analysis_as_of_a_past_date_uses_nothing_after_it(self):
        for d in self.STRESS:
            full = m.analyze(m.cut_asof(self.series, d, include_partial=True))
            trimmed = m.analyze({k: v[v.index <= d] for k, v in self.series.items()})
            self.assertEqual(full.analogs.horizons, trimmed.analogs.horizons)
            self.assertLessEqual(full.analogs.last_date, d)
            for e in full.analogs.episode_table:
                self.assertLess(e["match"], d)

    def test_shrunk_probabilities_stay_between_the_raw_rate_and_the_base_rate(self):
        rep = m.analyze(m.cut_asof(self.series, "2022-06-16", include_partial=True))
        for h in rep.analogs.horizons:
            self.assertLessEqual(min(h.crash20_raw, h.base_crash20) - 1e-12, h.crash20)
            self.assertLessEqual(h.crash20, max(h.crash20_raw, h.base_crash20) + 1e-12)


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
        self.assertLess(len(text), 1100)
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

    @staticmethod
    def flags_for(s):
        """The seven red-flag booleans exactly as the rules define them."""
        none = lambda v, f: None if v is None else f(v)
        return {"below_sma": none(s.dist_sma, lambda v: v < 0), "falling_sma": none(s.slope, lambda v: v < 0),
                "deep_dd": none(s.dd, lambda v: v <= -0.20), "fast_fall": none(s.fall20, lambda v: v <= -0.10),
                "high_vix": none(s.vix, lambda v: v >= 30), "inverted": none(s.term, lambda v: v >= 1.0),
                "narrow": s.narrow}

    def rep(self, snap, on=None):
        on = self.flags_for(snap) if on is None else on
        keys = ["below_sma", "falling_sma", "deep_dd", "fast_fall", "high_vix", "inverted", "narrow"]
        flags = [m.Flag(k, k, on.get(k)) for k in keys]
        def hstat(h, n, ret, dip, crash, win, base_ret, base_dip, base_crash, base_win):
            return m.HorizonStats(h, n, 120, ret_p10=-0.05, ret_p50=ret, ret_p90=0.17, dd_p10=dip, dd_p50=-0.03, dd_p90=0.0,
                                  win_raw=win, down10_raw=0.06, crash20_raw=crash, win=win, down10=0.06, crash20=crash,
                                  base_win=base_win, base_down10=0.12, base_crash20=base_crash, base_ret_p50=base_ret,
                                  base_dd_p10=base_dip, low_sample=n < 8)
        hs = [hstat(20, 32, 0.034, -0.076, 0.0, 0.74, 0.012, -0.095, 0.02, 0.62),
              hstat(60, 32, 0.074, -0.088, 0.01, 0.80, 0.034, -0.158, 0.06, 0.69),
              hstat(120, 30, 0.100, -0.101, 0.03, 0.66, 0.075, -0.220, 0.11, 0.75)]
        full = m.AnalogResult("full", "Historical analogs since 1990", 120, 32, False, "1990-03-16", "2026-10-23",
                              spread="Matched 32 episodes (120 days): drawdown -12.7% to -6.2%", horizons=hs,
                              episode_table=[{"start": "2008-06-05", "end": "2008-06-05", "days": 1, "match": "2008-06-05",
                                              "ret_60d": -0.089, "worst_dd_120d": -0.496}])
        return m.Report(label=m.LABEL_PULLBACK, snapshot=snap, flags=flags, flags_on=sum(1 for v in on.values() if v),
                        flags_known=sum(1 for v in on.values() if v is not None), analogs=full, notes=[])

    def level(self, key, **kw):
        s = self.snap(**kw)
        return next(c for c in m._checks(self.rep(s)) if c["key"] == key)

    # --- graded checks: exactly the thresholds you specified
    def test_trend_levels(self):
        self.assertEqual([self.level("trend", dist_sma=v)["level"] for v in (0.10, 0.0301, 0.029, 0.0, -0.001)],
                         ["ok", "ok", "watch", "watch", "red"])

    def test_slope_levels(self):
        self.assertEqual([self.level("slope", slope=v)["level"] for v in (0.02, 0.0051, 0.004, 0.0, -0.001)],
                         ["ok", "ok", "watch", "watch", "red"])

    def test_drawdown_levels(self):
        self.assertEqual([self.level("drawdown", dd=v)["level"] for v in (-0.02, -0.149, -0.15, -0.199, -0.20, -0.30)],
                         ["ok", "ok", "watch", "watch", "red", "red"])

    def test_drop_speed_levels(self):
        self.assertEqual([self.level("speed", fall20=v)["level"] for v in (-0.02, -0.074, -0.075, -0.091, -0.0999, -0.10)],
                         ["ok", "ok", "watch", "watch", "watch", "red"])

    def test_vix_levels(self):
        self.assertEqual([self.level("vix", vix=v)["level"] for v in (12.0, 17.99, 18.0, 24.99, 25.0, 29.99, 30.0)],
                         ["ok", "ok", "caution", "caution", "watch", "watch", "red"])

    def test_term_structure_levels(self):
        self.assertEqual([self.level("term", term=v, inverted=v >= 1.0)["level"] for v in (0.80, 0.949, 0.95, 0.999, 1.0, 1.3)],
                         ["ok", "ok", "watch", "watch", "red", "red"])

    def test_breadth_levels(self):
        lv = lambda ch, narrow: self.level("breadth", br_change=ch, narrow=narrow)["level"]
        self.assertEqual([lv(0.02, False), lv(-0.019, False), lv(-0.02, False), lv(-0.029, False), lv(-0.035, True)],
                         ["ok", "ok", "watch", "watch", "red"])

    def test_todays_numbers_put_drop_speed_and_trend_on_watch(self):
        checks = {c["key"]: c for c in m._checks(self.rep(self.snap()))}
        watch = sorted(k for k, c in checks.items() if c["level"] == "watch")
        self.assertEqual(watch, ["speed", "trend"])
        self.assertEqual(checks["vix"]["level"], "caution")                     # VIX 19.4 is on the shared 18-25 caution band
        self.assertTrue(all(c["level"] in ("ok", "watch", "caution") for c in checks.values()))
        self.assertEqual(m.view_model(self.rep(self.snap()))["watch_count"], 3)

    def test_checks_sort_red_then_watch_then_ok(self):
        s = self.snap(dist_sma=-0.06, slope=0.003, dd=-0.10, fall20=-0.12, vix=22.0, term=0.97, inverted=False)
        levels = [c["level"] for c in m._checks(self.rep(s))]
        self.assertEqual(levels, sorted(levels, key=lambda x: m.LEVEL_RANK[x]))
        self.assertEqual(levels[:2], ["red", "red"])
        self.assertEqual(levels[-1], "ok")

    def test_gauges_show_how_close_each_value_is_to_its_red_line(self):
        c = {x["key"]: x for x in m._checks(self.rep(self.snap()))}
        self.assertAlmostEqual(c["speed"]["gauge"], 0.91, places=2)
        self.assertAlmostEqual(c["trend"]["gauge"], 0.84, places=2)
        self.assertAlmostEqual(c["drawdown"]["gauge"], 0.455, places=3)
        self.assertEqual(c["slope"]["gauge"], 0.0)                              # far from flipping: empty bar
        red = self.level("trend", dist_sma=-0.05)
        self.assertEqual(red["gauge"], 1.0)
        self.assertTrue(all(0.0 <= x["gauge"] <= 1.0 for x in c.values()))
        self.assertTrue(all(x["limit"].startswith("Red flag") for x in c.values()))

    def test_status_text_never_calls_a_problem_fine(self):
        s = self.snap(dist_sma=-0.06, slope=-0.01, vix=34.0, term=1.1, dd=-0.22, fall20=-0.12, br_change=-0.04, narrow=True)
        for c in m._checks(self.rep(s)):
            self.assertEqual(c["level"], "red")
            self.assertEqual(c["badge"], "RED FLAG")
        ok = {c["key"]: c["status"] for c in m._checks(self.rep(self.snap(dist_sma=0.08, slope=0.01, dd=-0.02, fall20=-0.01,
                                                                       vix=14.0, term=0.85, br_change=0.01)))}
        self.assertIn("above the 200-day average", ok["trend"])
        for text in ok.values():
            for bad in ("below the 200", "falling", "inverted", "panic-speed", "past the", "only a few", "close to"):
                self.assertNotIn(bad, text)

    def test_unavailable_checks_say_no_data_not_ok(self):
        s = self.snap(term=None, inverted=None, br_change=None, narrow=None)
        by = {c["key"]: c for c in m._checks(self.rep(s))}
        self.assertEqual(by["term"]["badge"], "NO DATA")
        self.assertEqual(by["breadth"]["level"], "na")
        self.assertIsNone(by["breadth"]["ok"])

    # --- KPI cards: the number is coloured by its sign, the pill carries the verdict
    def test_four_kpi_cards_with_scales(self):
        vm = m.view_model(self.rep(self.snap()))
        k = {c["key"]: c for c in vm["kpis"]}
        self.assertEqual(list(k), ["drawdown", "trend", "vix", "today"])
        self.assertEqual(k["drawdown"]["value"], "-9.1%")
        self.assertEqual(k["drawdown"]["scale"], "Normal pullback: 5–10% | Bear market: 20%+")
        self.assertEqual(k["trend"]["value"], "+1.6% Above & Rising")
        self.assertEqual(k["trend"]["scale"], "Bullish above 200d SMA | Bearish below")
        self.assertEqual(k["vix"]["value"], "19.4")
        self.assertEqual(k["vix"]["pill"], "Caution")
        self.assertEqual(k["vix"]["scale"], "<18 Calm | 18–25 Caution | 25–30 High Stress | 30+ Panic")
        self.assertEqual(k["today"]["value"], "-3.46%")
        self.assertEqual(k["today"]["scale"], "Shock rarity: ~1 in 115 trading days")

    def test_a_negative_number_is_red_while_a_healthy_status_stays_green(self):
        k = {c["key"]: c for c in m.view_model(self.rep(self.snap()))["kpis"]}
        self.assertEqual((k["drawdown"]["value_tone"], k["drawdown"]["pill"], k["drawdown"]["pill_tone"]),
                         ("bad", "Normal pullback", "good"))
        self.assertEqual(k["today"]["value_tone"], "bad")
        up = {c["key"]: c for c in m.view_model(self.rep(self.snap(ret=0.012, z=0.8)))["kpis"]}
        self.assertEqual(up["today"]["value_tone"], "good")
        self.assertEqual(k["trend"]["value_tone"], "good")                      # a positive number

    def test_kpi_pills_follow_the_regime(self):
        pill = lambda key, **kw: {c["key"]: c for c in m.view_model(self.rep(self.snap(**kw)))["kpis"]}[key]
        self.assertEqual(pill("drawdown", dd=-0.03)["pill_tone"], "good")
        self.assertEqual(pill("drawdown", dd=-0.15)["pill_tone"], "warn")
        self.assertEqual(pill("drawdown", dd=-0.25)["pill_tone"], "bad")
        self.assertEqual(pill("trend", dist_sma=-0.05, slope=-0.02)["pill"], "Trend broken")
        self.assertEqual(pill("trend", dist_sma=0.016)["pill"], "Thin cushion: watch")
        self.assertEqual(pill("trend", dist_sma=0.08)["pill"], "Comfortable cushion")
        self.assertEqual(pill("today", z=-3.0)["pill"], "Rare shock")
        self.assertEqual(pill("vix", vix=35.0)["value_tone"], "bad")
        self.assertEqual(pill("vix", vix=27.0)["value_tone"], "orange")
        self.assertEqual(pill("vix", vix=22.0)["value_tone"], "warn")

    def test_vix_bands_follow_the_published_scale(self):
        self.assertEqual([m.vix_band(v)[0] for v in (12, 17.9, 18, 24.9, 25, 29.9, 30, 60)],
                         ["Calm", "Calm", "Caution", "Caution", "High Stress", "High Stress", "Panic", "Panic"])

    def test_one_vix_ladder_drives_the_banner_the_card_and_the_check(self):
        for v in (12.0, 17.9, 18.0, 22.0, 24.9, 25.0, 27.0, 29.9, 30.0, 45.0):
            s = self.snap(vix=v)
            word, _ = m.vix_band(v)
            level = self.level("vix", vix=v)["level"]
            expected = {"Calm": "ok", "Caution": "caution", "High Stress": "watch", "Panic": "red"}[word]
            self.assertEqual(level, expected, v)
            card = next(c for c in m.view_model(self.rep(s))["kpis"] if c["key"] == "vix")
            self.assertEqual(card["pill"], word, v)
        self.assertIn("<18 Calm | 18–25 Caution | 25–30 High Stress | 30+ Panic", m.VIX_SCALE)

    def test_analog_row_carries_shrunk_probabilities_and_the_base_rate_beside_them(self):
        vm = m.view_model(self.rep(self.snap()))
        sets = vm["analogs"]["sets"]
        self.assertEqual([x["key"] for x in sets], ["full"])
        self.assertNotIn("compare", vm["analogs"])
        rows = sets[0]["rows"]
        self.assertEqual([r["timeframe"] for r in rows], ["1 Month (20d)", "3 Months (60d)", "6 Months (120d)"])
        r = rows[1]
        self.assertAlmostEqual(r["win_rate"], 0.80)
        self.assertEqual((r["typical"], r["worst_dip"], r["crash_risk"]), (0.074, -0.088, 0.01))
        self.assertEqual((r["base_win_rate"], r["base_typical"], r["base_worst_dip"], r["base_crash_risk"]),
                         (0.69, 0.034, -0.158, 0.06))
        self.assertEqual((r["n"], r["low_sample"]), (32, False))
        self.assertEqual(sets[0]["shrink_m"], 12.0)
        self.assertEqual(vm["title"], "Nasdaq-100 (NDX / QQQ) Market Regime")
        self.assertEqual(len(sets[0]["raw"]["horizons"]), 3)
        self.assertIn("Matched 32 episodes", sets[0]["spread"])
        json.dumps(vm)                                                # the web API serialises it as is

    def test_bear_summary_describes_today_and_does_not_forecast(self):
        text = m.SUMMARIES[m.LABEL_BEAR]
        self.assertIn("current conditions, not a forecast", text)
        self.assertNotIn("deeper bear market", text)

    def test_stretched_market_and_recovery_pattern_notes(self):
        self.assertEqual(m.state_notes(self.snap()), [])
        stretched = m.state_notes(self.snap(dist_sma=0.52))
        self.assertEqual(len(stretched), 1)
        self.assertIn("52% above its 200-day average", stretched[0])
        recovery = m.state_notes(self.snap(dd=-0.25, dist_sma=0.139, slope=0.01, high_date="2008-11-04"))
        self.assertEqual(len(recovery), 1)
        self.assertIn("-25% drawdown is measured from the 2008-11-04 high", recovery[0])
        self.assertEqual(m.state_notes(self.snap(dd=-0.25, dist_sma=0.139, slope=-0.01)), [])       # falling average: no recovery claim
        self.assertEqual(m.state_notes(self.snap(dd=-0.25, dist_sma=0.02, slope=0.01)), [])         # thin cushion: not yet

    # --- Telegram: the agreed layout, exactly
    EXPECTED_HTML = "\n".join([
        "📊 <b>Nasdaq-100 Regime · 30 Oct 2026</b>",
        "🟢 <b>Normal Bull-Market Pullback</b> (0/7 red flags · 3 watch)",
        "",
        "• <b>Trend:</b> 🔻 <b>-9.1%</b> from high | <code>+1.6%</code> above rising 200d ⚠️",
        "• <b>Speed & Stress:</b> 🔻 <b>-9.1%</b> in 20d ⚠️ | VIX <b>19.4</b> (Caution) ⚠️",
        "• <b>Structure:</b> VIX curve & Breadth OK",
        "• <b>Today:</b> 🔻 <b>-3.46%</b> (1-in-115d shock)",
        "",
        "🕰 <b>3-Month Historical Outlook (32 episodes):</b>",
        "📈 Typical: <code>+7.4%</code> (all days +3.4%) | Higher <code>80%</code> (all days 69%)",
        "🛡️ Worst-case dip: 🔻 <b>-8.8%</b> (all days -15.8%) | Severe crash risk (-20%): <b>1%</b> (all days 6%)"])

    def test_telegram_layout_is_exactly_the_agreed_one(self):
        self.assertEqual(m.render_telegram(self.rep(self.snap())), self.EXPECTED_HTML)

    def test_gains_are_green_code_and_drops_are_bold_with_a_red_marker(self):
        text = m.render_telegram(self.rep(self.snap()))
        for gain in ("+1.6%", "+7.4%", "80%"):
            self.assertIn(f"<code>{gain}</code>", text)
        for drop in ("-9.1%", "-3.46%", "-8.8%"):
            self.assertIn(f"🔻 <b>{drop}</b>", text)
            self.assertNotIn(f"<code>{drop}</code>", text)                  # a drop must never turn green
        self.assertNotIn("<code>-", text)

    def test_win_rates_are_green_only_when_high(self):
        rep = self.rep(self.snap())
        rep.analogs.horizons[1].win = 0.55
        self.assertIn("Higher <b>55%</b>", m.render_telegram(rep))
        rep.analogs.horizons[1].win = 0.70                                  # 70% is the green line
        self.assertIn("Higher <code>70%</code>", m.render_telegram(rep))

    def test_telegram_shows_a_low_sample_warning_and_no_modern_line(self):
        rep = self.rep(self.snap())
        rep.analogs.horizons[1].n = 5
        rep.analogs.horizons[1].low_sample = True
        text = m.render_telegram(rep)
        self.assertIn("(5 episodes):</b> ⚠️ low sample", text)
        self.assertNotIn("Modern", text)
        self.assertNotIn("🧭", text)

    def test_a_break_below_the_200_day_line_is_marked_red(self):
        text = m.render_telegram(self.rep(self.snap(dist_sma=-0.05, slope=-0.01, ret=0.012, z=0.8)))
        self.assertIn("🔻 <b>-5.0%</b> below falling 200d", text)
        self.assertIn("<b>Today:</b> <code>+1.20%</code>", text)            # an up day is green

    def test_nothing_in_the_output_uses_em_dashes_or_unicode_minus(self):
        for text in (m.render_telegram(self.rep(self.snap())), m.render_text(m.analyze(synthetic(seed=7)))):
            self.assertNotIn("\u2014", text)
            self.assertNotIn("\u2212", text)

    def test_telegram_plain_version_for_the_terminal_has_no_tags(self):
        plain = m.render_telegram(self.rep(self.snap()), html_markup=False)
        self.assertNotIn("<", plain)
        self.assertIn("• Trend: 🔻 -9.1% from high | +1.6% above rising 200d ⚠️", plain)
        self.assertIn("• Speed & Stress: 🔻 -9.1% in 20d ⚠️ | VIX 19.4 (Caution) ⚠️", plain)

    def test_telegram_flags_problems_in_the_right_places(self):
        s = self.snap(dist_sma=-0.05, slope=-0.01, dd=-0.22, fall20=-0.12, vix=34.0, term=1.1, inverted=True,
                      br_change=-0.04, narrow=True)
        text = m.render_telegram(self.rep(s), delayed=True)
        self.assertIn("data may be delayed", text.splitlines()[0])
        self.assertIn("🚨", text.split("Trend:")[1].split("\n")[0])
        self.assertIn("🚨", text.split("Speed & Stress:")[1].split("\n")[0])
        self.assertIn("VIX curve 🚨 inverted | Breadth 🚨 narrowing", text)
        self.assertIn("7/7 red flags", text)
        self.assertIn("below falling 200d", text)

    def test_telegram_without_vix3m_or_breadth_says_so(self):
        s = self.snap(term=None, inverted=None, br_change=None, narrow=None)
        self.assertIn("VIX curve n/a | Breadth n/a", m.render_telegram(self.rep(s)))

    def test_dashboard_url_and_the_send_button_and_html_mode(self):
        import urllib.parse, urllib.error
        from unittest import mock
        with mock.patch.dict(os.environ, {"RISK_SIZER_PUBLIC_URL": "https://example.test/"}, clear=True):
            self.assertEqual(m.dashboard_url(), "https://example.test/regime")
        with mock.patch.dict(os.environ, {"RISK_SIZER_PUBLIC_URL": "http://insecure"}, clear=True):
            self.assertIsNone(m.dashboard_url())
        bodies = []

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout=None):
            bodies.append(urllib.parse.parse_qs(req.data.decode()))
            return Resp()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch("urllib.request.urlopen", side_effect=fake):
            m.send_telegram("<b>hello</b>", "https://example.test/regime")
        self.assertEqual(bodies[0]["parse_mode"], ["HTML"])
        markup = json.loads(bodies[0]["reply_markup"][0])
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "🔗 Open Full Market Regime Dashboard")
        self.assertEqual(markup["inline_keyboard"][0][0]["url"], "https://example.test/regime")

        calls = []

        def reject_markup(req, timeout=None):
            body = urllib.parse.parse_qs(req.data.decode())
            calls.append(body)
            if "parse_mode" in body:
                raise urllib.error.HTTPError("u", 400, "bad entities", {}, None)
            return Resp()
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "T", "TELEGRAM_CHAT_ID": "1"}), \
                mock.patch("urllib.request.urlopen", side_effect=reject_markup):
            m.send_telegram("<b>hello</b> &amp; <code>x</code>", "https://example.test/regime")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["text"], ["hello & x"])                       # resent as plain text, button kept
        self.assertIn("reply_markup", calls[1])

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
        self.assertIn("watch_count", vm)



class CliTests(unittest.TestCase):
    """main() is the entry point systemd and the user run; it once drifted from analyze() unnoticed."""

    def setUp(self):
        full = synthetic(end="2026-10-30", seed=3)
        self.series = {k: v for k, v in full.items()}
        self.sent = []

    def run_main(self, argv, load=None, send=None):
        import contextlib
        import io
        from unittest import mock
        buf, err = io.StringIO(), io.StringIO()
        with mock.patch.object(m, "load_series", load or (lambda refresh=False: (self.series, []))), \
             mock.patch.object(m, "send_telegram", send or (lambda text, url=None, html=True: self.sent.append((text, url)))), \
             contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = m.main(argv)
        return rc, buf.getvalue(), err.getvalue()

    def test_default_run_prints_the_text_report(self):
        rc, out, _ = self.run_main([])
        self.assertEqual(rc, 0)
        self.assertIn("MARKET REGIME", out)
        self.assertEqual(self.sent, [])

    def test_send_posts_the_html_summary_once_and_prints_the_plain_one(self):
        rc, out, _ = self.run_main(["--send"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.sent), 1)
        text, _url = self.sent[0]
        self.assertIn("Nasdaq-100 Regime", text.splitlines()[0])
        self.assertIn("<b>", text)
        self.assertNotIn("<b>", out)

    def test_send_failure_exits_nonzero(self):
        def boom(text, url=None, html=True):
            raise RuntimeError("telegram sendMessage failed")
        rc, _, err = self.run_main(["--send"], send=boom)
        self.assertEqual(rc, 1)
        self.assertIn("telegram sendMessage failed", err)

    def test_json_and_telegram_flags_run(self):
        rc, out, _ = self.run_main(["--json"])
        self.assertEqual(rc, 0)
        json.loads(out)
        rc, out, _ = self.run_main(["--telegram"])
        self.assertEqual(rc, 0)
        self.assertIn("Nasdaq-100 Regime", out)

    def test_data_failure_exits_nonzero_without_sending(self):
        def bad(refresh=False):
            raise RuntimeError("no data")
        rc, _, err = self.run_main(["--send"], load=bad)
        self.assertEqual(rc, 1)
        self.assertIn("no data", err)
        self.assertEqual(self.sent, [])


class DailyJobTests(unittest.TestCase):
    NOW = datetime(2026, 11, 3, 1, 10, tzinfo=NY)             # Tuesday 01:10 New York: Monday's bar is the latest final one

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)
        self.sent = []
        self.sleeps = []
        full = synthetic(end="2026-11-02", seed=11)             # ends on Monday 2 Nov
        self.by_symbol = {m.SYMBOLS[k]: v for k, v in full.items()}

    def tearDown(self):
        self.tmp.cleanup()

    def run_daily(self, fetch=None, send=None, **kw):
        kw.setdefault("now", self.NOW)
        return m.daily(fetch=fetch or (lambda sym: self.by_symbol[sym]), cache_dir=self.cache,
                       send=send or (lambda text, url: self.sent.append((text, url))), sleep=self.sleeps.append, **kw)

    def test_expected_session_is_the_last_final_weekday_bar(self):
        d = lambda *a: m.expected_session(datetime(*a, tzinfo=NY)).isoformat()
        self.assertEqual(d(2026, 11, 3, 1, 10), "2026-11-02")      # Tuesday morning -> Monday
        self.assertEqual(d(2026, 11, 3, 17, 0), "2026-11-03")      # Tuesday after the close -> Tuesday
        self.assertEqual(d(2026, 11, 3, 11, 0), "2026-11-02")      # during the session the bar is not final
        self.assertEqual(d(2026, 11, 7, 1, 10), "2026-11-06")      # Saturday morning -> Friday
        self.assertEqual(d(2026, 11, 8, 12, 0), "2026-11-06")      # Sunday -> Friday

    def test_sends_one_summary_with_the_dashboard_button_and_remembers_the_session(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"RISK_SIZER_PUBLIC_URL": "https://example.test"}):
            self.assertEqual(self.run_daily(), 0)
        self.assertEqual(len(self.sent), 1)
        text, url = self.sent[0]
        self.assertIn("Nasdaq-100 Regime", text.splitlines()[0])
        self.assertIn("<b>", text)                                  # sent as HTML
        self.assertEqual(url, "https://example.test/regime")
        self.assertNotIn("delayed", text)
        self.assertEqual(m.read_last_sent(self.cache), "2026-11-02")

    def test_the_same_session_is_never_sent_twice_unless_forced(self):
        self.run_daily()
        self.assertEqual(self.run_daily(), 0)
        self.assertEqual(len(self.sent), 1)
        self.run_daily(force=True)
        self.assertEqual(len(self.sent), 2)

    def test_download_failures_are_retried_before_sending(self):
        calls = {"n": 0}

        def flaky(sym):
            calls["n"] += 1
            if calls["n"] <= 10:                                   # the first two full attempts (5 symbols each) fail
                raise ConnectionError("yahoo hiccup")
            return self.by_symbol[sym]
        self.assertEqual(self.run_daily(fetch=flaky), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("delayed", self.sent[0][0])
        self.assertEqual(self.sleeps, [20.0, 60.0])                # waited between attempts, not before the first

    def test_stale_data_is_retried_and_finally_sent_with_a_delay_marker(self):
        stale = {k: v[v.index <= "2026-10-30"] for k, v in self.by_symbol.items()}   # Friday: Monday's bar missing
        self.assertEqual(self.run_daily(fetch=lambda sym: stale[sym]), 0)
        self.assertEqual(self.sleeps[:2], [20.0, 60.0])
        self.assertIn("⚠️ data may be delayed", self.sent[0][0])
        self.assertIn("30 Oct 2026", self.sent[0][0])

    def test_a_holiday_stays_quiet(self):
        self.run_daily()
        self.sent.clear()
        later = datetime(2026, 11, 4, 1, 10, tzinfo=NY)            # Wednesday, but no new bar arrived (market holiday)
        self.assertEqual(self.run_daily(now=later), 0)
        self.assertEqual(self.sent, [])

    def test_total_failure_exits_non_zero_and_sends_nothing(self):
        def down(sym):
            raise ConnectionError("offline")
        self.assertEqual(self.run_daily(fetch=down), 1)
        self.assertEqual(self.sent, [])

    def test_a_telegram_failure_is_retried_then_reported_and_not_remembered(self):
        attempts = []

        def broken(text, url):
            attempts.append(1)
            raise RuntimeError("telegram send failed (URLError)")
        self.assertEqual(self.run_daily(send=broken), 1)
        self.assertEqual(len(attempts), 3)
        self.assertIsNone(m.read_last_sent(self.cache))            # tomorrow's run (or a manual one) will try again

    def test_a_telegram_hiccup_that_clears_still_delivers(self):
        attempts = []

        def hiccup(text, url):
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("telegram send failed (URLError)")
            self.sent.append((text, url))
        self.assertEqual(self.run_daily(send=hiccup), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(m.read_last_sent(self.cache), "2026-11-02")

    def test_failure_notice_command(self):
        import io, contextlib
        from unittest import mock
        with mock.patch.object(m, "send_telegram") as send, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(["--notify-failure"]), 0)
        self.assertIn("could not be produced", send.call_args.args[0])

    def test_the_timer_is_a_separate_unit_so_it_cannot_touch_the_stop_loss_job(self):
        root = Path(__file__).resolve().parent.parent / "deploy"
        regime = (root / "risk-sizer-regime.service").read_text()
        eod = (root / "risk-sizer-eod.service").read_text()
        self.assertIn("market_regime.py --daily", regime)
        self.assertIn("OnFailure=risk-sizer-regime-failed.service", regime)
        self.assertNotIn("market_regime", eod)
        self.assertNotIn("risk-sizer-regime", eod)
        timer = (root / "risk-sizer-regime.timer").read_text()
        self.assertIn("OnCalendar=Tue..Sat 01:10:00 America/New_York", timer)
        self.assertIn("ExecStart=/opt/risk-sizer/.venv/bin/python market_regime.py --notify-failure",
                      (root / "risk-sizer-regime-failed.service").read_text())
        eod_src = (Path(__file__).resolve().parent.parent / "eod_stops.py").read_text()
        self.assertNotIn("market_regime", eod_src)                 # the stop-loss code does not even import it

if __name__ == "__main__":
    unittest.main()
