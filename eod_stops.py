#!/usr/bin/env python3
"""End-of-day technical trailing stops for Active positions.

Run once after the US close.  For every open Active position it computes a
recommended stop from completed daily bars, compares it with the stop recorded
yesterday, and sends one Telegram message (or prints it with --dry-run).

The stop is a *ratchet*: it never moves down.  Indicators only decide how tight
the ratchet is allowed to be today; they never lower a stop that was already set.

Decision flow (see RULES below for the numbers):

  1. Build candidate stop prices from structure:
       support zones  = clustered confirmed swing lows
       volume profile = high-volume nodes (HVN) strengthen a zone
  2. Pick a tier - LOOSE / NORMAL / TIGHT - from regime and momentum:
       ADX + DI       trend strength and direction
       RSI            momentum exhaustion (divergence), never a level alone
       volume         distribution days and volume-confirmed support breaks
  3. Sector weakness may tighten the tier by ONE notch, never widen it, and
     is ignored when the stock shows idiosyncratic strength or sits oversold on
     support.  It never sets a stop price by itself.
  4. new stop = max(previous stop, ATR-ladder floor, tier candidate), capped
     below the last close, and only reported as a move if it rises by at least
     max(0.5 x ATR, 1.5% of the current stop).  Smaller steps are not saved, so
     they accumulate until one clears the bar.

Prices come with auto_adjust=False (no dividend adjustment) because broker stops are
entered in real prices.  Yahoo still rewrites history for SPLITS, so a split since the
position was opened makes every stored price stale: those positions are SKIPped with an
explicit message instead of producing a false BREACH/RAISE.  ATR is Wilder's over the
full history, as in fetch_data.py.

Schedule: the job runs the morning after each session (see deploy/risk-sizer-eod.timer).
Broker stops only matter for the next session anyway, and by then Yahoo's volume and
split data are settled.  Nothing is persisted until Telegram accepted every message, so a
failed send is retried by simply running the job again.
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
    "min_touch_gap": 5,         # bars between two touches of a zone for them to count as two
    "min_zone_touches": 2,      # a zone needs this many separate bounces ...
    "single_touch_reaction_atr": 3.0,  # ... or ONE bounce of this size that also sits on a volume node
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
    "chandelier_extra_tight_atr": 2.0,
    "tight_clearance_atr": 0.75,
    "entry_min_atr_trend": 1.5,  # initial stop must sit this far (ATR) below entry in a clean trend
    "entry_min_atr_chop": 2.0,   # ... and this far when ADX says range or downtrend
    "min_move_atr": 0.5,        # a raise must clear BOTH this many ATR ...
    "min_move_pct": 1.5,        # ... and this % of the current stop, else it is HOLD
}

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

    @property
    def qualified(self) -> bool:
        """A real floor: bounced at least twice, or once hard while sitting on a volume node.
        Anything weaker is a wiggle, not support, and must never anchor a stop."""
        return (self.touches >= RULES["min_zone_touches"]
                or (self.hvn and self.reaction >= RULES["single_touch_reaction_atr"]))


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
    for price, idx, react in sorted(pivots):
        if zones and price - zones[-1].lo <= RULES["zone_merge_atr"] * atr_now:
            zones[-1].hi, zones[-1].reaction = price, max(zones[-1].reaction, react)
            bars_of[-1].append(int(idx))
        else:
            zones.append(Zone(price, price, 1, reaction=react))
            bars_of.append([int(idx)])
    for z, idxs in zip(zones, bars_of):              # separate bounces only, not one wiggle counted twice
        counted, last = 0, -10**9
        for i in sorted(idxs):
            if i - last >= RULES["min_touch_gap"]:
                counted, last = counted + 1, i
        z.touches = counted
    pad = RULES["zone_merge_atr"] * atr_now
    for z in zones:
        z.hvn = bool(len(hvn) and ((hvn >= z.lo - pad) & (hvn <= z.hi + pad)).any())
    return zones


# ---------------------------------------------------------------------------- ladder
def ladder_stop(df: pd.DataFrame, pos: Mapping[str, Any], settings: Mapping[str, float]) -> tuple[float, bool]:
    """Replay the existing README ladder from the entry date: initial stop, arming from
    the intraday high, then a close-high trail that never falls below entry.
    Returns (stop, armed).  Used to seed state and as the floor under technical stops."""
    entry, a = float(pos["entry_price"]), float(pos["atr"])
    dist = min(max(settings["initmult"] * a / entry, settings["minstop"] / 100), settings["maxstop"] / 100)
    stop = entry * (1 - dist)
    arm_at = entry * (1 + max(settings["armpct"] / 100, settings["armatrmult"] * a / entry))
    opened = pd.Timestamp(str(pos["opened_at"])[:10])
    bars = df[df.index >= opened]
    armed = pos.get("risk_status") == "ARMED_ZERO_RISK" or pos.get("rung", 0) > 0
    hh = entry
    for _, bar in bars.iterrows():
        if bar["High"] >= arm_at:
            armed = True
        hh = max(hh, float(bar["Close"]))
        if armed:
            stop = max(stop, entry, hh - settings["trailmult"] * a)
    if armed:
        stop = max(stop, entry)
    return stop, armed


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
    action: str                      # RAISE | HOLD | BREACH | SKIP
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    notice: str | None = None        # one-off heads-up shown in the alert (adoption, ladder-only)
    notice_kind: str | None = None   # "start" | "ladder": notices of a kind share ONE alert line
    source: str | None = None        # stored in place of the tier name, e.g. "LADDER"

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


def breach_reason(low: float, day: str, stop: float) -> str:
    return (f"On {_d(day)} it traded down to {_p(low)}, at or below your stop of {_p(stop)}. "
            f"Check whether your broker already sold it.")


def is_excluded(ticker: str) -> bool:
    """Tel Aviv (.TA) tickers are outside this engine: Yahoo quotes them in agorot, so entry
    prices, ATR and stops are not comparable with the shekel figures kept in the database."""
    return ticker.strip().upper().endswith(".TA")


def baseline_stop(ticker: str, df: pd.DataFrame, pos: Mapping[str, Any],
                  settings: Mapping[str, float]) -> tuple[float, bool]:
    """Starting stop from TODAY's price, for a position whose entry-based ladder stop is already
    above the market (so it is meaningless).  Structural support if a qualified one sits far
    enough below the close, else the ATR stop at the configured multiple and bounds."""
    close = float(df["Close"].iloc[-1])
    found = initial_stop(ticker, df, close, settings).get("stop")
    if found and 0 < found < close:
        return float(found), True
    a_now = float(atr(df).iloc[-1]) if len(df) > 14 else float("nan")
    a = a_now if math.isfinite(a_now) and a_now > 0 else float(pos["atr"])
    dist = min(max(settings["initmult"] * a / close, settings["minstop"] / 100), settings["maxstop"] / 100)
    return round(close * (1 - dist), 4 if close < 1 else 2), False


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


def _ladder_only(ticker, df, pos, prev_stop, settings, since) -> Decision:
    """Fewer than min_bars of history: no structure to trust, so trail by the plain ATR ladder."""
    close = float(df["Close"].iloc[-1])
    a_now = float(atr(df).iloc[-1]) if len(df) > 14 else float("nan")
    a = a_now if math.isfinite(a_now) and a_now > 0 else float(pos["atr"])
    ladder, _ = ladder_stop(df, pos, settings)
    tick = _tick(close)
    ceiling = _floor_tick(close - RULES["tight_clearance_atr"] * a, tick)
    new_stop = max(prev_stop, min(_round_tick(ladder, tick), ceiling))
    reasons = ["Too new to read chart levels (under 120 days of history), so the stop just follows "
               "a simple distance rule."]
    hit = _breach(df, prev_stop, since)
    if hit:
        action, new_stop = "BREACH", prev_stop
        reasons.insert(0, breach_reason(hit[0], hit[1], prev_stop))
    elif new_stop - prev_stop >= max(RULES["min_move_atr"] * a, RULES["min_move_pct"] / 100 * prev_stop):
        action = "RAISE"
    else:
        action, new_stop = "HOLD", prev_stop
    return Decision(ticker, close, prev_stop, new_stop, NORMAL, action, reasons,
                    dict(atr=a, ladder=ladder), source="LADDER")


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
    """`since`: date of the last review of this position (bars after it are checked for a
    breach).  `asof`: newest session date across the download; a ticker behind it is stale."""
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
    if len(df) < RULES["min_bars"]:
        return _ladder_only(ticker, df, pos, prev_stop, settings, since)

    close = float(df["Close"].iloc[-1])
    entry = float(pos["entry_price"])
    a_series = atr(df)
    a = float(a_series.iloc[-1])
    rs_series = rsi(df["Close"])
    rsi_now = float(rs_series.iloc[-1])
    ax = adx(df)
    adx_now, pdi, mdi = (float(ax[c].iloc[-1]) for c in ("adx", "pdi", "mdi"))
    adx_recent_max = float(ax["adx"].tail(10).max())
    ema20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
    avg_vol = float(df["Volume"].iloc[-21:-1].mean())
    vol_ratio = float(df["Volume"].iloc[-1] / avg_vol) if avg_vol > 0 else 1.0

    hvn, _, poc = volume_profile(df, a)
    zones = support_zones(df, a, hvn)
    reasons: list[str] = []

    # --- structure candidates --------------------------------------------------
    usable = sorted(
        (z for z in zones
         if z.qualified
         and z.hi <= close - RULES["support_clearance_atr"] * a
         and z.hi >= close - RULES["support_max_distance_atr"] * a),
        key=lambda z: z.hi, reverse=True,
    )
    s1 = usable[0] if usable else None
    s2 = usable[1] if len(usable) > 1 else None
    ladder, armed = ladder_stop(df, pos, settings)
    since_entry = df[df.index >= pd.Timestamp(str(pos["opened_at"])[:10])]["Close"]
    hh_close = max(float(since_entry.max()) if len(since_entry) else close, entry)
    chandelier = hh_close - settings["trailmult"] * a if armed else -np.inf

    cand_loose = max(chandelier, stop_under(s2, a, close) if s2 else -np.inf)
    cand_normal = max(cand_loose, stop_under(s1, a, close) if s1 else -np.inf)
    cand_tight = max(cand_normal, hh_close - RULES["chandelier_extra_tight_atr"] * a)

    # --- tier from technicals ----------------------------------------------------
    score = 0
    trend_strong = adx_now >= RULES["adx_trend"] and pdi > mdi
    downtrend = adx_now >= RULES["adx_range"] and mdi > pdi
    peaked = (adx_recent_max >= RULES["adx_peak"] and adx_now < adx_recent_max - 3
              and ax["adx"].iloc[-1] < ax["adx"].iloc[-4])
    if downtrend:
        score += 2
        reasons.append("It's in a clear downtrend, so the stop stays close.")
    if peaked:
        score += 1
        reasons.append("The uptrend is losing steam.")

    gain_atr = (close - entry) / a
    hot_recently = float(rs_series.tail(5).max()) >= RULES["rsi_hot"]
    cooled = rsi_now < RULES["rsi_cool"]
    # bearish divergence: price at/near its 20d high while RSI is below its own 20d high
    div = (close >= 0.98 * float(df["High"].tail(20).max())
           and float(rs_series.tail(5).max()) < float(rs_series.tail(20).max()) - 5
           and float(rs_series.tail(20).max()) > 65)
    if gain_atr >= RULES["exhaustion_min_gain_atr"] and ((hot_recently and cooled) or div):
        score += 1
        reasons.append("It ran up a lot and the buying push is fading.")

    last10 = df.tail(10)
    dist_days = int(((last10["Close"] < df["Close"].shift(1).tail(10))
                     & (last10["Volume"] > RULES["dist_vol_ratio"] * avg_vol)).sum()) if avg_vol > 0 else 0
    if dist_days >= RULES["dist_days_trigger"]:
        score += 1
        reasons.append(f"There were {dist_days} heavy-selling days in the last 10.")

    confirmed_break = bool(s1 and close < s1.lo and vol_ratio >= RULES["break_vol_ratio"])
    broken_zone = next((z for z in sorted(zones, key=lambda z: z.hi, reverse=True)
                        if z.qualified and z.lo > close
                        and z.lo <= close * 1.03), None)
    if broken_zone and vol_ratio >= RULES["break_vol_ratio"]:
        confirmed_break = True
        reasons.append(f"It closed below a price floor near {_p(broken_zone.lo)} on heavy selling.")
        score += 2

    tier = LOOSE if (score == 0 and trend_strong) else NORMAL if score <= 1 else TIGHT

    # oversold on support: do not sell the low unless support actually broke on volume
    near_support = bool(s1 and close - s1.hi <= 1.5 * a) or bool(
        zones and any(z.lo - a <= close <= z.hi + 1.5 * a for z in zones if z.qualified))
    oversold_hold = rsi_now < RULES["rsi_oversold"] and near_support and not confirmed_break
    if oversold_hold:
        tier = min(tier, NORMAL)
        reasons.append("It's been beaten down and is sitting on support, so I'm not tightening yet.")

    # --- sector: at most one notch tighter, never looser -------------------------
    idio_strength = bool(close > ema20 and rsi_now > 50
                         and ctx.stock_vs_sector_10d is not None and ctx.stock_vs_sector_10d > 0.02)
    if ctx.weak and not oversold_hold and tier < TIGHT:
        if idio_strength:
            reasons.append(f"Its sector ({ctx.etf}) is weak, but this stock is holding up better, so no change.")
        else:
            tier += 1
            reasons.append(f"Its sector ({ctx.etf}) has been sliding for several days, so the stop is a bit tighter.")

    if tier == LOOSE and not reasons:
        reasons.append("Strong, steady uptrend, so it gets room to move.")

    cand = {LOOSE: cand_loose, NORMAL: cand_normal, TIGHT: cand_tight}[tier]
    if confirmed_break:
        cand = max(cand, close - RULES["tight_clearance_atr"] * a)

    # --- combine: ratchet, cap under price, noise filter -------------------------
    # Round the target to the tick FIRST, then cap it at the tick-floored ceiling, so rounding
    # can never lift the stop above the cap.  prev_stop is kept exactly as recorded.
    tick = _tick(close)
    ceiling = _floor_tick(close - RULES["tight_clearance_atr"] * a, tick)
    new_stop = max(prev_stop, min(_round_tick(max(cand, ladder), tick), ceiling))

    hit = _breach(df, prev_stop, since)
    if hit:
        action = "BREACH"
        reasons.insert(0, breach_reason(hit[0], hit[1], prev_stop))
        new_stop = prev_stop
    elif new_stop - prev_stop >= max(RULES["min_move_atr"] * a,
                                     RULES["min_move_pct"] / 100 * prev_stop):
        action = "RAISE"
        if s1 and tier >= NORMAL and abs(new_stop - stop_under(s1, a, close)) < 0.02:
            reasons.insert(0, f"The stop now sits just under a support level near {_p(s1.lo)}, a price where it "
                              f"has bounced {s1.touches} time{'s' if s1.touches != 1 else ''}"
                              f"{' and where lots of shares changed hands' if s1.hvn else ''}.")
        elif s2 and tier == LOOSE and abs(new_stop - stop_under(s2, a, close)) < 0.02:
            reasons.insert(0, f"The stop now sits just under a major support level near {_p(s2.lo)}.")
        elif tier == TIGHT and abs(new_stop - (hh_close - RULES["chandelier_extra_tight_atr"] * a)) < 0.02:
            reasons.insert(0, f"Locking in gains: the stop follows close behind its recent high close of {_p(hh_close)}.")
        else:
            reasons.insert(0, f"Following the price up: the stop trails a comfortable distance below its "
                              f"highest close ({_p(hh_close)}).")
    else:
        action = "HOLD"
        new_stop = prev_stop
        if not reasons or tier == LOOSE:
            reasons.insert(0, "No solid support level higher up yet, so the stop stays where it is.")
        if s1 and new_stop > s1.hi:
            reasons.append(f"The stop sits above the nearest support ({_p(s1.lo)}), so it triggers before that "
                           f"level is even tested.")
        elif s1:
            reasons.append(f"Next support to watch: {_p(s1.lo)}.")

    if s1 is None and action != "BREACH":
        reasons.append("No clear support level nearby, so the stop is just a distance below the price.")
    metrics = dict(atr=a, rsi=rsi_now, adx=adx_now, pdi=pdi, mdi=mdi, vol_ratio=vol_ratio,
                   poc=poc, ladder=ladder, dist_days=dist_days)
    return Decision(ticker, close, prev_stop, new_stop, tier, action, reasons, metrics)


# ------------------------------------------------------------------- initial stop
def initial_stop(ticker: str, df: pd.DataFrame, entry: float, settings: Mapping[str, float]) -> dict[str, Any]:
    """Structural stop for a NEW position, from the same zones/volume/ADX as the daily job.

    Picks the nearest qualified support whose stop sits far enough below entry to survive
    normal noise (1.5 ATR in a clean uptrend, 2.0 ATR when ADX shows range or downtrend).
    `extended` is True when that support is more than settings['extendedpct'] below entry:
    the stock has run too far from structure, so any stop that respects structure makes a
    poor risk/reward entry.  Returns stop=None when no qualified support exists."""
    if len(df) < RULES["min_bars"]:
        return {"ticker": ticker, "stop": None, "no_support": True, "extended": False,
                "reasons": ["Not enough price history yet."], "justification": "Not enough price history yet."}
    a = float(atr(df).iloc[-1])
    ax = adx(df)
    adx_now, pdi, mdi = (float(ax[c].iloc[-1]) for c in ("adx", "pdi", "mdi"))
    rsi_now = float(rsi(df["Close"]).iloc[-1])
    trend_strong = adx_now >= RULES["adx_trend"] and pdi > mdi
    downtrend = adx_now >= RULES["adx_range"] and mdi > pdi
    regime = "uptrend" if trend_strong else "downtrend" if downtrend else "range/weak trend"
    min_atr = RULES["entry_min_atr_trend"] if trend_strong else RULES["entry_min_atr_chop"]

    hvn, _, poc = volume_profile(df, a)
    last_close = float(df["Close"].iloc[-1])
    zones = [z for z in support_zones(df, a, hvn)
             if z.qualified and z.hi < min(entry, last_close)]      # a floor price already fell through is not support
    zones.sort(key=lambda z: z.hi, reverse=True)
    chosen = next((z for z in zones if entry - stop_under(z, a, entry) >= min_atr * a), None)
    out: dict[str, Any] = {"ticker": ticker, "entry": entry, "atr": a, "adx": adx_now, "rsi": rsi_now,
                           "regime": regime, "poc": poc, "last_close": float(df["Close"].iloc[-1]),
                           "asof": df.index[-1].date().isoformat()}
    ext_limit = float(settings.get("extendedpct", 22.5))
    if chosen is None:
        out.update(stop=None, no_support=True, extended=True, reasons=["No solid support level below this price."],
                   justification="No solid support level below this price.")
        return out
    stop = stop_under(chosen, a, entry)
    assert stop < chosen.lo, "a support-based stop must sit below the support"
    support_gap = (entry - chosen.hi) / entry * 100
    mood = {"uptrend": "It's in a steady uptrend.", "downtrend": "It's been falling.",
            "range/weak trend": "It's moving sideways without a clear trend."}[regime]
    reasons = [f"Just under a support level around {_p(chosen.lo)}-{_p(chosen.hi)} that held "
               f"{chosen.touches} time{'s' if chosen.touches != 1 else ''}"
               f"{' (lots of shares traded there)' if chosen.hvn else ''}.",
               mood, f"The stop is about {(entry - stop) / entry * 100:.0f}% below your entry."]
    if downtrend:
        reasons.append("Because it is falling, that support may not hold.")
    out.update(stop=stop, no_support=False, extended=(support_gap > ext_limit
                                       or entry - chosen.hi > RULES["support_max_distance_atr"] * a), support_lo=chosen.lo,
               support_hi=chosen.hi, touches=chosen.touches, hvn=chosen.hvn,
               support_gap_pct=support_gap, distance_pct=(entry - stop) / entry * 100,
               reasons=reasons, justification=" ".join(reasons))
    return out


def stop_info(db_path: str | Path) -> dict[int, dict[str, Any]]:
    """Live stop per position id: the single record the Positions tab and the Telegram job share.
    `stop_why` is the reason of the latest ENTRY/RAISE, i.e. why the stop sits where it does;
    `stop_last_action` is the latest review outcome (HOLD, BREACH ...)."""
    conn = open_state(db_path)
    try:
        info: dict[int, dict[str, Any]] = {}
        for r in conn.execute("SELECT position_id, ticker, stop, tier, asof, updated_at FROM eod_stop_state"):
            if is_excluded(r["ticker"]):
                continue
            info[r["position_id"]] = {"stop": r["stop"], "stop_source": r["tier"],
                                      "stop_asof": r["asof"], "stop_updated_at": r["updated_at"],
                                      "stop_why": None, "stop_last_action": None}
        for r in conn.execute("""SELECT position_id, action, reasons, asof, prev_stop FROM eod_stop_log
                                 WHERE id IN (SELECT MAX(id) FROM eod_stop_log GROUP BY position_id)"""):
            if r["position_id"] in info:
                info[r["position_id"]]["stop_last_action"] = r["action"]
                info[r["position_id"]]["stop_prev"] = r["prev_stop"] if r["action"] == "RAISE" else None
                info[r["position_id"]]["stop_last_asof"] = r["asof"]
        for r in conn.execute("""SELECT position_id, reasons FROM eod_stop_log
                                 WHERE id IN (SELECT MAX(id) FROM eod_stop_log
                                              WHERE action IN ('ENTRY', 'RAISE') GROUP BY position_id)"""):
            if r["position_id"] in info:
                try:
                    info[r["position_id"]]["stop_why"] = " ".join(json.loads(r["reasons"] or "[]"))
                except ValueError:
                    pass
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


# --------------------------------------------------------------------------- message
def format_alert(decisions: list[Decision], asof: str, quiet_when_no_changes: bool = True) -> list[str]:
    """One consolidated Telegram HTML message holding only actionable items: stops to
    raise, breaches and one-off notices.  HOLD is never mentioned.  With nothing actionable the
    result is empty (silent) or a single quiet line, per quiet_when_no_changes.  Positions that
    could not be evaluated are appended as one footnote so a data failure is not mistaken for
    'nothing to do'.  Split only if the message would exceed Telegram's 4096 limit."""
    breaches = [d for d in decisions if d.action == "BREACH"]
    raises = [d for d in decisions if d.action == "RAISE"]
    skipped = [d for d in decisions if d.action == "SKIP"]
    notices = [d for d in decisions if d.notice]
    if not breaches and not raises and not skipped and not notices:
        return [] if quiet_when_no_changes else ["✅ EOD Check: No stop adjustments required today."]

    blocks = []
    for d in breaches:
        blocks.append(f"🚨 <b>{html.escape(d.ticker)}</b>  {_p(d.close)}\n"
                      f"Your stop of {_p(d.prev_stop)} was reached\n"
                      f"Why: {html.escape(' '.join(d.reasons))}")
    for d in sorted(raises, key=lambda x: x.ticker):
        chg = (d.new_stop / d.prev_stop - 1) * 100
        blocks.append(f"🟢⬆️ <b>{html.escape(d.ticker)}</b>  {_p(d.close)}\n"
                      f"Move your stop up: {_p(d.prev_stop)} → <b>{_p(d.new_stop)}</b> ({chg:+.1f}%)\n"
                      f"Why: {html.escape(' '.join(d.reasons))}")
    start = [d.notice for d in sorted(notices, key=lambda x: x.ticker) if d.notice_kind == "start"]
    if start:
        line = "ℹ️ Starting stops, confirm the broker order matches: " + html.escape(" · ".join(start))
        if any(n.endswith("*") for n in start):
            line += html.escape("  (* just under a support level; your entry-based stop was already above the price)")
        if any(n.endswith("†") for n in start):
            line += html.escape("  († no clear support nearby, so it is just a distance below today's price)")
        blocks.append(line)
    ladder = [d.notice for d in sorted(notices, key=lambda x: x.ticker) if d.notice_kind == "ladder"]
    if ladder:
        blocks.append(f"ℹ️ Too new to read chart levels (under {int(RULES['min_bars'])} days of history), "
                      f"so the stop follows a simple distance rule: " + html.escape(" · ".join(ladder)))
    if skipped:
        blocks.append("⚠️ Skipped: " + html.escape("  ".join(
            f"{d.ticker}: {' '.join(d.reasons)}" for d in skipped)))

    head = (f"<b>EOD stops · {html.escape(asof)}</b>\n"
            f"{len(raises)} stop{'s' if len(raises) != 1 else ''} to move up"
            + (f" · <b>{len(breaches)} reached</b>" if breaches else ""))
    messages, cur = [], head
    for blk in blocks:
        if len(cur) + len(blk) + 2 > 3900:
            messages.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + blk
    messages.append(cur)
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

    def post(text: str, parse_mode: str | None) -> None:
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
        if parse_mode:
            params["parse_mode"] = parse_mode
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode())
        with opener(req, timeout=20) as r:
            if r.status != 200:
                raise TelegramError(f"telegram HTTP {r.status}")

    for m in messages:
        mode: str | None = "HTML"
        text = m
        for attempt in range(1, attempts + 1):
            try:
                post(text, mode)
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
                ev.decisions.append(Decision(t, 0.0, forced or 0.0, forced or 0.0, NORMAL, "SKIP", ["no price data"]))
                continue
            state = load_state(conn, p["id"])
            adopted = baseline = False
            mark = ""
            if forced:
                prev, since = float(forced), pd.Timestamp(last_session_date(now))
            elif state:
                prev, since = float(state["stop"]), pd.Timestamp(state["asof"])
            else:
                # Never recorded, so never placed at the broker: it cannot have been "breached"
                # (since = last bar leaves nothing to check).  A ladder stop from the old entry
                # that already sits above the market is useless, so start from today's price.
                adopted = True
                since = df.index[-1]
                prev = ladder_stop(df, p, settings)[0]
                if prev >= float(df["Close"].iloc[-1]):
                    (prev, backed), baseline = baseline_stop(t, df, p, settings), True
                    mark = "*" if backed else "†"
            etf = etfs.get(t)
            ctx = sector_context(bars.get(etf) if etf else None, etf, df)
            d = decide(t, df, p, prev, settings, ctx, since=since, asof=asof_ts)
            if adopted and d.action == "RAISE":
                d.action = "ENTRY"      # the first recorded stop is a starting stop, not a raise from a stop nobody placed
            if adopted and d.action != "SKIP":
                d.notice, d.notice_kind = f"{t} ${d.new_stop:,.2f}{mark}", "start"
                if baseline:
                    d.source = d.source or "BASELINE"
            elif d.source == "LADDER" and (state is None or state["tier"] != "LADDER"):
                d.notice, d.notice_kind = f"{t} ({len(df)} days)", "ladder"
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
        msgs = format_alert(ev.decisions, ev.asof, quiet)
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
