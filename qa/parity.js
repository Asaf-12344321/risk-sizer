// Replay the Python cases through the real page and compare the initial stop.
const { boot, ck, report } = require('./lib');
const fs = require('fs');
const ref = JSON.parse(fs.readFileSync('/tmp/parity.json', 'utf8'));
const t = boot();
// read the page's own parameters out of its settings so a mismatch is explicit
const jsParams = { init: +t.$('cfg-initmult').value, trail: +t.$('cfg-trailmult').value,
                   arm: +t.$('cfg-armpct').value, armatrmult: +t.$('cfg-armatrmult').value,
                   min: +t.$('cfg-minstop').value, max: +t.$('cfg-maxstop').value };
console.log('  page parameters  :', JSON.stringify(jsParams));
console.log('  riskml parameters:', JSON.stringify(ref.params));
for (const k of Object.keys(ref.params)) {
  ck(`parameter "${k}" matches between tool and research code`,
     Math.abs(jsParams[k] - ref.params[k]) < 1e-9, `page ${jsParams[k]} vs riskml ${ref.params[k]}`);
}
let mismatch = 0;
for (const c of ref.cases) {
  t.price(c.price, c.atr);
  const jsStop = t.stop();
  // compare on the % distance, which is precision-independent
  const jsPct = (c.price - jsStop) / c.price * 100;
  const pyPct = (c.price - c.py_stop) / c.price * 100;
  const ok = Math.abs(jsPct - pyPct) < 0.06;
  if (!ok) mismatch++;
  console.log(`    px ${String(c.price).padStart(8)} ATR ${String(c.atr).padStart(6)}` +
    `  page ${jsPct.toFixed(2).padStart(6)}%  riskml ${pyPct.toFixed(2).padStart(6)}%  ${ok ? 'ok' : 'MISMATCH'}`);
}
ck('initial stop agrees with the research simulator on every case', mismatch === 0,
   mismatch ? `${mismatch} of ${ref.cases.length} disagree` : `${ref.cases.length} cases`);

// arming trigger must match too, or the tool arms at a different point than was validated
let armBad = 0;
console.log('    arming trigger:');
for (const c of ref.cases) {
  t.price(c.price, c.atr);
  const L = t.ladder();
  const armRow = L.find(r => Math.abs(r.stop - c.price) < 0.02);
  if (!armRow) continue;
  const jsTrig = (armRow.px / c.price - 1) * 100;
  const ok = Math.abs(jsTrig - c.py_arm_trigger_pct) < 0.6;
  if (!ok) armBad++;
  console.log(`      px ${String(c.price).padStart(8)} ATR ${String(c.atr).padStart(6)}` +
    `  page +${jsTrig.toFixed(1)}%  riskml +${c.py_arm_trigger_pct.toFixed(1)}%  ${ok ? 'ok' : 'MISMATCH'}`);
}
ck('arming trigger agrees with the research simulator', armBad === 0,
   armBad ? `${armBad} disagree` : `${ref.cases.length} cases`);
report('PARITY');
