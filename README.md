# Risk Sizer

**A server-backed position-sizing and holistic portfolio-risk terminal.**

## Open the tool

**https://34.10.181.21.sslip.io**

Works in any browser on any device. The first visit asks once for the API key
and remembers it on that device. On iPhone, open the link in Safari, then tap
Share and choose *Add to Home Screen* for an app icon.

Typing the bare IP works too - it redirects to the address above, which is the
one holding the TLS certificate.

The old GitHub Pages address no longer works. Pages can only serve static
files, and since this build reads and writes its state through `/api/*`, every
request there fails with `API HTTP 404`.

- **How much should I buy?**
- **Where does my initial stop go?**
- **When do I move the stop up?**

There is no broker connection or automatic execution. Core holdings, Active positions,
and rules live in a private SQLite database on the FastAPI server; the phone is a stateless
client. Personal values are never sent to the public market-data feed.

---

## Purpose and scope

Risk Sizer is a discipline and risk-management tool for **long equity positions**. It moves position-size and exit decisions to before entry, when they can be expressed as rules instead of improvised while a trade is open.

It is deliberately **not** a stock picker. It provides no screening, buy signal, short strategy, profit target or order execution. Its current job begins after you have chosen a stock.

The default rules were calibrated for multi-week swing positions, but they are choices supported by a limited historical sample-not guarantees or universal optima. See [`PRD.md`](PRD.md) for the complete requirements, evidence and calibration caveats.

---

## Quick start

### 0. Start the private risk server

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/init_db.py --db data/risk_sizer.db
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`. For phone access, deploy the app behind HTTPS using
[`DEPLOYMENT.md`](DEPLOYMENT.md). In production, the REST API requires the
`RISK_SIZER_API_KEY` header; the page prompts once per browser session and does not persist
that secret.

### 1. Set up once

The first-use screen requires exactly two values:

| Question | Why |
|---|---|
| **Total capital** | The active trading sleeve used to scale risk and exposure limits |
| **Most you'd put in one speculative name** | Used to derive the crash-loss budget |

Everything else starts with a documented default and remains editable under **Settings**.

### 2. Size a position

1. Open **Calculator**.
2. Enter a ticker and select **Fill**. The latest daily close, Wilder ATR(14), beta, average dollar volume and 52-week range come from the published feed.
3. If necessary, enter or override price, ATR, currency, USD/ILS and beta manually.
4. Read the outputs:
   - **Recommended position size** and share quantity.
   - **Initial stop** to place at the broker.
   - **Stop ladder** showing when the stop may move upward.
   - **Why this size?**, which identifies every cap and the binding constraint.
   - **Holistic quantitative risk gate**, showing weighted correlation, variance change,
     incremental 99% one-day VaR in ILS, and the final approval verdict.

Position sizing updates immediately. The quantitative gate runs asynchronously and fails
closed: **Track approved position** remains disabled until the verdict for the current
ticker and position value is approved. Existing holdings are read directly from SQLite,
not trusted from the browser request.

### 3. Track the position

1. Enter the ticker in **Ticker to track** and select **Track this position**.
2. Open **Portfolio**.
3. If daily bars are published for that ticker, the app replays them from the entry date and derives the current stop, high since entry and latest daily close.
4. If bars are unavailable, the position remains usable in manual mode with **Reached**, **Undo**, **Edit**, and **Delete** actions.

The Portfolio screen shows one operational instruction: **Your stop**. Only an explicit
**Move your broker stop to …** after-close update should be used for a broker update;
the frozen-policy EOD engine never lowers a stop. Details retain the browser comparison
and HAR-Parkinson research reference without cluttering the decision.

### Stop alerts on iPhone

Risk Sizer can send a direct iPhone notification only when that after-close engine moves
a stop. In Safari, open the tool, select **Share → Add to Home Screen**, then open the
new Risk Sizer icon. On **Portfolio**, select **Enable alerts** and then **Send a test**.
This is standards-based Web Push from Risk Sizer itself-no messaging app is involved.
It does not send a notification when a stop is unchanged.

Risk Sizer does not transmit an order. You remain responsible for entering and updating the stop at your broker.

---

## The interface

The approved responsive design is the **Risk-Sizer Desktop Terminal** introduced in [PR #1](../../pull/1). It provides desktop and iPhone layouts while preserving the existing calculation, storage, lookup, position and ladder behavior.

### Calculator

Ticker lookup fills the measured market inputs while manual entry remains available. A lookup summary reports the ticker, company, as-of date, daily close, ATR and ATR%, beta, volatility band and derived crash assumption.

Position size is the smallest applicable candidate:

```text
· risk-based:       1R divided by the volatility-implied risk distance
▸ crash cap:        crash budget divided by the ATR-derived crash assumption  ← binding example
· capital cap:      maximum percentage of active capital in one name
· beta cap:         maximum beta-weighted exposure
· liquidity cap:    maximum percentage of average dollar volume
```

The beta cap is always present. A measured non-positive beta is handled explicitly, while an unmeasurable beta is estimated conservatively from ATR% and disclosed as an estimate.

### Portfolio

Each tracked Active position stores ticker, entry price, ATR, ladder state, entry date,
quantity, currency, FX at entry, risk status, and actual ILS position value in SQLite.
Core holdings are maintained in the Portfolio screen and are also server-persisted.
Both sleeves support optional friendly display names alongside the Yahoo ticker, including
bilingual names such as `Leumi / לאומי`.

Existing Active holdings can be grandfathered with `legacy=true`. They remain fully
represented in combined correlation, covariance, variance, and historical VaR, while
only non-legacy `RISK_ON` positions consume the configurable forward-looking R-slot
budget. New positions created by the calculator always start with `legacy=false`.

The Settings modal persists total capital, fixed or percentage-based 1R, the 99% daily
VaR ceiling, maximum Risk-On R slots, and crash/volatility sizing parameters through
`GET/PUT /api/settings`. The trade-evaluation request contains only the proposed trade;
server-side SQLite settings are authoritative for approval.

### Holistic risk gate

Before a new Active position can be tracked, the backend reads and merges Core and Active
positions from SQLite and evaluates the combined book:

- Pearson correlation against the current value-weighted portfolio over 90 aligned days;
- current and proposed MPT variance using `wᵀΣw`; and
- conservative 99% historical one-day VaR over 500 aligned ILS P&L observations.

Correlation above 0.75, a variance increase above 20%, or proposed VaR above the configured
daily ILS budget blocks the trade. USD holdings include USD/ILS return effects through
`ILS=X`; cross-exchange missing dates are never forward-filled.

When bars are available, Risk Sizer:

- replays each daily high, low and close since entry;
- detects whether a stop was already breached;
- arms breakeven protection from the daily intraday high;
- advances the trailing stop from closing-price highs; and
- warns when a position has exceeded the configured review age.

When bars are missing, the same ladder remains available as a manual workflow.

---

## How the numbers are decided

### 1R

`1R` is the maximum configured loss on a normal stop-out:

```text
1R = fixed risk amount, when set
     otherwise total capital × risk percentage
```

The default percentage is 2%. A fixed shekel amount, when configured, takes precedence.

### Initial stop and risk distance

With the current defaults:

```text
raw ATR distance = 2.5 × ATR
placed stop      = raw distance clamped to 8%–30% below entry
```

The 8% floor keeps the placed stop outside very small daily noise. The 30% ceiling prevents an operationally meaningless initial order on an extremely volatile name.

When the raw `2.5 × ATR` distance exceeds 30%, the **order** remains at the 30% ceiling but the **position size** is calculated against the full volatility-implied distance. A stop order cannot guarantee protection from a larger move or overnight gap, so sizing must not pretend that the ceiling eliminates that risk.

### Technical and manual initial stops

The calculator no longer defaults to the ATR stop when a better one exists. Stop precedence:

1. **Your chart stop** - optional field (a price, or a % below entry). Sizing uses its exact distance, still limited by 1R, the maximum position %, the crash, beta and liquidity caps.
2. **Technical stop** - nearest qualified support (clustered swing lows, strengthened by volume-profile nodes) with the stop placed 0.3 ATR beneath it, at least 1.5 ATR from entry in a clean uptrend (2 ATR when ADX shows range or downtrend). The justification is shown under the stop.
3. **ATR ladder** - the previous behaviour, used only when no qualified support exists or the lookup fails.

If the nearest support is more than *Extended-from-support warn* (default 22.5%) below entry, or none exists, the calculator shows **"⚠️ Extended from support - poor R/R entry timing"**. The honest size is still shown; it is not hidden.

Saving a position sends the chosen stop to the server, which records it as the starting stop for the daily `eod_stops.py` job, so no `--set-stop` is needed for new trades.

### Daily stop review (`eod_stops.py`)

Runs from `deploy/risk-sizer-eod.timer` at **01:00 New York, Tue-Sat** (the morning after each session, about 07:00-08:00 Israel time).

**A stop exists only where the chart says so.** There is no ATR ladder, no trail from the entry price and no "price minus N ATR" anywhere in the job.

- **Proven supports (all three count):** a confirmed *swing low* that price defended (two separate bounces, or one 4+ ATR swing, or 2.5+ ATR on a volume node); a *volume node* (the VRVP/high-volume level) that price held on at least two separate visits; and *former resistance* that price broke above (two closes), never lost since, and retested or that sits on a volume node. Overlapping ones merge into one zone. Lows older than about 7 months, floors price already fell through, and levels more than 10 normal days below the price are ignored.
- **Which level:** the stop goes just under the NEAREST proven support that sits at least a *cushion* below the price. The cushion is 1.5 normal days of movement in a clean uptrend and 2 otherwise, where a "normal day" is the largest of ATR, the last 5 days' range and a high-low (Parkinson) estimate. Trend, momentum and sector weakness shift the cushion by a quarter day, which only changes *which level* is used. The stop is priced below the zone's lowest low by at least 0.3 ATR or 0.5%.
- **No proven support within reach means no stop.** The position is flagged "No chart support below, manual decision required" (once, then a weekly reminder) and the Positions card shows "No chart stop".
- **Ratchet:** a chart stop only rises, when a higher proven support forms. Closing through it is a BREACH: the support broke.
- **Holding on purpose:** on a breached or no-support position, *I'm holding on purpose* shows the cost in shekels, asks why, and re-bases the stop on the chart from today's price (or on nothing, if there is no support). It is the one time a recorded stop may go down. A second hold on the same position needs the ticker typed. The daily message adds a one-line scorecard (price when you decided vs now).
- **Telegram buttons.** A reached stop, or a position with no chart support, comes with a *Holding on purpose* button (plus a link to the Positions page when `RISK_SIZER_PUBLIC_URL` is set). Tapping it shows the cost in shekels and asks why; tapping a reason records the decision and edits the message. A second hold on the same position needs one more confirming tap. This works through a Telegram webhook, `POST /api/telegram/webhook` (`telegram_hold.py`), protected by `TELEGRAM_WEBHOOK_SECRET` and limited to `TELEGRAM_CHAT_ID`. One-time setup: add `TELEGRAM_WEBHOOK_SECRET` (a long random string) and `RISK_SIZER_PUBLIC_URL` to `/etc/risk-sizer.env`, restart the web app, then `python telegram_hold.py set-webhook https://<host>/api/telegram/webhook`. While a webhook is set the bot cannot be polled with getUpdates.
- **Daily market regime summary.** `risk-sizer-regime.timer` runs `market_regime.py --daily` at **01:10 New York, Tue-Sat**, ten minutes after the stop-loss review, as its own unit: it can never delay or break the stop alerts. It downloads NDX, VIX, VIX3M, QQQ and QQEW with up to 3 attempts (20 s and 60 s apart) until the latest completed session is there, then sends the six-line summary with the dashboard button as a separate Telegram message. Each session is sent once (a holiday stays quiet); if fresh data cannot be fetched the summary is sent from the best data available and marked "data may be delayed"; a Telegram failure is retried 3 times; a hard failure triggers a short Telegram notice via `risk-sizer-regime-failed.service`. Install: copy the three `deploy/risk-sizer-regime*` units to `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now risk-sizer-regime.timer`.
- **Earlier distance-based stops** (tiers, ladder, ATR) are replaced once by chart stops the first time the new job runs; the alert says what each one replaces.
- **Send first, save second.** The Telegram message (retried with backoff, `Retry-After` honoured, plain-text fallback) must be accepted before any stop is written. `OnFailure=` pings Telegram if the job crashes.
- **Holidays and missed days.** No new completed session means no run. Breaches are checked on every bar since the last review. A ticker whose last bar is behind the others is reported as stale.
- **Splits.** A split since the position was opened makes the stored prices meaningless, so the position is reported and skipped until you update it and re-enter the stop.
- **Short history.** Under 120 days of history there is no chart support to read, so the position is flagged rather than given a distance stop.
- **Source of truth.** The Positions tab and `/api/positions` show this job's recorded stop (`eod_stop_state`), the same number the Telegram alert carries. Only a position with no record yet falls back to the post-close engine, then to the browser ATR ladder.
- `--dry-run` computes and prints without saving; `--force` runs even when no new session has completed.

### Continuous crash assumption

Risk Sizer derives a crash assumption continuously from ATR%. It interpolates between eight configurable anchors instead of assigning a manual Quality/Growth/Speculative tier.

The crash cap is:

```text
crash cap = crash-loss budget ÷ ATR-derived crash percentage
```

This is one candidate among the risk, capital, beta and liquidity caps; the smallest candidate sets the position.

### Stop ladder

The ladder follows four rules:

1. **The stop never moves down.**
2. The breakeven trigger is volatility-aware:

   ```text
   arm trigger = max(+15%, 3.0 × ATR%)
   ```

3. Arming is judged from the daily **intraday high**, so a fast spike can activate protection even if the stock closes lower.
4. After arming, the trail follows closing-price highs at the configured distance-currently `3.5 × ATR`-and never falls below entry.

Ladder trigger levels use round numbers because they are intended for manual alerts and broker updates. The ladder contains at most seven useful, non-repeating rows.

Breakeven protection is not a promise of zero loss: an overnight gap can open below the stop and fill at a worse price.

### Input warnings

The app displays at most two decision-relevant warnings or notes. Current checks include:

- an ATR that appears to be an intraday reading rather than a daily ATR;
- a volatility-implied stop wider than the 30% placed-stop ceiling;
- a stop widened to the 8% floor; and
- a deep drawdown from the 52-week high.

Risk Sizer does **not** warn simply because RSI is high or a stock recently rose sharply; that rule was removed after it failed validation.

---

## Settings - *Your rules*

Everything below is editable and applies immediately.

| Setting | Current default | Meaning |
|---|---:|---|
| Total capital | setup required | Active trading capital used by the model |
| Fixed risk per trade | blank | Fixed 1R in ₪; overrides the percentage when set |
| Risk per trade | 2% | Percentage-based 1R when no fixed amount is set |
| Crash-loss budget | setup-derived | Maximum accepted loss in the crash scenario |
| Maximum position | 20% | Capital cap for one name |
| Maximum beta-weighted exposure | 25% | Reduces high-beta exposure |
| Crash anchors | 22/25/34/44/51/56/60/63% | Continuous ATR%-to-crash curve |
| Maximum share of average volume | 5% | Liquidity cap |
| Initial stop | 2.5 × ATR | Raw initial risk distance |
| Arm trail | max(15%, 3.0 × ATR%) | Trigger for breakeven protection |
| Trail distance | 3.5 × ATR | Distance behind closing-price highs |
| Minimum / maximum placed stop | 8% / 30% | Operational stop bounds |
| Drawdown note | 40% off high | Context note for a deep drawdown |
| Position-age review | 90 days | Reminder to make an explicit holding decision |
| Default USD/ILS | 3.65 | Fallback when live FX is unavailable |

**Reset & redo setup** resets the rules and returns to the first-use screen. Existing tracked positions remain until individually marked **Sold**.

---

## Market-data pipeline

Yahoo Finance does not provide the browser CORS headers required for direct client-side access. A scheduled GitHub Action therefore fetches data server-side and force-pushes a single snapshot commit to the `data` branch:

```text
GitHub Action - every 30 minutes during configured US market hours
   └─ fetch_data.py
        ├─ reads the committed SEC-derived universe.csv
        ├─ downloads ~1 year of adjusted daily OHLCV from Yahoo
        ├─ computes Wilder ATR(14), beta vs SPY, ADV and 52-week range
        └─ publishes to the data branch
             ├─ quotes.json          metrics for every eligible liquid US ticker
             └─ bars/{TICKER}.json   daily bars only for tickers in tickers.txt
```

The default pipeline examines up to 3,000 US tickers and excludes names below `$3` or `$5M` average daily dollar volume. The browser downloads the compact quote snapshot for lookup; position bars are fetched only for tracked symbols and cached for the session.

The feed receives no capital, rules, positions or other personal state.

### Verifying a lookup

Every lookup states that the price is a daily **close**, not a live quote. Common reasons another chart may disagree:

| Symptom | Likely cause |
|---|---|
| Price differs during the session | Risk Sizer uses the latest published daily close |
| ATR differs | Risk Sizer uses Wilder/RMA(14) over the full available daily series; another chart may use a simple mean or intraday bars |
| Beta differs | Risk Sizer measures approximately one year of daily returns against SPY over matching dates |
| Feed is old over a weekend | The scheduled job runs on weekdays; the UI allows additional closed-market time before calling it stale |

### Adding bars for a position

Ticker lookup comes from the full eligible universe; `tickers.txt` is **not** the lookup list. It controls which held US tickers receive a `bars/{TICKER}.json` history file for automatic position replay.

Add one symbol per line to [`tickers.txt`](tickers.txt). The next successful scheduled run publishes its bars. Until then, the position tracker falls back to manual ladder mode.

The separate **Run post-close stop engine** workflow runs at 22:15 UTC on US weekdays.
It calls the protected server batch endpoint only after the regular close and fails
closed if the session is not finalized.  Its repository secrets must be
`RISK_SIZER_API_URL` and `RISK_SIZER_API_KEY`; no secret is exposed to the browser or
stored in the data branch. If an actionable stop move is calculated, the same run sends
direct Web Push notifications to each phone that enabled stop alerts.

---

## Privacy and storage

All durable state is in `data/risk_sizer.db` by default (or `RISK_SIZER_DB_PATH`). The
browser contains no persistent storage code, so clearing phone site data does not erase a
portfolio and multiple devices see the same server state. Production endpoints are
protected by `RISK_SIZER_API_KEY`; use HTTPS because CORS does not protect credentials.

The database and its WAL files are gitignored. Use `scripts/backup_sqlite.py` for a
consistent online backup and copy backups off the VM. See [`DEPLOYMENT.md`](DEPLOYMENT.md).

---

## Honest limitations

- **Daily data, not real-time.** Appropriate for multi-week planning, not intraday trading.
- **Long equities only.** No shorts, options, futures, FX trading or crypto strategy.
- **No stock selection.** Risk Sizer manages a chosen position; it does not decide what to buy.
- **No take-profit target or scale-out.** The active exit model is trail-only.
- **No broker integration.** Stops and alerts must be entered manually.
- **No parameter is universally optimal.** Defaults were selected from limited historical research and should be treated as calibrated policy choices.
- **Stops can gap.** A stop cannot guarantee its price or distinguish a temporary dip from a permanent collapse.
- **The research sample is imperfect.** It contains survivorship bias, overlapping observations and a limited set of market regimes; see the PRD and QA dataset disclosure.
- **Discipline can feel worse before it performs better.** Letting winners run can create more breakeven exits and a lower win rate even when total outcomes improve.

---

## Repository map

| Path | Purpose |
|---|---|
| `index.html` | Complete dependency-free browser application |
| `app.py` | FastAPI web server, authenticated CRUD API, CORS, and risk endpoint |
| `database.py` | SQLite schema, CRUD repository, portfolio aggregation, and backup API |
| `quant_risk_engine.py` | Pearson correlation, MPT variance, and historical VaR gate |
| `scripts/init_db.py` | Idempotent database initialization |
| `scripts/seed_portfolio.py` | Idempotent private JSON portfolio import with live ATR calculation |
| `scripts/backup_sqlite.py` | WAL-safe timestamped backups with retention |
| `DEPLOYMENT.md` | VM, systemd, HTTPS, CORS, and cron instructions |
| `PRD.md` | Traceable product requirements, formulas, defects and calibration caveats |
| `fetch_data.py` | Yahoo/SEC-derived market-data pipeline |
| `universe.csv` | Committed SEC-derived US ticker universe |
| `tickers.txt` | Symbols that receive daily bars for position replay |
| `.github/workflows/data.yml` | Scheduled market-data publication |
| `qa/` | Invariants, golden cases, universe sweeps, feed tests, parity and regression suites |
| `requirements.txt` | Pinned Python pipeline dependencies |

The `data` branch contains the generated `quotes.json` and per-position bar snapshots. It is intentionally kept out of `main` history.

---

## QA expectations

Risk Sizer has a substantial regression suite under [`qa/`](qa/). Before changing calculations, storage, ticker lookup, position tracking or the stop ladder:

1. Read the related PRD requirements and tests.
2. Document the intended behavioral change.
3. Keep product/visual changes separate from calculation changes.
4. Run the full suite: `cd qa && npm install && sh run_all.sh`. CI runs it on every push and pull request, minus `parity` - that one needs the private `riskml` checkout, so run `QA_REQUIRE_PARITY=1 sh run_all.sh` locally before merging any change to a **rule**.

See [`qa/README.md`](qa/README.md) and [`qa/DATASET.md`](qa/DATASET.md) for suite design and research limitations.

---

## Troubleshooting

**Market data unavailable** - enter price, ATR, currency, FX and beta manually. The calculator must remain usable without the feed.

**Feed marked stale** - check the repository Actions workflow. Closed-market weekends receive a larger allowance before the UI reports staleness.

**Ticker will not fill** - it may be below the feed's price/liquidity filters, newly listed or absent from the committed universe. Manual entry remains available.

**Tracked position has no automatic update** - add the symbol to `tickers.txt`; until bars are published, use manual **Reached** mode.

**Settings or positions disappeared** - browser site data was probably cleared, storage was unavailable, or a different browser/device is being used.

---

*Risk Sizer applies user-configured position-size and stop rules. It is not financial advice, and its outputs are inputs to your own judgement rather than guarantees.*
