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
     MIN_MOVE_ATR x ATR.

Prices are unadjusted (auto_adjust=False) because broker stops are entered in
real prices.  ATR is Wilder's over the full history, as in fetch_data.py.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
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
    "sr_lookback": 160,         # bars scanned for swing lows
    "zone_merge_atr": 0.5,      # swing lows closer than this form one zone
    "min_zone_score": 2,        # touches + 2 x HVN-overlap needed to trust a zone
    "support_clearance_atr": 1.0,   # a zone must sit this far below the close
    "stop_buffer_atr": 0.3,     # stop goes this far under the zone low
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
    "min_move_atr": 0.25,       # smaller raises are noise, reported as HOLD
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
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


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

    @property
    def score(self) -> int:
        return self.touches + 2 * int(self.hvn)


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
    bars on each side, so the newest candidate is pivot_k bars old: nothing repaints."""
    k = int(RULES["pivot_k"])
    low = df["Low"].tail(int(RULES["sr_lookback"])).to_numpy()
    pivots: list[float] = []
    last_i = -10**9
    for i in range(k, len(low) - k):
        if low[i] == low[i - k:i + k + 1].min():
            if i - last_i <= k and pivots:           # plateau: keep the lower low only
                pivots[-1] = min(pivots[-1], low[i])
            else:
                pivots.append(float(low[i]))
            last_i = i
    zones: list[Zone] = []
    for price in sorted(pivots):
        if zones and price - zones[-1].lo <= RULES["zone_merge_atr"] * atr_now:
            zones[-1].hi, zones[-1].touches = price, zones[-1].touches + 1
        else:
            zones.append(Zone(price, price, 1))
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


def decide(
    ticker: str,
    df: pd.DataFrame,
    pos: Mapping[str, Any],
    prev_stop: float,
    settings: Mapping[str, float],
    ctx: Context | None = None,
) -> Decision:
    ctx = ctx or Context()
    if len(df) < RULES["min_bars"]:
        return Decision(ticker, float(df["Close"].iloc[-1]) if len(df) else 0.0, prev_stop,
                        prev_stop, NORMAL, "SKIP", [f"only {len(df)} bars of history"])

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
         if z.score >= RULES["min_zone_score"]
         and z.hi <= close - RULES["support_clearance_atr"] * a),
        key=lambda z: z.hi, reverse=True,
    )
    buf = RULES["stop_buffer_atr"] * a
    s1 = usable[0] if usable else None
    s2 = usable[1] if len(usable) > 1 else None
    ladder, armed = ladder_stop(df, pos, settings)
    since_entry = df[df.index >= pd.Timestamp(str(pos["opened_at"])[:10])]["Close"]
    hh_close = max(float(since_entry.max()) if len(since_entry) else close, entry)
    chandelier = hh_close - settings["trailmult"] * a if armed else -np.inf

    cand_loose = max(chandelier, s2.lo - buf if s2 else -np.inf)
    cand_normal = max(cand_loose, s1.lo - buf if s1 else -np.inf)
    cand_tight = max(cand_normal, hh_close - RULES["chandelier_extra_tight_atr"] * a)

    # --- tier from technicals ----------------------------------------------------
    score = 0
    trend_strong = adx_now >= RULES["adx_trend"] and pdi > mdi
    downtrend = adx_now >= RULES["adx_range"] and mdi > pdi
    peaked = (adx_recent_max >= RULES["adx_peak"] and adx_now < adx_recent_max - 3
              and ax["adx"].iloc[-1] < ax["adx"].iloc[-4])
    if downtrend:
        score += 2
        reasons.append(f"downtrend (ADX {adx_now:.0f}, -DI {mdi:.0f} > +DI {pdi:.0f})")
    if peaked:
        score += 1
        reasons.append(f"trend fading (ADX {adx_now:.0f} off {adx_recent_max:.0f} peak)")

    gain_atr = (close - entry) / a
    hot_recently = float(rs_series.tail(5).max()) >= RULES["rsi_hot"]
    cooled = rsi_now < RULES["rsi_cool"]
    # bearish divergence: price at/near its 20d high while RSI is below its own 20d high
    div = (close >= 0.98 * float(df["High"].tail(20).max())
           and float(rs_series.tail(5).max()) < float(rs_series.tail(20).max()) - 5
           and float(rs_series.tail(20).max()) > 65)
    if gain_atr >= RULES["exhaustion_min_gain_atr"] and ((hot_recently and cooled) or div):
        score += 1
        reasons.append("momentum exhaustion (RSI " + ("divergence" if div else
                       f"rolled over from {float(rs_series.tail(5).max()):.0f}") + ")")

    last10 = df.tail(10)
    dist_days = int(((last10["Close"] < df["Close"].shift(1).tail(10))
                     & (last10["Volume"] > RULES["dist_vol_ratio"] * avg_vol)).sum()) if avg_vol > 0 else 0
    if dist_days >= RULES["dist_days_trigger"]:
        score += 1
        reasons.append(f"{dist_days} distribution days in 10")

    confirmed_break = bool(s1 and close < s1.lo and vol_ratio >= RULES["break_vol_ratio"])
    broken_zone = next((z for z in sorted(zones, key=lambda z: z.hi, reverse=True)
                        if z.score >= RULES["min_zone_score"] and z.lo > close
                        and z.lo <= close * 1.03), None)
    if broken_zone and vol_ratio >= RULES["break_vol_ratio"]:
        confirmed_break = True
        reasons.append(f"close under support {broken_zone.lo:.2f} on {vol_ratio:.1f}x volume")
        score += 2

    tier = LOOSE if (score == 0 and trend_strong) else NORMAL if score <= 1 else TIGHT

    # oversold on support: do not sell the low unless support actually broke on volume
    near_support = bool(s1 and close - s1.hi <= 1.5 * a) or bool(
        zones and any(z.lo - a <= close <= z.hi + 1.5 * a for z in zones if z.score >= 2))
    oversold_hold = rsi_now < RULES["rsi_oversold"] and near_support and not confirmed_break
    if oversold_hold:
        tier = min(tier, NORMAL)
        reasons.append(f"RSI {rsi_now:.0f} oversold on support - no tightening")

    # --- sector: at most one notch tighter, never looser -------------------------
    idio_strength = bool(close > ema20 and rsi_now > 50
                         and ctx.stock_vs_sector_10d is not None and ctx.stock_vs_sector_10d > 0.02)
    if ctx.weak and not oversold_hold and tier < TIGHT:
        if idio_strength:
            reasons.append(f"sector weak ({ctx.etf}) but stock outperforming - ignored")
        else:
            tier += 1
            reasons.append(f"{ctx.note} - tightened one notch")

    if tier == LOOSE and not reasons:
        reasons.append(f"strong uptrend (ADX {adx_now:.0f}), room given")

    cand = {LOOSE: cand_loose, NORMAL: cand_normal, TIGHT: cand_tight}[tier]
    if confirmed_break:
        cand = max(cand, close - RULES["tight_clearance_atr"] * a)

    # --- combine: ratchet, cap under price, noise filter -------------------------
    ceiling = close - RULES["tight_clearance_atr"] * a
    new_stop = max(prev_stop, min(max(cand, ladder), ceiling))
    tick = 0.01 if close >= 1 else 0.0001
    new_stop = round(round(new_stop / tick) * tick, 4)

    breached = float(df["Low"].iloc[-1]) <= prev_stop
    if breached:
        action = "BREACH"
        reasons.insert(0, f"low {df['Low'].iloc[-1]:.2f} touched stop {prev_stop:.2f}"
                          f" (close {close:.2f}) - confirm fill at broker")
        new_stop = prev_stop
    elif new_stop - prev_stop >= RULES["min_move_atr"] * a:
        action = "RAISE"
        if s1 and tier >= NORMAL and abs(new_stop - (s1.lo - buf)) < 0.02:
            reasons.insert(0, f"trailing below support {s1.lo:.2f} "
                              f"({s1.touches} touch{'es' if s1.touches != 1 else ''}"
                              f"{', volume node' if s1.hvn else ''})")
        elif s2 and tier == LOOSE and abs(new_stop - (s2.lo - buf)) < 0.02:
            reasons.insert(0, f"trailing below major support {s2.lo:.2f}")
        elif tier == TIGHT and abs(new_stop - (hh_close - RULES["chandelier_extra_tight_atr"] * a)) < 0.02:
            reasons.insert(0, f"tight trail {RULES['chandelier_extra_tight_atr']:.0f} ATR under high close {hh_close:.2f}")
        else:
            reasons.insert(0, f"{settings['trailmult']:.1f} ATR trail under high close {hh_close:.2f}")
    else:
        action = "HOLD"
        new_stop = prev_stop
        if not reasons or tier == LOOSE:
            reasons.insert(0, "no qualified level above current stop")
        if s1:
            reasons.append(f"next support {s1.lo:.2f}")

    metrics = dict(atr=a, rsi=rsi_now, adx=adx_now, pdi=pdi, mdi=mdi, vol_ratio=vol_ratio,
                   poc=poc, ladder=ladder, dist_days=dist_days)
    return Decision(ticker, close, prev_stop, new_stop, tier, action, reasons, metrics)


# ------------------------------------------------------------------------------ data
def completed_bars(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Drop today's bar while the US market is still open so no indicator sees a partial day."""
    now = (now or datetime.now(NY)).astimezone(NY)
    df = df.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    df.index = pd.DatetimeIndex(pd.to_datetime(df.index)).tz_localize(None)
    if len(df) and df.index[-1].date() == now.date() and now.time() < dtime(16, 10):
        df = df.iloc[:-1]
    return df


def download(symbols: list[str], downloader: Callable[..., pd.DataFrame] | None = None) -> dict[str, pd.DataFrame]:
    fetch = downloader or (yf.download if yf else None)
    if fetch is None:
        raise RuntimeError("yfinance is not installed")
    raw = fetch(symbols, period="2y", interval="1d", auto_adjust=False,
                group_by="ticker", progress=False, threads=True)
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        try:
            sub = raw[s] if isinstance(raw.columns, pd.MultiIndex) else raw
            out[s] = completed_bars(sub[["Open", "High", "Low", "Close", "Volume"]])
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
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True))
    return cache[ticker] or None


# ----------------------------------------------------------------------------- state
STATE_DDL = """
CREATE TABLE IF NOT EXISTS eod_stop_state (
    position_id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    stop REAL NOT NULL,
    tier TEXT,
    asof TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eod_stop_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof TEXT NOT NULL,
    position_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    close REAL, prev_stop REAL, new_stop REAL,
    tier TEXT, action TEXT, reasons TEXT
);
"""


def open_state(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(STATE_DDL)
    return conn


def load_prev(conn: sqlite3.Connection, position_id: int) -> float | None:
    row = conn.execute("SELECT stop FROM eod_stop_state WHERE position_id = ?", (position_id,)).fetchone()
    return float(row["stop"]) if row else None


def save_state(conn: sqlite3.Connection, position_id: int, d: Decision, asof: str) -> None:
    now = datetime.now(NY).isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO eod_stop_state (position_id, ticker, stop, tier, asof, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(position_id) DO UPDATE SET stop=excluded.stop, tier=excluded.tier,
               asof=excluded.asof, updated_at=excluded.updated_at""",
        (position_id, d.ticker, d.new_stop, TIER_NAME[d.tier], asof, now))
    conn.execute(
        """INSERT INTO eod_stop_log (asof, position_id, ticker, close, prev_stop, new_stop, tier, action, reasons)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (asof, position_id, d.ticker, d.close, d.prev_stop, d.new_stop, TIER_NAME[d.tier],
         d.action, json.dumps(d.reasons)))


# --------------------------------------------------------------------------- message
ICON = {"RAISE": "🔼", "HOLD": "⏸", "BREACH": "🚨", "SKIP": "⚠️"}


def format_alert(decisions: list[Decision], asof: str) -> list[str]:
    """Telegram HTML messages, each under the 4096-char limit."""
    head = f"<b>EOD stops · {html.escape(asof)}</b>"
    moved = sum(d.moved for d in decisions)
    breach = sum(d.action == "BREACH" for d in decisions)
    head += f"\n{len(decisions)} positions · {moved} to raise" + (f" · <b>{breach} BREACH</b>" if breach else "")
    order = {"BREACH": 0, "RAISE": 1, "HOLD": 2, "SKIP": 3}
    blocks = []
    for d in sorted(decisions, key=lambda x: (order[x.action], x.ticker)):
        t = html.escape(d.ticker)
        if d.action == "SKIP":
            blocks.append(f"{ICON['SKIP']} <b>{t}</b> skipped: {html.escape('; '.join(d.reasons))}")
            continue
        chg = (d.new_stop / d.prev_stop - 1) * 100 if d.prev_stop else 0
        line = f"{ICON[d.action]} <b>{t}</b>  ${d.close:,.2f}  [{TIER_NAME[d.tier]}]\n"
        if d.action == "RAISE":
            line += f"Prev stop ${d.prev_stop:,.2f} → <b>New ${d.new_stop:,.2f}</b> ({chg:+.1f}%)\n"
        elif d.action == "BREACH":
            line += f"Stop ${d.prev_stop:,.2f} was touched today\n"
        else:
            line += f"Prev stop ${d.prev_stop:,.2f} → New ${d.new_stop:,.2f} (unchanged)\n"
        line += "Why: " + html.escape("; ".join(d.reasons))
        blocks.append(line)
    messages, cur = [], head
    for b in blocks:
        if len(cur) + len(b) + 2 > 3900:
            messages.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    messages.append(cur)
    return messages


def send_telegram(messages: list[str], token: str, chat_id: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for m in messages:
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": m, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=20) as r:
            if r.status != 200:
                raise RuntimeError(f"telegram HTTP {r.status}")


# -------------------------------------------------------------------------- pipeline
def run(db_path: str | Path, *, dry_run: bool, overrides_stop: Mapping[str, float] | None = None,
        downloader: Callable[..., pd.DataFrame] | None = None,
        sector_lookup: Callable[[str], str | None] | None = None) -> tuple[list[Decision], str]:
    from database import Database

    db = Database(db_path)
    settings = db.get_settings()["settings"]
    positions = [p for p in db.list_active()]
    if not positions:
        return [], datetime.now(NY).date().isoformat()

    cache = ROOT / "data" / "sector_cache.json"
    ov_file = ROOT / "data" / "sector_overrides.json"
    overrides = json.loads(ov_file.read_text()) if ov_file.exists() else {}
    lookup = sector_lookup or (lambda t: sector_etf_for(t, cache, overrides))
    etfs = {p["ticker"]: lookup(p["ticker"]) for p in positions}
    symbols = sorted({p["ticker"] for p in positions} | {e for e in etfs.values() if e} | {MARKET})
    bars = download(symbols, downloader)

    conn = open_state(db_path)
    decisions: list[Decision] = []
    asof = max((b.index[-1] for b in bars.values() if len(b)), default=pd.Timestamp.now()).date().isoformat()
    for p in positions:
        t = p["ticker"]
        df = bars.get(t)
        if df is None or df.empty:
            decisions.append(Decision(t, 0.0, 0.0, 0.0, NORMAL, "SKIP", ["no price data"]))
            continue
        prev = (overrides_stop or {}).get(t) or load_prev(conn, p["id"])
        if prev is None:
            prev = ladder_stop(df, p, settings)[0]
        etf = etfs.get(t)
        ctx = sector_context(bars.get(etf) if etf else None, etf, df)
        d = decide(t, df, p, prev, settings, ctx)
        decisions.append(d)
        if not dry_run and d.action != "SKIP":
            save_state(conn, p["id"], d, asof)
    if not dry_run:
        conn.commit()
    conn.close()
    return decisions, asof


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.environ.get("RISK_SIZER_DB_PATH", str(ROOT / "data" / "risk_sizer.db")))
    ap.add_argument("--dry-run", action="store_true", help="print the alert, send nothing, save no state")
    ap.add_argument("--set-stop", action="append", default=[], metavar="TICKER=PRICE",
                    help="record the stop actually placed at the broker (use once to sync)")
    ap.add_argument("--json", action="store_true", help="print decisions as JSON")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    manual = {k.upper(): float(v) for k, v in (s.split("=") for s in a.set_stop)}
    decisions, asof = run(a.db, dry_run=a.dry_run, overrides_stop=manual)
    if not decisions:
        print("no active positions")
        return 0
    msgs = format_alert(decisions, asof)
    if a.json:
        print(json.dumps([d.__dict__ for d in decisions], default=float, indent=2))
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if a.dry_run or not (token and chat):
        if not a.dry_run:
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - printing instead", file=sys.stderr)
        print("\n\n".join(msgs))
    else:
        send_telegram(msgs, token, chat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
