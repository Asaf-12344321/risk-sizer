// EDGE CASES — degenerate and hostile inputs must fail safely, never silently wrongly.
const { boot, ck, report } = require('./lib');
const CAP = +(process.env.QA_CAPITAL || 100000), RISK = +(process.env.QA_RISK || 1300);

const cases = [
  ['zero price',        0,    5,    'blank'],
  ['zero ATR',          100,  0,    'blank'],
  ['negative price',    -50,  5,    'blank'],
  ['negative ATR',      100,  -5,   'blank'],
  ['ATR larger than price', 100, 250, 'sized'],
  ['sub-penny price',   0.004, 0.001, 'sized'],
  ['huge price',        1e6,  3e4,  'sized'],
  ['absurd ATR',        100,  1e9,  'sized'],
  ['tiny ATR',          100,  1e-9, 'sized'],
];
const t = boot();
for (const [name, p, a, expect] of cases) {
  t.price(p, a);
  const sz = t.size(), txt = t.$('out-size').textContent;
  if (expect === 'blank') {
    ck(`${name} → refuses to answer`, txt.includes('—'), `showed "${txt}"`);
  } else {
    const st = t.stop(), sp = t.stopPctStated();
    ck(`${name} → answers within the caps`,
       Number.isFinite(sz) && sz > 0 && sz <= CAP * 0.2 + 1 && st > 0 && st < p
         && sp >= 4.95 && sp <= 30.05 && t.riskStated() <= RISK + 1,
       `size ₪${sz} stop ${st} (${sp}%) risk ₪${t.riskStated()}`);
  }
}
// non-numeric junk
for (const junk of ['abc', '', '1e999', '--5', '1,2,3', 'NaN', 'Infinity']) {
  t.set('in-price', junk); t.set('in-atr', '5');
  ck(`junk price "${junk}" → no NaN leaks to the screen`,
     !/NaN|Infinity|undefined/.test(t.$('out-size').textContent + t.$('out-stopsub').textContent
       + t.$('out-ladder').textContent), t.$('out-size').textContent);
}
// settings that could divide by zero
for (const [field, val] of [['cfg-riskabs','0'], ['cfg-breakbudget','0'], ['cfg-maxpct','0'],
                            ['cfg-minstop','0'], ['cfg-maxstop','0'], ['cfg-initmult','0'],
                            ['cfg-armpct','0'], ['cfg-gapt1','0'], ['cfg-gapt5','0']]) {
  const z = boot(); z.set(field, val); z.price(100, 3);
  const out = z.$('out-size').textContent + z.$('out-stopsub').textContent + z.$('out-ladder').textContent;
  ck(`${field}=0 → no NaN/Infinity on screen`, !/NaN|Infinity|undefined/.test(out), out.slice(0, 70));
}
// no feed at all
const nf = boot({ quotes: '/nonexistent.json' });
nf.price(60.45, 6.48);
ck('feed unavailable → manual sizing still works', nf.size() > 0, `₪${nf.size()}`);
nf.lookup('AAPL');
ck('feed unavailable → lookup explains itself', /unavailable|hasn.t loaded/i.test(nf.$('feedStatus').textContent),
   nf.$('feedStatus').textContent.slice(0, 60));
// setup gate
const ns = boot({ capital: 0, maxspec: 0, riskabs: 0 });
ns.price(100, 3);
ck('no setup → refuses to size anything', ns.$('out-size').textContent.includes('—'));
report('EDGE CASES');
