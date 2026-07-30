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
  rows.push({ sym, atrPct: v.a / v.l * 100, size: sz, stop: st,
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

// ordering: sort by ATR and confirm size is non-increasing
const byAtr = [...rows].sort((x, y) => x.atrPct - y.atrPct);
let inv = 0;
for (let i = 1; i < byAtr.length; i++) if (byAtr[i].size > byAtr[i - 1].size + 1) inv++;
ck('ranking by ATR gives a non-increasing size curve', inv === 0, `${inv} inversions`);

ck('nothing produced NaN or a blank answer',
   rows.every(r => Number.isFinite(r.size) && Number.isFinite(r.stop) && r.size > 0));

const pctl = (p) => { const s = rows.map(r => r.size).sort((a, b) => a - b);
                      return s[Math.floor(p / 100 * (s.length - 1))]; };
console.log(`\n  size distribution: p5 ₪${pctl(5).toLocaleString()} · p25 ₪${pctl(25).toLocaleString()}`
  + ` · median ₪${pctl(50).toLocaleString()} · p75 ₪${pctl(75).toLocaleString()} · p95 ₪${pctl(95).toLocaleString()}`);
report('UNIVERSE SWEEP');
