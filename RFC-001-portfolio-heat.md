# RFC-001 — Accounting for cash allocation in position sizing

**Status:** ❌ **CLOSED — superseded** · **Date:** 2026-08-02

> **Resolution.** Neither the original `cash_allocation_pct` proposal nor the portfolio-heat
> design in §3 was built. Both were made unnecessary by a configuration change: setting
> `capital` to the **active trading sleeve (₪450,000)** rather than total capital, so every
> percentage cap scopes to the money actually at risk. The ₪250,000 reserve is represented
> by being *outside* the model — the honest treatment of capital carrying no market risk.
>
> Adopted instead (settings only, no code):
> `capital 450,000` · `breakbudget 30,000` · `betaexpct 40%` · `riskabs 9,000` (= 2.0% of sleeve) · `maxpct 20%`
>
> This keeps the tool **stateless** — no open-position tracking, no portfolio-state sync —
> which was the explicit requirement. The analysis in §1 stands and is retained: scaling a
> per-position crash cap by a cash reserve remains a category error, and should not be
> reintroduced.
>
> **Retained for the record**, not for implementation.

---

## 1. Why the proposed mechanism is wrong

The proposal is to scale the per-position Crash Budget by the cash reserve, on the
grounds that cash halves aggregate drawdown risk.

**Cash does not reduce the loss on a single position.** If NVDA is ₪75,733 and falls
38%, the loss is ₪28,779 whether the rest of the portfolio is in cash or in equities.
The Crash Cap is a *single-name* limit; the cash reserve is a *portfolio-level* fact.
Using one to relax the other is a category error — it would permit a larger single-name
loss on the strength of protection that does not apply to single names.

There is also a circularity: the justification for larger positions is the cash buffer,
but taking larger positions consumes the cash buffer. Applied consistently the argument
licenses its own erosion.

### 1.1 And it would not do what you expect

Doubling the Crash Budget from ₪25,000 to ₪50,000:

| | at ₪25k | bound by | at ₪50k | bound by |
|---|---|---|---|---|
| AAPL | ₪90,348 | crash cap | **₪112,500** | risk-based |
| MSFT | ₪76,917 | crash cap | **₪98,619** | risk-based |
| NVDA | ₪65,678 | crash cap | **₪75,733** | risk-based |
| TSLA | ₪51,341 | risk-based | ₪51,341 | unchanged |
| OKLO | ₪31,074 | risk-based | ₪31,074 | unchanged |
| QBTS | ₪28,418 | risk-based | ₪28,418 | unchanged |
| IREN | ₪21,180 | risk-based | ₪21,180 | unchanged |
| NBIS | ₪18,170 | risk-based | ₪18,170 | unchanged |

**The volatile names do not move at all** — they are risk-bound, not crash-bound. Only
mega-caps grow. So the "compression" you describe is real, but its cause is narrower
than the proposal assumes, and a cash multiplier is the wrong instrument for it.

---

## 2. What is actually true

The instinct is sound at the level it belongs to. A cash reserve genuinely does permit
more per-name risk — **in aggregate**, not per position. For a fixed tolerance for
total portfolio drawdown, holding 50% cash means the equity sleeve can absorb roughly
twice the drawdown before that tolerance is breached.

The reason the Crash Cap currently feels too tight is that **nothing else constrains
total exposure.** With no portfolio-level limit, the per-position cap is doing double
duty as a crude proxy for one. Add the real aggregate constraint and the per-position
cap can safely relax — which delivers exactly the outcome you want, with a justification
that holds.

### 2.1 Current de-facto exposure

Eight positions at today's sizing:

```
deployed              ₪255,741   (37% of capital)
cash remaining        ₪444,259   (63%)
loss if ALL crash     ₪136,867   (19.6% of capital)
loss if all stop out   ₪60,344   ( 8.6% of capital)
```

Note this already leaves more than the 50% cash target — the current model is not
preventing the reserve, it is producing it.

---

## 3. Proposed design

### 3.1 New parameters

| Parameter | Default | Meaning |
|---|---|---|
| `portfoliocrashpct` | 20 | Maximum total loss, as % of **total** capital, if every open position crashes simultaneously |
| `tradingpct` | 100 | Share of capital allocated to active trading. Scopes the **capital cap** only |

`cash_allocation_pct` as originally proposed is **not** included: once the aggregate
constraint exists, scaling the per-position budget by cash would double-count the same
protection.

### 3.2 Mathematics

**Portfolio heat cap** (new, added to the existing `min()`):

```
committed_crash   = Σ over open positions p of
                      size(p) × crashPct(ATR%(p)) / 100

portfolio_budget  = capital × portfoliocrashpct / 100

remaining         = max(0, portfolio_budget − committed_crash)

heat_cap          = remaining / (crashPct(ATR%_candidate) / 100)
```

**Capital cap** (modified — currently applies to total capital):

```
capital_cap = capital × (tradingpct / 100) × (maxpct / 100)
```

**Sizing hierarchy** (unchanged in structure, one new member):

```
size = min( risk_based, crash_cap, capital_cap, beta_cap, liquidity_cap, heat_cap )
```

### 3.3 How the cash reserve enters — correctly

It enters through `committed_crash`, and it is *derived* rather than asserted. Capital
that is not deployed contributes zero committed crash, so it leaves headroom in the
portfolio budget. Concretely: with six positions open you get more room per name than
with twelve, because the aggregate budget is shared. That is the cash effect you are
describing, obtained without a multiplier and without double-counting.

### 3.4 Consequence, stated plainly

**This is a risk-loosening change**, and it should be adopted as one. The Crash Budget
can be raised — say ₪25,000 → ₪45,000, which moves the mega-caps toward their 1R sizing
— *only because* the aggregate cap now prevents that from compounding across positions.
Raising the budget without adding the aggregate cap would be the unguarded version of
the same change.

The capital-cap correction moves the other way: at `tradingpct = 50` the cap falls from
₪140,000 to ₪70,000, which is the honest fix for a 20% cap that currently permits three
positions to exceed the entire trading sleeve.

---

## 4. PRD deltas

| ID | Change |
|---|---|
| **FR-SIZE-03** | Add `heat_cap` to the candidate list. Applicable when one or more positions are tracked |
| **FR-SIZE-08** *(new)* | The tool **shall** compute committed crash exposure across all tracked positions and **shall not** permit a new position whose crash exposure would breach `portfoliocrashpct` of capital |
| **FR-SIZE-09** *(new)* | When `heat_cap` is binding, the UI **shall** state remaining portfolio crash budget in ₪ and the number of positions consuming it |
| **FR-SIZE-10** *(new)* | With no tracked positions, `heat_cap` **shall** equal the full portfolio budget and **shall not** constrain the first trade |
| **FR-SIZE-11** *(new)* | `capital_cap` **shall** be scoped by `tradingpct` |
| **FR-POS-13** *(new)* | Each tracked position **shall** display its own crash exposure and its share of the portfolio budget |
| **§7** | Add `portfoliocrashpct` (20) and `tradingpct` (100) |
| **§8** | Add: portfolio budget exhausted → size 0 with an explicit "no room left" message, never a silent ₪0 |
| **§11** | Note: `portfoliocrashpct` is **[CALIBRATION]** — it has no empirical backing from the 271k study, which never modelled concurrent positions |

---

## 5. QA assertions required

### 5.1 Invariants
- `heat_cap` is **non-increasing** in the number of tracked positions
- With zero positions, `size` is identical to the pre-change value (no regression on the first trade)
- Committed crash exposure never exceeds `portfoliocrashpct × capital`, over any sequence of adds
- `heat_cap` is monotonic in `portfoliocrashpct`
- Removing a position releases exactly the budget it consumed (add → remove → size returns to baseline)

### 5.2 Sweep
- Across all ~2,600 tickers with a fixed set of 5 held positions, no ticker breaches the portfolio budget
- Distinct sizes remain > 50% of tickers (the new cap must not re-introduce clustering — this is the DEF-001/DEF-005 failure mode)

### 5.3 Edge
- `portfoliocrashpct = 0` → every size is 0, with the explicit message, no `NaN`
- Budget exactly exhausted → size 0, message shown, no negative size
- `tradingpct = 0` → capital cap 0, explicit message
- `tradingpct = 100` → byte-identical behaviour to today
- Tracked position with a missing ATR → excluded from committed crash, and the exclusion is disclosed

### 5.4 Golden
- Regenerate. **Every mega-cap value will change** if the Crash Budget is raised — each change must be reviewed deliberately, not accepted wholesale

### 5.5 Parity
- `riskml` has no concept of concurrent positions, so portfolio heat is **out of parity scope**. The parity suite must continue to pass on single-trade sizing, and this exemption must be recorded — otherwise the research↔production guard silently weakens

### 5.6 Regression guarding the reasoning in §1
- Assert that the per-position crash cap is **not** scaled by any cash parameter, so a future change cannot reintroduce the double-count

---

## 6. Recommendation

Adopt §3, not the original proposal. Specifically:

1. Implement `heat_cap` and `tradingpct`.
2. **Then** raise the Crash Budget deliberately, as a separate and explicit decision.
3. Do not add `cash_allocation_pct`.

If the only goal is mega-caps nearer their 1R sizing and the aggregate work is not
wanted, the honest alternative is to raise the Crash Budget on its own and accept that
nothing bounds total exposure. That is a legitimate choice, but it should be made
knowingly rather than arrived at through a modelling argument that does not hold.
