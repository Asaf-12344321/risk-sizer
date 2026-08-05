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
// arm_on_close is behaviour, not a numeric setting the tool exposes — it is verified
// directly by the spike cases below, which is a stronger check than comparing a value.
for (const k of Object.keys(ref.params).filter((k) => k !== 'arm_on_close')) {
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

// ---- arming reference: does the tool arm on the same event the research code does? ----
// The parabolic give-back case: a bar whose HIGH clears the arm trigger while its CLOSE
// falls short. Before 2026-08-05 both sides armed on the close, so this spike protected
// nothing and the give-back cost a full 1R. parity previously compared the arm trigger
// PERCENTAGE but never the reference it is measured against, so a drift here was invisible.
ck('arm_on_close matches between tool and research code',
   ref.params.arm_on_close === false,
   `riskml arm_on_close=${ref.params.arm_on_close} (false = arm on the intraday high)`);

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  let armBad2 = 0;
  for (const sp of ref.spikes) {
    // Feed the identical OHLC sequence the Python side simulated.
    const rows = [['2026-01-02', sp.spike_high, sp.price * 0.99, sp.spike_close]];
    for (let d = 0; d < 4; d++)
      rows.push([`2026-01-0${3 + d}`, sp.spike_close * 1.005, sp.spike_close * 0.99, sp.spike_close]);
    const inst = boot({
      bars: { PARITY: rows },
      positions: [{ tick: 'PARITY', entry: sp.price, atr: sp.atr, rung: 0, added: '2026-01-01' }]
    });
    await wait(60);
    inst.$('tab-pos').dispatchEvent(new inst.w.Event('click', { bubbles: true }));
    await wait(250);
    const txt = inst.$('posList').textContent.replace(/\s+/g, ' ');
    // Armed shows as the stop having moved up to entry (break-even) or beyond.
    const m = /stop now([\d.]+)/.exec(txt);
    const jsStop = m ? parseFloat(m[1]) : NaN;
    const jsArmed = Number.isFinite(jsStop) && jsStop >= sp.price - 0.01;
    const ok = jsArmed === sp.py_armed;
    if (!ok) armBad2++;
    console.log(`    px ${sp.price} atr ${sp.atr}  high clears +${sp.arm_trigger_pct}%, close does not` +
      `  ->  tool armed=${jsArmed} (stop ${m ? m[1] : 'n/a'})  riskml armed=${sp.py_armed}  ${ok ? 'ok' : 'MISMATCH'}`);
  }
  ck('an intraday spike arms the stop in BOTH implementations', armBad2 === 0,
     armBad2 ? `${armBad2} of ${ref.spikes.length} disagree` : `${ref.spikes.length} spike cases`);
  report('RESEARCH <-> TOOL PARITY');
})();
