#!/usr/bin/env python3
"""market_regime.py - a standalone statistical barometer for the Nasdaq-100.

It answers two questions and nothing else:
  1. What state is the market in right now?  (a descriptive label + the evidence)
  2. What happened historically after days that looked like this?  (empirical analogs)

It gives NO trading advice, sizes nothing and talks to no other tool.

Method (rule-based, no machine learning):
  * Features from ^NDX (history from 1990), ^VIX, ^VIX3M (2006+), QQQ and QQEW (2006+):
      - drawdown from the 52-week high, and how fast the last 20 days fell
      - distance of price from the 200-day SMA/EMA and the slope of the SMA
      - today's shock: the daily return divided by the prior day's EWMA volatility (so the same
        -3.5% means more in a calm market), then ranked against every day since 1990
      - VIX level, VIX against its own 60-day average (available in every year) and, from 2006,
        the VIX/VIX3M term structure (>= 1.0 is inverted: stress)
      - breadth: the QQEW/QQQ ratio (equal-weight vs cap-weight Nasdaq-100)
  * A transparent flag count (red flags on/with data) and a label from simple rules:
      Bear / Systemic Stress   200d average broken AND falling with (down 15%+ or 4+ flags);
                               or down 20%+ with 4+ flags; or down 30%+
      Elevated Volatility      3+ flags, or down 10%+, or VIX 25+
      Calm / Near Highs        within 3% of the high and no flags
      Normal Pullback          everything else
  * Analogs by nearest neighbours, not hard buckets: every past day since 1990 is scored by its distance to
    today in percentile-RANK space on two equal-weight groups: price (drawdown from the 52w high, distance
    from the 200d SMA) and fear (log VIX, log VIX vs its 60-day average).  Ranks mean extremes (VIX 80)
    still find neighbours.  Slope, drop speed, VIX/VIX3M and breadth stay in the health checks only: in the
    walk-forward test they added no skill as matching dimensions.
      - only days whose outcome has already happened are candidates; the 120 closest are taken
      - they are grouped into episodes (20 trading days apart) and each episode counts ONCE, through its
        closest day
  * For 20, 60 and 120 trading days ahead: the 10th/50th/90th percentile of the forward return and of the
    maximum further drawdown from today's close, the share that ended higher, and the chance of a further
    10% / 20% drop.  The three probabilities are shrunk toward the all-days base rate,
    p = (E*p_raw + 12*base) / (E + 12) with E = episodes, because raw rates from ~10 episodes were noisier
    than the base rate itself.  Fewer than 8 episodes raises a LOW SAMPLE warning.

No look-ahead: every feature on day t uses data up to t only; forward outcomes are only read for
days whose horizon had already completed by the analysis date.

Usage:
  python market_regime.py                  # CLI report
  python market_regime.py --telegram       # the 6-line summary (add --send to post it with a dashboard button)
The formatted dashboard is the /regime page of the web app (see app.py).
  python market_regime.py --json           # machine-readable
  python market_regime.py --asof 2020-03-16 --episodes
  python market_regime.py --daily          # what the timer runs: fresh data, one summary per new session
Telegram sending uses TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ModuleNotFoundError:          # tests and --json on cached data can run without it
    yf = None

NY = ZoneInfo("America/New_York")
SYMBOLS = {"ndx": "^NDX", "vix": "^VIX", "vix3m": "^VIX3M", "qqq": "QQQ", "qqew": "QQEW"}
DOWNLOAD_START = "1985-01-01"        # extra lead-in so 52w/200d windows are warm on 1990-01-02
SAMPLE_START = pd.Timestamp("1990-01-01")
HORIZONS = (20, 60, 120)
CACHE_DIR = Path(os.environ.get("MARKET_REGIME_CACHE", Path.home() / ".cache" / "market_regime"))

# --- rules (every number the model uses is here) -----------------------------------------------
RULES = {
    "ewma_lambda": 0.94,            # volatility memory for the shock z-score
    "ewma_min_days": 60,
    "high_window": 252,             # 52 weeks
    "sma_window": 200,
    "slope_days": 20,               # slope of the 200d SMA = its change over this many days
    "fall_window": 20,
    "fast_fall": -0.10,             # a >=10% fall within 20 days is a "fast" drawdown
    "deep_dd": -0.20,
    "vix_calm": 18.0,               # one VIX ladder everywhere: <18 calm | 18-25 caution | 25-30 high stress | 30+ panic
    "vix_correction": 25.0,
    "vix_high": 30.0,
    "inversion": 1.0,               # VIX / VIX3M at or above this = inverted term structure
    "breadth_days": 60,
    "breadth_drop": -0.03,          # equal-weight lagging cap-weight by 3% over 60 days ...
    "breadth_avg_days": 100,        # ... and below its own 100-day average = narrowing
    "calm_dd": -0.03,
    "pullback_dd_max": -0.10,
    "bear_dd": -0.15,
    "bear_flags": 4,
    "capitulation_dd": -0.30,       # down 30% from the 52w high is a bear regime whatever the averages say
    "stress_flags": 3,
    "stretched": 0.30,              # price this far above the 200d average is "stretched" (the calm labels cannot see it)
    "recovery_cushion": 0.05,       # ... and this far above a RISING 200d average with a deep drawdown reads as a recovery
}
LABEL_CALM = "Calm Uptrend / Near Highs"
LABEL_PULLBACK = "Normal Bull-Market Pullback"
LABEL_ELEVATED = "Elevated Volatility Correction"
LABEL_BEAR = "Systemic Stress / Bear Regime"
EPISODE_GAP = 20


# =============================================================================== data ===========
def _to_series(frame: pd.DataFrame | pd.Series) -> pd.Series:
    """yfinance returns a one-column MultiIndex frame for a single ticker; flatten it."""
    if isinstance(frame, pd.DataFrame):
        frame = frame["Close"] if "Close" in frame.columns.get_level_values(0) else frame.iloc[:, 0]
        if isinstance(frame, pd.DataFrame):
            frame = frame.iloc[:, 0]
    s = pd.to_numeric(frame, errors="coerce").dropna()
    s.index = pd.DatetimeIndex(pd.to_datetime(s.index)).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s > 0]


def _download(symbol: str) -> pd.Series:
    if yf is None:
        raise RuntimeError("yfinance is not installed")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = yf.download(symbol, start=DOWNLOAD_START, progress=False, auto_adjust=True)
    return _to_series(raw)


def load_series(fetch: Callable[[str], pd.Series] | None = None, cache_dir: Path | None = None,
                max_age_h: float = 6.0, refresh: bool = False) -> tuple[dict[str, pd.Series], list[str]]:
    """Daily closes for the five inputs, cached on disk.  A failed download falls back to the cache
    (with a warning); a missing NDX or VIX is fatal, missing VIX3M/QQQ/QQEW just disables those flags."""
    fetch = fetch or _download
    cache_dir = cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, pd.Series] = {}
    notes: list[str] = []
    for key, symbol in SYMBOLS.items():
        path = cache_dir / f"{key}.csv"
        fresh = path.exists() and (time.time() - path.stat().st_mtime) < max_age_h * 3600 and not refresh
        series = None
        if fresh:
            series = _to_series(pd.read_csv(path, index_col=0, parse_dates=True).iloc[:, 0])
        else:
            try:
                series = fetch(symbol)
                if series is None or series.empty:
                    raise ValueError("empty download")
                series.to_csv(path, header=["close"])
            except Exception as exc:
                if path.exists():
                    series = _to_series(pd.read_csv(path, index_col=0, parse_dates=True).iloc[:, 0])
                    notes.append(f"{symbol}: download failed ({type(exc).__name__}); using cached data up to "
                                 f"{series.index[-1].date()}")
                else:
                    notes.append(f"{symbol}: unavailable ({type(exc).__name__})")
        if series is not None:
            out[key] = series
    for needed in ("ndx", "vix"):
        if needed not in out:
            raise RuntimeError(f"{SYMBOLS[needed]} is required but could not be loaded: " + "; ".join(notes))
    return out, notes


def cut_asof(series: Mapping[str, pd.Series], asof: str | pd.Timestamp | None,
             now: datetime | None = None, include_partial: bool = False) -> dict[str, pd.Series]:
    """Keep only information available at the analysis date.  Today's bar is dropped while the US
    market is open (a partial day would distort the shock and the VIX)."""
    now = (now or datetime.now(NY)).astimezone(NY)
    limit = pd.Timestamp(asof).normalize() if asof else None
    out = {}
    for k, s in series.items():
        s = s if limit is None else s[s.index <= limit]
        if (limit is None and not include_partial and len(s) and s.index[-1].date() == now.date()
                and now.hour * 60 + now.minute < 16 * 60 + 30):
            s = s.iloc[:-1]
        out[k] = s
    return out


# ========================================================================== features ===========
def build_frame(series: Mapping[str, pd.Series]) -> pd.DataFrame:
    """Align everything onto NDX trading days.  Gaps of up to 3 days are bridged (holiday mismatches);
    anything else stays NaN so a missing series can never be mistaken for a calm one."""
    idx = series["ndx"].index
    df = pd.DataFrame({"close": series["ndx"]})
    for key in ("vix", "vix3m", "qqq", "qqew"):
        df[key] = series[key].reindex(idx).ffill(limit=3) if key in series else np.nan
    return df


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """All columns on day t depend only on rows <= t (rolling/ewm windows look backwards)."""
    r = RULES
    c = df["close"]
    f = pd.DataFrame(index=df.index)
    f["close"] = c
    f["ret"] = c.pct_change()
    f["hi52"] = c.rolling(r["high_window"], min_periods=r["high_window"] // 2).max()
    f["dd"] = c / f["hi52"] - 1
    f["sma200"] = c.rolling(r["sma_window"], min_periods=r["sma_window"]).mean()
    f["ema200"] = c.ewm(span=r["sma_window"], adjust=False, min_periods=r["sma_window"]).mean()
    f["dist_sma"] = c / f["sma200"] - 1
    f["dist_ema"] = c / f["ema200"] - 1
    f["slope"] = f["sma200"] / f["sma200"].shift(r["slope_days"]) - 1
    f["fall20"] = c / c.rolling(r["fall_window"]).max() - 1
    var = (f["ret"] ** 2).ewm(alpha=1 - r["ewma_lambda"], adjust=False, min_periods=r["ewma_min_days"]).mean()
    f["z"] = f["ret"] / np.sqrt(var.shift(1))                # today's move vs yesterday's volatility
    f["vix"] = df["vix"]
    f["proxy"] = df["vix"] / df["vix"].rolling(60, min_periods=60).mean()      # fear vs its own 60-day average
    f["term"] = df["vix"] / df["vix3m"]
    ratio = df["qqew"] / df["qqq"]
    f["br_change"] = ratio / ratio.shift(r["breadth_days"]) - 1
    f["br_below"] = ratio < ratio.rolling(r["breadth_avg_days"], min_periods=r["breadth_avg_days"]).mean()
    f["br_ratio"] = ratio
    f["lvix"] = np.log(f["vix"])                             # matching works in log space: 10 to 20 is as big a move as 40 to 80
    f["lproxy"] = np.log(f["proxy"])
    narrow = (f["br_change"] <= r["breadth_drop"]) & f["br_below"]
    f["narrow"] = np.where(f["br_change"].notna() & f["br_below"].notna(), narrow.astype(float), np.nan)
    return f


def forward_outcomes(close: pd.Series, h: int) -> tuple[pd.Series, pd.Series]:
    """Per day t: the return to t+h, and the maximum further drawdown from close[t] over t+1..t+h
    (0 if price never fell below today's close).  NaN while the horizon is incomplete."""
    fwd = close.shift(-h) / close - 1
    fut_min = close.shift(-1)[::-1].rolling(h, min_periods=h).min()[::-1]
    maxdd = (fut_min / close - 1).clip(upper=0.0)
    return fwd, maxdd


# ============================================================================ state ============
@dataclass
class Flag:
    key: str
    text: str
    on: bool | None          # None = no data for this flag on this date
    detail: str = ""


def red_flags(row: pd.Series) -> list[Flag]:
    r = RULES
    def known(*names: str) -> bool:
        return all(pd.notna(row[n]) for n in names)
    flags = [
        Flag("below_sma", "Price below the 200-day average", bool(row["dist_sma"] < 0) if known("dist_sma") else None),
        Flag("falling_sma", "200-day average is falling", bool(row["slope"] < 0) if known("slope") else None),
        Flag("deep_dd", "Down 20%+ from the 52-week high", bool(row["dd"] <= r["deep_dd"]) if known("dd") else None),
        Flag("fast_fall", "Fell 10%+ within the last 20 days",
             bool(row["fall20"] <= r["fast_fall"]) if known("fall20") else None),
        Flag("high_vix", f"VIX at {r['vix_high']:.0f} or higher", bool(row["vix"] >= r["vix_high"]) if known("vix") else None),
        Flag("inverted", "VIX above VIX3M (inverted term structure)",
             bool(row["term"] >= r["inversion"]) if known("term") else None),
        Flag("narrow", "Breadth narrowing (equal-weight lagging cap-weight)",
             bool(row["narrow"]) if known("narrow") else None),
    ]
    return flags


def classify(row: pd.Series, flags: list[Flag]) -> str:
    r = RULES
    on = sum(1 for f in flags if f.on)
    dd = row["dd"] if pd.notna(row["dd"]) else 0.0
    vix = row["vix"] if pd.notna(row["vix"]) else 0.0
    by = {f.key: f.on for f in flags}
    trend_broken = bool(by.get("below_sma")) and bool(by.get("falling_sma"))
    if (trend_broken and (dd <= r["bear_dd"] or on >= r["bear_flags"])) \
            or (dd <= r["deep_dd"] and on >= r["bear_flags"]) or dd <= r["capitulation_dd"]:
        return LABEL_BEAR
    if on >= r["stress_flags"] or dd <= r["pullback_dd_max"] or vix >= r["vix_correction"]:
        return LABEL_ELEVATED
    if dd > r["calm_dd"] and on == 0:
        return LABEL_CALM
    return LABEL_PULLBACK


# ========================================================================== analogs =============
@dataclass
class HorizonStats:
    horizon: int
    n: int                             # separate EPISODES behind the numbers (each counts once)
    days: int = 0                      # matched days the episodes were picked from
    ret_p10: float | None = None
    ret_p50: float | None = None
    ret_p90: float | None = None
    dd_p10: float | None = None        # the bad-luck end: the deepest further drawdown in 10% of episodes
    dd_p50: float | None = None
    dd_p90: float | None = None
    win_raw: float | None = None       # share of episodes that ended higher (unshrunk)
    down10_raw: float | None = None    # share with a further 10% drop within the horizon
    crash20_raw: float | None = None   # share with a further 20% drop within the horizon
    win: float | None = None           # the same three, shrunk toward the all-days base rate
    down10: float | None = None
    crash20: float | None = None
    base_win: float | None = None      # unconditional rates over every finished day since 1990
    base_down10: float | None = None
    base_crash20: float | None = None
    base_ret_p50: float | None = None
    base_dd_p10: float | None = None
    low_sample: bool = False           # fewer than KNN["warn_episodes"] episodes


@dataclass
class AnalogResult:
    key: str
    title: str
    days: int
    episodes: int                      # separate episodes at the 60-day horizon (the headline one)
    low_confidence: bool
    first_date: str | None
    last_date: str | None
    spread: str = ""                   # "Matched 22 episodes (120 days): drawdown -12.2% to -6.5%, ..."
    ranges: dict = field(default_factory=dict)
    horizons: list[HorizonStats] = field(default_factory=list)
    episode_table: list[dict] = field(default_factory=list)


def _episodes(positions: np.ndarray, gap: int = EPISODE_GAP) -> list[tuple[int, int]]:
    """Group matched day-positions into episodes: a new one starts after `gap` trading days apart."""
    if len(positions) == 0:
        return []
    out, start, prev = [], positions[0], positions[0]
    for p in positions[1:]:
        if p - prev > gap:
            out.append((int(start), int(prev)))
            start = p
        prev = p
    out.append((int(start), int(prev)))
    return out


# --- nearest-neighbour matching --------------------------------------------------------------
# Walk-forward tested on 1,500+ dates since 1996 (see the audit notes): matching on percentile RANKS, one
# price group and one volatility group of equal weight, one observation per episode, and probabilities
# shrunk toward the base rate.  Raw hit rates from ~10 episodes were worse than quoting the base rate.
KNN = {"k": 120, "gap": EPISODE_GAP, "warn_episodes": 8, "shrink_m": 12.0}
MATCH_GROUPS = ((1.0, ("dd", "dist_sma")), (1.0, ("lvix", "lproxy")))     # (group weight, features)
MATCH_COLUMNS = tuple(c for _, cols in MATCH_GROUPS for c in cols)
FEATURE_LABELS = {"dd": ("drawdown", "pct"), "dist_sma": ("200d cushion", "pct"),
                  "vix": ("VIX", "num"), "proxy": ("VIX vs 60d average", "x")}
SPREAD_ORDER = ["dd", "dist_sma", "vix", "proxy"]


def _fmt_feature(kind: str, v: float) -> str:
    return {"pct": f"{v * 100:+.1f}%", "pct2": f"{v * 100:+.2f}%", "num": f"{v:.1f}", "x": f"{v:.2f}"}[kind]


def _rank_distance(f: pd.DataFrame, start: pd.Timestamp) -> tuple[np.ndarray, np.ndarray] | None:
    """Squared distance of every day to today (the last row) in percentile-rank space, and a mask of days
    whose matching features are all known.  Each group's squared gaps are averaged over its features, so a
    group counts the same however many features it holds.  Ranks are taken against the history up to today
    only, so the scale adapts to extremes (VIX 80 is simply 'the top percentile') and nothing leaks."""
    X = f[list(MATCH_COLUMNS)].to_numpy(dtype=float)
    if np.isnan(X[-1]).any():
        return None
    known = (f.index >= start) & ~np.isnan(X).any(axis=1)
    if known.sum() < 200:
        return None
    ranks = np.empty_like(X)
    for j in range(X.shape[1]):
        ref = np.sort(X[known, j])
        ranks[:, j] = (np.searchsorted(ref, X[:, j], "left") + np.searchsorted(ref, X[:, j], "right")) / (2 * len(ref))
    gaps = (ranks - ranks[-1]) ** 2
    dist, j = np.zeros(len(f)), 0
    for weight, cols in MATCH_GROUPS:
        dist += weight * gaps[:, j:j + len(cols)].mean(axis=1)
        j += len(cols)
    return dist, known


def knn_episodes(f: pd.DataFrame, horizon: int, start: pd.Timestamp = SAMPLE_START, k: int = KNN["k"],
                 gap: int = KNN["gap"]) -> dict | None:
    """The k closest days whose `horizon`-day outcome has ALREADY happened (chosen before ranking, so recent
    unfinished days cannot crowd out real analogs), grouped into episodes; each episode is represented by
    its single closest day.  Returns None when today lacks a matching dimension or history is too short."""
    ranked = _rank_distance(f, start)
    if ranked is None:
        return None
    dist, known = ranked
    last = len(f) - 1
    cand = np.flatnonzero(known & (np.arange(len(f)) <= last - horizon))
    if len(cand) < k:
        return None
    sel = np.sort(cand[np.argpartition(dist[cand], k - 1)[:k]])
    reps, spans = [], []
    for a, b in _episodes(sel, gap):
        seg = sel[(sel >= a) & (sel <= b)]
        reps.append(int(seg[np.argmin(dist[seg])]))
        spans.append((a, b, len(seg)))
    return {"days": sel, "reps": np.array(reps), "spans": spans}


def _pct(a: np.ndarray, q: float) -> float:
    return float(np.percentile(a, q)) if len(a) else float("nan")


def shrink(p: float, episodes: int, base: float, m: float = KNN["shrink_m"]) -> float:
    """Empirical-Bayes blend: with few episodes the answer stays near the base rate, with many it follows the data."""
    return (episodes * p + m * base) / (episodes + m)


def analog_stats(f: pd.DataFrame, horizons=HORIZONS) -> AnalogResult | None:
    close = f["close"]
    start_ok = f.index >= SAMPLE_START
    res_h, sel60 = [], None
    for h in horizons:
        pick = knn_episodes(f, h)
        if pick is None:
            return None
        fwd, mdd = forward_outcomes(close, h)
        fwd_a, mdd_a = fwd.to_numpy(), mdd.to_numpy()
        a_ret, a_dd = fwd_a[pick["reps"]], mdd_a[pick["reps"]]
        n = len(pick["reps"])
        base = start_ok & np.isfinite(fwd_a) & np.isfinite(mdd_a)
        hs = HorizonStats(horizon=h, n=n, days=len(pick["days"]), low_sample=n < KNN["warn_episodes"])
        hs.ret_p10, hs.ret_p50, hs.ret_p90 = (_pct(a_ret, q) for q in (10, 50, 90))
        hs.dd_p10, hs.dd_p50, hs.dd_p90 = (_pct(a_dd, q) for q in (10, 50, 90))
        hs.win_raw = float((a_ret > 0).mean())
        hs.down10_raw = float((a_dd <= -0.10).mean())
        hs.crash20_raw = float((a_dd <= -0.20).mean())
        hs.base_win = float((fwd_a[base] > 0).mean())
        hs.base_down10 = float((mdd_a[base] <= -0.10).mean())
        hs.base_crash20 = float((mdd_a[base] <= -0.20).mean())
        hs.base_ret_p50 = _pct(fwd_a[base], 50)
        hs.base_dd_p10 = _pct(mdd_a[base], 10)
        hs.win = shrink(hs.win_raw, n, hs.base_win)
        hs.down10 = shrink(hs.down10_raw, n, hs.base_down10)
        hs.crash20 = shrink(hs.crash20_raw, n, hs.base_crash20)
        res_h.append(hs)
        if h == 60:
            sel60 = pick
    if sel60 is None:
        sel60 = knn_episodes(f, horizons[len(horizons) // 2])
    days = sel60["days"]
    ranges = {}
    for col in SPREAD_ORDER:
        name, kind = FEATURE_LABELS[col]
        vals = f[col].to_numpy()[days]
        ranges[col] = {"label": name, "lo": float(vals.min()), "hi": float(vals.max()), "kind": kind}
    n60 = len(sel60["reps"])
    spread = (f"Matched {n60} episodes ({len(days)} days): "
              + ", ".join(f"{r['label']} {_fmt_feature(r['kind'], r['lo'])} to {_fmt_feature(r['kind'], r['hi'])}"
                          for r in ranges.values()))
    res = AnalogResult(key="full", title="Historical analogs since 1990", days=len(days), episodes=n60,
                       low_confidence=n60 < KNN["warn_episodes"], first_date=str(f.index[days[0]].date()),
                       last_date=str(f.index[days[-1]].date()), spread=spread, ranges=ranges, horizons=res_h)
    fwd60, _ = forward_outcomes(close, 60)
    _, mdd120 = forward_outcomes(close, 120)
    for (a, b, nd), rep in zip(sel60["spans"], sel60["reps"]):
        res.episode_table.append({
            "start": str(f.index[a].date()), "end": str(f.index[b].date()), "days": nd,
            "match": str(f.index[rep].date()),
            "ret_60d": None if pd.isna(fwd60.iloc[rep]) else float(fwd60.iloc[rep]),
            "worst_dd_120d": None if pd.isna(mdd120.iloc[rep]) else float(mdd120.iloc[rep])})
    return res


def find_analogs(f: pd.DataFrame) -> AnalogResult | None:
    """The analog set for today (the last row of `f`); None if a matching dimension is missing."""
    return analog_stats(f)


# =========================================================================== report =============
@dataclass
class Snapshot:
    date: str
    close: float
    dd: float
    high_date: str
    dist_sma: float | None
    dist_ema: float | None
    slope: float | None
    fall20: float | None               # change from the highest close of the last 20 days
    ret: float | None
    z: float | None
    shock_pct: float | None            # share of days since 1990 with a MORE negative (or positive) shock
    shock_one_in: float | None
    raw_one_in: float | None           # how often a daily move this large (unadjusted) happened since 1990
    vix: float | None
    term: float | None
    inverted: bool | None
    br_change: float | None
    narrow: bool | None


@dataclass
class Report:
    label: str
    snapshot: Snapshot
    flags: list[Flag]
    flags_on: int
    flags_known: int
    analogs: AnalogResult              # one unified set, 1990+
    notes: list[str]


def _f(v) -> float | None:
    return None if v is None or (isinstance(v, float) and math.isnan(v)) or pd.isna(v) else float(v)


def analyze(series: Mapping[str, pd.Series], notes: list[str] | None = None) -> Report:
    f = compute_features(build_frame(series))
    f = f[f.index >= pd.Timestamp("1986-01-01")]
    if len(f) < 300 or pd.isna(f.iloc[-1]["dist_sma"]) or pd.isna(f.iloc[-1]["slope"]) or pd.isna(f.iloc[-1]["vix"]):
        raise RuntimeError("not enough data to describe the current state (need NDX history and VIX)")
    cur = f.iloc[-1]
    notes = list(notes or [])
    flags = red_flags(cur)
    label = classify(cur, flags)

    hist_z = f.loc[f.index >= SAMPLE_START, "z"].dropna().to_numpy()
    z = _f(cur["z"])
    shock_pct = shock_one_in = None
    if z is not None and len(hist_z):
        shock_pct = float((hist_z <= z).mean()) if z < 0 else float((hist_z >= z).mean())
        shock_one_in = 1.0 / shock_pct if shock_pct > 0 else float(len(hist_z))
    raw_one_in = None
    hist_r = f.loc[f.index >= SAMPLE_START, "ret"].dropna().to_numpy()
    if _f(cur["ret"]) is not None and len(hist_r):
        share = float((hist_r <= cur["ret"]).mean() if cur["ret"] < 0 else (hist_r >= cur["ret"]).mean())
        raw_one_in = 1.0 / share if share > 0 else float(len(hist_r))
    window = f["close"].iloc[-RULES["high_window"]:]
    snap = Snapshot(
        date=str(f.index[-1].date()), close=float(cur["close"]), dd=float(cur["dd"]),
        high_date=str(window.idxmax().date()), dist_sma=_f(cur["dist_sma"]), dist_ema=_f(cur["dist_ema"]),
        slope=_f(cur["slope"]), fall20=_f(cur["fall20"]), ret=_f(cur["ret"]), z=z, shock_pct=shock_pct, shock_one_in=shock_one_in,
        raw_one_in=raw_one_in, vix=_f(cur["vix"]), term=_f(cur["term"]),
        inverted=None if pd.isna(cur["term"]) else bool(cur["term"] >= RULES["inversion"]),
        br_change=_f(cur["br_change"]), narrow=None if pd.isna(cur["narrow"]) else bool(cur["narrow"]))
    analogs = find_analogs(f)
    if analogs is None:
        raise RuntimeError("not enough history to find similar days")
    notes += state_notes(snap)
    for hs in analogs.horizons:
        if hs.low_sample:
            notes.append(f"LOW SAMPLE ({hs.horizon}-day outlook): only {hs.n} separate episodes matched "
                         f"(fewer than {KNN['warn_episodes']}); treat the numbers as rough.")
    return Report(
        label=label, snapshot=snap, flags=flags, flags_on=sum(1 for x in flags if x.on),
        flags_known=sum(1 for x in flags if x.on is not None), analogs=analogs, notes=notes)


def state_notes(s: "Snapshot") -> list[str]:
    """Context the label cannot carry: a stretched market (calm labels hide it) and a recovery that the
    52-week drawdown (still measured from an old high) makes look worse than it is."""
    out = []
    if s.dist_sma is not None and s.dist_sma >= RULES["stretched"]:
        out.append(f"Stretched: price is {s.dist_sma * 100:.0f}% above its 200-day average. Calm labels do not capture "
                   f"how far the market has run.")
    if (s.dist_sma is not None and s.slope is not None and s.dd <= RULES["bear_dd"]
            and s.dist_sma >= RULES["recovery_cushion"] and s.slope > 0):
        out.append(f"Recovery pattern: the {s.dd * 100:.0f}% drawdown is measured from the {s.high_date} high, but price is "
                   f"{s.dist_sma * 100:.0f}% above a rising 200-day average, so the damage may be mostly repaired.")
    return out


# =========================================================================== rendering ===========
def _p(v: float | None, signed: bool = True, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    return f"{v * 100:+.{digits}f}%" if signed else f"{v * 100:.{digits}f}%"


def _one_in(n: float | None) -> str:
    return "n/a" if n is None else (f"1 in {n:,.0f} days" if n >= 2 else "an ordinary day")


def render_text(rep: Report, episodes: bool = False) -> str:
    s, a = rep.snapshot, rep.analogs
    L = []
    L.append(f"NASDAQ-100 MARKET REGIME - {s.date}")
    L.append("=" * 62)
    L.append(f"Regime: {rep.label}    (red flags: {rep.flags_on} of {rep.flags_known} measurable)")
    L.append("")
    L.append("CURRENT SNAPSHOT")
    L.append(f"  NDX {s.close:,.0f}   drawdown from 52w high: {_p(s.dd)}  (high {s.high_date})")
    if s.dist_sma is not None:
        L.append(f"  200d SMA: price {_p(s.dist_sma)} from it, slope {_p(s.slope, digits=2)} over 20 days"
                 f"   |   200d EMA: price {_p(s.dist_ema)} from it")
    if s.z is not None and s.ret is not None:
        side = "worse" if s.z < 0 else "better"
        L.append(f"  Today: {_p(s.ret, digits=2)}   shock z = {s.z:+.2f} (vs yesterday's volatility)   "
                 f"{side} than {(1 - s.shock_pct) * 100:.1f}% of days since 1990 ({_one_in(s.shock_one_in)})")
        L.append(f"          raw size alone (not volatility-adjusted): {_one_in(s.raw_one_in)}")
    if s.vix is not None:
        if s.term is not None:
            L.append(f"  VIX {s.vix:.1f}   VIX/VIX3M {s.term:.2f}  ({'INVERTED' if s.inverted else 'normal curve'})")
        else:
            L.append(f"  VIX {s.vix:.1f}   (no VIX3M data for this date)")
    if s.br_change is not None:
        L.append(f"  Breadth QQEW/QQQ: {_p(s.br_change)} over 60 days  ({'narrowing' if s.narrow else 'not narrowing'})")
    else:
        L.append("  Breadth QQEW/QQQ: no data for this date")
    L.append("")
    L.append("RED FLAGS")
    for fl in rep.flags:
        mark = "[ ? ]" if fl.on is None else ("[ X ]" if fl.on else "[   ]")
        L.append(f"  {mark} {fl.text}" + ("   (no data)" if fl.on is None else ""))
    L.append("")
    L.append("HISTORICAL ANALOGS  (the days since 1990 closest to today; each episode counts once)")
    a = rep.analogs
    L.append("")
    L.append(f"  {a.title}   -   {a.episodes} episodes at 60 days" + ("   (LOW SAMPLE)" if a.low_confidence else ""))
    L.append(f"  {a.spread}")
    L.append("  Probabilities are shrunk toward the all-days base rate (in brackets).")
    L.append("  Forward return                 p10      median     p90      higher (all days)")
    for h in a.horizons:
        L.append(f"    {h.horizon:>3} trading days      {_p(h.ret_p10):>9} {_p(h.ret_p50):>9} {_p(h.ret_p90):>9}"
                 f"      {_p(h.win, False, 0)} ({_p(h.base_win, False, 0)})   (episodes={h.n})")
    L.append("  Maximum further drawdown from today's close   p10(worst)  median     p90   (all days p10)")
    for h in a.horizons:
        L.append(f"    {h.horizon:>3} trading days                 {_p(h.dd_p10):>9} {_p(h.dd_p50):>9} {_p(h.dd_p90):>9}"
                 f"   ({_p(h.base_dd_p10)})")
    L.append("  Chance of ...                 further -10%   further -20%")
    for h in a.horizons:
        L.append(f"    within {h.horizon:>3} days          {_p(h.down10, False, 0):>6} ({_p(h.base_down10, False, 0)})"
                 f"   {_p(h.crash20, False, 0):>6} ({_p(h.base_crash20, False, 0)})")
    if episodes and a.episode_table:
        L.append("  EPISODES  (outcome counted from each episode's closest matching day)")
        L.append("    start       end         days   match day    60d later   worst drop within 120d")
        for e in a.episode_table:
            L.append(f"    {e['start']}  {e['end']}  {e['days']:>5}   {e['match']}  {_p(e['ret_60d']):>9}   {_p(e['worst_dd_120d']):>10}")
    L.append("")
    for n in rep.notes:
        L.append(f"  * {n}")
    take = view_model(rep)["analogs"]["takeaway"]
    if take:
        L.append("  " + take["text"])
    L.append("  * Statistics describe the past, not a forecast. Trust the episode count, not the day count.")
    return "\n".join(L)


SUMMARIES = {
    LABEL_CALM: "The market is near its highs and nothing is flashing. Business as usual.",
    LABEL_PULLBACK: "A normal dip inside a healthy uptrend. These happen several times a year and usually pass.",
    LABEL_ELEVATED: "Stress is higher than usual: bigger swings and a deeper drop. The long-term trend may still be intact.",
    LABEL_BEAR: "Several stress signals are present today (a broken trend, a deep drawdown, high fear). This describes current conditions, not a forecast: markets can stay here or recover.",
}
TONES = {LABEL_CALM: "good", LABEL_PULLBACK: "good", LABEL_ELEVATED: "warn", LABEL_BEAR: "bad"}
TELEGRAM_ICON = {"good": "🟢", "warn": "🟠", "bad": "🔴"}
VIX_SCALE = "<18 Calm | 18–25 Caution | 25–30 High Stress | 30+ Panic"
HORIZON_NAMES = {20: "1 Month (20d)", 60: "3 Months (60d)", 120: "6 Months (120d)"}


def vix_band(vix: float) -> tuple[str, str]:
    """The one VIX ladder (RULES): <18 Calm | 18-25 Caution | 25-30 High Stress | 30+ Panic."""
    if vix < RULES["vix_calm"]:
        return "Calm", "good"
    if vix < RULES["vix_correction"]:
        return "Caution", "warn"
    if vix < RULES["vix_high"]:
        return "High Stress", "warn"
    return "Panic", "bad"


# --- the seven health checks, graded: ok / caution / watch / red (or na when there is no data) ------------
WATCH = {"trend": 0.03, "slope": 0.005, "dd": -0.15, "fall": -0.075, "vix_caution": RULES["vix_calm"], "vix_watch": RULES["vix_correction"],
         "term": 0.95, "breadth": -0.02}
BADGES = {"ok": "OK", "caution": "CAUTION", "watch": "WATCH", "red": "RED FLAG", "na": "NO DATA"}
LEVEL_RANK = {"red": 0, "watch": 1, "caution": 2, "na": 3, "ok": 4}


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _checks(rep: Report) -> list[dict]:
    """The 7 health checks.  Each has a level (ok / caution / watch / red / na), a plain status line, and a
    gauge: 0 = comfortably far from its red-flag line, 1 = at or past it.  A red flag never looks fine,
    and a value close to flipping shows as WATCH instead of hiding behind a green OK."""
    s = rep.snapshot
    on = {f.key: f.on for f in rep.flags}
    d = (lambda v: _p(v, digits=1))

    def item(key, title, value, flag_key, fill, limit, ok_text, watch_text, red_text, level_fn):
        flag = on[flag_key]
        if flag is None or value is None:
            return {"key": key, "title": title, "level": "na", "ok": None, "badge": BADGES["na"],
                    "status": "No data for this date", "gauge": 0.0, "limit": limit}
        level = "red" if flag else level_fn(value)
        text = red_text if level == "red" else ok_text if level == "ok" else watch_text
        return {"key": key, "title": title, "level": level, "ok": level == "ok" if level != "na" else None,
                "badge": BADGES[level], "status": text, "gauge": 1.0 if level == "red" else _clamp(fill(value)), "limit": limit}

    def lvl(watch_if):
        return lambda v: "watch" if watch_if(v) else "ok"

    out = [
        item("trend", "Long-Term Trend", s.dist_sma, "below_sma", lambda v: (0.10 - v) / 0.10,
             "Red flag if price drops below the 200-day line (0%)",
             f"Price is {d(s.dist_sma)} above the 200-day average",
             f"Price is {d(s.dist_sma)} above the 200-day average: a thin cushion",
             f"Price is {d(abs(s.dist_sma or 0))} below the 200-day average",
             lvl(lambda v: v < WATCH["trend"])),
        item("slope", "200-Day Direction", s.slope, "falling_sma", lambda v: (0.015 - v) / 0.015,
             "Red flag if the 200-day line turns down (below 0%)",
             f"The 200-day line is rising ({_p(s.slope, digits=2)} in 20 days)",
             f"The 200-day line is only flattening upward ({_p(s.slope, digits=2)} in 20 days)",
             f"The 200-day line is falling ({_p(s.slope, digits=2)} in 20 days)",
             lvl(lambda v: v < WATCH["slope"])),
        item("drawdown", "Drawdown Depth", s.dd, "deep_dd", lambda v: -v / 0.20,
             "Red flag at -20% (official bear market)",
             f"{d(s.dd)} from the 52-week high (bear market starts at -20%)",
             f"{d(s.dd)} from the 52-week high: approaching the -20% bear-market line",
             f"{d(s.dd)} from the 52-week high: past the -20% bear-market line",
             lvl(lambda v: v <= WATCH["dd"])),
        item("speed", "Drop Speed", s.fall20, "fast_fall", lambda v: -v / 0.10,
             "Red flag at -10% within 20 days (panic speed)",
             f"{d(s.fall20)} over the last 20 days (alarm at -10%)",
             f"{d(s.fall20)} over the last 20 days: close to the -10% panic-speed line",
             f"{d(s.fall20)} within 20 days: a panic-speed drop",
             lvl(lambda v: v <= WATCH["fall"])),
        item("vix", "Market Fear (VIX)", s.vix, "high_vix", lambda v: (v - 12.0) / 18.0,
             "Red flag at 30+ (institutional panic)",
             f"VIX is {s.vix:.1f} (panic starts at 30)" if s.vix is not None else "",
             (f"VIX is {s.vix:.1f}: " + ("high stress, close to panic (30)" if s.vix >= WATCH["vix_watch"]
                                         else "elevated caution")) if s.vix is not None else "",
             f"VIX is {s.vix:.1f}: institutional panic territory (30+)" if s.vix is not None else "",
             lambda v: "watch" if v >= WATCH["vix_watch"] else "caution" if v >= WATCH["vix_caution"] else "ok"),
        item("term", "Fear Term Structure", s.term, "inverted", lambda v: (v - 0.80) / 0.20,
             "Red flag at 1.00 or above (inverted)",
             f"VIX/VIX3M is {s.term:.2f} (normal: below 1.0)" if s.term is not None else "",
             f"VIX/VIX3M is {s.term:.2f}: close to inversion (1.0)" if s.term is not None else "",
             f"VIX/VIX3M is {s.term:.2f}: inverted, short-term fear above 3-month fear" if s.term is not None else "",
             lvl(lambda v: v >= WATCH["term"])),
        item("breadth", "Market Breadth", s.br_change, "narrow", lambda v: (0.02 - v) / 0.05,
             "Red flag if equal-weight lags cap-weight by 3% over 60 days",
             f"Equal-weight vs QQQ: {d(s.br_change)} over 60 days (not narrowing)" if s.br_change is not None else "",
             f"Equal-weight vs QQQ: {d(s.br_change)} over 60 days: close to the narrowing line" if s.br_change is not None else "",
             f"Equal-weight vs QQQ: {d(s.br_change)} over 60 days: only a few mega-caps are holding the index up"
             if s.br_change is not None else "",
             lvl(lambda v: v <= WATCH["breadth"])),
    ]
    return sorted(out, key=lambda c: (LEVEL_RANK[c["level"]], -c["gauge"]))


def _kpis(rep: Report, checks: list[dict]) -> list[dict]:
    """Four headline cards.  The VALUE is coloured by its sign (a minus is red); the status PILL carries the
    regime verdict, so '-9.1%' can be red while 'Normal pullback' is green."""
    s = rep.snapshot
    level = {c["key"]: c["level"] for c in checks}
    sign = lambda v: "bad" if v < 0 else "good"
    if s.dd > -0.05:
        dd_note, dd_tone = "Near the highs", "good"
    elif s.dd > -0.10:
        dd_note, dd_tone = "Normal pullback", "good"
    elif s.dd > -0.20:
        dd_note, dd_tone = "Correction", "warn"
    else:
        dd_note, dd_tone = "Bear-market territory", "bad"
    kpis = [{"key": "drawdown", "title": "Pullback from 52w High", "value": _p(s.dd), "value_tone": sign(s.dd),
             "pill": dd_note, "pill_tone": dd_tone, "scale": "Normal pullback: 5–10% | Bear market: 20%+"}]
    if s.dist_sma is not None:
        above, rising = s.dist_sma >= 0, (s.slope or 0) >= 0
        trend_level = level.get("trend", "ok")
        if not above:
            pill, pill_tone = "Trend broken", "bad"
        elif trend_level == "watch":
            pill, pill_tone = "Thin cushion: watch", "warn"
        elif not rising:
            pill, pill_tone = "200d line falling", "warn"
        else:
            pill, pill_tone = "Comfortable cushion", "good"
        kpis.append({"key": "trend", "title": "Long-Term Trend (200d SMA)",
                     "value": f"{_p(s.dist_sma)} {'Above' if above else 'Below'} & {'Rising' if rising else 'Falling'}",
                     "value_tone": "good" if above else "bad", "pill": pill, "pill_tone": pill_tone,
                     "scale": "Bullish above 200d SMA | Bearish below"})
    if s.vix is not None:
        word, _ = vix_band(s.vix)
        tone = {"ok": "good", "caution": "warn", "watch": "orange", "red": "bad"}.get(level.get("vix", "ok"), "good")
        kpis.append({"key": "vix", "title": "Market Fear (VIX)", "value": f"{s.vix:.1f}", "value_tone": tone,
                     "pill": word, "pill_tone": tone, "scale": VIX_SCALE})
    if s.ret is not None:
        z = s.z or 0
        pill, pill_tone = (("Rare shock", "bad") if z <= -2.5 else ("Unusual drop", "warn") if z <= -1.5
                           else ("Ordinary day", "good") if s.ret < 0 else ("Up day", "good"))
        note = (f"Shock rarity: ~1 in {s.shock_one_in:,.0f} trading days"
                if s.shock_one_in and s.shock_one_in >= 2 else "An ordinary day")
        kpis.append({"key": "today", "title": "Today's Move", "value": _p(s.ret, digits=2), "value_tone": sign(s.ret),
                     "pill": pill, "pill_tone": pill_tone, "scale": note,
                     "raw": f"Raw size alone: ~{_one_in(s.raw_one_in)}"})
    return kpis


def _set_model(res: AnalogResult) -> dict:
    """Rows carry the shrunk probabilities plus the all-days base rate beside each number, so a reader can
    always tell whether today's setup is better or worse than an average day."""
    rows = []
    for h in res.horizons:
        if not h.n:
            continue
        rows.append({"horizon": h.horizon, "timeframe": HORIZON_NAMES.get(h.horizon, f"{h.horizon} days"),
                     "win_rate": h.win, "typical": h.ret_p50, "worst_dip": h.dd_p10, "crash_risk": h.crash20,
                     "base_win_rate": h.base_win, "base_typical": h.base_ret_p50, "base_worst_dip": h.base_dd_p10,
                     "base_crash_risk": h.base_crash20, "n": h.n, "low_sample": h.low_sample})
    return {"key": res.key, "title": res.title, "days": res.days, "episodes": res.episodes,
            "low_confidence": res.low_confidence, "spread": res.spread, "rows": rows,
            "shrink_m": KNN["shrink_m"], "first_date": res.first_date, "last_date": res.last_date,
            "raw": {"horizons": [asdict(h) for h in res.horizons], "episodes": res.episode_table}}


TAKEAWAYS = {
    "better": "💡 Bottom line: Historically, this setup beats a random market day on every metric - higher typical return with lower crash risk.",
    "volatile": "💡 Bottom line: Higher historical upside than an average day, but comes with elevated short-term drawdown risk - expect volatility.",
    "weaker": "💡 Bottom line: Historically weaker odds than an average market day with elevated crash risk - lower returns paired with wider tail exposure.",
    "inline": "💡 Bottom line: Historical odds are roughly in line with an average market day - no unusual edge or tail risk.",
}
TAKE_RETURN, TAKE_CRASH, TAKE_DIP = 0.01, 0.015, 0.02     # gaps smaller than this count as "roughly in line"


def takeaway(row: dict | None) -> dict | None:
    """One plain sentence from the 3-month row: today's matched numbers against the all-days baseline.
    Fewer than KNN['warn_episodes'] episodes never claims an edge."""
    if not row:
        return None
    if row["low_sample"]:
        return {"key": "low_sample", "text": f"💡 Bottom line: Only {row['n']} separate episodes matched, too few to say whether "
                                             f"the odds differ from an average market day."}
    ret_gap = row["typical"] - row["base_typical"]
    risk_higher = (row["crash_risk"] - row["base_crash_risk"] > TAKE_CRASH
                   or row["worst_dip"] < row["base_worst_dip"] - TAKE_DIP)
    risk_no_worse = row["crash_risk"] <= row["base_crash_risk"] and row["worst_dip"] >= row["base_worst_dip"]
    if ret_gap > TAKE_RETURN and risk_no_worse:
        key = "better"
    elif ret_gap > TAKE_RETURN and risk_higher:
        key = "volatile"
    elif ret_gap < -TAKE_RETURN and risk_higher:
        key = "weaker"
    else:
        key = "inline"
    return {"key": key, "text": TAKEAWAYS[key]}


def view_model(rep: Report) -> dict:
    """Everything a screen needs, in plain language.  The web page and the Telegram text both read this."""
    s = rep.snapshot
    checks = _checks(rep)
    sets = [_set_model(rep.analogs)]
    return {
        "title": "Nasdaq-100 (NDX / QQQ) Market Regime", "as_of": s.date, "label": rep.label,
        "tone": TONES[rep.label], "summary": SUMMARIES[rep.label], "flags_on": rep.flags_on,
        "flags_known": rep.flags_known,
        "watch_count": sum(1 for c in checks if c["level"] in ("watch", "caution")),
        "kpis": _kpis(rep, checks), "checks": checks,
        "analogs": {"sets": sets, "takeaway": takeaway(next((r for r in sets[0]["rows"] if r["horizon"] == 60), None))},
        "notes": rep.notes,
    }


def compute_view(refresh: bool = False, asof: str | None = None) -> dict:
    """Download (cached), analyse, and return the view model: what the /regime page shows."""
    series, notes = load_series(refresh=refresh)
    return view_model(analyze(cut_asof(series, asof), notes=notes))


def dashboard_url() -> str | None:
    explicit = os.environ.get("REGIME_DASHBOARD_URL", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("RISK_SIZER_PUBLIC_URL", "").strip().rstrip("/")
    return f"{base}/regime" if base.startswith("https://") else None


_MARK = {"watch": " ⚠️", "caution": " ⚠️", "red": " 🚨"}


def render_telegram(rep: Report, delayed: bool = False, html_markup: bool = True) -> str:
    """The phone summary: bold anchors, green gains, red-marked drops, one idea per line.  With html_markup=False
    the tags are stripped (for the terminal)."""
    from html import escape
    vm = view_model(rep)
    s = rep.snapshot
    lvl = {c["key"]: c["level"] for c in vm["checks"]}
    mark = lambda key: _MARK.get(lvl.get(key, "ok"), "")
    b = lambda t: f"<b>{t}</b>"

    def c(text: str, value: float | None = None, good_if: float | None = None) -> str:
        """Colour a number for Telegram, whose dark theme paints <code> bright green:
        gains (and high win rates) go in <code> = green; drops stay bold and get a red marker; the rest is bold."""
        if value is None:
            return b(text)
        if good_if is not None:                      # an unsigned figure such as a win rate
            return f"<code>{text}</code>" if value >= good_if else b(text)
        if value > 0:
            return f"<code>{text}</code>"
        if value < 0:
            return f"🔻 {b(text)}"
        return b(text)
    d = datetime.fromisoformat(s.date)
    head = (f"📊 {b(f'Nasdaq-100 Regime · {d:%-d %b %Y}')}" + (" ⚠️ data may be delayed" if delayed else "") + "\n"
            f"{TELEGRAM_ICON[vm['tone']]} {b(escape(rep.label))} ({rep.flags_on}/{rep.flags_known} red flags · "
            f"{vm['watch_count']} watch)")
    lines = [head, ""]
    trend = ""
    if s.dist_sma is not None:
        worst = min((lvl.get(k, "ok") for k in ("trend", "slope")), key=lambda x: LEVEL_RANK[x])
        trend = (f" | {c(_p(s.dist_sma), s.dist_sma)} {'above' if s.dist_sma >= 0 else 'below'} "
                 f"{'rising' if (s.slope or 0) >= 0 else 'falling'} 200d{_MARK.get(worst, '')}")
    dd_mark = _MARK.get(lvl.get("drawdown", "ok"), "")
    lines.append(f"• {b('Trend:')} {c(_p(s.dd), s.dd)} from high{dd_mark}{trend}")
    speed = f"{c(_p(s.fall20), s.fall20)} in 20d{mark('speed')}" if s.fall20 is not None else "20d move n/a"
    if s.vix is not None:
        word, _ = vix_band(s.vix)
        fear = f"VIX {b(f'{s.vix:.1f}')} ({word}){mark('vix')}"
    else:
        fear = "VIX n/a"
    lines.append(f"• {b('Speed & Stress:')} {speed} | {fear}")
    parts = []
    for key, name in (("term", "VIX curve"), ("breadth", "Breadth")):
        if lvl.get(key) == "na":
            parts.append((name, "n/a"))
        else:
            parts.append((name, {"ok": "OK", "caution": "⚠️ near the line", "watch": "⚠️ near the line",
                                 "red": "🚨 " + ("inverted" if key == "term" else "narrowing")}[lvl[key]]))
    if all(v == "OK" for _, v in parts):
        structure = "VIX curve & Breadth OK"
    else:
        structure = " | ".join(f"{n} {v}" for n, v in parts)
    lines.append(f"• {b('Structure:')} {structure}")
    if s.ret is not None:
        shock = (f" (1-in-{s.shock_one_in:,.0f}d shock)" if s.shock_one_in and s.shock_one_in >= 2 else " (ordinary day)")
        lines.append(f"• {b('Today:')} {c(_p(s.ret, digits=2), s.ret)}{shock}")
    lines.append("")
    full = vm["analogs"]["sets"][0]
    row = next((r for r in full["rows"] if r["horizon"] == 60), None)
    if row:
        n_eps = row["n"]
        lines.append(f"🕰 {b(f'3-Month Historical Outlook ({n_eps} episodes):')}"
                     + (" ⚠️ low sample" if row["low_sample"] else ""))
        lines.append(f"📈 Typical: {c(_p(row['typical']), row['typical'])} (all days {_p(row['base_typical'])}) | "
                     f"Higher {c(_p(row['win_rate'], False, 0), row['win_rate'], good_if=0.70)} (all days {_p(row['base_win_rate'], False, 0)})")
        lines.append(f"🛡️ Worst-case dip: {c(_p(row['worst_dip']), row['worst_dip'])} (all days {_p(row['base_worst_dip'])}) | "
                     f"Severe crash risk (-20%): {b(_p(row['crash_risk'], False, 0))} (all days {_p(row['base_crash_risk'], False, 0)})")
        take = vm["analogs"]["takeaway"]
        if take:
            lines += ["", take["text"]]
    else:
        lines.append("🕰 Not enough similar history for an outlook")
    text = "\n".join(lines)
    if not html_markup:
        from html import unescape
        text = unescape(re.sub(r"</?(b|code)>", "", text))
    return text


def to_json(rep: Report) -> str:
    return json.dumps(asdict(rep), indent=2, default=lambda o: None if (isinstance(o, float) and math.isnan(o)) else str(o))


def send_telegram(text: str, button_url: str | None = None, html: bool = True) -> None:
    """Post to Telegram.  `text` is HTML (<b>, <code>) by default; if Telegram rejects the markup the
    message is resent once as plain text rather than lost."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set to use --send")
    markup = None
    if button_url:
        markup = json.dumps({"inline_keyboard": [[{"text": "🔗 Open Full Market Regime Dashboard", "url": button_url}]]})

    def post(body_text: str, parse_html: bool) -> None:
        params = {"chat_id": chat, "text": body_text, "disable_web_page_preview": "true"}
        if parse_html:
            params["parse_mode"] = "HTML"
        if markup:
            params["reply_markup"] = markup
        with urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=urllib.parse.urlencode(params).encode()),
                timeout=20) as r:
            if r.status != 200:
                raise RuntimeError(f"telegram HTTP {r.status}")
    try:
        try:
            post(text, html)
        except urllib.error.HTTPError as exc:
            if exc.code != 400 or not html:
                raise
            from html import unescape
            post(unescape(re.sub(r"</?(b|code)>", "", text)), False)
    except Exception as exc:                       # never echo the URL: it contains the bot token
        raise RuntimeError(f"telegram send failed ({type(exc).__name__})") from None


# ============================================================================ daily job ==========
def expected_session(now: datetime | None = None) -> date:
    """The most recent weekday whose 16:30 New York close has passed: the bar that should be final now.
    Holidays are not modelled; a holiday simply produces no new bar (the job then stays quiet)."""
    now = (now or datetime.now(NY)).astimezone(NY)
    d = now.date()
    if now.time() < dtime(16, 30):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fetch_fresh(*, fetch: Callable[[str], pd.Series] | None = None, cache_dir: Path | None = None,
                now: datetime | None = None, attempts: int = 3, delays: tuple[float, ...] = (20.0, 60.0),
                sleep: Callable[[float], None] = time.sleep) -> tuple[dict[str, pd.Series], list[str], bool]:
    """Download everything, retrying until NDX and VIX both have the latest completed session.
    Returns (series, notes, fresh).  After the last attempt it returns the best data it has with
    fresh=False; it raises only when nothing at all could be loaded."""
    expected = pd.Timestamp(expected_session(now))
    best: tuple[dict[str, pd.Series], list[str]] | None = None
    last_exc: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            series, notes = load_series(fetch=fetch, cache_dir=cache_dir, refresh=True)
            best = (series, notes)
            downloaded = not any(n.startswith(("^NDX:", "^VIX:")) for n in notes)
            if downloaded and series["ndx"].index[-1] >= expected and series["vix"].index[-1] >= expected:
                return series, notes, True
        except RuntimeError as exc:
            last_exc = exc
        if i < attempts - 1:
            sleep(delays[min(i, len(delays) - 1)] if delays else 0)
    if best is None:
        raise last_exc or RuntimeError("no market data")
    return best[0], best[1], False


def _state_path(cache_dir: Path) -> Path:
    return cache_dir / "last_sent.json"


def read_last_sent(cache_dir: Path) -> str | None:
    try:
        return json.loads(_state_path(cache_dir).read_text()).get("asof")
    except (OSError, ValueError):
        return None


def write_last_sent(cache_dir: Path, asof: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _state_path(cache_dir).write_text(json.dumps({"asof": asof, "sent_at": datetime.now(NY).isoformat(timespec="seconds")}))


def daily(*, now: datetime | None = None, fetch: Callable[[str], pd.Series] | None = None,
          send: Callable[[str, str | None], None] | None = None, cache_dir: Path | None = None,
          attempts: int = 3, delays: tuple[float, ...] = (20.0, 60.0),
          sleep: Callable[[float], None] = time.sleep, force: bool = False) -> int:
    """The scheduled run: fresh data (with retries), one Telegram summary per new session, never twice.
    Runs in its own process/unit, so a failure here cannot touch the stop-loss job.  Exit code 0 also
    covers 'nothing new to say' (holiday); 1 means something went wrong and the failure unit pings."""
    cache_dir = cache_dir or CACHE_DIR
    send = send or send_telegram
    try:
        series, notes, fresh = fetch_fresh(fetch=fetch, cache_dir=cache_dir, now=now, attempts=attempts,
                                           delays=delays, sleep=sleep)
        rep = analyze(cut_asof(series, None, now=now), notes=notes)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    last = read_last_sent(cache_dir)
    if not force and last and rep.snapshot.date <= last:
        print(f"no new completed session since {last}: nothing to send"
              + ("" if fresh else " (fresh data could not be fetched)"))
        return 0
    text = render_telegram(rep, delayed=not fresh)
    for attempt in range(3):
        try:
            send(text, dashboard_url())
            break
        except RuntimeError as exc:
            if attempt == 2:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            sleep(5 * (attempt + 1))
    write_last_sent(cache_dir, rep.snapshot.date)
    print(re.sub(r"</?(b|code)>", "", text))
    return 0


# ================================================================================= CLI ===========
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Standalone Nasdaq-100 market-regime barometer (no trading advice).")
    ap.add_argument("--asof", help="analyse as of this date (YYYY-MM-DD); only data up to then is used")
    ap.add_argument("--telegram", action="store_true", help="print the concise Telegram version")
    ap.add_argument("--send", action="store_true", help="post the concise version to Telegram")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--episodes", action="store_true", help="list each historical analog episode")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-download")
    ap.add_argument("--include-partial", action="store_true", help="keep today's bar even before the close")
    ap.add_argument("--daily", action="store_true",
                    help="scheduled run: fetch fresh data (retrying), send one summary per new session")
    ap.add_argument("--force", action="store_true", help="with --daily: send even if this session was already sent")
    ap.add_argument("--attempts", type=int, default=3, help="with --daily: download attempts before giving up on fresh data")
    ap.add_argument("--notify-failure", action="store_true", help="send a 'regime job failed' Telegram message")
    a = ap.parse_args(argv)
    if a.notify_failure:
        try:
            send_telegram("⚠️ The Nasdaq-100 market regime summary could not be produced today.\n"
                          "Check: journalctl -u risk-sizer-regime -n 50")
            return 0
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if a.daily:
        return daily(attempts=a.attempts, force=a.force)
    try:
        series, notes = load_series(refresh=a.refresh)
        series = cut_asof(series, a.asof, include_partial=a.include_partial)
        rep = analyze(series, notes)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if a.json:
        print(to_json(rep))
    elif a.telegram or a.send:
        print(render_telegram(rep, html_markup=False))
    else:
        print(render_text(rep, episodes=a.episodes))
    if a.send:
        try:
            send_telegram(render_telegram(rep), dashboard_url())
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
