// Regression tests for the three documented defects. Each fails on the old build.
const { boot, ck, report } = require('./lib');
const { JSDOM } = require('jsdom'); const fs = require('fs');
const HTML = fs.readFileSync(__dirname + '/../risk-sizer.html', 'utf8');

// ---------- DEF-004: sub-dollar prices must not render a zero stop ----------
const t = boot();
for (const [p, a, label] of [[0.004, 0.001, 'sub-penny'], [0.55, 0.06, 'sub-dollar'],
                             [4.5, 0.4, 'few dollars'], [338.19, 8.02, 'normal']]) {
  t.price(p, a);
  const shown = t.$('out-stopprice').textContent;
  ck(`DEF-004 ${label} (px ${p}) stop renders non-zero`, parseFloat(shown) > 0, `shows "${shown}"`);
}
t.price(0.004, 0.001);
ck('DEF-004 ladder rows also non-zero',
   t.ladder().every(r => r.stop > 0 && r.px > 0), JSON.stringify(t.ladder().slice(0, 2)));

// ---------- DEF-003: no setting may exist without logic reading it ----------
const js = /<script>\n([\s\S]*?)\n<\/script>/.exec(HTML)[1];
const defs = /var DEFAULTS = \{([\s\S]*?)\};/.exec(js)[1];
const keys = [...defs.matchAll(/^\s{4}(\w+):/gm)].map(m => m[1]);
const dead = keys.filter(k => !new RegExp('cfg\\.' + k + '\\b').test(js));
ck('DEF-003 every setting is read by some logic', dead.length === 0, dead.join(', ') || `${keys.length} settings`);
ck('DEF-003 falsified RSI knobs are gone', !/rsiwarn|runupwarn/.test(HTML));
ck('DEF-003 holddays now drives a position-age warning', /ageDays >= cfg\.holddays/.test(js));
ck('DEF-003 holddays defaults to the evidenced 90 days', /holddays: 90/.test(js));

// the warning must actually fire, and only when old enough
for (const [added, shouldWarn] of [['2020-01-01', true], [new Date().toISOString().slice(0, 10), false]]) {
  const d = new JSDOM(HTML, { runScripts: 'dangerously', url: 'https://qa.local/', beforeParse(w) {
    w.localStorage.setItem('riskSizerPositions_v4',
      JSON.stringify([{ tick: 'TEST', entry: 100, atr: 3, rung: 0, added }]));
    w.fetch = () => Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) });
  }});
  const g = (id) => d.window.document.getElementById(id);
  const set = (id, v) => { const n = g(id); n.value = v; n.dispatchEvent(new d.window.Event('input', { bubbles: true })); };
  set('setup-capital', '100000'); set('setup-maxspec', '6000');
  g('setupSave').dispatchEvent(new d.window.Event('click', { bubbles: true }));
  g('tab-pos').dispatchEvent(new d.window.Event('click', { bubbles: true }));
  const warned = /Held \d+ days/.test(g('posList').textContent);
  ck(`DEF-003 age warning ${shouldWarn ? 'fires' : 'stays quiet'} for a position added ${added}`,
     warned === shouldWarn);
}

// ---------- DEF-002: storage keys aligned, and the old flag migrates ----------
ck('DEF-002 setup key is now v4', /SETUP_KEY = "riskSizerSetup_v4"/.test(js));
const mig = new JSDOM(HTML, { runScripts: 'dangerously', url: 'https://qa.local/', beforeParse(w) {
  w.localStorage.setItem('riskSizerSetup_v3', '1');            // only the OLD flag
  w.localStorage.setItem('riskSizerSettings_v4', JSON.stringify({ capital: 100000, breakbudget: 3300 }));
  w.fetch = () => Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) });
}});
ck('DEF-002 an existing user is not sent back through setup',
   mig.window.document.getElementById('appBody').hidden === false
   && mig.window.document.getElementById('setupCard').hidden === true);
ck('DEF-002 the migration writes the v4 flag',
   mig.window.localStorage.getItem('riskSizerSetup_v4') === '1');


// ---------- DEF-005: capping the stop must not cap the RISK used for sizing ----------
// Placing the order at 30% does not stop the price falling 3x ATR. Sizing off the capped
// figure assumed protection the stop cannot deliver, and made every name above 10% ATR
// the same size.
{
  const t2 = boot();
  const sizes = {};
  for (const [label, atrPct] of [['10.6%', 10.6], ['12.2%', 12.2], ['14.2%', 14.2], ['16.5%', 16.5]]) {
    t2.price(100, atrPct);
    sizes[label] = t2.size();
  }
  const vals = Object.values(sizes);
  ck('DEF-005 names above the stop ceiling get DIFFERENT sizes',
     new Set(vals).size === vals.length, JSON.stringify(sizes));
  ck('DEF-005 more volatile still means smaller',
     vals.every((v, i) => i === 0 || v < vals[i - 1]), vals.join(' > '));

  t2.price(100, 16.5);
  const stopShown = t2.stopPctStated();
  ck('DEF-005 the ORDER is still placed at the 30% ceiling',
     Math.abs(stopShown - 30) < 0.05, `${stopShown}%`);
  ck('DEF-005 sizing used the full 3x ATR distance, not 30%',
     Math.abs(t2.size() - (1300 / 0.495)) / t2.size() < 0.02,
     `₪${t2.size()} vs expected ~₪${Math.round(1300 / 0.495)}`);
  ck('DEF-005 the fuller loss is disclosed on screen',
     /if it runs the full/.test(t2.stopSub()), t2.stopSub());
  ck('DEF-005 loss at the placed stop is still within 1R',
     t2.riskStated() <= 1300 + 1, `₪${t2.riskStated()}`);

  // a stock below the ceiling must be untouched by this change
  t2.price(100, 3);
  ck('DEF-005 uncapped stocks are unaffected',
     !/if it runs the full/.test(t2.stopSub()) && Math.abs(t2.stopPctStated() - 9) < 0.05,
     t2.stopSub());
}

report('DEFECT REGRESSIONS');
