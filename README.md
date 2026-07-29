# Risk Sizer

**A phone-friendly tool that answers two questions before you buy a stock, and one while you hold it.**

👉 **[Open the tool](https://asaf-12344321.github.io/risk-sizer/)** — then Share → *Add to Home Screen* for an app icon.

- **How much should I buy?**
- **Where does my stop go?**
- **When do I move it?**

No login, no account, nothing to install. Your settings and positions are stored on your own phone and never leave it.

---

## Why it exists

Two mistakes cost most traders more than bad stock picking does:

1. **Selling winners too early** — taking +5% out of a move that goes +40%, because you're afraid of giving the profit back.
2. **Holding losers too long** — a −10% loss becomes −50% while you wait for it to "come back".

Both come from making the exit decision *while you're in the trade*, when you're anxious and invested. This tool moves every decision to **before you buy**, when you're calm, and then gives you a rule to follow instead of a judgement to make.

It is deliberately **not** a stock picker. It has no opinion about what to buy.

---

## Quick start

### 1. Set up (once)

The first time you open it, you'll be asked two things:

| Question | Why |
|---|---|
| **Total capital** | Everything is sized as a fraction of this |
| **Most you'd put in one speculative name** | The tool works backwards from this to a safe loss budget |

That's it. Everything else has a sensible default you can change later under **Your rules**.

### 2. Before you buy

1. Open the **Size a trade** tab
2. Type the ticker → tap **Fill** — price, volatility, beta, volume, 52-week range and momentum all load automatically
3. Pick the **risk tier**: Quality / Growth / Speculative
4. Read the three answers:
   - **Position size** — and which limit is holding it back
   - **Initial stop** — the price you put in your broker *today*
   - **Stop ladder** — where the stop moves as the price rises

### 3. While you hold it

1. Type the ticker in the box at the bottom and tap **Track this position**
2. Open the **My positions** tab whenever you like
3. It shows your current stop for every position, updated from real prices — nothing to calculate

---

## The two screens

### Size a trade

**Look up a ticker** fills everything from live market data in one tap. You can also type any field by hand.

| Input | Where it comes from |
|---|---|
| Entry price, ATR, beta, volume, 52-week range | Filled automatically |
| RSI, 1-month change | Filled automatically — powers the "buying extended" warning |
| Support / resistance | Optional, read off your chart |
| Risk tier | Your judgement — the one real decision |

**Position size** shows every limit that applies and marks the one that binds:

```
· risk-based:        the most you can lose if the stop is hit
▸ thesis-break cap:  what a bad drawdown costs at this size   ← binding
· capital cap:       max % of capital in one name
· beta-exposure cap: stops high-beta names taking a full slot
· liquidity cap:     never bigger than the stock can absorb
```

The **smallest** number wins. Seeing *which* one binds tells you what's actually constraining you.

### My positions

One line per position, all on one screen:

```
ASTS   entry 60.45 · ATR 6.48
       stop now  60.45   at breakeven — cannot lose
       live 57.75 (-4.5%) · high since entry 60.45
       Trailing on its own. Set the stop to 60.45 at your broker.
       [Sold]
```

The stop **advances by itself** as the price makes new highs — it's recalculated from real price history, not from you remembering to update it. If a stop was already breached, it says so plainly.

---

## How the numbers are decided

### The stop comes first, then the size

```
stop distance  =  3 × ATR          (clamped to a sane 8%–30% of price)
position size  =  risk budget ÷ stop distance %
```

This is the important idea: **you can't have both a big position and a survivable stop.** A wider stop means a smaller position. The tool derives the size *from* the stop, not the other way round.

Why 3 × ATR? ATR is how much the stock moves on an average day. A stop closer than about 2.5 × ATR sits *inside* normal daily noise and gets triggered by wiggles rather than by anything meaningful — in backtesting, stops that tight were wrong **80–93% of the time** they fired.

### Support can only widen the stop, never tighten it

If you enter a support level *further* than 3 × ATR, the stop goes just below it. If it's *closer*, the tool ignores it and tells you why — a "support level" inside the noise band isn't support, it's a number on a chart you'll be stopped out at.

### The stop ladder

The ladder is the answer to "when do I move it":

| price reaches | move stop to | if the stop is hit there | why |
|---|---|---|---|
| 60.45 (now) | 42.32 | you lose 30.0% | set this at entry |
| **69.52 (+15%)** | **60.45** | **you break even** | **jumps to breakeven** |
| 80.00 | 63.80 | you make +5.5% | trail |
| 90.00 | 73.80 | you make +22.1% | trail |

Three rules:

1. **The stop only ever moves up.** Never down.
2. **Once the price is +15%, the stop sits at your entry** — from that moment the trade cannot lose you money. This is the rung that matters.
3. **Above that it follows the highest price**, staying a fixed distance behind.

This is what fixes *both* mistakes at once. You stop selling early because the rule holds you in while the price rises. You stop riding losers down because anything that once worked exits at breakeven.

Rungs land on round numbers, because you type them into your broker by hand.

**Two trail styles**, switchable:

- **ATR distance** — the gap narrows in % terms as the price climbs, protecting more of a bigger gain
- **Constant %** — the gap stays the same forever

### The "buying extended" warning

If RSI is above 60, or the stock is up more than 15% in a month, you get a warning. Buying *after* a sharp run tested materially worse than buying into weakness.

---

## Settings — *Your rules*

Everything is editable. Defaults in brackets.

| Setting | Meaning |
|---|---|
| Total capital | Your base for everything |
| Risk budget / trade [2%] | Most you'll lose on a normal stop-out |
| Thesis-break budget | Most you'll lose when things go badly wrong |
| Max position [20%] | Cap on any single name |
| Max beta-weighted exposure [25%] | Keeps high-beta names small |
| Gap scenario — quality / growth / speculative [20 / 35 / 55%] | How far each tier can realistically fall |
| Max % of avg volume [5%] | Never bigger than you can exit |
| Initial stop [3 × ATR] | How far the first stop sits |
| Trail distance [2.5 × ATR] | How far behind the high the stop follows |
| Arm trail at [+15%] | When the stop jumps to breakeven |
| Min / max stop [8% / 30%] | Sanity bounds |
| Warn if RSI above [60] | Extended-entry warning |
| Warn if 1-month run-up > [15%] | Extended-entry warning |
| Drawdown warn [40% off high] | Flags deep drawdowns |
| Max holding days [90] | Review reminder |
| Default USD/ILS | Fallback if the live rate is unavailable |

**Reset & redo setup** clears everything and starts over.

---

## Where the data comes from

**Yahoo Finance daily bars**, and nothing else.

Yahoo doesn't allow web pages to call it directly (no CORS header), so a scheduled GitHub Action fetches it server-side and publishes a small data file the page can read:

```
GitHub Action (every 30 min, market hours)
   └─ fetch_data.py  → reads tickers.txt, pulls ~1 year of daily bars
        └─ publishes data.json to the `data` branch
             └─ the page reads it and computes the rest on your phone
```

ATR, RSI, the 1-month change and each position's high-since-entry are all calculated **in your browser** from those bars. The feed knows nothing about your positions, your capital, or your trades.

### Checking the numbers yourself

Every lookup states its own provenance:

> **ASTS · AST SpaceMobile, Inc.** — filled from the close of **2026-07-29** (57.75), **not a live price**.
> ATR is Wilder/RMA(14) — set your chart's ATR smoothing to RMA to compare. RSI is Wilder(14). 1-month change is calendar.
> Source: Yahoo Finance daily bars, 252 of them, published 12 min ago.

So if a number looks wrong you can check *exactly* what it was derived from. Common reasons a chart disagrees:

| Symptom | Cause |
|---|---|
| Price differs during the day | The tool uses the last daily **close**, not a live quote |
| ATR differs by ~10–20% | Your chart's ATR smoothing is SMA; this uses **RMA/Wilder** (TradingView's default) |
| 1-month change differs | This uses a **calendar** month, some tools use 22 trading days |

### Adding a ticker

Edit [`tickers.txt`](tickers.txt) on github.com — this works fine from a phone. One symbol per line; the next scheduled run picks it up. Israeli stocks use the `.TA` suffix (e.g. `LUMI.TA`).

If a tracked ticker isn't in the feed, the tool says so and falls back to manual mode with a **✓ Reached** button, so nothing breaks.

---

## Honest limitations

- **Daily closes, not real-time.** Fine for multi-week holds; useless for day trading.
- **It cannot tell you what to buy.** No screening, no signals, no opinion.
- **The parameters are reasonable, not optimal.** They were checked against a real trade history, but that's a limited sample from one market period. The *mechanisms* (an initial stop beats none; a breakeven-floored trail beats discretion) held up consistently. The exact numbers — 3× ATR, +15%, 2.5× ATR — sit on a broad plateau where nearby values work about as well. Don't treat them as precise.
- **No stop can separate a dip from a disaster.** In testing, a stock that fell 46% and then doubled looked identical, at the moment of the stop, to one that fell 54% and never recovered. This is why position sizing matters more than stop placement: size it so the bad case is survivable, because you won't be able to tell which one you're in.
- **A stop will be wrong most of the times it fires.** That's the premium you pay for the occasional time it saves you from a disaster. Expect it and don't abandon the rule over it.
- **Your win rate will fall while you make more money.** Letting winners run means more trades ending at breakeven. It feels worse. That's the hardest part of using this.

---

## What's in the repo

| File | Purpose |
|---|---|
| `index.html` | The whole tool — one self-contained file, no dependencies |
| `fetch_data.py` | Fetches quotes and history from Yahoo |
| `tickers.txt` | Your watchlist — **edit this to add stocks** |
| `requirements.txt` | Pinned Python dependencies |
| `.github/workflows/data.yml` | The 30-minute schedule |

The `data` branch holds only the published `data.json` snapshot.

---

## Troubleshooting

**"Market data unavailable"** — the feed couldn't be reached. Type the numbers by hand; nothing is blocked. Check the [Actions tab](../../actions) for a failed run.

**Feed age shown in amber** — the data is over 24 hours old, so a scheduled run is probably failing.

**A ticker won't fill** — it's not in `tickers.txt` yet.

**Settings disappeared** — they live in your browser's local storage. Clearing site data, or a private-browsing tab, will lose them.

---

*This tool suggests position sizes and stop levels from rules you set yourself. It is not financial advice, and every number it produces is a starting point for your own judgement.*
