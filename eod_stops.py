#!/usr/bin/env python3
"""End-of-day chart-based stops for Active positions.

Run once the morning after each US session.  A stop exists ONLY where the chart says so:
it is priced just under a proven support level, and nowhere else.

  Support sources (all must be proven by price action and volume):
    swing low      a confirmed swing low that price defended (two bounces, or one major swing)
    volume node    a high-volume price level (the volume-profile "VRVP" node) that price held
    flipped level  former resistance that price broke above, confirmed, and then held
  Which level:
    The stop goes under the NEAREST proven support that sits at least a "cushion" below the
    price, the cushion being 1.5-2 normal trading days of movement (true volatility: the
    largest of ATR, the last 5 days' range and a high-low estimate).  Trend, momentum and
    sector weakness nudge the cushion by a quarter day, which only changes WHICH level is used.
  Never a distance rule:
    No qualified support within reach means NO STOP.  The position is flagged "No chart
    support below, manual decision required" and left to the owner (see the hold-on-purpose
    records); the tool never invents a number.
  Ratchet:
    A chart stop only rises, when a higher proven support forms.  If the price closes through
    it, that is a BREACH: the support broke.

Prices come with auto_adjust=False (no dividend adjustment) because broker stops are entered in
real prices.  Yahoo still rewrites history for SPLITS, so a split since the position was opened
makes every stored price stale: those positions are SKIPped with an explicit message.

Schedule: the job runs the morning after each session (see deploy/risk-sizer-eod.timer).
Nothing is persisted until Telegram accepted every message, so a failed send is retried by
simply running the job again.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ModuleNotFoundError:  # pure calculations/tests inject their own downloader
    yf = None

ROOT = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")

# --------------------------------------------------------------------------- rules
RULES: dict[str, float] = {
    "min_bars": 120,            # need this much history or the position is skipped
    "pivot_k": 3,               # bars each side that confirm a swing low
    "sr_lookback": 140,         # bars scanned for swing lows (~7 months): older lows are stale
    "zone_merge_atr": 0.5,      # swing lows closer than this form one zone
    "pivot_reaction_atr": 1.5,  # a swing low only counts if price bounced this far (ATR) within ...
    "pivot_reaction_bars": 12,  # ... this many bars: filters wiggles that never attracted buyers
    "recent_floor_bars": 60,    # when >=2 separate touches are this recent, they alone define the floor
    "min_touch_gap": 5,         # bars between two touches of a zone for them to count as two
    "min_zone_touches": 2,      # a zone needs this many separate bounces ...
    "major_bounce_atr": 4.0,    # ... or ONE bounce this big (a major swing low) ...
    "node_bounce_atr": 2.5,     # ... or one bounce this big that also sits on a volume node
    "support_max_distance_atr": 10,    # support further than this below the close is not "nearby"
    "support_clearance_atr": 1.0,   # a zone must sit this far below the close
    "stop_buffer_atr": 0.3,     # stop goes this far under the zone low ...
    "stop_buffer_pct": 0.5,     # ... or this % of it, whichever is larger
    "vp_lookback": 120,
    "vp_bin_atr": 0.25,
    "hvn_ratio": 1.5,           # bin volume / median bin volume to be a node
    "adx_trend": 25,
    "adx_range": 20,
    "adx_peak": 30,
    "dist_vol_ratio": 1.25,     # distribution day: down close on this x 20d volume
    "dist_days_trigger": 3,     # ... at least this many in the last 10 bars
    "break_vol_ratio": 1.5,     # support break only counts on this x 20d volume
    "rsi_hot": 75,
    "rsi_cool": 70,
    "rsi_oversold": 30,
    "exhaustion_min_gain_atr": 2.0,  # exhaustion only matters once well in profit
    "cushion_trend": 1.5,       # a stop must sit this many "normal days" under the price in a clean trend ...
    "cushion_chop": 2.0,        # ... and this many when ADX says range or downtrend
    "cushion_context": 0.25,    # sector/momentum shift the cushion by this much: it only picks WHICH level
    "cushion_min": 1.25,
    "flip_break_atr": 0.25,     # former resistance counts once two closes clear it by this much (ATR)
    "flip_lost_atr": 0.5,       # ... and stops counting if price closes back under it by this much
    "min_move_atr": 0.5,        # a raise must clear BOTH this many "normal days" ...
    "min_move_pct": 1.5,        # ... and this % of the current stop, else it is HOLD
    "no_support_reminder_days": 7,   # a "no chart support" flag repeats at most this often
}

CHART_SOURCES = ("CHART", "STRUCTURAL", "MANUAL")   # stop records that are anchored to a chart level or the user

LOOSE, NORMAL, TIGHT = 0, 1, 2
TIER_NAME = {LOOSE: "LOOSE", NORMAL: "NORMAL", TIGHT: "TIGHT"}

SECTOR_ETF = {
    "Technology": "XLK", "Financial Services": "XLF", "Healthcare": "XLV",
    "Consumer Cyclical": "XLY", "Consumer Defensive": "XLP", "Energy": "XLE",
    "Industrials": "XLI", "Utilities": "XLU", "Basic Materials": "XLB",
    "Real Estate": "XLRE", "Communication Services": "XLC",
}
MARKET = "SPY"


# ----------------------------------------------------------------------- indicators
def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["Close"].shift(1)
    return pd.concat(
        [df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _wilder(true_range(df), n)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = _wilder(d.clip(lower=0), n), _wilder((-d).clip(lower=0), n)
    out = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    # No down moves in the window is RSI 100 (and no moves at all is neutral), not "missing":
    # a NaN here silently switched off every momentum test for the strongest uptrends.
    warm = up.notna() & dn.notna()
    out = out.where(~(warm & (dn == 0)), np.where(up > 0, 100.0, 50.0))
    return out


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["High"].diff()
    dn = -df["Low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = _wilder(true_range(df), n)
    pdi, mdi = 100 * _wilder(plus, n) / tr, 100 * _wilder(minus, n) / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"adx": _wilder(dx.fillna(0), n), "pdi": pdi, "mdi": mdi})


# ------------------------------------------------------------- structure: S/R + volume
@dataclass
class Zone:
    lo: float
    hi: float
    touches: int
    hvn: bool = False
    reaction: float = 0.0     # biggest bounce off this zone, in ATR
    kinds: tuple[str, ...] = ("swing",)   # swing | volume | flip
    proven: bool = False      # volume nodes and flipped levels are proven by construction

    @property
    def qualified(self) -> bool:
        """A real floor: bounced at least twice (a fresh retest that is holding counts), or
        once with a major swing (4+ ATR), or once with a decent swing on a volume node.
        Anything weaker is a wiggle, not support, and must never anchor a stop."""
        return (self.proven
                or self.touches >= RULES["min_zone_touches"]
                or self.reaction >= RULES["major_bounce_atr"]
                or (self.hvn and self.reaction >= RULES["node_bounce_atr"]))


def volume_profile(df: pd.DataFrame, atr_now: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Volume-at-price over the lookback.  Each bar's volume is spread over the price
    bins its high-low range covers.  Returns (high-volume-node prices, bin centres, POC)."""
    d = df.tail(int(RULES["vp_lookback"]))
    lo, hi = float(d["Low"].min()), float(d["High"].max())
    width = max(RULES["vp_bin_atr"] * atr_now, (hi - lo) / 200, 1e-6)
    edges = np.arange(lo, hi + width, width)
    vol = np.zeros(len(edges) - 1)
    for bar_lo, bar_hi, v in zip(d["Low"], d["High"], d["Volume"]):
        a = min(max(int((bar_lo - lo) // width), 0), len(vol) - 1)
        b = min(max(int((bar_hi - lo) // width), 0), len(vol) - 1)
        vol[a:b + 1] += v / (b - a + 1)
    centres = (edges[:-1] + edges[1:]) / 2
    nonzero = vol[vol > 0]
    floor = RULES["hvn_ratio"] * (np.median(nonzero) if len(nonzero) else 0)
    hvn = [
        centres[i] for i in range(len(vol))
        if vol[i] >= floor > 0 and vol[i] == vol[max(0, i - 2):i + 3].max()
    ]
    return np.array(hvn), centres, float(centres[int(vol.argmax())])


def support_zones(df: pd.DataFrame, atr_now: float, hvn: np.ndarray) -> list[Zone]:
    """Cluster confirmed swing lows into zones.  A swing low needs pivot_k lower-or-equal
    bars on each side, so the newest candidate is pivot_k bars old: nothing repaints.  It
    must also have been *defended*: price rallied at least pivot_reaction_atr within
    pivot_reaction_bars, otherwise it is a quiet wiggle and is ignored.  Two lows of one
    zone only count as separate touches when min_touch_gap bars apart."""
    k = int(RULES["pivot_k"])
    d = df.tail(int(RULES["sr_lookback"]))
    low, high = d["Low"].to_numpy(), d["High"].to_numpy()
    pivots: list[list[float]] = []                   # [price, bar index, reaction in ATR]
    last_i = -10**9
    for i in range(k, len(low) - k):
        if low[i] == low[i - k:i + k + 1].min():
            react = float(high[i + 1:i + 1 + int(RULES["pivot_reaction_bars"])].max() - low[i]) / atr_now \
                if atr_now > 0 else 0.0
            if i - last_i <= k and pivots:           # plateau: keep the lower low only
                if low[i] < pivots[-1][0]:
                    pivots[-1][0] = float(low[i])
                pivots[-1][2] = max(pivots[-1][2], react)
            else:
                pivots.append([float(low[i]), i, react])
            last_i = i
    pivots = [p for p in pivots if p[2] >= RULES["pivot_reaction_atr"]]
    zones: list[Zone] = []
    bars_of: list[list[int]] = []
    prices_of: list[list[float]] = []
    for price, idx, react in sorted(pivots):
        if zones and price - zones[-1].lo <= RULES["zone_merge_atr"] * atr_now:
            zones[-1].hi, zones[-1].reaction = price, max(zones[-1].reaction, react)
            bars_of[-1].append(int(idx))
            prices_of[-1].append(price)
        else:
            zones.append(Zone(price, price, 1, reaction=react))
            bars_of.append([int(idx)])
            prices_of.append([price])
    cutoff = len(low) - int(RULES["recent_floor_bars"])
    for z, idxs, prices in zip(zones, bars_of, prices_of):   # separate bounces only, not one wiggle counted twice
        counted, last = 0, -10**9
        for i in sorted(idxs):
            if i - last >= RULES["min_touch_gap"]:
                counted, last = counted + 1, i
        z.touches = counted
        # A floor that has been defended again and again RECENTLY is defined by those recent lows;
        # one old, deeper wick (months ago) must not drag the stop far below where buyers keep stepping in.
        recent = sorted((p, i) for p, i in zip(prices, idxs) if i >= cutoff)
        if len({i // int(RULES["min_touch_gap"]) for _, i in recent}) >= 2:
            z.lo, z.hi = recent[0][0], recent[-1][0]
    # A fresh retest cannot be a confirmed swing low yet (needs pivot_k bars after it), but a floor
    # the last few bars came back down to and held IS evidence: count it as a touch.
    recent_low = float(low[-(k + 1):].min())
    newest = len(low) - 1
    for z, idxs in zip(zones, bars_of):
        if (newest - max(idxs) >= RULES["min_touch_gap"] and recent_low >= z.lo - 0.1 * atr_now
                and recent_low <= z.hi + RULES["zone_merge_atr"] * atr_now):
            z.touches += 1
    pad = RULES["zone_merge_atr"] * atr_now
    for z in zones:
        z.hvn = bool(len(hvn) and ((hvn >= z.lo - pad) & (hvn <= z.hi + pad)).any())
    return zones


def true_range_size(df: pd.DataFrame, atr14: float) -> float:
    """What a normal trading day looks like right now, in dollars: the largest of Wilder ATR,
    the mean true range of the last 5 sessions (a fresh shock counts at once) and a Parkinson
    high-low volatility estimate over 20 sessions scaled to range units."""
    recent = float(true_range(df).tail(5).mean())
    hl = np.log(df["High"] / df["Low"]).tail(20)
    park = float(np.sqrt((hl ** 2).mean() / (4 * np.log(2)))) * float(df["Close"].iloc[-1]) * 1.6
    vals = [v for v in (atr14, recent, park) if np.isfinite(v) and v > 0]
    return max(vals) if vals else atr14


def _episodes(idxs: list[int], gap: int) -> list[int]:
    out: list[int] = []
    for i in sorted(idxs):
        if not out or i - out[-1] >= gap:
            out.append(i)
    return out


def _core_low(hit_idxs: list[int], lows: np.ndarray, n_bars: int, fallback: float) -> float:
    """Lowest low of the floor's defences, using only the RECENT ones when it has been defended at
    least twice recently (so one old deeper wick does not drag the stop far below where buyers step in)."""
    recent = [i for i in hit_idxs if i >= n_bars - int(RULES["recent_floor_bars"])]
    use = recent if len(_episodes(recent, int(RULES["min_touch_gap"]))) >= 2 else hit_idxs
    return float(min(lows[i] for i in use)) if use else fallback


def node_supports(df: pd.DataFrame, atr_now: float, hvn: np.ndarray, close: float) -> list[Zone]:
    """High-volume price levels that price has HELD: at least two separate visits (>= min_touch_gap
    bars apart) where the low dipped into the node's band and the day closed back above it, each
    followed by a real bounce.  A volume node nobody defended is just a busy price, not support."""
    d = df.tail(int(RULES["sr_lookback"])).reset_index(drop=True)
    low, high, cl = d["Low"].to_numpy(), d["High"].to_numpy(), d["Close"].to_numpy()
    zones: list[Zone] = []
    for n in hvn:
        if n >= close or atr_now <= 0:
            continue
        hits = [i for i in range(len(d) - 1)
                if n - 0.75 * atr_now <= low[i] <= n + 0.25 * atr_now and cl[i] > n
                and (high[i + 1:i + 1 + int(RULES["pivot_reaction_bars"])].max() - low[i]) / atr_now
                >= RULES["pivot_reaction_atr"]]
        visits = _episodes(hits, int(RULES["min_touch_gap"]))
        if len(visits) >= 2:
            react = max((high[i + 1:i + 1 + int(RULES["pivot_reaction_bars"])].max() - low[i]) / atr_now
                        for i in visits)
            zones.append(Zone(_core_low(hits, low, len(d), float(n)), float(n), len(visits), True,
                              float(react), ("volume",), True))
    return zones


def flip_supports(df: pd.DataFrame, atr_now: float, hvn: np.ndarray, close: float) -> list[Zone]:
    """Former resistance turned support.  A swing high that price was rejected from (fell at least
    pivot_reaction_atr within pivot_reaction_bars), later cleared by TWO closes at least flip_break_atr
    above it, never lost since (no close back under it by flip_lost_atr), and PROVEN as support by
    a retest that held (low dipped to it, close back above) or by sitting on a volume node."""
    k = int(RULES["pivot_k"])
    d = df.tail(int(RULES["sr_lookback"])).reset_index(drop=True)
    low, high, cl = d["Low"].to_numpy(), d["High"].to_numpy(), d["Close"].to_numpy()
    out: list[Zone] = []
    for i in range(k, len(d) - k):
        if high[i] != high[i - k:i + k + 1].max():
            continue
        level = float(high[i])
        if level >= close or atr_now <= 0:
            continue
        reject = (level - low[i + 1:i + 1 + int(RULES["pivot_reaction_bars"])].min()) / atr_now
        if reject < RULES["pivot_reaction_atr"]:
            continue
        brk = next((j for j in range(i + k, len(d) - 1)
                    if cl[j] > level + RULES["flip_break_atr"] * atr_now
                    and cl[j + 1] > level + RULES["flip_break_atr"] * atr_now), None)
        if brk is None:
            continue
        after = cl[brk:]
        if (after < level - RULES["flip_lost_atr"] * atr_now).any():
            continue                                    # the breakout failed: no longer support
        holds = [m for m in range(brk + 2, len(d))
                 if low[m] <= level + 0.5 * atr_now and low[m] >= level - 0.75 * atr_now and cl[m] > level]
        on_node = bool(len(hvn) and ((hvn >= level - 0.5 * atr_now) & (hvn <= level + 0.5 * atr_now)).any())
        visits = _episodes(holds, int(RULES["min_touch_gap"]))
        if not visits and not on_node:
            continue
        lo = _core_low(holds, low, len(d), level - 0.5 * atr_now) if holds else level - 0.5 * atr_now
        out.append(Zone(min(lo, level), level, max(1, len(visits)), on_node, float(reject), ("flip",), True))
    return out


def find_supports(df: pd.DataFrame, atr_now: float, hvn: np.ndarray, close: float | None = None) -> list[Zone]:
    """Every PROVEN support, newest structure first by price: swing-low zones, held volume nodes and
    flipped resistance, with overlapping ones merged into one zone that remembers all its kinds."""
    close = float(df["Close"].iloc[-1]) if close is None else close
    cands = [z for z in support_zones(df, atr_now, hvn) if z.qualified]
    for z in cands:
        z.kinds = ("swing", "volume") if z.hvn else ("swing",)
    cands += node_supports(df, atr_now, hvn, close) + flip_supports(df, atr_now, hvn, close)
    pad = RULES["zone_merge_atr"] * atr_now
    merged: list[Zone] = []
    for z in sorted(cands, key=lambda z: z.hi, reverse=True):
        for m in merged:
            if z.hi >= m.lo - pad and z.lo <= m.hi + pad:
                m.lo, m.hi = min(m.lo, z.lo), max(m.hi, z.hi)
                m.touches += z.touches
                m.reaction = max(m.reaction, z.reaction)
                m.hvn = m.hvn or z.hvn or "volume" in z.kinds
                m.proven = m.proven or z.proven
                m.kinds = tuple(dict.fromkeys(m.kinds + z.kinds))
                break
        else:
            merged.append(z)
    return sorted(merged, key=lambda z: z.hi, reverse=True)


# ----------------------------------------------------------------------- the decision
@dataclass
class Context:
    """Sector/market inputs for one stock; None means 'no data, ignore'."""
    etf: str | None = None
    weak: bool = False
    note: str = ""
    stock_vs_sector_10d: float | None = None


@dataclass
class Decision:
    ticker: str
    close: float
    prev_stop: float
    new_stop: float
    tier: int
    action: str                      # ENTRY | RAISE | HOLD | BREACH | NO_SUPPORT | SKIP
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    notice: str | None = None        # one-off heads-up shown in the alert
    notice_kind: str | None = None   # notices of a kind share ONE alert line
    source: str | None = None        # stored as the stop's source: CHART | NONE (no chart support)
    position_id: int | None = None   # lets the alert attach a per-position button

    @property
    def moved(self) -> bool:
        return self.action == "RAISE"


def sector_context(sector: pd.DataFrame | None, etf: str | None, stock: pd.DataFrame) -> Context:
    """Sector is 'weak' on a confirmed multi-day downtrend: close under a falling SMA20 and
    at least 3 down closes in the last 5.  Both conditions - one red day or one dip below
    a flat average is routine noise."""
    if sector is None or len(sector) < 30:
        return Context()
    c = sector["Close"]
    sma = c.rolling(20).mean()
    down_closes = int((c.diff().tail(5) < 0).sum())
    weak = bool(c.iloc[-1] < sma.iloc[-1] and sma.iloc[-1] < sma.iloc[-4] and down_closes >= 3)
    rs = None
    if len(stock) > 11:
        rs = float(stock["Close"].iloc[-1] / stock["Close"].iloc[-11] - c.iloc[-1] / c.iloc[-11])
    note = f"{etf} below falling 20d avg with {down_closes}/5 down closes" if weak else ""
    return Context(etf=etf, weak=weak, note=note, stock_vs_sector_10d=rs)


def _p(x: float) -> str:
    return f"${x:,.2f}" if x >= 1 else f"${x:,.4f}"


def _d(iso: str) -> str:
    return datetime.fromisoformat(str(iso)[:10]).strftime("%b %-d")


def _bounced(z: Zone) -> str:
    return f"bounced {z.touches} times" if z.touches >= 2 else "bounced sharply"


def _span(z: Zone) -> str:
    return _p(z.lo) if abs(z.hi - z.lo) < 1e-9 else f"{_p(z.lo)}-{_p(z.hi)}"


def breach_reason(low: float, day: str, stop: float) -> str:
    return (f"On {_d(day)} it traded down to {_p(low)}, at or below your stop of {_p(stop)}. "
            f"Check whether your broker already sold it.")


def is_excluded(ticker: str) -> bool:
    """Tel Aviv (.TA) tickers are outside this engine: Yahoo quotes them in agorot, so entry
    prices, ATR and stops are not comparable with the shekel figures kept in the database."""
    return ticker.strip().upper().endswith(".TA")


def stop_under(zone: Zone, atr_now: float, price: float) -> float:
    """THE one place a support-based stop is priced: strictly below the zone's lowest low, by at
    least max(0.3 ATR, 0.5%) and never less than one tick, so ordinary noise at the floor cannot
    trigger it and it only fires if the floor genuinely breaks."""
    tick = _tick(price)
    buffer = max(RULES["stop_buffer_atr"] * atr_now, RULES["stop_buffer_pct"] / 100 * zone.lo)
    return _floor_tick(min(zone.lo - buffer, zone.lo - tick), tick)


def _tick(price: float) -> float:
    return 0.01 if price >= 1 else 0.0001


def _round_tick(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 4)


def _floor_tick(x: float, tick: float) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 4)


def last_session_date(now: datetime | None = None) -> date:
    """Date of the most recent weekday whose 16:00 New York close has already passed.
    Holidays are not modelled: a holiday has no bar, so an earlier 'since' date costs nothing."""
    now = (now or datetime.now(NY)).astimezone(NY)
    d = now.date()
    if now.time() < dtime(16, 0):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def split_since(df: pd.DataFrame, start: pd.Timestamp | None) -> tuple[str, float] | None:
    """First split on/after `start`.  Uses Yahoo's 'Stock Splits' column; only when that column
    is absent does it fall back to a >2x one-day close jump (a real crash must never be
    mistaken for a split, because a split SKIPs the position and hides a breach)."""
    window = df if start is None else df[df.index >= start]
    if "Stock Splits" in df.columns:
        hit = window["Stock Splits"].fillna(0)
        hit = hit[hit > 0]
        return (hit.index[0].date().isoformat(), float(hit.iloc[0])) if len(hit) else None
    r = (df["Close"] / df["Close"].shift(1)).loc[window.index]
    bad = r[(r < 0.5) | (r > 2.0)]
    return (bad.index[0].date().isoformat(), float(bad.iloc[0])) if len(bad) else None


def _breach(df: pd.DataFrame, prev_stop: float, since: pd.Timestamp | None) -> tuple[float, str] | None:
    """Lowest low on every bar the recorded stop was in force (after `since`; just the last bar
    when nothing is known), so a session missed by a failed run still reports its breach."""
    window = df.tail(1) if since is None else df[df.index > since]
    if window.empty:
        return None
    low = float(window["Low"].min())
    return (low, window["Low"].idxmin().date().isoformat()) if low <= prev_stop else None


def _skip(ticker: str, df: pd.DataFrame, prev_stop: float, reason: str) -> Decision:
    close = float(df["Close"].iloc[-1]) if len(df) else 0.0
    return Decision(ticker, close, prev_stop, prev_stop, NORMAL, "SKIP", [reason])


def anchor_phrase(z: Zone) -> str:
    """Plain-language description of the chart level a stop sits under."""
    kinds = set(z.kinds)
    zone = _span(z)
    if "flip" in kinds:
        return (f"just under former resistance that price broke above and has since held (the zone around {zone})"
                f"{', where lots of shares also changed hands' if 'volume' in kinds or z.hvn else ''}")
    if kinds == {"volume"}:
        return (f"just under a heavy-volume price level around {zone}, where lots of shares changed hands "
                f"and buyers kept stepping in")
    return (f"just under a support level around {zone}, a price where it has {_bounced(z)}"
            f"{' and where lots of shares changed hands' if z.hvn else ''}")


def no_support_reason(df: pd.DataFrame, supports: list[Zone], close: float, vol: float, gap: float) -> str:
    below = [z for z in supports if z.hi < close]
    if below:
        nearest = max(below, key=lambda z: z.hi)
        if close - nearest.hi < gap:
            return (f"The nearest proven support ({_p(nearest.lo)}) is too close: a stop under it would sit "
                    f"inside normal daily swings.")
        return (f"The nearest proven support ({_p(nearest.lo)}) is more than "
                f"{int(RULES['support_max_distance_atr'])} days of normal swings below the price, too far to use.")
    low_window = float(df["Low"].tail(int(RULES["sr_lookback"])).min())
    if close <= low_window * 1.10:
        return ("The price is at or near its lowest level in about 7 months, so there is no proven "
                "support underneath to anchor a stop to.")
    return "There is no proven support level underneath the price."


NO_SUPPORT_SENTENCE = "No chart support below, manual decision required."


def decide(
    ticker: str,
    df: pd.DataFrame,
    pos: Mapping[str, Any],
    prev_stop: float,
    settings: Mapping[str, float],
    ctx: Context | None = None,
    *,
    since: pd.Timestamp | None = None,
    asof: pd.Timestamp | None = None,
) -> Decision:
    """One decision per position.  `prev_stop` is the chart stop on record (0 = none).
    `since`: date of the last review (bars after it are checked for a breach).
    `asof`: newest session date across the download; a ticker behind it is stale."""
    ctx = ctx or Context()
    if len(df) == 0:
        return _skip(ticker, df, prev_stop, "Couldn't get price data for it today.")
    if asof is not None and df.index[-1] < asof:
        return _skip(ticker, df, prev_stop,
                     f"Its latest price is from {_d(df.index[-1].date().isoformat())}, older than the rest of the "
                     f"market, so I skipped it.")
    sp = split_since(df, pd.Timestamp(str(pos["opened_at"])[:10]))
    if sp:
        return _skip(ticker, df, prev_stop,
                     f"It had a stock split on {_d(sp[0])}, so your saved prices no longer match. "
                     f"Update the position and set the stop again.")
    if not float(df["Volume"].iloc[-1]) > 0:
        return _skip(ticker, df, prev_stop, "Today's trading data isn't complete yet, so I skipped it.")

    close = float(df["Close"].iloc[-1])
    hit = _breach(df, prev_stop, since) if prev_stop > 0 else None
    if hit:
        return Decision(ticker, close, prev_stop, prev_stop, NORMAL, "BREACH",
                        [breach_reason(hit[0], hit[1], prev_stop)], {}, source="CHART")
    if len(df) < RULES["min_bars"]:
        why = [f"Too new to read chart levels (under {int(RULES['min_bars'])} days of history), so there is "
               f"no chart support to anchor a stop to."]
        if prev_stop > 0:
            return Decision(ticker, close, prev_stop, prev_stop, NORMAL, "HOLD", why, {}, source="CHART")
        return Decision(ticker, close, 0.0, 0.0, NORMAL, "NO_SUPPORT", why + [NO_SUPPORT_SENTENCE], {}, source="NONE")

    a = float(atr(df).iloc[-1])
    vol = true_range_size(df, a)
    rs_series = rsi(df["Close"])
    rsi_now = float(rs_series.iloc[-1])
    ax = adx(df)
    adx_now, pdi, mdi = (float(ax[c].iloc[-1]) for c in ("adx", "pdi", "mdi"))
    adx_recent_max = float(ax["adx"].tail(10).max())
    ema20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
    avg_vol = float(df["Volume"].iloc[-21:-1].mean())
    vol_ratio = float(df["Volume"].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0
    entry = float(pos["entry_price"])

    hvn, _, poc = volume_profile(df, a)
    supports = find_supports(df, a, hvn, close)
    below = [z for z in supports if z.hi < close]

    # --- context: trend, momentum, sector.  They only shift the cushion, i.e. WHICH level is used ----
    reasons: list[str] = []
    score = 0
    trend_strong = adx_now >= RULES["adx_trend"] and pdi > mdi
    downtrend = adx_now >= RULES["adx_range"] and mdi > pdi
    peaked = (adx_recent_max >= RULES["adx_peak"] and adx_now < adx_recent_max - 3
              and ax["adx"].iloc[-1] < ax["adx"].iloc[-4])
    if downtrend:
        score += 2
        reasons.append("It's in a clear downtrend, so I picked the nearest solid support.")
    if peaked:
        score += 1
        reasons.append("The uptrend is losing steam, so I picked a nearer support.")
    gain_atr = (close - entry) / a
    hot_recently = float(rs_series.tail(5).max()) >= RULES["rsi_hot"]
    cooled = rsi_now < RULES["rsi_cool"]
    div = (close >= 0.98 * float(df["High"].tail(20).max())
           and float(rs_series.tail(5).max()) < float(rs_series.tail(20).max()) - 5
           and float(rs_series.tail(20).max()) > 65)
    if gain_atr >= RULES["exhaustion_min_gain_atr"] and ((hot_recently and cooled) or div):
        score += 1
        reasons.append("It ran up a lot and the buying push is fading, so I picked a nearer support.")
    last10 = df.tail(10)
    dist_days = int(((last10["Close"] < df["Close"].shift(1).tail(10))
                     & (last10["Volume"] > RULES["dist_vol_ratio"] * avg_vol)).sum()) if avg_vol > 0 else 0
    if dist_days >= RULES["dist_days_trigger"]:
        score += 1
        reasons.append(f"There were {dist_days} heavy-selling days in the last 10, so I picked a nearer support.")
    broken_zone = next((z for z in supports if z.lo > close and z.lo <= close * 1.03), None)
    confirmed_break = bool(broken_zone and vol_ratio >= RULES["break_vol_ratio"])
    if confirmed_break:
        score += 2
        reasons.append(f"It closed below a price floor near {_p(broken_zone.lo)} on heavy selling.")

    tier = LOOSE if (score == 0 and trend_strong) else NORMAL if score <= 1 else TIGHT
    near_support = bool(below and close - max(z.hi for z in below) <= 1.5 * vol)
    oversold_hold = rsi_now < RULES["rsi_oversold"] and near_support and not confirmed_break
    if oversold_hold:
        tier = min(tier, NORMAL)
        reasons.append("It's beaten down and sitting on support, so I did not tighten.")
    idio_strength = bool(close > ema20 and rsi_now > 50
                         and ctx.stock_vs_sector_10d is not None and ctx.stock_vs_sector_10d > 0.02)
    if ctx.weak and not oversold_hold and tier < TIGHT:
        if idio_strength:
            reasons.append(f"Its sector ({ctx.etf}) is weak, but this stock is holding up better, so no change.")
        else:
            tier += 1
            reasons.append(f"Its sector ({ctx.etf}) has been sliding for several days, so I picked a nearer support.")
    if tier == LOOSE and not reasons:
        reasons.append("Strong, steady uptrend, so it gets room: a deeper, roomier support.")

    k = RULES["cushion_trend"] if trend_strong else RULES["cushion_chop"]
    k = max(RULES["cushion_min"], k + {LOOSE: 1, NORMAL: 0, TIGHT: -1}[tier] * RULES["cushion_context"])
    gap = k * vol
    reach = RULES["support_max_distance_atr"] * vol

    # --- THE stop: under the nearest proven support that sits at least the cushion below the price ---
    chosen, chart_stop = None, None
    for z in sorted(below, key=lambda z: z.hi, reverse=True):
        st = stop_under(z, a, close)
        if close - st >= gap and z.hi >= close - reach:
            chosen, chart_stop = z, st
            break

    metrics = dict(atr=a, vol=vol, rsi=rsi_now, adx=adx_now, pdi=pdi, mdi=mdi, vol_ratio=vol_ratio,
                   poc=poc, dist_days=dist_days, cushion=k)
    if chart_stop is None:
        if prev_stop > 0:
            return Decision(ticker, close, prev_stop, prev_stop, tier, "HOLD",
                            ["No higher proven support has formed, so the stop stays where it is."], metrics,
                            source="CHART")
        why = no_support_reason(df, supports, close, vol, gap)
        return Decision(ticker, close, 0.0, 0.0, tier, "NO_SUPPORT", [why, NO_SUPPORT_SENTENCE], metrics,
                        source="NONE")

    anchor = anchor_phrase(chosen)
    if prev_stop <= 0:
        return Decision(ticker, close, 0.0, chart_stop, tier, "ENTRY",
                        [f"The stop sits {anchor}."] + reasons, metrics, source="CHART")
    new_stop = max(prev_stop, chart_stop)
    if new_stop - prev_stop >= max(RULES["min_move_atr"] * vol, RULES["min_move_pct"] / 100 * prev_stop):
        return Decision(ticker, close, prev_stop, new_stop, tier, "RAISE",
                        [f"The stop now sits {anchor}."] + reasons, metrics, source="CHART")
    return Decision(ticker, close, prev_stop, prev_stop, tier, "HOLD",
                    ["The chart stop is unchanged: no higher proven support has formed."] + reasons[:1],
                    metrics, source="CHART")


# ------------------------------------------------------------------- initial stop
def initial_stop(ticker: str, df: pd.DataFrame, entry: float, settings: Mapping[str, float]) -> dict[str, Any]:
    """Chart-based stop for a NEW position, from the same proven supports the daily job uses.

    Picks the nearest proven support whose stop sits at least the volatility cushion below entry
    (1.5 normal days in a clean uptrend, 2 when ADX shows range or downtrend).  `extended` is True
    when that support is more than settings['extendedpct'] below entry or more than
    support_max_distance_atr normal days away: the stock has run too far from structure, so any stop
    that respects structure makes a poor risk/reward entry.  Returns stop=None when no proven
    support exists - the caller must say so, never substitute a distance rule silently."""
    if len(df) < RULES["min_bars"]:
        why = "Not enough price history yet."
        return {"ticker": ticker, "stop": None, "no_support": True, "extended": False,
                "reasons": [why], "justification": why}
    a = float(atr(df).iloc[-1])
    vol = true_range_size(df, a)
    ax = adx(df)
    adx_now, pdi, mdi = (float(ax[c].iloc[-1]) for c in ("adx", "pdi", "mdi"))
    rsi_now = float(rsi(df["Close"]).iloc[-1])
    trend_strong = adx_now >= RULES["adx_trend"] and pdi > mdi
    downtrend = adx_now >= RULES["adx_range"] and mdi > pdi
    regime = "uptrend" if trend_strong else "downtrend" if downtrend else "range/weak trend"
    cushion = (RULES["cushion_trend"] if trend_strong else RULES["cushion_chop"]) * vol

    hvn, _, poc = volume_profile(df, a)
    last_close = float(df["Close"].iloc[-1])
    zones = [z for z in find_supports(df, a, hvn, last_close)
             if z.hi < min(entry, last_close)]      # a floor price already fell through is not support
    chosen = next((z for z in zones if entry - stop_under(z, a, entry) >= cushion), None)
    out: dict[str, Any] = {"ticker": ticker, "entry": entry, "atr": a, "vol": vol, "adx": adx_now, "rsi": rsi_now,
                           "regime": regime, "poc": poc, "last_close": last_close,
                           "asof": df.index[-1].date().isoformat()}
    ext_limit = float(settings.get("extendedpct", 22.5))
    if chosen is None:
        why = "No solid support level below this price."
        out.update(stop=None, no_support=True, extended=True, reasons=[why], justification=why)
        return out
    stop = stop_under(chosen, a, entry)
    assert stop < chosen.lo, "a support-based stop must sit below the support"
    support_gap = (entry - chosen.hi) / entry * 100
    mood = {"uptrend": "It's in a steady uptrend.", "downtrend": "It's been falling.",
            "range/weak trend": "It's moving sideways without a clear trend."}[regime]
    reasons = [f"The stop sits {anchor_phrase(chosen)}.", mood,
               f"The stop is about {(entry - stop) / entry * 100:.0f}% below your entry."]
    if downtrend:
        reasons.append("Because it is falling, that support may not hold.")
    out.update(stop=stop, no_support=False,
               extended=(support_gap > ext_limit or entry - chosen.hi > RULES["support_max_distance_atr"] * vol),
               support_lo=chosen.lo, support_hi=chosen.hi, touches=chosen.touches, hvn=chosen.hvn,
               kinds=list(chosen.kinds),
               support_gap_pct=support_gap, distance_pct=(entry - stop) / entry * 100,
               reasons=reasons, justification=" ".join(reasons))
    return out


def stop_info(db_path: str | Path) -> dict[int, dict[str, Any]]:
    """Live stop per position id: the single record the Positions tab and the Telegram job share.
    `stop` is None when the chart offers no support (source NONE): there is no stop to show.
    `stop_why` is the reason of the latest ENTRY/RAISE/NO_SUPPORT, i.e. why the stop sits where it does
    (or why there is none); `stop_last_action` is the latest review outcome (HOLD, BREACH ...).
    `held_*` describe the owner's latest "holding on purpose" decision."""
    conn = open_state(db_path)
    try:
        info: dict[int, dict[str, Any]] = {}
        for r in conn.execute("SELECT position_id, ticker, stop, tier, asof, updated_at FROM eod_stop_state"):
            if is_excluded(r["ticker"]):
                continue
            info[r["position_id"]] = {"stop": r["stop"] if r["stop"] and r["stop"] > 0 else None,
                                      "stop_source": r["tier"],
                                      "stop_asof": r["asof"], "stop_updated_at": r["updated_at"],
                                      "stop_why": None, "stop_last_action": None, "stop_prev": None,
                                      "held_since": None, "held_reason": None, "hold_count": 0}
        for r in conn.execute("""SELECT position_id, action, reasons, asof, prev_stop FROM eod_stop_log
                                 WHERE id IN (SELECT MAX(id) FROM eod_stop_log GROUP BY position_id)"""):
            if r["position_id"] in info:
                info[r["position_id"]]["stop_last_action"] = r["action"]
                info[r["position_id"]]["stop_prev"] = r["prev_stop"] if r["action"] == "RAISE" else None
                info[r["position_id"]]["stop_last_asof"] = r["asof"]
        for r in conn.execute("""SELECT position_id, reasons FROM eod_stop_log
                                 WHERE id IN (SELECT MAX(id) FROM eod_stop_log
                                              WHERE action IN ('ENTRY', 'RAISE', 'NO_SUPPORT')
                                              GROUP BY position_id)"""):
            if r["position_id"] in info:
                try:
                    info[r["position_id"]]["stop_why"] = " ".join(json.loads(r["reasons"] or "[]"))
                except ValueError:
                    pass
        for r in conn.execute("""SELECT position_id, decided_at, reason,
                                        (SELECT COUNT(*) FROM eod_holds h2 WHERE h2.position_id = h.position_id) AS n
                                 FROM eod_holds h WHERE id IN (SELECT MAX(id) FROM eod_holds GROUP BY position_id)"""):
            if r["position_id"] in info:
                info[r["position_id"]].update(held_since=r["decided_at"][:10], held_reason=r["reason"],
                                              hold_count=r["n"])
        return info
    finally:
        conn.close()


# ------------------------------------------------------------------------------ data
BAR_FINAL_AFTER = dtime(20, 0)   # NY: before this a bar dated today may still be forming/settling
OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def completed_bars(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Keep only finished, settled sessions.  Today's bar is dropped until 20:00 New York
    (Yahoo's same-day volume is provisional until well after the close); a run the next
    morning, the normal schedule, keeps the previous session's completed bar.  Missing volume
    becomes 0 so it cannot poison the volume profile; the decision step refuses a last bar
    whose volume is 0."""
    now = (now or datetime.now(NY)).astimezone(NY)
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    df.index = pd.DatetimeIndex(pd.to_datetime(df.index)).tz_localize(None)
    df["Volume"] = df["Volume"].fillna(0)
    if "Stock Splits" in df.columns:
        df["Stock Splits"] = df["Stock Splits"].fillna(0)
    if len(df) and df.index[-1].date() == now.date() and now.time() < BAR_FINAL_AFTER:
        df = df.iloc[:-1]
    return df


def download(symbols: list[str], downloader: Callable[..., pd.DataFrame] | None = None) -> dict[str, pd.DataFrame]:
    fetch = downloader or (yf.download if yf else None)
    if fetch is None:
        raise RuntimeError("yfinance is not installed")
    raw = fetch(symbols, period="2y", interval="1d", auto_adjust=False, actions=True,
                group_by="ticker", progress=False, threads=True)
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        try:
            sub = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
            cols = OHLCV + (["Stock Splits"] if "Stock Splits" in sub.columns else [])
            out[s] = completed_bars(sub[cols])
        except (KeyError, ValueError):
            continue
    return out


def sector_etf_for(ticker: str, cache_path: Path, overrides: Mapping[str, str]) -> str | None:
    if ticker in overrides:
        return overrides[ticker]
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if ticker not in cache:
        sector = None
        try:
            sector = yf.Ticker(ticker).info.get("sector") if yf else None
        except Exception:
            pass
        cache[ticker] = SECTOR_ETF.get(sector or "", "")
        try:                                  # a read-only data/ must not abort the job
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
            os.replace(tmp, cache_path)
        except OSError:
            pass
    return cache[ticker] or None


# ----------------------------------------------------------------------------- state
STATE_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS eod_stop_state (
        position_id INTEGER PRIMARY KEY,
        ticker TEXT NOT NULL,
        stop REAL NOT NULL,
        tier TEXT,
        asof TEXT NOT NULL,
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS eod_stop_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asof TEXT NOT NULL,
        position_id INTEGER NOT NULL,
        ticker TEXT NOT NULL,
        close REAL, prev_stop REAL, new_stop REAL,
        tier TEXT, action TEXT, reasons TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_eod_stop_log_position ON eod_stop_log(position_id, id)",
    "CREATE TABLE IF NOT EXISTS eod_run_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS eod_holds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id INTEGER NOT NULL,
        ticker TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        asof TEXT NOT NULL,
        reason TEXT NOT NULL,
        note TEXT,
        price REAL, breached_stop REAL, new_stop REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_eod_holds_position ON eod_holds(position_id, id)",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """One statement at a time (never executescript) so it is safe inside an open transaction."""
    for stmt in STATE_STATEMENTS:
        conn.execute(stmt)


def open_state(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    ensure_schema(conn)
    conn.commit()
    return conn


def load_state(conn: sqlite3.Connection, position_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT stop, tier, asof FROM eod_stop_state WHERE position_id = ?",
                        (position_id,)).fetchone()


def load_prev(conn: sqlite3.Connection, position_id: int) -> float | None:
    row = load_state(conn, position_id)
    return float(row["stop"]) if row else None


def last_run_asof(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM eod_run_state WHERE key = 'last_asof'").fetchone()
    return row["value"] if row else None


def save_state(conn: sqlite3.Connection, position_id: int, d: Decision, asof: str) -> None:
    now = datetime.now(NY).isoformat(timespec="seconds")
    tier = d.source or TIER_NAME[d.tier]
    conn.execute(
        """INSERT INTO eod_stop_state (position_id, ticker, stop, tier, asof, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(position_id) DO UPDATE SET stop=excluded.stop, tier=excluded.tier,
               asof=excluded.asof, updated_at=excluded.updated_at""",
        (position_id, d.ticker, d.new_stop, tier, asof, now))
    conn.execute(
        """INSERT INTO eod_stop_log (asof, position_id, ticker, close, prev_stop, new_stop, tier, action, reasons)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (asof, position_id, d.ticker, d.close, d.prev_stop, d.new_stop, tier,
         d.action, json.dumps(d.reasons)))


def seed_stop_conn(conn: sqlite3.Connection, position_id: int, ticker: str, stop: float, source: str,
                   note: str | None = None, now: datetime | None = None) -> None:
    """Record the stop chosen at entry as the 'previous stop' for the daily job, with the
    reason it was chosen, so the Positions tab and Telegram read the same record.  Runs on the
    caller's connection so it joins the caller's transaction (no second writer, no lock).
    `asof` is the last completed session: the stop is in force from the next one."""
    if is_excluded(ticker):
        return
    now = (now or datetime.now(NY)).astimezone(NY)
    asof = last_session_date(now).isoformat()
    ensure_schema(conn)
    conn.execute(
        """INSERT INTO eod_stop_state (position_id, ticker, stop, tier, asof, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(position_id) DO UPDATE SET stop=excluded.stop, tier=excluded.tier,
               asof=excluded.asof, updated_at=excluded.updated_at""",
        (position_id, ticker, float(stop), source, asof, now.isoformat(timespec="seconds")))
    conn.execute(
        """INSERT INTO eod_stop_log (asof, position_id, ticker, new_stop, tier, action, reasons)
           VALUES (?, ?, ?, ?, ?, 'ENTRY', ?)""",
        (asof, position_id, ticker, float(stop), source, json.dumps([note or f"{source} stop chosen at entry"])))


def seed_stop(db_path: str | Path, position_id: int, ticker: str, stop: float, source: str,
              note: str | None = None) -> None:
    conn = open_state(db_path)
    try:
        with conn:
            seed_stop_conn(conn, position_id, ticker, stop, source, note)
    finally:
        conn.close()


def purge_position(conn: sqlite3.Connection, position_id: int) -> None:
    """Drop a closed/deleted position's stop record and history (same transaction as the delete)."""
    ensure_schema(conn)
    conn.execute("DELETE FROM eod_stop_state WHERE position_id = ?", (position_id,))
    conn.execute("DELETE FROM eod_stop_log WHERE position_id = ?", (position_id,))
    conn.execute("DELETE FROM eod_holds WHERE position_id = ?", (position_id,))


# ------------------------------------------------------------------- holding on purpose
HOLD_REASONS = {
    "earnings": "earnings are coming up",
    "thesis": "my reasons for owning it still hold",
    "liquidity": "I can't sell it right now",
    "other": "another reason",
}


def hold_preview(position: Mapping[str, Any], settings: Mapping[str, float],
                 downloader: Callable[..., pd.DataFrame] | None = None) -> dict[str, Any]:
    """What 'holding on purpose' would do, WITHOUT writing anything: today's price and the chart
    stop the engine would record from it (None when the chart has no support below)."""
    sym = position["ticker"]
    df = download([sym], downloader).get(sym)
    if df is None or df.empty:
        raise ValueError(f"no price history for {sym}")
    d = decide(sym, df, position, 0.0, settings, Context(), since=df.index[-1])
    if d.action == "SKIP":
        raise ValueError(" ".join(d.reasons))
    stop = d.new_stop if d.action == "ENTRY" else None
    return {"ticker": sym, "price": d.close, "new_stop": stop, "asof": df.index[-1].date().isoformat(),
            "reasons": d.reasons, "no_support": stop is None}


def hold_numbers(position: Mapping[str, Any], preview: Mapping[str, Any]) -> dict[str, float]:
    """Shekel cost of holding on: what could still be lost from today's price to the new stop (or the
    whole position when there is no stop), and what is already lost versus the entry price."""
    qty, fx = float(position["quantity"]), float(position.get("fx_to_ils") or 1)
    price, entry = float(preview["price"]), float(position["entry_price"])
    stop = preview["new_stop"]
    return {"extra_risk_ils": round((price - stop) * qty * fx, 2) if stop else round(price * qty * fx, 2),
            "loss_so_far_ils": round((entry - price) * qty * fx, 2),
            "position_value_ils": round(price * qty * fx, 2)}


def record_hold(db_path: str | Path, position: Mapping[str, Any], reason: str, note: str | None,
                preview: Mapping[str, Any], confirm_ticker: str | None = None,
                now: datetime | None = None) -> dict[str, Any]:
    """Record that the owner chose to keep the position although its stop was reached, and re-base
    the stop on the chart from today's price (or on nothing, if the chart has no support).  The one
    place a recorded stop may go DOWN.  A second hold on the same position must be confirmed by
    typing its ticker.  One transaction."""
    if reason not in HOLD_REASONS:
        raise ValueError("reason must be one of: " + ", ".join(HOLD_REASONS))
    ticker, pid = position["ticker"], position["id"]
    now = (now or datetime.now(NY)).astimezone(NY)
    conn = open_state(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            prior = conn.execute("SELECT COUNT(*) AS n FROM eod_holds WHERE position_id = ?", (pid,)).fetchone()["n"]
            if prior >= 1 and (confirm_ticker or "").strip().upper() != ticker.upper():
                raise PermissionError(f"This position already has a hold decision. Type {ticker.upper()} to confirm another.")
            state = load_state(conn, pid)
            breached = float(state["stop"]) if state and state["stop"] else 0.0
            new_stop = preview.get("new_stop")
            asof = str(preview["asof"])
            conn.execute("""INSERT INTO eod_holds (position_id, ticker, decided_at, asof, reason, note, price,
                                                   breached_stop, new_stop) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (pid, ticker, now.isoformat(timespec="seconds"), asof, reason, note,
                          float(preview["price"]), breached, new_stop))
            tier = "CHART" if new_stop else "NONE"
            conn.execute("""INSERT INTO eod_stop_state (position_id, ticker, stop, tier, asof, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(position_id) DO UPDATE SET stop=excluded.stop, tier=excluded.tier,
                                asof=excluded.asof, updated_at=excluded.updated_at""",
                         (pid, ticker, float(new_stop or 0.0), tier, asof, now.isoformat(timespec="seconds")))
            why = [f"You chose to keep holding because {HOLD_REASONS[reason]}."]
            why.append(f"The new chart-based stop is {_p(new_stop)}." if new_stop
                       else "There is no chart support below, so there is no stop.")
            conn.execute("""INSERT INTO eod_stop_log (asof, position_id, ticker, close, prev_stop, new_stop, tier,
                                                      action, reasons) VALUES (?, ?, ?, ?, ?, ?, ?, 'OVERRIDE', ?)""",
                         (asof, pid, ticker, float(preview["price"]), breached, float(new_stop or 0.0), tier,
                          json.dumps(why)))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {"position_id": pid, "ticker": ticker, "new_stop": new_stop, "held_since": now.date().isoformat(),
            "hold_count": prior + 1}


# --------------------------------------------------------------------------- message
class AlertMessage(str):
    """A Telegram message that still behaves as a plain string (tests and callers that only read the
    text keep working) but also carries the inline-keyboard buttons to attach to it."""
    buttons: list[dict[str, str]]

    def __new__(cls, text: str, buttons: list[dict[str, str]] | None = None) -> "AlertMessage":
        obj = super().__new__(cls, text)
        obj.buttons = list(buttons or [])
        return obj


def hold_button(d: Decision) -> dict[str, str] | None:
    """'Holding on purpose' for a position whose stop was reached or that has no chart support.
    callback_data h:<position id> is handled by telegram_hold.py (the webhook)."""
    if d.position_id is None:
        return None
    return {"text": f"📌 Holding on purpose: {d.ticker}", "callback_data": f"h:{d.position_id}"}


def keyboard(buttons: list[dict[str, str]]) -> dict[str, Any] | None:
    """Telegram reply_markup: one button per row, plus a link to the Positions page when
    RISK_SIZER_PUBLIC_URL is set.  None when there is nothing to attach."""
    rows = [[b] for b in buttons]
    base = os.environ.get("RISK_SIZER_PUBLIC_URL", "").strip().rstrip("/")
    if rows and base.startswith("https://"):
        rows.append([{"text": "🔗 Open the Positions page", "url": f"{base}/#positions"}])
    return {"inline_keyboard": rows} if rows else None


def format_alert(decisions: list[Decision], asof: str, quiet_when_no_changes: bool = True,
                 holds: list[dict[str, Any]] | None = None) -> list[str]:
    """One consolidated Telegram HTML message holding only actionable items: breaches, stops to
    raise, new chart stops, and positions with no chart support (flagged for a manual decision).
    HOLD is never mentioned.  With nothing actionable the result is empty (silent) or a single
    quiet line, per quiet_when_no_changes.  Positions that could not be evaluated are appended as
    one footnote so a data failure is not mistaken for 'nothing to do'.  `holds` adds a one-line
    scorecard of the owner's "holding on purpose" decisions.  Split only if the message would
    exceed Telegram's 4096 limit."""
    breaches = [d for d in decisions if d.action == "BREACH"]
    raises = [d for d in decisions if d.action == "RAISE"]
    entries = [d for d in decisions if d.action == "ENTRY"]
    nosup = [d for d in decisions if d.action == "NO_SUPPORT"]
    skipped = [d for d in decisions if d.action == "SKIP"]
    if not (breaches or raises or entries or nosup or skipped):
        return [] if quiet_when_no_changes else [AlertMessage("✅ EOD Check: No stop adjustments required today.")]

    blocks: list[tuple[str, list[dict[str, str]]]] = []

    def add(text: str, *buttons: dict[str, str] | None) -> None:
        blocks.append((text, [b for b in buttons if b]))

    for d in breaches:
        add(f"🚨 <b>{html.escape(d.ticker)}</b>  {_p(d.close)}\n"
            f"Your chart-based stop of {_p(d.prev_stop)} was reached\n"
            f"Why: {html.escape(' '.join(d.reasons))}\n"
            f"Sell it, or tap \"Holding on purpose\" below if you are keeping it on purpose.", hold_button(d))
    for d in sorted(raises, key=lambda x: x.ticker):
        chg = (d.new_stop / d.prev_stop - 1) * 100
        add(f"🟢⬆️ <b>{html.escape(d.ticker)}</b>  {_p(d.close)}\n"
            f"Move your stop up: {_p(d.prev_stop)} → <b>{_p(d.new_stop)}</b> ({chg:+.1f}%)\n"
            f"Why: {html.escape(' '.join(d.reasons))}")
    for d in sorted(entries, key=lambda x: x.ticker):
        extra = f" ({html.escape(d.notice)})" if d.notice else ""
        add(f"🆕 <b>{html.escape(d.ticker)}</b>  {_p(d.close)}\n"
            f"Chart-based stop: <b>{_p(d.new_stop)}</b>{extra}\n"
            f"Why: {html.escape(' '.join(d.reasons))}")
    groups: dict[str, list[Decision]] = {}
    for d in sorted(nosup, key=lambda x: x.ticker):
        groups.setdefault(d.reasons[0], []).append(d)
    for cause, ds in groups.items():
        names = " · ".join(f"{html.escape(d.ticker)} {_p(d.close)}" for d in ds)
        add(f"🧭 <b>No chart support below, manual decision required</b>\n"
            f"{html.escape(cause)}\n{names}", *[hold_button(d) for d in ds])
    if skipped:
        add("⚠️ Skipped: " + html.escape("  ".join(
            f"{d.ticker}: {' '.join(d.reasons)}" for d in skipped)))
    if holds:
        def one(h: dict[str, Any]) -> str:
            then, now = h.get("price_then"), h["price_now"]
            move = f", {(now / then - 1) * 100:+.1f}%" if then else ""
            return f"{html.escape(h['ticker'])} since {_d(h['since'])} ({_p(then) if then else '?'} → {_p(now)}{move})"
        add("📌 Holding on purpose: " + " · ".join(one(h) for h in holds))

    parts = []
    if raises:
        parts.append(f"{len(raises)} stop{'s' if len(raises) != 1 else ''} to move up")
    if entries:
        parts.append(f"{len(entries)} new chart stop{'s' if len(entries) != 1 else ''}")
    if breaches:
        parts.append(f"<b>{len(breaches)} reached</b>")
    if nosup:
        parts.append(f"{len(nosup)} without chart support")
    head = f"<b>EOD stops · {html.escape(asof)}</b>\n" + (" · ".join(parts) if parts else "no changes")
    messages: list[AlertMessage] = []
    cur, cur_buttons = head, []
    for blk, btns in blocks:
        if len(cur) + len(blk) + 2 > 3900:
            messages.append(AlertMessage(cur, cur_buttons))
            cur, cur_buttons = "", []
        cur += ("\n\n" if cur else "") + blk
        cur_buttons += btns
    messages.append(AlertMessage(cur, cur_buttons))
    return messages


class TelegramError(RuntimeError):
    pass


def _retry_after(err: urllib.error.HTTPError) -> float:
    try:
        return float(err.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        return float(json.loads(err.read().decode()).get("parameters", {}).get("retry_after"))
    except Exception:
        return 5.0


def send_telegram(messages: list[str], token: str, chat_id: str, *, opener: Callable[..., Any] = urllib.request.urlopen,
                  sleep: Callable[[float], None] = time.sleep, attempts: int = 4) -> None:
    """Send every message or raise TelegramError.  Retries network errors and 5xx with
    exponential backoff, honours 429 Retry-After, and on a 400 (Telegram rejected the markup)
    resends that message once as plain text.  Errors are re-raised without the request URL,
    which contains the bot token."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def post(text: str, parse_mode: str | None, markup: dict[str, Any] | None = None) -> None:
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
        if parse_mode:
            params["parse_mode"] = parse_mode
        if markup:
            params["reply_markup"] = json.dumps(markup)
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode())
        with opener(req, timeout=20) as r:
            if r.status != 200:
                raise TelegramError(f"telegram HTTP {r.status}")

    for m in messages:
        mode: str | None = "HTML"
        text = m
        markup = keyboard(getattr(m, "buttons", []))
        for attempt in range(1, attempts + 1):
            try:
                post(text, mode, markup)
                break
            except urllib.error.HTTPError as err:
                if err.code == 400 and mode:
                    mode, text = None, html.unescape(re.sub(r"<[^>]+>", "", m))
                    continue                      # same attempt budget, plain text this time
                if err.code == 429:
                    wait = min(_retry_after(err), 120.0)
                elif err.code >= 500:
                    wait = 2.0 ** attempt
                else:
                    raise TelegramError(f"telegram HTTP {err.code}") from None
                failure = f"telegram HTTP {err.code}"
            except TelegramError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                wait, failure = 2.0 ** attempt, f"telegram unreachable ({type(err).__name__})"
            if attempt == attempts:
                raise TelegramError(f"{failure} after {attempts} attempts") from None
            sleep(wait)


# -------------------------------------------------------------------------- pipeline
@dataclass
class Evaluation:
    decisions: list[Decision]
    asof: str
    pending: list[tuple[int, Decision]] = field(default_factory=list)   # to persist after the send
    seeds: list[tuple[int, str, float]] = field(default_factory=list)   # --set-stop records
    advance_asof: bool = False
    note: str = ""
    holds: list[dict[str, Any]] = field(default_factory=list)           # "holding on purpose" scorecard


def evaluate(db_path: str | Path, *, overrides_stop: Mapping[str, float] | None = None, force: bool = False,
             dry_run: bool = False, downloader: Callable[..., pd.DataFrame] | None = None,
             sector_lookup: Callable[[str], str | None] | None = None,
             now: datetime | None = None) -> Evaluation:
    """Compute every decision without writing anything."""
    from database import Database

    now = (now or datetime.now(NY)).astimezone(NY)
    db = Database(db_path)
    settings = db.get_settings()["settings"]
    positions = [p for p in db.list_active() if not is_excluded(p["ticker"])]
    if not positions:
        return Evaluation([], now.date().isoformat(), note="no active positions")

    cache = ROOT / "data" / "sector_cache.json"
    ov_file = ROOT / "data" / "sector_overrides.json"
    overrides = json.loads(ov_file.read_text()) if ov_file.exists() else {}
    lookup = sector_lookup or (lambda t: sector_etf_for(t, cache, overrides))
    etfs = {p["ticker"]: lookup(p["ticker"]) for p in positions}
    symbols = sorted({p["ticker"] for p in positions} | {e for e in etfs.values() if e} | {MARKET})
    bars = download(symbols, downloader)
    last_bars = [b.index[-1] for b in bars.values() if len(b)]
    if not last_bars:
        raise RuntimeError("no market data downloaded")
    asof_ts = max(last_bars)
    asof = asof_ts.date().isoformat()

    forced_stops = dict(overrides_stop or {})
    conn = open_state(db_path)
    try:
        last = last_run_asof(conn)
        if last and asof <= last and not (force or forced_stops or dry_run):
            return Evaluation([], asof, note=f"no new completed session since {last} (market holiday?)")

        ev = Evaluation([], asof)
        for p in positions:
            t = p["ticker"]
            forced = forced_stops.get(t)
            if forced:
                ev.seeds.append((p["id"], t, float(forced)))
            df = bars.get(t)
            if df is None or df.empty:
                ev.decisions.append(Decision(t, 0.0, forced or 0.0, forced or 0.0, NORMAL, "SKIP",
                                             ["Couldn't get price data for it today."]))
                continue
            state = load_state(conn, p["id"])
            legacy = bool(state) and state["tier"] not in CHART_SOURCES + ("NONE",)
            if forced:
                prev, since = float(forced), pd.Timestamp(last_session_date(now))
            elif state and not legacy and state["tier"] != "NONE":
                prev, since = float(state["stop"]), pd.Timestamp(state["asof"])
            else:
                # No chart stop on record (new position, an earlier distance-based stop, or "no support"):
                # never placed at the broker as a chart stop, so there is nothing to have been "breached".
                prev, since = 0.0, None
            etf = etfs.get(t)
            ctx = sector_context(bars.get(etf) if etf else None, etf, df)
            d = decide(t, df, p, prev, settings, ctx, since=since, asof=asof_ts)
            if legacy and d.action == "ENTRY":
                d.notice = f"replaces the earlier distance-based stop of ${float(state['stop']):,.2f}"
            held = conn.execute("SELECT decided_at, price FROM eod_holds WHERE position_id = ? ORDER BY id DESC LIMIT 1",
                                (p["id"],)).fetchone()
            if held:
                ev.holds.append(dict(ticker=t, since=held["decided_at"][:10], price_then=held["price"],
                                     price_now=float(df["Close"].iloc[-1])))
            if d.action == "NO_SUPPORT":
                was_none = bool(state) and state["tier"] == "NONE"
                flagged = conn.execute("SELECT MAX(asof) AS a FROM eod_stop_log WHERE position_id = ? "
                                       "AND action = 'NO_SUPPORT'", (p["id"],)).fetchone()["a"]
                recent = bool(flagged) and (datetime.fromisoformat(asof) - datetime.fromisoformat(flagged)).days \
                    < RULES["no_support_reminder_days"]
                if held or (was_none and recent):          # decided or already flagged: say nothing again
                    d = Decision(t, d.close, 0.0, 0.0, d.tier, "HOLD", d.reasons[:1], d.metrics, source="NONE")
            d.position_id = p["id"]
            ev.decisions.append(d)
            if d.action != "SKIP":
                ev.pending.append((p["id"], d))
        # A SKIP is retryable (data gap): keep the run open so a rerun is not gated off.
        ev.advance_asof = not any(d.action == "SKIP" for d in ev.decisions)
        return ev
    finally:
        conn.close()


def persist(db_path: str | Path, ev: Evaluation) -> None:
    """Write every state change in ONE transaction on ONE connection."""
    conn = open_state(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for pid, ticker, stop in ev.seeds:
                seed_stop_conn(conn, pid, ticker, stop, "MANUAL", "stop set manually at the broker (--set-stop)")
            for pid, d in ev.pending:
                save_state(conn, pid, d, ev.asof)
            if ev.advance_asof:
                conn.execute("""INSERT INTO eod_run_state (key, value) VALUES ('last_asof', ?)
                                ON CONFLICT(key) DO UPDATE SET value = MAX(value, excluded.value)""", (ev.asof,))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def run(db_path: str | Path, *, dry_run: bool, overrides_stop: Mapping[str, float] | None = None,
        force: bool = False, downloader: Callable[..., pd.DataFrame] | None = None,
        sector_lookup: Callable[[str], str | None] | None = None,
        send: Callable[[list[str]], None] | None = None, quiet: bool = True,
        now: datetime | None = None) -> tuple[list[Decision], str]:
    """compute -> send -> persist.  `send` must raise on failure; nothing is written to the
    database until it returned, so a failed alert is re-sent by the next run instead of lost."""
    ev = evaluate(db_path, overrides_stop=overrides_stop, force=force, dry_run=dry_run,
                  downloader=downloader, sector_lookup=sector_lookup, now=now)
    if dry_run:
        return ev.decisions, ev.asof
    if send and ev.decisions:
        msgs = format_alert(ev.decisions, ev.asof, quiet, ev.holds)
        if msgs:
            send(msgs)
    if ev.pending or ev.seeds or ev.advance_asof:
        persist(db_path, ev)
    return ev.decisions, ev.asof


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.environ.get("RISK_SIZER_DB_PATH", str(ROOT / "data" / "risk_sizer.db")))
    ap.add_argument("--dry-run", action="store_true", help="print the alert, send nothing, save no state")
    ap.add_argument("--force", action="store_true", help="run even if no new session has completed")
    ap.add_argument("--set-stop", action="append", default=[], metavar="TICKER=PRICE",
                    help="record the stop actually placed at the broker (use once to sync)")
    ap.add_argument("--json", action="store_true", help="print decisions as JSON")
    ap.add_argument("--notify-failure", action="store_true",
                    help="send a 'job failed' Telegram message (used by the systemd OnFailure unit)")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if a.notify_failure:
        if not (token and chat):
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
            return 1
        send_telegram(["🚨 <b>Risk Sizer EOD job FAILED</b> - no stop review ran.\n"
                       "Check: journalctl -u risk-sizer-eod -n 50"], token, chat)
        return 0
    manual = {k.strip().upper(): float(v) for k, v in (s.split("=") for s in a.set_stop)}
    quiet = os.environ.get("QUIET_WHEN_NO_CHANGES", "true").strip().lower() not in ("0", "false", "no", "off")
    printed: list[str] = []

    def send(msgs: list[str]) -> None:
        if token and chat:
            send_telegram(msgs, token, chat)
        else:
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - printing instead", file=sys.stderr)
            print("\n\n".join(msgs))
        printed.extend(msgs)

    decisions, asof = run(a.db, dry_run=a.dry_run, overrides_stop=manual, force=a.force,
                          send=send, quiet=quiet)
    if not decisions:
        print("no active positions or no new session - nothing to do")
        return 0
    if a.json:
        print(json.dumps([d.__dict__ for d in decisions], default=float, indent=2))
    if a.dry_run:
        msgs = format_alert(decisions, asof, quiet)
        print("\n\n".join(msgs) if msgs else "nothing actionable - no alert sent")
    elif not printed:
        print("nothing actionable - no alert sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
