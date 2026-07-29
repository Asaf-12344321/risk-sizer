#!/usr/bin/env python3
"""Publish a compact quote/history file for risk-sizer.html to read.

Runs in GitHub Actions. Yahoo sends no CORS header, so the browser cannot call it
directly; this fetches server-side and commits the result, which raw.githubusercontent.com
does serve cross-origin. The page derives ATR, RSI, 1-month change and each position's
high-since-entry from `bars` itself, so this file needs no knowledge of open positions.
"""
import json, sys, datetime, warnings
warnings.filterwarnings("ignore")
import yfinance as yf

BARS = 260          # ~1 trading year: enough for ATR(14), RSI(14) and a year-old position
OUT = "data.json"


def tickers():
    out = []
    for line in open("tickers.txt", encoding="utf-8"):
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s.upper())
    return sorted(set(out))


def usdils():
    try:
        h = yf.Ticker("ILS=X").history(period="5d")["Close"]
        return round(float(h.iloc[-1]), 4)
    except Exception:
        return None


def main():
    syms = tickers()
    print(f"fetching {len(syms)} tickers: {' '.join(syms)}")
    out = {"updated": datetime.datetime.now(datetime.timezone.utc)
                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
           "fx": usdils(), "tickers": {}}

    hist = yf.download(syms, period="1y", interval="1d", auto_adjust=False,
                       progress=False, group_by="ticker", threads=True)
    ok = fail = 0
    for s in syms:
        try:
            sub = hist[s][["High", "Low", "Close"]].dropna().tail(BARS)
            if len(sub) < 30:
                raise ValueError(f"only {len(sub)} bars")
            bars = [[d.strftime("%Y-%m-%d"), round(float(r.High), 4),
                     round(float(r.Low), 4), round(float(r.Close), 4)]
                    for d, r in sub.iterrows()]
            rec = {"bars": bars, "last": bars[-1][3]}
            try:                                  # metadata is best-effort
                i = yf.Ticker(s).info
                rec["name"] = i.get("shortName") or s
                rec["ccy"] = i.get("currency") or "USD"
                for key, src in (("beta", "beta"), ("avgvol", "averageVolume"),
                                 ("h52", "fiftyTwoWeekHigh"), ("l52", "fiftyTwoWeekLow")):
                    v = i.get(src)
                    if isinstance(v, (int, float)):
                        rec[key] = round(float(v), 4)
            except Exception as e:
                print(f"  {s}: metadata unavailable ({e})")
            out["tickers"][s] = rec
            ok += 1
            print(f"  {s}: {len(bars)} bars, last {rec['last']}, beta {rec.get('beta')}")
        except Exception as e:
            fail += 1
            print(f"  {s}: FAILED — {e}")

    if not out["tickers"]:
        sys.exit("no tickers fetched; refusing to publish an empty file")
    json.dump(out, open(OUT, "w"), separators=(",", ":"))
    import os
    print(f"wrote {OUT}: {os.path.getsize(OUT)/1024:.0f} KB, {ok} ok, {fail} failed, fx {out['fx']}")


if __name__ == "__main__":
    main()
