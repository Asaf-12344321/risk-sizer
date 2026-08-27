# QA suite for risk-sizer.html

Run everything:

```bash
cd qa && npm install && sh run_all.sh
```

`run_all.sh` is the only complete list — it runs every suite, fetches the quotes feed if
it is missing, and reports a crashed suite as a **failure** rather than a silent zero. The
hand-written chain that used to live here drifted: it named four suites out of ten, so
`defects`, `ux`, `feed`, `custom_edge` and `tracker` never ran for anyone who copied it.

The holistic-risk addition also has Python suites, run by CI before `run_all.sh`:

```bash
python -m unittest qa.test_quant_risk_engine qa.test_database qa.test_seed_portfolio qa.test_api -v
```

`quant_integration.js` verifies the browser contract with a stubbed same-origin API:
proposal-only risk requests, verdict/metric rendering, incremental VaR copy, and Active
position POST persistence with value, quantity, currency, and risk status.

CI runs the same script on every push and pull request (`.github/workflows/qa.yml`), with
one exception: `parity` needs the `riskml` checkout, which CI has no access to, so it is
reported SKIPPED there. Before shipping a change to any **rule**,
run it locally with parity demanded:

```bash
QA_REQUIRE_PARITY=1 sh run_all.sh
```

Each suite exists because a **different class of bug** got past the previous ones.

## invariants.js — properties that must hold for every input
The step-function bug (2,607 tickers collapsing onto 5 position sizes) survived dozens
of passing tests because every one of them checked a single stock and asked "is this
answer right?". For MSFT, ₪64,706 *was* right. The defect was in the **relationship
between** answers, which example-based tests cannot see.

- **monotonicity** — size never rises as ATR rises
- **continuity** — a 0.1pp ATR change never moves size >6%. *This is the test that catches step functions.*
- **spread** — distinct ATRs give distinct sizes, below the saturation zone
- **saturation is asserted as intended** — above 11% ATR sizes must coincide, because both ceilings are maxed
- **hard bounds** — size ≤ 20% capital, stop ∈ [8%,30%], risk ≤ 1R, stop < price
- **scale invariance** — the same ATR% gives the same size at ₪7 or ₪4,000
- **metamorphic** — doubling 1R doubles a risk-bound position; more capital never shrinks one
- **ladder** — only rises, starts at the initial stop, reaches breakeven at `max(15%, 3×ATR%)`, never above price
- **the copy states the rule in force** — the footer's multipliers are read back and compared with `cfg`, including after a settings change. It said "3× ATR" long after the default moved to 2.5× (DEF-014). Every other stale multiplier here sat in a comment; that one reached a person

## sweep.js — the whole universe at once
Runs all ~2,600 real tickers and checks the **distribution**, not individual answers.
Asserts no single size claims >15% of the universe, and that every cap holds against the
variable **its own formula uses**.

That last part was wrong for a long time and is the lesson of this suite. It used to sort
each binding-constraint group by ATR and demand size fall — including the **beta cap**,
whose formula is `capital × betaexpct / beta` and contains no ATR term at all. Beta tracks
ATR only as a band-median fit (corr 0.946), never per ticker, so the assertion demanded
something that was never promised. It failed on BMNR ATR 6.35% ₪6,172 → QUBT ATR 6.55%
₪6,750 — two names whose betas run the other way — and the failure stood as a disclosed
"known" for months. **The tool was right and the test was wrong** (DEF-012). ATR-ordering
is now asserted only where ATR drives the answer, and the beta cap is checked against the
invariant it does promise: `size × beta == exposure budget`, and size falling as beta rises.

A test that fails against correct behaviour is worse than no test: it teaches you to read
red as normal.

## edge.js — degenerate and hostile inputs
Zero/negative/absurd prices and ATRs, junk strings (`abc`, `1e999`, `--5`, `NaN`), every
setting forced to 0, no feed at all, and no setup. The rule: **fail visibly, never
silently wrongly.** Nothing may render `NaN`, `Infinity` or `undefined`.

## golden.json — frozen answers
14 representative cases. Any logic change that moves them fails here, so a shift is
always a decision. Regenerate deliberately: `node golden.js --update`.

## tracker.js — the position-replay path
Drives `liveState()` through real bar fixtures and asserts the **fill price** of a
breached stop, which nothing had ever checked. `parity.js` reaches this same code but only
ever asserts on *arming*.

The bug it was written for: the feed published `[date, high, low, close]` with no **open**,
so the tracker could only ask `low <= stop` and then reported the fill *at the stop price*.
On any bar that opened below the stop that is false — the stop price never traded and you
were filled lower, at the open. It flattered the exit on exactly the days a stop exists
for, and 4.93% of armed trades end negative through gaps. Open is now the fifth bar field
and the tracker fills open-first (DEF-011).

Covers: ordinary fill at the stop, gap fill at the open, gap wording, stop-first ordering
when a bar both breaches and arms, opening below the stop beating arming, no breach when
the low holds, correct breach date, and a **legacy 4-field bar** still reporting the breach
at the old price so an old cached feed degrades rather than going silent.

## parity.py + parity.js — tool vs research code
The browser tool and `~/riskml` implement the same ladder. If they disagree, one is
wrong. **This suite immediately caught a real regression**: the tool's minimum stop had
been lowered 8% → 5% without re-validating, so the rule being traded was no longer the
rule that had been tested on 271,464 trades. Re-running the study showed 8% was better
in capital terms (+0.3332% vs +0.3139% per trade) and the change was reverted.

Nothing else guards the research → production link. Run it after any parameter change.

## Two lessons worth keeping

**Never re-derive a value from a rounded display string.** The first sweep reported 123
cap breaches; all 123 were rounding artefacts, because a stop shown as `16.42` (truly
16.422) makes a 30.000% stop compute as 30.008%. Tolerances now come from display
precision, and the tool's own reported figures are treated as authoritative.

**Write the intent down, including where clustering is deliberate.** The saturation zone
above 11% ATR looked like a failure until it was asserted as intended.

**A test that fails against correct behaviour must be fixed, not disclosed.** See sweep.js
above and DEF-012. Standing red teaches you to stop reading the output.

## Known open findings

None. The sub-dollar `0.00` stop that used to be listed here was fixed as DEF-004
(adaptive precision: 4dp under $1, 3dp under $10) and is covered by the edge suite.

The gaps that remain are *uncovered areas*, not known-wrong behaviour — real-browser and
iOS Safari testing, theming and accessibility, concurrent API mutations, database lock
failure injection, data-pipeline failure injection, and `FR-*`-to-test traceability. See
PRD §10.
