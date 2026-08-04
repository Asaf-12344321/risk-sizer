#!/usr/bin/env python3
"""Publish market data for risk-sizer.html.

Yahoo sends no CORS header so the browser cannot call it; this runs in GitHub Actions
and force-pushes the result to a `data` branch, which raw.githubusercontent.com does
serve cross-origin.

Two outputs, because putting daily bars for the whole market in one file would be ~50 MB:

  quotes.json        every liquid US ticker, metrics only (no bars). ~500 KB.
                     This is all the position-size calculator needs.
  bars/{TICKER}.json ~1y of daily bars, only for tickers listed in tickers.txt.
                     Only the position tracker needs these, and only for what you hold.

No per-ticker .info calls: they are one HTTP request each and would take hours over a
few thousand symbols. Beta is computed from returns against SPY, and company names come
from the SEC's own ticker file.
"""
import json, os, shutil, sys, time, datetime, warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import yfinance as yf

UNIVERSE_LIMIT = int(os.environ.get("UNIVERSE_LIMIT", "3000"))
MIN_ADV_USD    = float(os.environ.get("MIN_ADV_USD", "5e6"))
MIN_PRICE      = 3.0
BATCH          = 250
BARS_KEEP      = 260
UNIVERSE_FILE  = "universe.csv"


def sec_universe(limit):
    """Tickers + names from universe.csv, which is the SEC's own company_tickers.json
    flattened and committed here. Fetching it at runtime needs a User-Agent carrying
    personal contact details, which does not belong in a public repo — and a committed
    list means one less thing that can fail mid-run. Refresh it by hand occasionally."""
    import csv
    out = []
    with open(UNIVERSE_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            t = row["ticker"].strip().upper()
            if t:
                out.append((t, row.get("name", "").strip()))
    return out[:limit]


def tracked():
    if not os.path.exists("tickers.txt"):
        return []
    out = []
    for line in open("tickers.txt", encoding="utf-8"):
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s.upper())
    return sorted(set(out))


def wilder_atr(h, l, c, n=14):
    """True Range from explicit OHLC, then Wilder's smoothing. Never Yahoo's summary
    metrics — those are undocumented and were not reproducible.

    `ewm(alpha=1/n, adjust=False)` seeds on TR[0] where textbook Wilder seeds on the
    mean of the first n TRs. Verified identical to 0.000% on 10 tickers over the ~250-bar
    series this is always called with: the seed's weight decays as (1-1/14)^k, which is
    1.4e-8 by bar 250. Do not "fix" this.

    Two things that look like improvements and are not:
      - Shortening the window to ~30 bars. The seed no longer decays, so the result
        drifts -4.3% to +4.3% against true Wilder (measured). It also breaks beta.
      - A simple 14-bar mean of TR instead of Wilder. Differs by up to 15% on volatile
        names (QBTS 1.4254 vs 1.6793) and no longer matches TradingView's default.
    Keep the full ~1y window and Wilder smoothing.
    """
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean().iloc[-1]


# This file publishes beta as MEASURED and leaves policy to the page: `b` is present
# only when a beta could actually be computed, and carries whatever value came out —
# including zero or negative. Deciding what to divide by lives in one place
# (betaForSizing in index.html) rather than being half-applied on each side.
#
# Betas <= 0 are common and usually real, not noise: ABBV -0.078, ADP -0.025, AEP
# -0.038, TMUS -0.39 in the current window. Defensive dividend names genuinely
# decouple from an SPY that a handful of tech names dominate. 253 of 2,604 tickers
# sit at or below zero, and treating them as broken would misreport a real fact.
# 4dp because 3 rounds a small positive beta to exactly 0.000 (TRI did), which then
# reads downstream as "no beta at all".
BETA_DP = 4


def main():
    t0 = time.time()
    uni = sec_universe(UNIVERSE_LIMIT)
    names = dict(uni)
    syms = [t for t, _ in uni]
    keep = tracked()
    for k in keep:                      # always include what you hold
        if k not in names:
            syms.append(k); names[k] = k
    print(f"universe: {len(syms)} tickers (limit {UNIVERSE_LIMIT}), {len(keep)} tracked for bars")

    # SPY first — needed as the beta benchmark
    spy = yf.download("SPY", period="1y", interval="1d", auto_adjust=True,
                      progress=False)["Close"]
    if isinstance(spy, pd.DataFrame):
        spy = spy.iloc[:, 0]
    spy_ret = spy.pct_change().dropna()
    print(f"benchmark SPY: {len(spy)} bars")

    quotes, bars_out, fails = {}, {}, 0
    beta_est = 0
    for i in range(0, len(syms), BATCH):
        chunk = syms[i:i + BATCH]
        try:
            df = yf.download(chunk, period="1y", interval="1d", auto_adjust=True,
                             progress=False, group_by="ticker", threads=True)
        except Exception as e:
            print(f"  batch {i//BATCH+1} failed entirely: {e}")
            fails += len(chunk); continue
        for s in chunk:
            try:
                sub = df[s][["Open", "High", "Low", "Close", "Volume"]].dropna()
                if len(sub) < 60:
                    fails += 1; continue
                c = sub.Close
                adv = float((c * sub.Volume).tail(20).mean())
                last = float(c.iloc[-1])
                if last < MIN_PRICE or adv < MIN_ADV_USD:
                    continue
                atr = wilder_atr(sub.High, sub.Low, c)
                if not np.isfinite(atr) or atr <= 0:
                    continue
                # Beta against SPY from returns. Never ticker.info['beta']: that is one
                # HTTP request per symbol (hours over 2,600 names) and is itself opaque.
                # Variance of the benchmark must be taken over the SAME overlapping dates
                # as the covariance — using the full SPY series as the denominator mixes
                # two different samples and skews beta for any short-history ticker.
                r = c.pct_change().dropna()
                j = r.index.intersection(spy_ret.index)
                beta = None
                if len(j) > 120:
                    sv = float(spy_ret.loc[j].var())
                    if sv > 0:
                        beta = float(np.cov(r.loc[j], spy_ret.loc[j])[0][1] / sv)

                q = {"n": names.get(s, s)[:40], "l": round(last, 4),
                     "a": round(float(atr), 4), "v": int(adv),
                     "h": round(float(c.max()), 4), "o": round(float(c.min()), 4),
                     "d": sub.index[-1].strftime("%Y-%m-%d")}
                if beta is not None and np.isfinite(beta):
                    q["b"] = round(beta, BETA_DP)
                else:
                    beta_est += 1        # no `b` key: the page estimates from ATR%
                quotes[s] = q
                if s in keep:
                    tail = sub.tail(BARS_KEEP)
                    bars_out[s] = [[d.strftime("%Y-%m-%d"), round(float(r_.High), 4),
                                    round(float(r_.Low), 4), round(float(r_.Close), 4)]
                                   for d, r_ in tail.iterrows()]
            except Exception:
                fails += 1
        print(f"  batch {i//BATCH+1}/{(len(syms)-1)//BATCH+1}: "
              f"{len(quotes):,} kept, {fails} skipped, {time.time()-t0:.0f}s")

    if len(quotes) < 100:
        sys.exit(f"only {len(quotes)} tickers survived; refusing to publish")

    fx = None
    try:
        h = yf.Ticker("ILS=X").history(period="5d")["Close"]
        fx = round(float(h.iloc[-1]), 4)
    except Exception:
        pass

    os.makedirs("out/bars", exist_ok=True)
    meta = {"updated": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fx": fx, "count": len(quotes), "tickers": quotes}
    with open("out/quotes.json", "w") as f:
        json.dump(meta, f, separators=(",", ":"))
    for s, b in bars_out.items():
        with open(f"out/bars/{s}.json", "w") as f:
            json.dump({"s": s, "bars": b}, f, separators=(",", ":"))

    qkb = os.path.getsize("out/quotes.json") / 1024
    print(f"\nquotes.json: {len(quotes):,} tickers, {qkb:.0f} KB")
    nonpos = sum(1 for v in quotes.values() if v.get("b", 1) <= 0)
    print(f"beta: {len(quotes)-beta_est:,} measured ({nonpos} of them <= 0, real and kept), "
          f"{beta_est} not measurable — page estimates those from ATR%")
    print(f"bars/: {len(bars_out)} files")
    print(f"fx {fx} · skipped {fails} · {time.time()-t0:.0f}s total")


if __name__ == "__main__":
    main()
