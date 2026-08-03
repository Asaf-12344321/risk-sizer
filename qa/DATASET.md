# Dataset — full disclosure

**Source:** Yahoo Finance daily bars via `yfinance` (`auto_adjust=True`, so split- and
dividend-adjusted **as of the fetch date**). Fetched 2026-08-03.

| | |
|---|---|
| Symbols with price data | 168 |
| Symbols producing trades | 167 |
| Labelled candidate trades | 303,427 |
| Entry-date range | 2015-11-03 → 2026-07-24 |
| Horizon per trade | 45 calendar days |
| Liquidity filter | ≥ $10M 20-day average dollar volume, price ≥ $5, both point-in-time |

## Trades by year

| Year | Trades | Symbols | Median ATR% |
|---|---|---|---|
| 2015 | 3,116 | 76 | 2.1% |
| 2016 | 19,899 | 82 | 2.0% |
| 2017 | 20,958 | 91 | 1.6% |
| 2018 | 22,432 | 94 | 2.4% |
| 2019 | 23,383 | 98 | 2.2% |
| 2020 | 26,494 | 117 | 3.8% |
| 2021 | 31,255 | 144 | 3.3% |
| 2022 | 32,493 | 147 | 4.8% |
| 2023 | 32,032 | 148 | 3.2% |
| 2024 | 33,902 | 151 | 3.1% |
| 2025 | 36,706 | 159 | 3.9% |
| 2026 | 20,757 | 158 | 4.4% |

## Every symbol, by sector

**Mega/large-cap tech** — AAPL (2015-01, 2,693t), MSFT (2015-01, 2,693t), NVDA (2015-01, 1,924t), AMZN (2015-01, 2,693t), GOOGL (2015-01, 2,693t), META (2015-01, 2,693t), TSLA (2015-01, 2,693t), AMD (2015-01, 2,532t), INTC (2015-01, 2,693t), MU (2015-01, 2,693t), AVGO (2015-01, 2,693t), QCOM (2015-01, 2,693t), TXN (2015-01, 2,693t), ADBE (2015-01, 2,693t), CRM (2015-01, 2,693t), ORCL (2015-01, 2,693t), NFLX (2015-01, 2,693t)

**Financials** — JPM (2015-01, 2,693t), BAC (2015-01, 2,693t), GS (2015-01, 2,693t), MS (2015-01, 2,693t), WFC (2015-01, 2,693t), V (2015-01, 2,693t), MA (2015-01, 2,693t), AXP (2015-01, 2,693t), PYPL (2015-07, 2,567t)

**Energy** — XOM (2015-01, 2,693t), CVX (2015-01, 2,693t), COP (2015-01, 2,693t), SLB (2015-01, 2,693t), OXY (2015-01, 2,693t)

**Healthcare** — JNJ (2015-01, 2,693t), PFE (2015-01, 2,693t), MRK (2015-01, 2,693t), ABBV (2015-01, 2,693t), UNH (2015-01, 2,693t), LLY (2015-01, 2,693t), DXCM (2015-01, 2,693t), BAX (2015-01, 2,693t)

**Consumer / retail** — WMT (2015-01, 2,693t), COST (2015-01, 2,693t), TGT (2015-01, 2,693t), HD (2015-01, 2,693t), LOW (2015-01, 2,693t), NKE (2015-01, 2,693t), SBUX (2015-01, 2,693t), MCD (2015-01, 2,693t), DIS (2015-01, 2,693t), KO (2015-01, 2,693t), PEP (2015-01, 2,693t), PG (2015-01, 2,693t), EL (2015-01, 2,693t), VFC (2015-01, 2,693t), ETSY (2015-04, 2,608t), W (2015-01, 2,693t)

**Industrial / defence** — CAT (2015-01, 2,693t), DE (2015-01, 2,693t), BA (2015-01, 2,693t), GE (2015-01, 2,693t), HON (2015-01, 2,693t), UPS (2015-01, 2,693t), RTX (2015-01, 2,693t), LMT (2015-01, 2,693t), MMM (2015-01, 2,693t)

**Telecom** — T (2015-01, 2,693t), VZ (2015-01, 2,693t)

**Software / SaaS** — CRWD (2019-06, 1,576t), PANW (2015-01, 2,693t), SNOW (2020-09, 1,257t), ZS (2018-03, 1,887t), DDOG (2019-09, 1,507t), NET (2019-09, 1,511t), TTD (2016-09, 2,185t), ZM (2019-04, 1,613t), DOCU (2018-04, 1,858t), TWLO (2016-06, 2,322t), SHOP (2015-05, 2,385t)

**Travel / leisure** — UBER (2019-05, 1,598t), ABNB (2020-12, 1,197t), DAL (2015-01, 2,693t), UAL (2015-01, 2,693t), CCL (2015-01, 2,693t), NCLH (2015-01, 2,693t), AAL (2015-01, 2,693t), MGM (2015-01, 2,693t), DKNG (2019-07, 1,546t), LYFT (2019-03, 1,627t)

**Autos / EV** — RIVN (2021-11, 966t), LCID (2020-09, 1,253t), F (2015-01, 2,593t), GM (2015-01, 2,693t)

**Meme / retail-frenzy** — GME (2015-01, 1,688t), AMC (2015-01, 1,820t), BBBY (2015-01, 2,092t), CLOV (2020-06, 173t), OPEN (2020-06, 474t)

**Fintech / crypto-adjacent** — COIN (2021-04, 1,113t), HOOD (2021-07, 1,039t), SOFI (2021-01, 1,136t), AFRM (2021-01, 1,175t), UPST (2020-12, 1,193t), CVNA (2017-04, 1,867t)

**Biotech** — MRNA (2018-12, 1,703t), BNTX (2019-10, 1,492t), RXRX (2021-04, 670t), ABSI (2021-07, 59t), SDGR (2020-02, 1,403t), TEM (2024-06, 317t)

**Social** — SNAP (2017-03, 2,096t), PINS (2019-04, 1,613t)

**Telehealth** — TDOC (2015-06, 2,410t)

**Media** — WBD (2015-01, 2,693t), BABA (2015-01, 2,693t), PLTR (2020-09, 1,247t)

**QUANTUM** — QBTS (2020-12, 397t), IONQ (2021-01, 1,071t), RGTI (2021-04, 403t), QUBT (2015-01, 427t), ARQQ (2021-04, 253t)

**NUCLEAR / SMR / uranium** — OKLO (2021-07, 600t), SMR (2022-03, 628t), NNE (2024-05, 343t), LEU (2015-01, 622t), UEC (2015-01, 694t), CCJ (2015-01, 2,695t), DNN (2015-01, 0t), NXE (2015-01, 775t)

**BITCOIN MINERS / AI datacentre** — IREN (2021-11, 637t), NBIS (2024-10, 229t), CRWV (2025-03, 121t), CIFR (2020-10, 384t), HUT (2018-03, 1,264t), MARA (2015-01, 1,437t), RIOT (2016-03, 1,478t), CLSK (2016-11, 1,178t), WULF (2015-01, 370t), HIVE (2015-01, 303t), CORZ (2024-01, 416t), BTDR (2021-07, 600t)

**SPACE / satellite** — ASTS (2019-11, 891t), LUNR (2021-11, 610t), RKLB (2020-11, 810t), PL (2021-04, 383t), BKSY (2019-12, 487t), SPIR (2020-11, 152t)

**eVTOL** — JOBY (2020-11, 962t), ACHR (2020-12, 577t), EVTL (2020-11, 109t)

**AI small-cap** — SOUN (2022-04, 499t), BBAI (2021-04, 189t), AI (2020-12, 1,200t)

**DRONES / defence tech** — ONDS (2020-12, 226t)

**Hydrogen / EV charging / solar** — PLUG (2015-01, 868t), FCEL (2015-01, 1,539t), BLDP (2015-01, 869t), EVGO (2020-11, 421t), BLNK (2015-01, 816t), CHPT (2019-09, 1,202t), RUN (2015-08, 2,127t), ENPH (2015-01, 1,940t), SEDG (2015-03, 2,568t), FSLR (2015-01, 2,693t)

**Lidar / sensors** — INVZ (2020-06, 53t), MVIS (2015-01, 296t), OUST (2020-10, 565t), AEVA (2020-02, 535t)

**Uncategorised** — ROKU, SPCE

Format: `TICKER (first bar, trade count)`.

## Volatility distribution

| ATR% band | Trades | Share | Symbols |
|---|---|---|---|
| <1.5% | 28,567 | 9.4% | 67 |
| 1.5–2.5% | 87,769 | 28.9% | 99 |
| 2.5–4% | 79,450 | 26.2% | 129 |
| 4–6% | 50,699 | 16.7% | 160 |
| 6–8% | 27,833 | 9.2% | 164 |
| 8–10% | 15,231 | 5.0% | 142 |
| 10–12% | 7,229 | 2.4% | 122 |
| 12–15% | 4,268 | 1.4% | 99 |
| >15% | 2,381 | 0.8% | 66 |

## Known limitations

**1. Survivorship bias.** Yahoo drops delisted tickers, so companies that went to zero
are absent — precisely the catastrophes a stop exists to prevent. Confirmed missing:
`WBA` `PARA` `WISH` `NKLA` `SQ` `BITF` `GREE` `ASTR` `VLD` `LAZR`. Every tail estimate is
therefore **optimistic**.

**2. Short history on the newest names.** Several of the most relevant symbols listed only
recently, so their contribution is thin:

| Symbol | First bar | Years | Trades |
|---|---|---|---|
| CRWV | 2025-03-28 | 1.3 | 121 |
| NBIS | 2024-10-21 | 1.8 | 229 |
| ONDS | 2020-12-04 | 5.7 | 226 |
| OKLO | 2021-07-08 | 5.1 | 600 |
| IREN | 2021-11-17 | 4.7 | 637 |
| CIFR | 2020-10-20 | 5.8 | 384 |
| QBTS | 2020-12-11 | 5.6 | 397 |
| IONQ | 2021-01-04 | 5.6 | 1,071 |
| ASTS | 2019-11-01 | 6.7 | 891 |
| INVZ | 2020-06-29 | 6.1 | 53 |

**3. One market regime.** 2015–2026 contains the 2020 crash, the 2021 mania, the 2022
bear and a long bull. It contains no 2000-style multi-year tech unwind and no 1970s
inflation regime.

**4. Overlapping labels.** A trade entered on day *t* overlaps one entered on *t+1*, so
the 303,427 rows are far from independent. Roughly 3,000 distinct dates and ~160 symbols.

**5. Retroactive price adjustment.** `auto_adjust=True` rewrites history for later splits.
Prices therefore differ from what was actually payable at the time.

**6. Selection of the volatile cohort.** The quantum/SMR/miner/space names were chosen
*because* they are the kind of stock the user trades — a deliberate, non-random sample.
It fixes a coverage gap but is not a neutral draw from the market.

