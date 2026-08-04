// The FEED PATH — fillFromFeed(), i.e. what happens when a ticker is looked up.
//
// This surface had no coverage at all, which is exactly why DEF-006 survived: every
// other suite calls price(p, atr, beta) directly and so never touched the code that
// copies feed values into the form. The truncation bug lived in that copy step and was
// structurally invisible to 108 passing assertions.
//
// Two things make this file different from its siblings:
//   - the feed arrives on a promise, so assertions must wait for it (the earlier
//     version of this measurement silently compared 2,604 empty lookups and reported
//     "0 changed", which looked like a clean bill of health);
//   - it needs a real quotes.json. run_all.sh fetches one if it is missing.
const { boot, ck, report } = require('./lib');
const fs = require('fs');
const QUOTES = process.env.QA_QUOTES || '/tmp/q.json';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const quotes = JSON.parse(fs.readFileSync(QUOTES, 'utf8')).tickers;
  const syms = Object.keys(quotes);
  const t = boot();
  await wait(300);                       // let the injected feed promise settle

  // ---------- coverage guard: prove the lookup path actually ran ----------
  t.lookup(syms[0]);
  ck('feed path is exercised at all (lookup populates the form)',
     +t.$('in-atr').value > 0 && t.size() > 0,
     `${syms[0]}: atr field "${t.$('in-atr').value}", size ₪${t.size()}`);

  // ---------- DEF-006: feed ATR must reach the form at full precision ----------
  // .toFixed(2) discarded two of the feed's four decimals. It changed the size of 909
  // of 2,604 tickers (34.9%), 24 of them by more than 1%, worst ABEV at 3.05%. The
  // extreme relative ATR errors (TALK 12.4%) did NOT move size, because the 8% minimum
  // stop floors them — which is why this needs measuring, not reasoning about.
  let atrBad = [], pxBad = [];
  for (const s of syms) {
    t.lookup(s);
    const shown = parseFloat(t.$('in-atr').value);
    if (!(Math.abs(shown - quotes[s].a) < 1e-9)) atrBad.push(`${s} ${shown} vs ${quotes[s].a}`);
    const sp = parseFloat(t.$('in-price').value);
    if (!(Math.abs(sp - quotes[s].l) < 1e-9)) pxBad.push(`${s} ${sp} vs ${quotes[s].l}`);
  }
  ck('DEF-006 feed ATR reaches the form unrounded, every ticker',
     atrBad.length === 0, atrBad.length ? `${atrBad.length} wrong, e.g. ${atrBad[0]}` : `${syms.length} tickers exact`);
  ck('DEF-006 feed price reaches the form unrounded too',
     pxBad.length === 0, pxBad.length ? `${pxBad.length} wrong, e.g. ${pxBad[0]}` : `${syms.length} tickers exact`);

  // ---------- DEF-007: the beta cap must never be silently absent ----------
  // It used to be applied only `if (beta > 0)`, so a missing or non-positive beta
  // dropped the constraint instead of tightening it — 278 of 2,604 tickers (10.7%).
  const noCap = [], badCap = [];
  for (const s of syms) {
    t.lookup(s);
    const m = /beta cap ₪([\d,]+)/.exec(t.formula());
    if (!m) { noCap.push(s); continue; }
    const v = parseFloat(m[1].replace(/,/g, ''));
    if (!(v > 0) || !Number.isFinite(v)) badCap.push(`${s} → ₪${m[1]}`);
  }
  ck('DEF-007 beta cap is present for every ticker in the feed',
     noCap.length === 0, noCap.length ? `${noCap.length} missing, e.g. ${noCap.slice(0,4)}` : `${syms.length} tickers`);
  ck('DEF-007 beta cap is always positive and finite',
     badCap.length === 0, badCap.length ? badCap[0] : 'no zero/negative/NaN caps');

  // ---------- the two cases must be reported differently, and truthfully ----------
  const notMeasurable = syms.filter((s) => quotes[s].b === undefined);
  const nonPositive   = syms.filter((s) => quotes[s].b !== undefined && quotes[s].b <= 0);

  if (notMeasurable.length) {
    const s = notMeasurable.sort((a, b) => quotes[b].a / quotes[b].l - quotes[a].a / quotes[a].l)[0];
    t.lookup(s);
    ck('unmeasurable beta is estimated from ATR and disclosed as such',
       /estimated from ATR/.test(t.formula()), `${s}: ${(/beta cap[^)]*\)/.exec(t.formula()) || [''])[0]}`);
    ck('an estimated beta is marked (est) on the stock line',
       /\(est\)/.test(t.stockLine()), t.stockLine().slice(0, 90));
  }
  if (nonPositive.length) {
    const s = nonPositive[0];
    t.lookup(s);
    const f = t.formula();
    // A measured beta <= 0 is usually REAL (WMT -0.10, JNJ -0.21, XOM -0.48 — defensive
    // names decoupling from a tech-led SPY), so it must NOT be called unmeasurable.
    ck('a measured beta <= 0 is NOT mislabelled as estimated',
       !/estimated from ATR/.test(f) && /≤ 0, floored/.test(f), `${s}: ${(/beta cap[^)]*\)/.exec(f) || [''])[0]}`);
    ck('a measured beta <= 0 leaves the market-exposure cap non-binding',
       !/▸ beta cap/.test(f), `${s} binds on ${(/▸ ([a-z -]+)/.exec(f) || ['', '?'])[1]}`);
    ck('a real negative beta is shown as measured, not hidden',
       !/\(est\)/.test(t.stockLine()), t.stockLine().slice(0, 90));
  }

  // ---------- the estimate must stay inside the data it was fitted on ----------
  // Unclamped, 0.235 + 0.232 x ATR% reached beta 2.3e8 at the absurd-ATR edge case and
  // drove the cap to ~0, making the tool refuse to size anything.
  const capAt = (atrPct) => { t.price(100, atrPct, undefined);
    const m = /÷ beta ([\d.]+)/.exec(t.formula()); return m ? parseFloat(m[1]) : NaN; };
  const lo = capAt(0.5), hi = capAt(1e7);
  ck('estimated beta is floored at 1.0', Math.abs(lo - 1.0) < 0.01, `ATR 0.5% → beta ${lo}`);
  ck('estimated beta is capped at 6.0 (the fit\'s support)', Math.abs(hi - 6.0) < 0.01,
     `ATR 1e7% → beta ${hi}`);
  t.price(100, 1e9, undefined);
  ck('an absurd ATR still yields a finite positive size, not a refusal',
     Number.isFinite(t.size()) && t.size() > 0, `₪${t.size()}`);

  // ---------- hand entry: a blank beta must still be capped ----------
  const h = boot(); await wait(50);
  h.price(100, 9, undefined);
  ck('blank hand-typed beta is estimated, not skipped',
     /beta cap/.test(h.formula()) && /estimated from ATR/.test(h.formula()),
     (/beta cap[^)]*\)/.exec(h.formula()) || [''])[0]);
  h.price(100, 9, -0.5);
  const nm = /beta cap ₪([\d,]+)/.exec(h.formula());
  ck('hand-typed negative beta yields a positive cap, not a negative one',
     nm && parseFloat(nm[1].replace(/,/g, '')) > 0 && h.size() > 0,
     `cap ${nm ? '₪' + nm[1] : 'absent'}, size ₪${h.size()}`);
  h.price(100, 9, 1.0);
  ck('a measured beta is used exactly as given',
     /÷ beta 1\.00/.test(h.formula()), (/beta cap[^)]*\)/.exec(h.formula()) || [''])[0]);

  report('FEED PATH');
})();
