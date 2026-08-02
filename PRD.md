# Risk Sizer — Product Requirements

| | |
|---|---|
| **Version** | 1.0 |
| **Date** | 2026-07-30 |
| **Component** | `risk-sizer.html` (single-file web app) + `fetch_data.py` (data pipeline) |
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

It answers exactly three questions:
1. **How much do I buy?** (₪ and share count)
2. **Where does the initial stop go?**
3. **When do I move the stop, and to what?**

### 1.3 Explicit non-goals
| ID | Not in scope |
|---|---|
| NG-1 | Stock selection, screening, or any buy/sell signal |
| NG-2 | Beating a market benchmark. Measured alpha vs passive holding is **negative** (−0.104R at 45 days); the product is a variance-reduction and discipline tool |
| NG-3 | Real-time or intraday data. Daily closes only |
| NG-4 | Order execution or broker integration |
| NG-5 | Multi-user accounts, sync, or any server-side state |
| NG-6 | Short positions, options, futures, FX, crypto |
| NG-7 | Non-US equities (Israeli/TASE explicitly deferred) |

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

risk-sizer.html  (GitHub Pages, static)
   ├─ reads quotes.json cross-origin  → sizing calculator
   ├─ reads bars/{T}.json on demand   → position tracker
   └─ all user state in localStorage
```

**Rationale (do not "simplify" away):** Yahoo sends no `access-control-allow-origin`
header, so the browser cannot call it directly. `raw.githubusercontent.com` does send
`*`. Bars are split per-ticker because whole-market bars would be ~50 MB.

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
function of ATR%, interpolated over five anchors:

| Anchor | ATR% | Crash % (setting) | Default |
|---|---|---|---|
| 1 | ≤ 1.0 | `gapt1` | 22 |
| 2 | 2.0 | `gapt2` | 25 |
| 3 | 3.25 | `gapt3` | 34 |
| 4 | 5.0 | `gapt4` | 44 |
| 5 | ≥ 8.0 | `gapt5` | 55 |

Values outside the range clamp to the nearest anchor. **It shall not be a step
function** — see DEF-001.

**FR-SIZE-03** — Position size **shall** be the minimum of all applicable candidates:

| Candidate | Formula | Applicable when |
|---|---|---|
| risk-based | `1R / (stopPct/100)` | always |
| crash cap | `breakbudget / (crashPct/100)` | always |
| capital cap | `capital × maxpct/100` | always |
| beta cap | `(capital × betaexpct/100) / beta` | `beta > 0` |
| liquidity cap | `ADV_nis × liqpct/100` | `avgvol > 0` |

**FR-SIZE-04** — The binding constraint **shall** be named in the UI whenever it is not
`risk-based`.

**FR-SIZE-05** — Share count **shall** be `(currency == USD ? size/fx : size) / price`.

**FR-SIZE-06** — Outputs **shall** include size in ₪, share count, and size as % of capital.

**FR-SIZE-07** — A collapsed disclosure **shall** list every candidate with its value and
derivation, mark the binding one, and state the crash cost at the chosen size.

**Worked example** — all preconditions stated explicitly, because the share count depends
on currency and FX and a partially-specified example is untestable.

> **Given** `capital = 100000`, `riskabs = 1300`, `maxspec = 6000` (→ `breakbudget = 3480`),
> all other settings at default, **currency = USD**, `fx = 3.0724`
> **When** `price = 338.19`, `ATR = 8.02`, `beta = 0.845`

```
ATR%       = 8.02 / 338.19 × 100                    =   2.371 %
crashPct   = interp(2.371) between anchors 2.0→25, 3.25→34
           = 25 + (0.371/1.25) × 9                  =  27.67 %
stopPct    = clamp(3 × 2.371, 8, 30)                =   8.0  %   (min floor binds)
stopPrice  = 338.19 × (1 − 0.08)                    = 311.13

candidates:
  risk-based    = 1300 / 0.08                       =  16,250
  crash cap     = 3480 / 0.2767                     =  12,577   ← BINDING
  capital cap   = 100000 × 0.20                     =  20,000
  beta cap      = (100000 × 0.25) / 0.845           =  29,586
  liquidity cap = not applicable (manual entry, no ADV)

→ size            12,577 ₪
→ % of capital    12.6 %
→ ₪ at risk        1,006       ( below 1R, because the crash cap bound, not risk )
```

**Note for test design:** share count depends on currency and FX, so both are mandatory
preconditions for any share-count assertion. Drive the suite's own parameters via
`QA_CAPITAL` / `QA_MAXSPEC` / `QA_RISK` rather than hard-coding them.

### 5.5 Initial stop — `FR-STOP`

| ID | Requirement | Priority |
|---|---|---|
| FR-STOP-01 | `stopPct_raw = initmult × ATR / price × 100` | Must |
| FR-STOP-02 | `stopPct = clamp(stopPct_raw, minstop, maxstop)` | Must |
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
| FR-LAD-03 | The arming row **shall** be at `price × (1 + armpct/100)` with stop = entry price, marked as protected | Must |
| FR-LAD-04 | Above arming, `stop = max(entry, min(level − trailmult×ATR, level))` | Must |
| FR-LAD-05 | Stops **shall** be monotonically non-decreasing down the table | Must |
| FR-LAD-06 | No stop **shall** exceed its own trigger level | Must |
| FR-LAD-07 | Rows repeating the previous stop **shall** be suppressed | Should |
| FR-LAD-08 | Price levels **shall** fall on round numbers (1/2/2.5/5/10 × power of ten) | Should |
| FR-LAD-09 | The ladder **shall** be capped at 7 rows | Should |
| FR-LAD-10 | Wide tables **shall** scroll horizontally without the page scrolling | Must |

### 5.7 Warnings — `FR-FLAG`

| ID | Requirement | Priority |
|---|---|---|
| FR-FLAG-01 | At most **2** flags **shall** be displayed | Must |
| FR-FLAG-02 | Flags that change a decision **shall** be coloured; context-only flags **shall** be neutral grey | Should |
| FR-FLAG-03 | A drawdown greater than `drawdown`% below the 52-week high **shall** produce a neutral note | Should |
| FR-FLAG-04 | The app **shall not** warn against high-RSI or extended entries — falsified on 271,464 trades (E[R] flat; RSI>70 marginally better) | Must |

### 5.8 Position tracking — `FR-POS`

| ID | Requirement | Priority |
|---|---|---|
| FR-POS-01 | A position **shall** be addable from the calculator, requiring a ticker, price and ATR | Must |
| FR-POS-02 | Stored fields **shall** be: ticker, entry price, ATR, rung index, added date. **No** price history | Must |
| FR-POS-03 | Tickers **shall** be upper-cased and HTML-escaped before rendering | Must |
| FR-POS-04 | The tab label **shall** show the open position count | Should |
| FR-POS-05 | With bars available, the current stop **shall** be derived by replaying the ladder over real bars since the entry date | Must |
| FR-POS-06 | A stop already breached **shall** be reported explicitly, with the date and price | Must |
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
| FR-CFG-07 | State **shall** live in `localStorage` under: `riskSizerSettings_v4`, `riskSizerPositions_v4`, `riskSizerSetup_v3` (see DEF-002) | Must |

### 5.10 Data pipeline — `FR-DATA`

| ID | Requirement | Priority |
|---|---|---|
| FR-DATA-01 | The job **shall** run every 30 min, 13–21 UTC, Mon–Fri, and support manual dispatch | Must |
| FR-DATA-02 | It **shall** read the universe from `universe.csv` and held tickers from `tickers.txt` | Must |
| FR-DATA-03 | It **shall** exclude tickers with price `< $3` or ADV `< $5M` | Must |
| FR-DATA-04 | ATR(14) **shall** be Wilder-smoothed; beta **shall** be 1y daily returns vs SPY | Must |
| FR-DATA-05 | It **shall** publish `quotes.json` for the full universe and `bars/{T}.json` only for `tickers.txt` entries | Must |
| FR-DATA-06 | It **shall refuse to publish** if fewer than 100 tickers survive | Must |
| FR-DATA-07 | It **shall** force-push a single-commit `data` branch, keeping snapshots out of `main` history | Must |
| FR-DATA-08 | It **shall** complete within the job timeout (25 min; observed ~163 s for 3,000 tickers) | Must |
| FR-DATA-09 | It **shall not** transmit personal contact details or financial parameters | Must |

---

## 6. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-PERF-01 | Calculator **shall** respond to input within 100 ms |
| NFR-PERF-02 | `quotes.json` **shall not** exceed 1 MB |
| NFR-PERF-03 | Initial page load (excluding feed) **shall** require no network calls — single self-contained file |
| NFR-PRIV-01 | Capital, 1R, crash budget and positions **shall** remain in `localStorage` and **shall never** be transmitted |
| NFR-PRIV-02 | The public repo **shall not** contain personal financial figures or contact details |
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
| `gapt1..gapt5` | 22/25/34/44/55 | Crash % anchors | Measured p96 drawdowns |
| `liqpct` | 5 | Max % of ADV | Never binds in practice |
| `initmult` | 3.0 | Initial stop in ATR multiples | **[CALIBRATION]** — plateau 1.5–3.5 |
| `trailmult` | 2.5 | Trail distance in ATR multiples | **[CALIBRATION]** |
| `armpct` | 15 | Move to breakeven at +N% | **[CALIBRATION]** |
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
| `localStorage` unavailable | App functions for the session; no persistence; no crash |
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

**No known open defects.** All four are covered by regression tests in `qa/defects.js`.

---

## 10. Verification status

An automated suite exists at `qa/` (**78 assertions, all passing**). It covers invariants (monotonicity,
continuity, bounds, scale invariance, metamorphic relations, ladder ordering), a
full-universe sweep of ~2,600 tickers, edge and hostile inputs, a golden snapshot, regression tests for every documented defect, and
**parity between this tool and the `riskml` research simulator**.

Parity is the only guard on the research → production link and has already caught one
real regression (an unvalidated `minstop` change).

**Not yet covered, and the natural starting point for an independent test plan:**
- Real-browser testing (all current coverage is jsdom)
- iOS Safari / PWA behaviour, including keyboards and safe-area insets
- Light/dark theming and accessibility
- `localStorage` disabled, full, or corrupted
- Concurrent tabs mutating the same state
- Data-pipeline failure injection (Yahoo down, partial batches, malformed rows)
- Requirement-to-test traceability for every `FR-*` above

---

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
