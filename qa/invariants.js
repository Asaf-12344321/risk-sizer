// INVARIANTS — properties that must hold for EVERY input, not just the ones I thought of.
// This is the class of test that would have caught the step-function bug: no single
// answer was wrong, the relationship BETWEEN answers was.
const { boot, ck, report } = require('./lib');
const CAP = +(process.env.QA_CAPITAL || 100000), RISK = +(process.env.QA_RISK || 1300);
const t = boot();

// ---------- 1. MONOTONICITY: more volatile must never mean a bigger position
let viol = 0, worst = '';
let prev = Infinity;
const atrs = [];
for (let a = 0.5; a <= 15; a += 0.1) atrs.push(Math.round(a * 10) / 10);
const sizes = atrs.map(a => { t.price(100, a); return t.size(); });
for (let i = 1; i < sizes.length; i++) {
  if (sizes[i] > sizes[i - 1] + 1) { viol++; if (!worst) worst = `ATR ${atrs[i-1]}%→${atrs[i]}% : ${sizes[i-1]}→${sizes[i]}`; }
}
ck('size never increases as ATR rises', viol === 0, viol ? `${viol} violations, first: ${worst}` : `${atrs.length} ATR levels`);

// ---------- 2. CONTINUITY: no cliffs. THE test that catches step functions.
let jumps = [];
for (let i = 1; i < sizes.length; i++) {
  const rel = Math.abs(sizes[i] - sizes[i - 1]) / Math.max(sizes[i - 1], 1);
  if (rel > 0.06) jumps.push(`ATR ${atrs[i-1]}→${atrs[i]}: ${sizes[i-1].toLocaleString()}→${sizes[i].toLocaleString()} (${(rel*100).toFixed(0)}%)`);
}
ck('a 0.1% ATR change never moves size more than 6%', jumps.length === 0,
   jumps.length ? `${jumps.length} cliffs, first: ${jumps[0]}` : 'smooth across 0.5–15% ATR');

// ---------- 3. SPREAD: distinct inputs must give distinct outputs.
// Deliberately excludes ATR>=10%, where the 30% stop ceiling AND the top crash anchor
// both saturate, so sizes there MUST coincide. That zone is asserted separately below,
// so the intent is written down rather than assumed.
const live = atrs.map((a, i) => ({ a, s: sizes[i] })).filter(x => x.a < 10);
const distinct = new Set(live.map(x => Math.round(x.s))).size;
ck('distinct ATRs yield distinct sizes (below the saturation zone)',
   distinct > live.length * 0.9, `${distinct} distinct sizes from ${live.length} ATR levels < 10%`);

// Sizing uses the UNCAPPED 3x ATR distance, so the 30% order ceiling no longer flattens
// the high-volatility end. Above 11% ATR sizes must still differ and still fall.
const sat = atrs.map((a, i) => ({ a, s: sizes[i] })).filter(x => x.a >= 11);
ck('above 11% ATR sizes still differentiate', new Set(sat.map(x => Math.round(x.s))).size > sat.length * 0.9,
   `${new Set(sat.map(x => Math.round(x.s))).size} distinct across ${sat.length} levels`);
ck('above 11% ATR size keeps falling as ATR rises',
   sat.every((x, i) => i === 0 || x.s <= sat[i - 1].s + 1),
   `${sat[0].s} down to ${sat[sat.length - 1].s}`);

// ---------- 4. HARD BOUNDS that must never be breached
let bad = [];
for (const a of [0.3, 1, 2, 3, 5, 8, 12, 25]) {
  for (const p of [5, 50, 300, 1200]) {
    t.price(p, p * a / 100);
    const sz = t.size(), st = t.stop();
    const stopPct = t.stopPctStated();
    if (sz > CAP * 0.20 + 1) bad.push(`size ${sz} > 20% cap (ATR ${a}%, px ${p})`);
    if (stopPct < 5 - 0.05 || stopPct > 30 + 0.05) bad.push(`stop ${stopPct.toFixed(1)}% outside [5,30] (ATR ${a}%, px ${p})`);
    if (t.riskStated() > RISK + 1) bad.push(`risk ₪${t.riskStated()} > 1R (ATR ${a}%, px ${p})`);
    if (st <= 0 || st >= p) bad.push(`stop ${st} not below price ${p}`);
  }
}
ck('size ≤ 20% of capital, stop in [5%,30%], risk ≤ 1R, stop < price', bad.length === 0,
   bad.length ? `${bad.length} breaches, first: ${bad[0]}` : '32 price/ATR combinations');

// ---------- 5. SCALE INVARIANCE: price shouldn't matter, only ATR as a % of it
const ref = (() => { t.price(100, 3); return t.size(); })();
let scaleBad = [];
for (const p of [7, 40, 250, 900, 4000]) {
  t.price(p, p * 0.03);
  if (Math.abs(t.size() - ref) / ref > 0.02) scaleBad.push(`px ${p}: ${t.size()} vs ${ref}`);
}
ck('same ATR% gives the same size at any price level', scaleBad.length === 0,
   scaleBad.length ? scaleBad[0] : 'ATR 3% across 5 price levels');

// ---------- 6. METAMORPHIC: known relationships between settings and output
// pick a case where risk-based actually binds (high ATR), or the assertion is vacuous.
// Beta must be supplied explicitly: at ATR 9% an unmeasured beta is estimated at 2.32,
// whose cap (₪10,762) correctly binds once 1R doubles — which would make the position
// beta-bound and the assertion untrue for a reason that has nothing to do with 1R.
// beta 1.0 puts the beta cap at ₪25,000, clear of both sizes.
const a = boot({ riskabs: RISK, maxspec: Math.round(CAP * 0.8) }); a.price(100, 9, 1.0);
const b = boot({ riskabs: RISK*2, maxspec: Math.round(CAP * 0.8) }); b.price(100, 9, 1.0);
ck('doubling 1R doubles a risk-bound position', Math.abs(b.size() - 2 * a.size()) / a.size() < 0.02,
   `${a.size()} → ${b.size()} (expected ~${2 * a.size()})`);
const c = boot({ maxspec: Math.round(CAP * 0.05) }); c.price(100, 3);
const dd = boot({ maxspec: Math.round(CAP * 0.5) }); dd.price(100, 3);
ck('a bigger speculative allowance does not shrink the position', dd.size() >= c.size(),
   `${c.size()} → ${dd.size()}`);
const e = boot({ capital: CAP * 0.3, maxspec: Math.round(CAP * 0.03) }); e.price(100, 1);
const f = boot({ capital: CAP * 3, maxspec: Math.round(CAP * 0.03) }); f.price(100, 1);
ck('more capital does not shrink the position', f.size() >= e.size(), `${e.size()} → ${f.size()}`);

// ---------- 7. LADDER invariants
let lbad = [];
for (const [p, at] of [[60.45, 6.48], [100, 1], [19, 2.85], [1000, 40], [8, 1.2]]) {
  t.price(p, at);
  const L = t.ladder(), st0 = t.stop();
  if (!L.length) { lbad.push(`no ladder at px ${p}`); continue; }
  for (let i = 1; i < L.length; i++)
    if (L[i].stop < L[i - 1].stop - 0.001) lbad.push(`stop moved DOWN at px ${p}: ${L[i-1].stop}→${L[i].stop}`);
  if (Math.abs(L[0].stop - st0) > 0.02) lbad.push(`first rung ${L[0].stop} != initial stop ${st0} at px ${p}`);
  // arming is volatility-scaled: max(15%, 3 x ATR%), so the expected rung moves with ATR
  const atrPct = at / p * 100;
  const expTrig = Math.max(15, 3 * atrPct);
  const armRow = L.find(r => Math.abs(r.stop - p) < 0.02);
  if (!armRow) lbad.push(`no breakeven rung at px ${p}`);
  else if (Math.abs((armRow.px / p - 1) * 100 - expTrig) > 1.5)
    lbad.push(`breakeven rung at +${((armRow.px/p-1)*100).toFixed(1)}%, expected +${expTrig.toFixed(1)}% (ATR ${atrPct.toFixed(1)}%)`);
  for (const r of L) if (r.stop > r.px) lbad.push(`stop ${r.stop} above trigger ${r.px} at px ${p}`);
}
ck('ladder: only rises, starts at the initial stop, hits breakeven at max(15%,3xATR), never above price',
   lbad.length === 0, lbad.length ? `${lbad.length} issues, first: ${lbad[0]}` : '5 scenarios');

report('INVARIANTS');
