// Regression tests for the three documented defects. Each fails on the old build.
const { boot, ck, report } = require('./lib');
const CAP = +(process.env.QA_CAPITAL || 100000), RISK = +(process.env.QA_RISK || 1300);
const { JSDOM } = require('jsdom'); const fs = require('fs');
const HTML = require('./lib').HTML;   // resolves index.html or risk-sizer.html

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

ck('DEF-003 age warning compares the server opened_at date',
   /added: row\.opened_at/.test(js) && /ageDays >= cfg\.holddays/.test(js));

// ---------- DEF-002: portfolio/settings state is exclusively server-owned ----------
// The API credential may be remembered per device; no application data may be.
const storageCalls = [...js.matchAll(/localStorage\.(?:getItem|setItem|removeItem)\(([^)]*)\)/g)];
ck('DEF-002 browser persistence is limited to the API credential',
   storageCalls.length > 0 && storageCalls.every(match => /^API_KEY_STORE\b/.test(match[1])));
ck('DEF-002 settings hydrate from the server', /apiFetch\("\/api\/settings"\)/.test(js));
ck('DEF-002 Active positions hydrate from the server', /apiFetch\("\/api\/positions"\)/.test(js));


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

  // derive from the live multipliers so retuning does not break the assertion
  const IM = +t2.$('cfg-initmult').value, MAXS = +t2.$('cfg-maxstop').value;
  const wildATR = 16.5;
  t2.price(100, wildATR);
  const stopShown = t2.stopPctStated();
  ck('DEF-005 the ORDER is still placed at the max-stop ceiling',
     Math.abs(stopShown - MAXS) < 0.05, `${stopShown}% vs cap ${MAXS}%`);
  const impliedRisk = (IM * wildATR) / 100;
  ck(`DEF-005 sizing used the full ${IM}x ATR distance, not the ${MAXS}% ceiling`,
     Math.abs(t2.size() - (RISK / impliedRisk)) / t2.size() < 0.02,
     `₪${t2.size()} vs expected ~₪${Math.round(RISK / impliedRisk)}`);
  ck('DEF-005 the fuller loss is disclosed on screen',
     /if it runs the full/.test(t2.stopSub()), t2.stopSub());
  ck('DEF-005 loss at the placed stop is still within 1R',
     t2.riskStated() <= 1300 + 1, `₪${t2.riskStated()}`);

  // a stock whose IM x ATR sits inside [minstop, maxstop] must be untouched
  const MINS = +t2.$('cfg-minstop').value;
  const calmATR = (MINS / IM) + 2;               // comfortably inside both bounds
  t2.price(100, calmATR);
  ck('DEF-005 stocks inside the stop bounds are unaffected',
     !/if it runs the full/.test(t2.stopSub())
       && Math.abs(t2.stopPctStated() - IM * calmATR) < 0.1,
     `ATR ${calmATR}% -> stop ${t2.stopPctStated()}%, expected ${(IM*calmATR).toFixed(1)}%`);
}

// ---------- Dynamic arming: max(armpct, armatrmult x ATR%) ----------
{
  const t3 = boot();
  const trig = (px, atr) => {
    t3.price(px, atr);
    const L = t3.ladder(), arm = L.find(r => Math.abs(r.stop - px) < 0.02);
    return arm ? (arm.px / px - 1) * 100 : NaN;
  };
  ck('ARM floor holds on calm names (ATR 2%)', Math.abs(trig(100, 2) - 15) < 0.6, `+${trig(100,2).toFixed(1)}%`);
  ck('ARM floor holds at ATR 4%', Math.abs(trig(100, 4) - 15) < 0.6, `+${trig(100,4).toFixed(1)}%`);
  ck('ARM scales above ATR 5%', trig(100, 7) > 19, `+${trig(100,7).toFixed(1)}%`);
  ck('ARM = 3x ATR on a wild name (ATR 12%)', Math.abs(trig(100, 12) - 36) < 1, `+${trig(100,12).toFixed(1)}%`);
  const seq = [1, 2, 4, 6, 8, 10, 14, 18].map(a => trig(100, a));
  ck('ARM trigger is non-decreasing in ATR', seq.every((v, i) => i === 0 || v >= seq[i - 1] - 0.01), seq.map(v=>v.toFixed(0)).join(' '));
  ck('ARM never below the floor', seq.every(v => v >= 15 - 0.6), seq.map(v=>v.toFixed(0)).join(' '));

  // disabling the multiplier must reproduce the old flat behaviour exactly
  const flat = boot(); flat.set('cfg-armatrmult', '0');
  const ftrig = (px, atr) => { flat.price(px, atr);
    const L = flat.ladder(), a = L.find(r => Math.abs(r.stop - px) < 0.02);
    return a ? (a.px / px - 1) * 100 : NaN; };
  ck('armatrmult=0 reverts to a flat +15% for every ATR',
     [2, 6, 12, 20].every(a => Math.abs(ftrig(100, a) - 15) < 0.6),
     [2,6,12,20].map(a=>'+'+ftrig(100,a).toFixed(0)+'%').join(' '));

  // ---------- crash anchor corrected 71 -> 63 ----------
  const t4 = boot();
  t4.price(100, 14);
  const cp = /÷ ([\d.]+)% crash/.exec(t4.formula());
  ck('top crash anchor is now 63%, not 71%', cp && Math.abs(parseFloat(cp[1]) - 63) < 0.6,
     cp ? cp[1] + '%' : 'not found');
  const c10 = (() => { t4.price(100, 10); return parseFloat(/÷ ([\d.]+)% crash/.exec(t4.formula())[1]); })();
  const c14 = (() => { t4.price(100, 14); return parseFloat(/÷ ([\d.]+)% crash/.exec(t4.formula())[1]); })();
  ck('crash curve still rises into the corrected anchor', c14 > c10, `${c10}% -> ${c14}%`);
}

report('DEFECT REGRESSIONS');
