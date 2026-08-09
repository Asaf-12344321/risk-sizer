// UNIVERSE SWEEP — run every real ticker through the calculator and inspect the
// DISTRIBUTION of answers. The step-function bug was invisible per-stock and obvious here.
const { boot, ck, report } = require('./lib');
const CAP = +(process.env.QA_CAPITAL || 100000), RISK = +(process.env.QA_RISK || 1300);
const t = boot();
const q = t.quotes();
const rows = [];
for (const [sym, v] of Object.entries(q)) {
  if (!(v.l > 0) || !(v.a > 0)) continue;
  t.price(v.l, v.a, v.b);
  const sz = t.size(), st = t.stop();
  const bm = /▸ ([a-z- ]+)/.exec(t.formula());
  rows.push({ sym, atrPct: v.a / v.l * 100, size: sz, stop: st, beta: v.b,
              bind: bm ? bm[1].trim() : '?',
              stopPct: t.stopPctStated(), risk: t.riskStated() });
}
console.log(`swept ${rows.length.toLocaleString()} tickers\n`);

const uniq = new Set(rows.map(r => Math.round(r.size))).size;
ck('sizes are broadly distributed, not clustered on a few values',
   uniq > rows.length * 0.5, `${uniq.toLocaleString()} distinct sizes across ${rows.length.toLocaleString()} tickers`);

// no more than 15% of the universe may share any single size
const counts = {};
rows.forEach(r => { const k = Math.round(r.size); counts[k] = (counts[k] || 0) + 1; });
const top = Object.entries(counts).sort((x, y) => y[1] - x[1])[0];
ck('no single size dominates the universe', top[1] / rows.length < 0.15,
   `most common size ₪${(+top[0]).toLocaleString()} appears ${top[1]} times (${(top[1]/rows.length*100).toFixed(1)}%)`);

// every ticker must satisfy the hard rules
// Tolerances reflect DISPLAY PRECISION: size is shown to ₪1 and the stop % to 0.1pp.
const breaches = rows.filter(r => r.size > CAP * 0.2 + 1 || r.risk > RISK + 1
                              || r.stopPct < 5 - 0.05 || r.stopPct > 30 + 0.05 || r.size <= 0);
ck('every ticker respects the caps', breaches.length === 0,
   breaches.length ? `${breaches.length} breaches, e.g. ${breaches[0].sym} risk ₪${breaches[0].risk} stop ${breaches[0].stopPct}%`
                   : `${rows.length.toLocaleString()} tickers, using the tool's own reported risk/stop`);

// Ordering: size must fall as ATR rises — but ONLY where ATR is actually the input.
// This check used to be asserted across EVERY binding constraint, which was wrong: the
// beta cap is `capital x betaexpct / beta` and ATR does not appear in it at all. Beta
// tracks ATR only as a band-median fit (corr 0.946), never per ticker, so sorting the
// beta-cap group by ATR and demanding monotone size asserts something the formula never
// promised. It failed on exactly that — BMNR ATR 6.35% ₪6,172 -> QUBT ATR 6.55% ₪6,750,
// two names whose measured betas run the other way. The tool was right and the test was
// wrong. Assert each cap against the variable its own formula uses.
const ATR_DRIVEN = new Set(['risk-based', 'crash cap']);
const seenBinds = new Set(rows.map(r => r.bind));
let inv = 0, invEg = '';
for (const bind of seenBinds) {
  if (!ATR_DRIVEN.has(bind)) continue;
  const g = rows.filter(r => r.bind === bind).sort((x, y) => x.atrPct - y.atrPct);
  for (let i = 1; i < g.length; i++)
    if (g[i].size > g[i - 1].size + 1) {
      inv++;
      if (!invEg) invEg = `${bind}: ${g[i-1].sym} ATR ${g[i-1].atrPct.toFixed(2)}% ₪${g[i-1].size} → ${g[i].sym} ATR ${g[i].atrPct.toFixed(2)}% ₪${g[i].size}`;
    }
}
ck('where ATR is the driving input, size falls as ATR rises', inv === 0,
   inv ? `${inv} inversions, e.g. ${invEg}`
       : `${[...seenBinds].filter(b => ATR_DRIVEN.has(b)).join(', ')}`);

// The beta cap gets the check its own formula supports, which is stronger than an
// ordering: size x beta must equal the exposure budget exactly.
const betaexpct = +t.$('cfg-betaexpct').value;
const budget = CAP * betaexpct / 100;
const betaRows = rows.filter(r => r.bind === 'beta cap' && r.beta > 0);
// Tolerance scales with beta: size is read back from a display rounded to ₪1, so one
// rounding unit becomes `beta` shekels once multiplied. A flat tolerance reads a high-beta
// name as a breach — the same rounded-display trap documented in lib.js.
const betaBad = betaRows.filter(r => Math.abs(r.size * r.beta - budget) > r.beta + 1);
ck('beta-cap size is exactly the exposure budget divided by measured beta',
   betaRows.length > 0 && betaBad.length === 0,
   betaBad.length ? `${betaBad.length} off, e.g. ${betaBad[0].sym} beta ${betaBad[0].beta} size ₪${betaBad[0].size} -> ₪${(betaBad[0].size * betaBad[0].beta).toFixed(0)} vs budget ₪${budget}`
                  : `${betaRows.length.toLocaleString()} measured-beta tickers at ₪${budget.toLocaleString()} exposure`);

// Within the beta cap, size must fall as BETA rises — the ordering that formula does promise.
const byBeta = betaRows.slice().sort((x, y) => x.beta - y.beta);
let betaInv = 0, betaEg = '';
for (let i = 1; i < byBeta.length; i++)
  if (byBeta[i].size > byBeta[i - 1].size + 1) {
    betaInv++;
    if (!betaEg) betaEg = `${byBeta[i-1].sym} beta ${byBeta[i-1].beta} ₪${byBeta[i-1].size} → ${byBeta[i].sym} beta ${byBeta[i].beta} ₪${byBeta[i].size}`;
  }
ck('within the beta cap, size falls as beta rises', betaInv === 0,
   betaInv ? `${betaInv} inversions, e.g. ${betaEg}` : `${byBeta.length.toLocaleString()} tickers`);

ck('nothing produced NaN or a blank answer',
   rows.every(r => Number.isFinite(r.size) && Number.isFinite(r.stop) && r.size > 0));

const pctl = (p) => { const s = rows.map(r => r.size).sort((a, b) => a - b);
                      return s[Math.floor(p / 100 * (s.length - 1))]; };
console.log(`\n  size distribution: p5 ₪${pctl(5).toLocaleString()} · p25 ₪${pctl(25).toLocaleString()}`
  + ` · median ₪${pctl(50).toLocaleString()} · p75 ₪${pctl(75).toLocaleString()} · p95 ₪${pctl(95).toLocaleString()}`);
report('UNIVERSE SWEEP');
