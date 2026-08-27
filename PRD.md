# Risk Sizer — Product Requirements

| | |
|---|---|
| **Version** | 2.0 |
| **Date** | 2026-08-21 |
| **Component** | `index.html` + local `app.py` API + `quant_risk_engine.py` + `fetch_data.py` |
| **Live** | https://asaf-12344321.github.io/risk-sizer/ |
| **Repo** | https://github.com/Asaf-12344321/risk-sizer |

Requirements are numbered for traceability. Every one is written to be independently
verifiable — where a value is computable, the formula and a worked example are given.
Where behaviour is a judgement call rather than a derived fact, it is marked
**[CALIBRATION]** and should not be asserted as correctness.

---

## 1. Purpose and scope

### 1.1 Problem
A discretionary swing trader picks stocks well but loses the gains to two exit errors:
selling winners early (measured: winners ran a further **+38.5%** on average after being
sold) and holding losers indefinitely (measured: **8 of 8** open positions underwater,
mean **−26.4%**). Net effect over 20 months: realised gains were almost exactly offset by unrealised
losses on positions still held — approximately flat.

### 1.2 What the product does
Moves every exit decision to **before entry**, when the user is not emotionally
committed, and expresses it as numbers to be executed rather than judgements to be made.

It answers four questions:
1. **How much do I buy?** (₪ and share count)
2. **Where does the initial stop go?**
3. **When do I move the stop, and to what?**
4. **Does this trade improve or over-concentrate my combined Core + Active portfolio?**

### 1.3 Explicit non-goals
| ID | Not in scope |
|---|---|
| NG-1 | Stock selection, screening, or any buy/sell signal |
| NG-2 | Beating a market benchmark. Measured alpha vs passive holding is **negative** (−0.104R at 45 days); the product is a variance-reduction and discipline tool |
| NG-3 | Real-time or intraday data. Daily closes only |
| NG-4 | Order execution or broker integration |
| NG-5 | Multi-user accounts, cloud sync, or remote server-side state |
| NG-6 | Short positions, options, futures, FX, crypto |
| NG-7 | Automatic sizing feeds for non-US equities. TASE holdings may participate in the Core risk model through Yahoo `.TA` symbols |

---

## 2. Users and context

**Single user**, self-directed retail trader, primary device **iPhone (Safari, added to
Home Screen as a PWA)**, secondary desktop browser. Broker is **Leumi Trade, which
supports no trailing-stop order type** — hence FR-LAD-*, which exists to make manual
stop management tractable.

Capital and risk parameters are personal and must never leave the device (see NFR-PRIV-*).

---

## 3. Glossary

Precise definitions matter more than usual here, because several terms differ from
common usage.

| Term | Definition |
|---|---|
| **ATR** | Wilder's Average True Range over 14 daily bars, in price units |
| **ATR%** | `ATR / price × 100` — volatility as a percentage of price |
| **1R** | The shekel amount lost if the initial stop is hit. The unit of risk |
| **Stop distance** | `price − stopPrice`, expressed as a % of price |
| **Initial stop** | The stop placed at entry, before any favourable move |
| **Armed** | The position has traded at or above `entry × (1 + armpct/100)`. At that point the stop moves to the entry price |
| **Breakeven floor** | Once armed, the stop may never fall below the entry price |
| **Ladder** | The precomputed table of (price level → new stop level) |
| **Crash assumption** | The adverse move the position is sized to survive. Derived from ATR%, **not** chosen by the user |
| **Crash budget** | The maximum shekel loss accepted in a crash scenario. `breakbudget` |
| **Binding constraint** | Whichever size cap produced the smallest number, and therefore set the position size |
| **Band** | A human label for volatility (`very calm`…`wild`). Display only; the maths uses continuous ATR% |
| **Core portfolio** | Static, long-term holdings stored in the server's SQLite `core_portfolio` table with current ILS values |
| **Active sleeve** | Dynamic momentum positions stored in SQLite `active_positions`, including quantity, currency, value and risk status |
| **Weighted portfolio correlation** | Pearson correlation between the proposed ticker's ILS-adjusted return and the current value-weighted Core + Active return |
| **Historical VaR** | Conservative observed 1st-percentile one-day loss from 500 aligned ILS return observations; reported as a positive ILS risk amount |

---

## 4. Architecture

```
GitHub Actions (cron */30, 13–21 UTC, Mon–Fri)
   └─ fetch_data.py
        ├─ reads universe.csv (SEC ticker list, committed) + tickers.txt (held names)
        ├─ pulls 1y daily bars from Yahoo via yfinance
        ├─ computes ATR(14), beta vs SPY, ADV, 52w range server-side
        └─ force-pushes single-commit `data` branch:
              quotes.json          — all liquid tickers, metrics only  (~300 KB)
              bars/{TICKER}.json   — 1y bars, only for held tickers    (~8 KB each)

app.py  (FastAPI process on the private VM)
   ├─ serves index.html at the same HTTPS origin
   ├─ authenticates state/risk APIs with X-API-Key
   ├─ provides CRUD for core_portfolio, active_positions and app_settings
   └─ POST /api/risk/evaluate
        ├─ reads and merges Core + Active holdings directly from SQLite
        ├─ QuantitativeRiskEngine fetches adjusted prices/FX in one yfinance batch
        └─ returns approval + correlation, MPT variance and 99% historical VaR

index.html
   ├─ reads quotes.json cross-origin  → sizing calculator
   ├─ reads bars/{T}.json on demand   → position tracker
   ├─ loads and mutates all durable state through the REST API
   └─ fails closed until the server-side quantitative risk gate approves the trade

data/risk_sizer.db (configurable with RISK_SIZER_DB_PATH)
   ├─ core_portfolio
   ├─ active_positions
   └─ app_settings (singleton settings/setup state)
```

**Rationale (do not "simplify" away):** Yahoo sends no `access-control-allow-origin`
header, so the browser cannot call it directly. `raw.githubusercontent.com` does send
`*`. Bars are split per-ticker because whole-market bars would be ~50 MB.
The SQLite file is gitignored and remains on the private VM, so personal holdings never
enter the public repository or the scheduled market-data feed. HTTPS and an API key are
required remotely; CORS is only a browser-origin policy, not authentication.

---

## 5. Functional requirements

### 5.1 First-run setup — `FR-SET`

| ID | Requirement | Priority |
|---|---|---|
| FR-SET-01 | On first load, if setup has not been completed, the app **shall** display only the setup card and hide the calculator, positions tab and rules strip | Must |
| FR-SET-02 | Setup **shall** require exactly two inputs: **Total capital (₪)** and **Most you'd put in one speculative name (₪)** | Must |
| FR-SET-03 | The Save button **shall** remain disabled until `capital > 0` AND `maxspec > 0` AND `maxspec ≤ capital` | Must |
| FR-SET-04 | When `maxspec > capital`, the app **shall** display "That's more than your total capital." | Must |
| FR-SET-05 | On save, `breakbudget` **shall** be computed as `maxspec × gapt5 / 100` and persisted | Must |
| FR-SET-06 | Before setup completes, all size/stop outputs **shall** show a placeholder (`₪ —`) and never a number | Must |
| FR-SET-07 | Setup completion **shall** persist across reload | Must |
| FR-SET-08 | "Reset & redo setup" **shall** clear settings, clear the setup flag, and return to the setup card | Must |
| FR-SET-09 | Settings present but the setup flag absent **shall** force setup to re-run (guards against stale placeholder values being trusted) | Must |

**Worked example:** capital `100000`, maxspec `6000` → `breakbudget = 6000 × 58/100 = 3,480`.

### 5.2 Ticker lookup and market data — `FR-FEED`

| ID | Requirement | Priority |
|---|---|---|
| FR-FEED-01 | On load, the app **shall** fetch `quotes.json` and report ticker count and data age | Must |
| FR-FEED-02 | A lookup **shall** populate: price, ATR, beta, and internally retain name, ADV, 52w high, 52w low, as-of date | Must |
| FR-FEED-03 | Lookup **shall** be case-insensitive | Must |
| FR-FEED-04 | A ticker absent from the feed **shall** produce an explanatory message naming the likely causes (illiquid / ETF / newly listed) and **shall not** block manual entry | Must |
| FR-FEED-05 | Feed unreachable **shall** degrade to manual entry with an explicit notice; the app **shall not** silently use stale or partial data | Must |
| FR-FEED-06 | Data older than 24h **shall** be visually flagged as stale | Should |
| FR-FEED-07 | The stock summary line **shall** state: ticker, name, price, as-of date, ATR (absolute and %), beta, band, and crash assumption | Must |
| FR-FEED-08 | The summary **shall** state that the price is a **close, not a live quote** | Must |
| FR-FEED-09 | `bars/{TICKER}.json` **shall** be fetched only for tracked positions, only when the positions tab is opened, and cached for the session | Must |
| FR-FEED-10 | Absence of `fetch` **shall** be handled without throwing | Must |
| FR-FEED-11 | Feed values **shall** reach the form at full published precision. The app **shall not** round ATR or price on the way in — see DEF-006 | Must |
| FR-FEED-12 | The summary line **shall** distinguish a measured beta from an estimated one, marking the latter `(est)` | Must |

### 5.3 Manual entry — `FR-IN`

| ID | Requirement | Priority |
|---|---|---|
| FR-IN-01 | Manual inputs **shall** be collapsed by default and expandable | Must |
| FR-IN-02 | Price, ATR, currency, FX and beta **shall** be manually enterable, overriding feed values | Must |
| FR-IN-03 | The USD/ILS field **shall** be visible only when currency is USD | Must |
| FR-IN-04 | With currency USD and no FX entered, the app **shall** fall back to the `fx` setting | Must |
| FR-IN-05 | Recalculation **shall** occur on every input event, with no explicit submit | Must |

### 5.4 Position sizing — `FR-SIZE` *(core)*

**FR-SIZE-01** — 1R **shall** be:
```
1R = riskabs > 0 ? riskabs : capital × riskpct / 100
```
An absolute `riskabs` **shall** take precedence, so risk per trade does not drift when
capital changes.

**FR-SIZE-02** — The crash assumption **shall** be a **continuous, piecewise-linear**
function of ATR%, interpolated over eight anchors:

| Anchor | ATR% | Crash % (setting) | Default |
|---|---|---|---|
| 1 | ≤ 1.0 | `gapt1` | 22 |
| 2 | 2.0 | `gapt2` | 25 |
| 3 | 3.25 | `gapt3` | 34 |
| 4 | 5.0 | `gapt4` | 44 |
| 5 | 7.0 | `gapt5` | 51 |
| 6 | 9.0 | `gapt6` | 56 |
| 7 | 11.0 | `gapt7` | 60 |
| 8 | ≥ 13.5 | `gapt8` | 63 |

Values outside the range clamp to the nearest anchor. **It shall not be a step
function** — see DEF-001.

**FR-SIZE-03** — Position size **shall** be the minimum of all applicable candidates:

| Candidate | Formula | Applicable when |
|---|---|---|
| risk-based | `1R / (riskStopPct/100)` — **not** `stopPct`, see FR-SIZE-03a | always |
| crash cap | `breakbudget / (crashPct/100)` | always |
| capital cap | `capital × maxpct/100` | always |
| beta cap | `(capital × betaexpct/100) / betaForSizing()` | **always** — see FR-SIZE-12 |
| liquidity cap | `ADV_nis × liqpct/100` | `avgvol > 0` |

**FR-SIZE-03a** — The tool **shall** carry **two distinct stop percentages**, and they
**shall not** be conflated:

| Name | Formula | Used for |
|---|---|---|
| `stopPct` | `clamp(stopPct_raw, minstop, maxstop)` | **where the order sits** — the stop price, the ladder, ₪ at risk |
| `riskStopPct` | `clamp(stopPct_raw, minstop, 100)` — floor applied, **ceiling not** | **how big the position is** — the risk-based size candidate |

They are equal whenever `stopPct_raw ≤ maxstop`, i.e. for ATR% up to 12% at the default
`initmult`. Above that they diverge, and the divergence is deliberate: capping the placed
order at 30% limits where the order sits, it does nothing to stop the price falling 2.5×
ATR. Sizing off the capped figure would assume protection the stop cannot deliver — that
was DEF-005, which made every name above ~10% ATR the same size.

Consequences that **shall** hold:

- `riskStopPct ≥ stopPct` always, so realised ₪ at risk (FR-STOP-05) never exceeds 1R.
- When they diverge, the position is **smaller** than `1R / stopPct` would give, and
  FR-STOP-06 **shall** warn that the stop will be tested.
- **1R is not a single number in this tool.** `1R` as a *budget* divides by
  `riskStopPct`; `1R` as a *distance to the placed stop* is `entry − stopPrice`. Any
  consumer that converts outcomes to R — including the `riskml` label simulator and any
  future Risk Scout handoff — **shall** state which of the two it used. Reconciling a
  capital-scaled result against an R-scaled one without that statement is invalid for
  every name above 12% ATR.

**FR-SIZE-04** — The binding constraint **shall** be named in the UI whenever it is not
`risk-based`.

**FR-SIZE-05** — Share count **shall** be `(currency == USD ? size/fx : size) / price`.

**FR-SIZE-06** — Outputs **shall** include size in ₪, share count, and size as % of capital.

**FR-SIZE-07** — A collapsed disclosure **shall** list every candidate with its value and
derivation, mark the binding one, and state the crash cost at the chosen size.

**FR-SIZE-12** — The beta cap **shall always be applied**. It **shall not** be skipped
when beta is absent or non-positive: skipping it *removes* a constraint, which is the
wrong direction for a missing input. Three cases, which **shall** be distinguished
because they are not the same fact (see DEF-007):

| Case | Beta used | Reported as |
|---|---|---|
| Measured, `> 0` | as measured | plain |
| Measured, `≤ 0` | floored at `BETA_MIN` (0.10) | *"measured beta X ≤ 0, floored — no market exposure to cap"* |
| Not measurable | `betaFallback(ATR%)` | *"estimated from ATR — beta not measurable"* |

A measured beta `≤ 0` is normally **real, not noise** — WMT −0.10, JNJ −0.21, XOM −0.48
against a tech-led SPY. A stock with no positive market co-movement adds no market
exposure, so a market-exposure cap correctly does not bind on it; the floor exists only
to keep the arithmetic positive and finite. It **shall not** be labelled "estimated".

**FR-SIZE-13** — `betaFallback(atrPct)` **shall** be
`clamp(0.235 + 0.232 × atrPct, 1.0, 6.0)`, and **shall** be clamped at both ends. Fitted
on the 2,579 universe tickers that have a measurable beta (band-median beta regressed on
ATR%, corr 0.946); the cap of 6.0 is where the fit leaves its support, the highest beta
measured anywhere being 5.40. Unclamped it reached beta 2.3e8 at ATR 1e9%, driving the
cap to ~0 and making the tool refuse to size at all. **[CALIBRATION]**

**Worked example** — all preconditions stated explicitly, because the share count depends
on currency and FX and a partially-specified example is untestable.

> **Given** `capital = 100000`, `riskabs = 1300`, `maxspec = 6000` (→ `breakbudget = 3480`),
> all other settings at default, **currency = USD**, `fx = 3.0724`
> **When** `price = 338.19`, `ATR = 8.02`, `beta = 0.845`

```
ATR%       = 8.02 / 338.19 × 100                    =   2.371 %
crashPct   = interp(2.371) between anchors 2.0→25, 3.25→34
           = 25 + (0.371/1.25) × 9                  =  27.67 %
stopPct    = clamp(2.5 × 2.371, 8, 30)              =   8.0  %   (min floor binds)
stopPrice  = 338.19 × (1 − 0.08)                    = 311.13

candidates:
  risk-based    = 1300 / 0.08                       =  16,250
  crash cap     = 3480 / 0.2767                     =  12,577   ← BINDING
  capital cap   = 100000 × 0.20                     =  20,000
  beta cap      = (100000 × 0.25) / 0.845           =  29,586
  liquidity cap = not applicable (manual entry, no ADV)

→ size            12,577 ₪
→ % of capital    12.6 %
→ ₪ at risk        1,006
```

**Note for test design:** share count depends on currency and FX; drive the suite via `QA_CAPITAL` / `QA_MAXSPEC` / `QA_RISK`.

### 5.5 Initial stop — `FR-STOP`

| ID | Requirement | Priority |
|---|---|---|
| FR-STOP-01 | `stopPct_raw = initmult × ATR / price × 100` | Must |
| FR-STOP-02 | `stopPct = clamp(stopPct_raw, minstop, maxstop)` — this is the **placed order** only | Must |
| FR-STOP-02a | `riskStopPct = clamp(stopPct_raw, minstop, 100)` — floor applied, ceiling **not**. This is the sizing denominator and **shall not** be substituted with `stopPct` (FR-SIZE-03a, DEF-005) | Must |
| FR-STOP-03 | `stopPrice = price × (1 − stopPct/100)`, and **shall** satisfy `0 < stopPrice < price` | Must |
| FR-STOP-04 | The stop line **shall** state price, distance %, and ₪ at risk | Must |
| FR-STOP-05 | ₪ at risk **shall not** exceed 1R (except by display rounding) | Must |
| FR-STOP-06 | When `stopPct_raw > maxstop`, a **warning** **shall** state ATR%, the uncapped distance, and that the stop will be tested | Must |
| FR-STOP-07 | When `stopPct_raw < minstop`, a **neutral note** **shall** state the stop was widened to the floor | Should |

### 5.6 Stop ladder — `FR-LAD`

| ID | Requirement | Priority |
|---|---|---|
| FR-LAD-01 | The ladder **shall** show price levels and the stop to move to at each | Must |
| FR-LAD-02 | Row 1 **shall** be the entry price with the initial stop, marked "set today" | Must |
| FR-LAD-03 | The arming row **shall** be at `price × (1 + armTrigger/100)` where `armTrigger = max(armpct, armatrmult × ATR%)`, with stop = entry price, marked as protected. A flat threshold arms inside the daily noise on high-ATR names | Must |
| FR-LAD-04 | Above arming, `stop = max(entry, min(level − trailmult×ATR, level))` | Must |
| FR-LAD-05 | Stops **shall** be monotonically non-decreasing down the table | Must |
| FR-LAD-06 | No stop **shall** exceed its own trigger level | Must |
| FR-LAD-07 | Rows repeating the previous stop **shall** be suppressed | Should |
| FR-LAD-08 | Price levels **shall** fall on round numbers (1/2/2.5/5/10 × power of ten) | Should |
| FR-LAD-09 | The ladder **shall** be capped at 7 rows | Should |
| FR-LAD-10 | Wide tables **shall** scroll horizontally without the page scrolling | Must |
| FR-LAD-08 | Arming **shall** be judged on the bar's **intraday high**; the trail **shall** continue to ride the **close**. The two references **shall** remain independent | Must |

### 5.7 Warnings — `FR-FLAG`

| ID | Requirement | Priority |
|---|---|---|
| FR-FLAG-01 | At most **2** flags **shall** be displayed | Must |
| FR-FLAG-02 | Flags that change a decision **shall** be coloured; context-only flags **shall** be neutral grey | Should |
| FR-FLAG-03 | A drawdown greater than `drawdown`% below the 52-week high **shall** produce a neutral note | Should |
| FR-FLAG-04 | The app **shall not** warn against high-RSI or extended entries — falsified on 271,464 trades (E[R] flat; RSI>70 marginally better) | Must |
| FR-FLAG-05 | When a ticker has been looked up, an ATR differing from the fetched daily ATR by more than `FEED_ATR_TOLERANCE` (40%) **shall** be flagged as a likely **intraday** value, naming the fetched figure and the approximate factor. The check **shall** be suppressed once entry price departs the quoted close by more than 25%, since the figures then belong to a different stock | Must |
| FR-FLAG-06 | With no lookup to compare against, an ATR below `ATR_PCT_IMPLAUSIBLE` (0.20% of price) **shall** carry the same warning. The threshold **shall** sit below the lowest current daily ATR% in the live universe (0.267%, GBTG) while remaining above the 0.134% hand-entered intraday regression case | Must |
| FR-FLAG-07 | Neither check **shall** fire on a manual override within ±19%, the largest legitimate disagreement between Wilder and simple-mean ATR | Must |

### 5.8 Position tracking — `FR-POS`

| ID | Requirement | Priority |
|---|---|---|
| FR-POS-01 | A position **shall** be addable from the calculator, requiring a ticker, price and ATR | Must |
| FR-POS-02 | Stored fields **shall** be: ticker, entry price, ATR, rung index, added date. **No** price history | Must |
| FR-POS-03 | Tickers **shall** be upper-cased and HTML-escaped before rendering | Must |
| FR-POS-04 | The tab label **shall** show the open position count | Should |
| FR-POS-05 | With bars available, the current stop **shall** be derived by replaying the ladder over real bars since the entry date | Must |
| FR-POS-06 | A stop already breached **shall** be reported explicitly, with the date and price | Must |
| FR-POS-06a | The reported fill **shall** be **stop-first and open-first**: if the bar opened at or below the active stop, the fill is the **open** and **shall** be labelled a gap; otherwise if the low reached the stop, the fill is the stop price. A bar that both breaches the stop and clears the arm trigger **shall** exit at the old stop — daily OHLC cannot order the high against the low, so the already-active stop wins (see DEF-011) | Must |
| FR-POS-06b | Against a bar with no open field (a feed published before FR-DATA-05a), the tracker **shall** fall back to the stop-price fill rather than dropping the breach | Must |
| FR-POS-07 | With bars available, live price, P&L% and high-since-entry **shall** be shown | Must |
| FR-POS-08 | Without bars, the app **shall** fall back to manual rungs with a "Reached" advance button and explain why | Must |
| FR-POS-09 | Rung index **shall** be advanceable and reversible, clamped to the ladder bounds | Must |
| FR-POS-10 | "Sold" **shall** remove the position | Must |
| FR-POS-11 | Positions **shall** persist across reload | Must |
| FR-POS-12 | Position state **shall** survive a settings change without corruption | Must |

### 5.9 Settings and persistence — `FR-CFG`

| ID | Requirement | Priority |
|---|---|---|
| FR-CFG-01 | All parameters in §7 **shall** be user-editable | Must |
| FR-CFG-02 | Changes **shall** apply immediately and persist | Must |
| FR-CFG-03 | Long money fields (capital, crash budget, 1R) **shall** display thousands separators as typed, preserving caret position | Must |
| FR-CFG-04 | Non-numeric settings input **shall** be ignored, leaving the prior value | Must |
| FR-CFG-05 | Persisted settings **shall** merge over defaults, so a new parameter gains its default without wiping the rest | Must |
| FR-CFG-06 | Legacy values **shall** migrate (currently `maxstop 45→30`, `minstop 5→8`) | Must |
| FR-CFG-07 | Settings and setup state **shall** persist in the SQLite `app_settings` singleton; the browser **shall not** use persistent client storage | Must |
| FR-CFG-08 | The Settings modal **shall** expose total capital, absolute/percentage 1R, 99% daily VaR, forward-looking Risk-On R budget, crash caps, and volatility/stop parameters | Must |
| FR-CFG-09 | Trade evaluation **shall** read VaR and Risk-On limits from SQLite; the client shall not be permitted to override those gate parameters in an evaluation request | Must |

### 5.10 Data pipeline — `FR-DATA`

| ID | Requirement | Priority |
|---|---|---|
| FR-DATA-01 | The job **shall** run every 30 min, 13–21 UTC, Mon–Fri, and support manual dispatch | Must |
| FR-DATA-02 | It **shall** read the universe from `universe.csv` and held tickers from `tickers.txt` | Must |
| FR-DATA-03 | It **shall** exclude tickers with price `< $3` or ADV `< $5M` | Must |
| FR-DATA-04 | ATR(14) **shall** be computed from explicit OHLC True Range with **Wilder smoothing over the full ~1y series** — never from a vendor summary metric, never over a short window, and never as a simple mean of TR. Verified equal to textbook Wilder to 0.000% at ~250 bars; a 30-bar window drifts −4.3% to +4.3% and a simple TR mean differs by up to 15% (QBTS 1.4254 vs 1.6793) | Must |
| FR-DATA-04a | Beta **shall** be 1y daily returns vs SPY, with the benchmark variance taken over the **same overlapping dates** as the covariance (see DEF-008) | Must |
| FR-DATA-04b | The feed **shall** publish beta **as measured**, including zero and negative values, and **shall** omit the field only when beta could not be computed. Substitution policy lives solely in the page (FR-SIZE-12), so the two sides cannot half-apply it | Must |
| FR-DATA-05 | It **shall** publish `quotes.json` for the full universe and `bars/{T}.json` only for `tickers.txt` entries | Must |
| FR-DATA-05a | Each bar **shall** be `[date, high, low, close, open]`, in that order. Open is **appended, never inserted**: the deployed page indexes bars positionally, so a new field at the end reaches old clients without breaking them. Open is what makes a gap fill priceable (FR-POS-06a) | Must |
| FR-DATA-06 | It **shall refuse to publish** if fewer than 100 tickers survive | Must |
| FR-DATA-07 | It **shall** force-push a single-commit `data` branch, keeping snapshots out of `main` history | Must |
| FR-DATA-08 | It **shall** complete within the job timeout (25 min; observed ~163 s for 3,000 tickers) | Must |
| FR-DATA-09 | It **shall not** transmit personal contact details or financial parameters | Must |

### 5.11 Holistic quantitative risk — `FR-QUANT`

| ID | Requirement | Priority |
|---|---|---|
| FR-QUANT-01 | The SQLite `core_portfolio` table **shall** store ticker, current `value_ils`, currency, and optional FX ticker for every Core holding | Must |
| FR-QUANT-02 | The SQLite `active_positions` table **shall** persist entry, ATR, quantity, currency, FX, `value_ils`, ladder state, opened date, and `RISK_ON`/`ARMED_ZERO_RISK` status | Must |
| FR-QUANT-03 | The backend **shall** read Core and Active holdings directly from SQLite, merge them, and aggregate duplicate ticker/currency pairs before evaluation; clients shall not supply existing holdings | Must |
| FR-QUANT-04 | Correlation and covariance **shall** use the most recent 90 aligned daily ILS-adjusted returns and shall not forward-fill exchange holidays | Must |
| FR-QUANT-05 | Weighted correlation **shall** be `corr(r_new, Σ w_i r_i)`; `ρ > 0.75` shall raise a Correlation Warning | Must |
| FR-QUANT-06 | Current and proposed daily portfolio variance **shall** be calculated as `wᵀΣw`; a relative increase above 20% shall raise a Variance Warning | Must |
| FR-QUANT-07 | Historical 99% one-day VaR **shall** use 500 aligned ILS P&L observations and NumPy's conservative observed `lower` quantile | Must |
| FR-QUANT-08 | Proposed VaR above `maxdailyvar` (default ₪25,000) **shall reject** the trade | Must |
| FR-QUANT-09 | Correlation and variance warnings **shall block** approval by default; the Track action shall remain disabled unless the verdict matches the current ticker and size | Must |
| FR-QUANT-10 | The dashboard **shall** display verdict, weighted correlation, variance change, incremental 99% VaR in ILS, and every blocking reason | Must |
| FR-QUANT-11 | Market-data failure or an empty combined portfolio **shall fail closed** | Must |
| FR-QUANT-12 | Authenticated GET/POST/PUT/DELETE endpoints shall manage both portfolio tables; authenticated GET/PUT shall manage settings | Must |
| FR-QUANT-13 | Active positions may be marked `legacy`; legacy capital remains in Pearson, covariance, variance, and historical VaR calculations but does not consume a forward-looking 1R slot | Must |
| FR-QUANT-14 | When the existing portfolio already breaches the VaR ceiling, a proposed trade is rejected for VaR only if it remains above the ceiling **and increases** VaR; a risk-reducing proposal may proceed to the other gates | Must |
| FR-QUANT-15 | Core and Active records may store a friendly `display_name` independently of the Yahoo ticker | Should |

---

## 6. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-PERF-01 | Calculator **shall** respond to input within 100 ms |
| NFR-PERF-02 | `quotes.json` **shall not** exceed 1 MB |
| NFR-PERF-03 | The calculator shall remain responsive while quantitative evaluation runs asynchronously; identical historical-data requests shall be cached for 15 minutes |
| NFR-PRIV-01 | Core, Active, and settings state shall remain in server-side SQLite; no durable portfolio or settings state shall be written to browser storage |
| NFR-PRIV-02 | The public repo **shall not** contain personal financial figures or contact details |
| NFR-PRIV-03 | SQLite database, WAL, SHM, backup files, and API secrets **shall not** be committed |
| NFR-SEC-01 | Production state and risk APIs shall require `X-API-Key` and shall be served only through HTTPS |
| NFR-OPS-01 | The SQLite database shall support consistent online backup and documented daily retention via cron |
| NFR-COMPAT-01 | **Shall** work in iOS Safari, Chrome, Firefox on current versions |
| NFR-COMPAT-02 | **Shall** install as a PWA via Add to Home Screen with a standalone icon |
| NFR-A11Y-01 | **Shall** render correctly in light and dark themes |
| NFR-A11Y-02 | Numeric inputs **shall** present an appropriate mobile keyboard, including a minus sign where negatives are valid |
| NFR-A11Y-03 | Interactive elements **shall** expose focus states and correct ARIA attributes |
| NFR-ROBUST-01 | No input, setting or feed state **shall** cause `NaN`, `Infinity` or `undefined` to render |
| NFR-ROBUST-02 | Failures **shall** be visible and explained, never silent |

---

## 7. Configuration reference

| Setting | Default | Meaning | Notes |
|---|---|---|---|
| `capital` | 0 | Total trading capital (₪) | Setup required |
| `riskabs` | 0 | 1R in ₪ | Overrides `riskpct` when > 0 |
| `riskpct` | 2 | 1R as % of capital | Used only when `riskabs = 0` |
| `breakbudget` | 0 | Max ₪ lost in a crash | Derived at setup |
| `maxpct` | 20 | Max position as % of capital | **[CALIBRATION]** |
| `betaexpct` | 25 | Max beta-weighted exposure % | **[CALIBRATION]** |
| `gapt1..gapt8` | 22/25/34/44/51/56/60/63 | Crash % anchors at ATR 1/2/3.25/5/7/9/11/13.5% | Measured p96 drawdowns |
| `liqpct` | 5 | Max % of ADV | Never binds in practice |
| `initmult` | 2.5 | Initial stop in ATR multiples | Out-of-sample grid: +0.341 mean OOS Sharpe vs +0.137 at 3.0x |
| `trailmult` | 3.5 | Trail distance in ATR multiples | Out-of-sample grid: +0.273 vs +0.206 at 2.5x |
| `armpct` | 15 | **Floor** for the arming trigger | Earlier tested worse on calm names |
| `armatrmult` | 3.0 | Arm at `max(armpct, this × ATR%)`. 0 disables scaling | +48% on ATR>9% names |
| `minstop` | 8 | Min stop % | Validated better than 5 |
| `maxstop` | 30 | Max stop % | |
| `drawdown` | 40 | Drawdown note threshold % | |
| `fx` | 3.65 | USD/ILS fallback | |
| `holddays` | 90 | Warn when a position exceeds this age | Evidenced: 90+ days was the only losing bucket |

---

## 8. Failure modes and expected degradation

| Condition | Required behaviour |
|---|---|
| Feed unreachable | Notice shown; manual entry fully functional |
| Feed stale (>24h) | Visual staleness flag; values still usable |
| Ticker missing | Explanatory message; manual entry offered |
| Bars missing for a held position | Manual rung mode with explanation |
| SQLite/API unavailable | State mutations and risk approval fail closed with a visible error; existing database remains unchanged |
| Setup incomplete | No numeric output of any kind |
| Any setting set to 0 | No `NaN`/`Infinity` rendered |
| Scheduled job fails | Feed ages; staleness flag is the user-visible signal |

---

## 9. Known defects

| ID | Severity | Description | Detected by |
|---|---|---|---|
| **DEF-001** | *Fixed* | Crash assumption was a step function; 2,607 tickers produced only 5 distinct sizes | User |
| **DEF-002** | *Fixed* | `SETUP_KEY` was `riskSizerSetup_v3` while other keys were `_v4`. Aligned to `_v4`, with migration so no existing user is sent back through setup | PRD review |
| **DEF-003** | *Fixed* | `holddays`, `rsiwarn`, `runupwarn` appeared in settings but no logic read them. `rsiwarn`/`runupwarn` removed (the warning they drove was falsified); `holddays` retargeted to a position-age warning, defaulting to **90 days** — the only holding bucket that lost money in the trade history | PRD review |
| **DEF-004** | *Fixed* | Sub-dollar prices rendered the stop as `0.00`. Price formatting is now adaptive: 4dp below $1, 3dp below $10, 2dp above | QA edge suite |

| **DEF-005** | *Fixed* | Capping the placed stop at 30% also capped the risk used for **sizing**, so every name above ~10% ATR received an identical position. Placing the order at 30% does not prevent the price falling `initmult` × ATR (3× when this was found, 2.5× today). Sizing now uses the uncapped volatility-implied distance (floor applied, ceiling not); the order still sits at the ceiling. NBIS went from ₪30,000 to ₪18,170 | User |

| **DEF-006** | *Fixed* | Feed ATR was copied into the form via `.toFixed(2)`, discarding two of the four decimals the feed carries. 2,585 of 2,604 tickers were affected; 909 (34.9%) sized differently once corrected, 24 by more than 1%, worst ABEV at 3.05%. The extreme *relative* errors (TALK, 12.4%) did not move size because the 8% minimum stop floors them. Undetected by 108 assertions because every suite called `price()` directly and none exercised `fillFromFeed()` | User |
| **DEF-007** | *Fixed* | The beta-exposure cap was applied only `if (beta > 0)`, so an absent or non-positive beta **removed** the constraint rather than tightening it — 278 of 2,604 tickers (10.7%). Two distinct cases were being conflated; see FR-SIZE-12/13. Latent rather than active: at the configured settings the risk-based or crash cap already bound tighter on every affected ticker, so no size actually changed | QA feed suite |
| **DEF-008** | *Fixed* | `fetch_data.py` divided the covariance over the overlapping dates by the variance of the **full** SPY series, mixing two samples and skewing beta for any short-history ticker. Also rounded beta to 3dp, which turned a small positive beta into exactly `0.000` (TRI) and so read downstream as "no beta" | Code review |

| **DEF-009** | *Fixed* | Nothing detected an **intraday** ATR typed in place of a daily one. Yahoo Finance computes chart indicators on whatever bars it displays, and its **“1D” range shows 1-minute bars**: NVDA read 0.27 there against a true daily 7.47, INTC 0.17 vs 8.62, IREN 0.11 vs 4.58 — verified against 1-minute downloads. The failure was silent and severe: below ~0.5% ATR the min-stop floor and lowest crash anchor both bind, so unlike names collapse to an **identical** size (the DEF-001 signature from bad input). IREN would have been sized ₪15,818 against a correct ₪4,176 — 3.8× too large, with an 8% stop where it needs 30%. See FR-FLAG-05/06/07 | User |

| **DEF-010** | *Fixed* | Arming was judged on the **close**, so a parabolic spike that touched the trigger intraday and closed below it armed nothing and the give-back cost a full 1R — precisely the fast-move failure the ladder exists to prevent. Measured on 17,516 high-ATR entries: median outcome −14.56% → **−9.14%** (+5.42pp), mean 4.78% → 4.74% (−0.04pp), armed 35.4% → 39.9%, real losses (< −1%) 56.8% → **54.4%** as they become break-even scratches. Arming *and* trailing on the high costs 0.83pp of mean, because the trail then rides the intraday high and clips runners — the two references must stay separate. Parity compared the arm trigger *percentage* but never its *reference*, so this was invisible to the suite; `qa/parity.js` now replays a synthetic spike through both implementations | Data audit |

| **DEF-011** | *Fixed* | The feed published `[date, high, low, close]` with **no open**, so the position tracker could only test `low <= stop` and then reported the fill **at the stop price**. That is false on any bar that opened below the stop — the stop price never traded and the real fill was the open, lower. It overstated the exit on exactly the days the stop exists for, and armed positions that lost through a gap read as break-even scratches. 4.93% of armed trades end negative through gaps (mean −0.075R, worst −2.44R), so this was not a rare path. Open is now appended as a fifth field (FR-DATA-05a) and the tracker fills open-first (FR-POS-06a); bars without it fall back to the old answer. Regression suite `qa/tracker.js` | Code review |
| **DEF-012** | *Fixed* | `qa/sweep.js` asserted "within each binding constraint, size falls as ATR rises" across **every** cap, including the beta cap — whose formula is `capital × betaexpct / beta` and contains no ATR term. Beta tracks ATR only as a band-median fit (corr 0.946), never per ticker, so the assertion demanded something the formula never promised and failed on BMNR ATR 6.35% ₪6,172 → QUBT ATR 6.55% ₪6,750, two names whose measured betas run the other way. **The tool was right and the test was wrong**; the failure had been standing and disclosed rather than fixed. ATR-ordering is now asserted only where ATR is the driving input, and the beta cap is checked against the invariant it does promise: `size × beta == exposure budget`, plus size falling as beta rises. The companion check "any raw ATR-ordering inversion is attributable to a different binding cap" was also removed — sorting all 2,620 tickers by ATR and then requiring adjacent rows to share a binding constraint made it near-vacuous, and it passed by interleaving rather than by being true | Code review |
| **DEF-013** | *Fixed* | The QA suite had **no CI**. `.github/workflows/` ran only the data publisher, so 142 assertions executed when someone remembered to run them. Added `.github/workflows/qa.yml` on push, PR and manual dispatch. `parity` is reported SKIPPED there because it imports the `riskml` simulator from a separate private checkout; a missing riskml previously CRASHED the whole run, masking every other suite's result. `QA_REQUIRE_PARITY=1` turns the skip back into a failure for a release run | Code review |

| **DEF-014** | *Fixed* | The footer told the user "**the stop starts at 3× ATR**" for months after `initmult` moved to 2.5×, and gave the arming trigger as a flat "+15%" when it is `max(armpct, armatrmult × ATR%)` — so on a volatile name the stated trigger was well below the real one. Every other stale multiplier in this repo sat in a code comment; this one was **user-facing copy describing a rule the tool does not follow**. The footer is now written from `cfg` at render time and `qa/invariants.js` asserts it matches, including after a settings change, so a hardcoded number fails immediately. Three further stale `3x ATR` comments in `index.html` and one in DEF-005 were rewritten to name `initmult` rather than a frozen value | Code review |

**No known open defects.** All fourteen are covered by regression tests in `qa/defects.js`,
`qa/feed.js`, `qa/tracker.js`, `qa/sweep.js` and `qa/invariants.js`.

---

## 10. Verification status

An automated browser suite exists at `qa/` (**155 assertions, all passing**) plus Python
tests for the quantitative engine, SQLite repository, authenticated CRUD, CORS, backups,
and database-owned evaluation. `qa/run_all.sh` treats a crashed suite as a failure rather
than a silent zero. The browser suite covers invariants (monotonicity,
continuity, bounds, scale invariance, metamorphic relations, ladder ordering), a
full-universe sweep of ~2,600 tickers, edge and hostile inputs, a golden snapshot, regression tests for every documented defect, the
**feed path** (`qa/feed.js`), the
**position-tracker replay path** (`qa/tracker.js`), and
**parity between this tool and the `riskml` research simulator**.

It runs in CI on every push and pull request (`.github/workflows/qa.yml`) — see DEF-013.
CI reports all **155** browser assertions and the Python suites; `parity` needs the
`riskml` checkout, which CI has no access to, so it is reported SKIPPED there. Run
`QA_REQUIRE_PARITY=1 sh qa/run_all.sh`
locally before shipping any change to a rule; parity is the only guard on the
research → production link.

`qa/tracker.js` exists because of DEF-011. `qa/parity.js` already drove the replay path,
but only ever asserted on arming, so the fill *price* — the number a breached stop is
reported at — had never been checked at all.

`qa/feed.js` exists because of DEF-006. Every other suite drives the calculator through
`price(p, atr, beta)`, so no assertion had ever executed `fillFromFeed()` — the step that
copies feed values into the form. A rounding bug there was invisible to 108 passing
assertions. **A path with no test is not a path that works**, and the same reasoning
should be applied to the gaps listed below. Note also that the feed arrives on a promise:
an assertion that does not wait for it silently measures 2,604 empty lookups and passes.

Parity is the only guard on the research → production link and has already caught one
real regression (an unvalidated `minstop` change).

**Not yet covered, and the natural starting point for an independent test plan:**
- Real-browser testing (all current coverage is jsdom)
- iOS Safari / PWA behaviour, including keyboards and safe-area insets
- Light/dark theming and accessibility
- Concurrent clients mutating the same SQLite rows
- SQLite lock, disk-full, corruption, and restore drills
- Data-pipeline failure injection (Yahoo down, partial batches, malformed rows)
- Requirement-to-test traceability for every `FR-*` above

---

## 10a. Capital model — active sleeve

`capital` is the **active trading sleeve**, not net worth. A cash or money-market reserve
held deliberately outside the model is represented by its absence: capital carrying no
market risk should not scope risk limits.

Consequence: every percentage cap (`maxpct`, `betaexpct`) applies to the sleeve, and `1R`
as a fraction of it is the meaningful per-trade risk figure. Entering total net worth
instead inflates those caps — a 20% cap on total capital can exceed the entire sleeve,
which is how the setting silently stops capping anything.

Aggregate exposure is now modelled through the **Holistic Risk Management architecture**.
The static Core portfolio and dynamic Active sleeve are treated as one value-weighted
portfolio before a new Active trade is approved. Pearson correlation detects redundant
exposure, `wᵀΣw` measures the change in daily variance, and 99% historical VaR enforces a
hard ILS daily-loss budget. These portfolio-level gates supplement rather than replace the
existing 1R, crash, beta, liquidity, and stop-distance controls.

## 10b. Exit model — trail only, no targets

Settled by a 195-cell parameter grid with a 2015–2022 / 2023–2026 split, on a cohort that
includes structurally volatile names. Three findings drove it:

1. **No profit target.** Across 31,900 high-ATR trades every metric improves monotonically
   as the target moves out and finally disappears: R:R 1.03 (+25% target) → 1.98
   (trail-only); break-even margin +2.1pp → +8.4pp; net after fees and tax +0.9%/yr →
   +2.9%/yr. A target does **not** reduce the average loss (−₪8,160 vs −₪7,548), so it
   truncates gains without buying protection. Trail-only is also the only variant that
   stays positive if capital losses cannot be offset for tax.
2. **No scale-out.** The worst parameter in the grid: mean OOS Sharpe +0.18 with none,
   −0.36 at 25%, −0.33 at 50%.
3. **Continuation favours holding.** Once a high-ATR trade reaches +25%, it touches +50%
   **56.4%** of the time and round-trips to a loss 19.6%. Expected value of holding from
   +25% is +36.1% against +25.0% for taking it, for a median 10 extra days.

**[CALIBRATION]** caveat: net +2.9%/yr is modest, measured on a deliberately-selected
volatile cohort in one regime. Trail-only is the best of the options tested; that is a
weaker claim than it being good.

## 11. Calibration caveats

These bound what may legitimately be asserted as "correct":

1. Parameters marked **[CALIBRATION]** sit on broad plateaus. Nearby values perform
   comparably; they are choices, not derived facts.
2. All measured claims come from **118 symbols over 2015–2026** — one market regime,
   and a universe with **survivorship bias** (delisted names such as WBA, PARA, WISH,
   NKLA are absent, so tail estimates are optimistic).
3. Alpha versus passive holding is **negative at every horizon tested**. The product's
   value is a bounded left tail (worst 1%: −1.42R vs −3.47R) and the larger position a
   truncated tail permits.
4. "Once armed the trade cannot lose" is **95.1% true**, not absolute — 4.93% of armed
   trades still ended negative via overnight gaps through the breakeven stop.
5. **ATR is dividend-adjusted.** `fetch_data.py` downloads with `auto_adjust=True`, so
   published ATR, price, 52-week high and low are all on the adjusted series. A broker or
   TradingView chart shows **unadjusted** prices, so a manually typed ATR is on a slightly
   different basis than a looked-up one. On a non-payer they are identical; on a dividend
   payer the gap scales with the yield over the ~1y window and is small but real. It is
   documented rather than corrected because the adjusted series is the right basis for
   volatility. Any ATR arriving from outside this tool — a future Risk Scout handoff
   included — **shall** declare which basis it is on. `ATR_PCT_IMPLAUSIBLE` catches a typed
   intraday reading (DEF-009); nothing catches a piped value on the wrong basis.

## 11a. Open calibration decisions

Recorded so they are choices rather than defaults nobody revisited. Neither is a defect;
both materially change sizing and are the user's call.

| Decision | Current | Alternative | Effect |
|---|---|---|---|
| Crash-scenario percentile | ~**p96** 45-day drawdown by ATR tier | p90 | permits ~**23% larger** positions across the board |
| Crash tier source | subjective quality/growth/speculative call, mapped onto ATR anchors | take the tier from **measured ATR** directly | removes a judgement call the data already answers |

Measured 45-day drawdowns behind the current anchors (from the 271,464-trade study):

| ATR tier | median | p90 | p95 | p99 |
|---|---|---|---|---|
| <1.5% | −3.8% | −12.3% | −16.0% | −38.5% |
| 1.5–2.5% | −5.8% | −16.7% | −21.1% | −37.3% |
| 2.5–4% | −8.7% | −24.1% | −30.1% | −46.5% |
| 4–6% | −12.5% | −32.8% | −39.7% | −55.5% |
| >6% | −19.6% | −44.7% | −51.6% | −66.3% |

**Unexploited research result.** The same study measured ladder alpha as monotone and
interpretable in volatility — +0.013 below 1.5% ATR, −0.066, −0.154, −0.175 (4–6%),
−0.116 above 6%. The ladder generates alpha on quiet stocks and destroys it on volatile
ones, the opposite of the intuition it was built on; on volatile names it still wins on
tail-matched sizing (2.43×), so it stays correct for a trader who will not accept a −50%
single-name loss. **It costs ~0.15R of alpha, and that is the premium on the insurance.**
That is usable as a lookup table today with no model, and the tool does not surface it.
Displaying it is a product decision, not a fix, so it is recorded here rather than shipped.
