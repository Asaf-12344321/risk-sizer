# QA suite for risk-sizer.html

Run everything:

```bash
cd qa && npm install && node invariants.js && node sweep.js && node edge.js \
  && node golden.js && python3 parity.py > /tmp/parity.json && node parity.js
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
- **ladder** — only rises, starts at the initial stop, reaches breakeven at +15%, never above price

## sweep.js — the whole universe at once
Runs all ~2,600 real tickers and checks the **distribution**, not individual answers.
Asserts no single size claims >15% of the universe, and that sorting by ATR yields a
non-increasing size curve.

## edge.js — degenerate and hostile inputs
Zero/negative/absurd prices and ATRs, junk strings (`abc`, `1e999`, `--5`, `NaN`), every
setting forced to 0, no feed at all, and no setup. The rule: **fail visibly, never
silently wrongly.** Nothing may render `NaN`, `Infinity` or `undefined`.

## golden.json — frozen answers
14 representative cases. Any logic change that moves them fails here, so a shift is
always a decision. Regenerate deliberately: `node golden.js --update`.

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

## Known open finding

At sub-dollar prices the stop renders as `0.00`, because prices are formatted to two
decimals. Only reachable via manual entry — the feed filters below $3 — but a displayed
stop of zero is wrong. Fix is adaptive precision (4dp under $1, 3dp under $10).
