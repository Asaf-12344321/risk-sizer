// Initial-stop precedence in the calculator: manual chart stop > structural > ATR ladder,
// the extended-from-support warning, and the stop being sent to the server for seeding.
const { boot, ck, report } = require('./lib');
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));

function page(structural) {
  const t = boot({ capital: 200000, riskabs: 4000, maxspec: 100000 });
  const inner = t.w.fetch;
  t.w.fetch = (u, o) => {
    if (/\/api\/stop\/suggest/.test(String(u)))
      return Promise.resolve({ ok: !!structural, status: structural ? 200 : 503,
        json: () => Promise.resolve(structural) });
    return inner(u, o);
  };
  t.set('in-ticker', 'NEW');
  t.price(100, 6, 1.2);                       // 6% ATR: the ATR ladder alone would give a 15% stop
  return t;
}
const SUP = { stop: 90, no_support: false, extended: false, support_lo: 91, support_hi: 93,
              support_gap_pct: 7, justification: 'below support 91.00-93.00 (3 touches, volume node); uptrend, ADX 31' };

(async () => {
  const atrOnly = page(null);
  await wait(800);
  ck('no structural data: ATR ladder is used', Math.abs(atrOnly.stop() - 85) < 0.01, String(atrOnly.stop()));
  const atrSize = atrOnly.size();

  const s = page(SUP);
  await wait(800);
  ck('structural stop replaces the ATR stop', Math.abs(s.stop() - 90) < 0.01, String(s.stop()));
  ck('structural stop is labelled and justified',
     /technical stop/.test(s.stopSub()) && /3 touches, volume node/.test(s.$('out-stopwhy').textContent));
  ck('tighter structural stop gives a bigger position', s.size() > atrSize, `${s.size()} vs ${atrSize}`);
  ck('risk stays within 1R', s.riskStated() <= 4000, String(s.riskStated()));
  ck('no timing warning when support is near', !/Extended from support/.test(s.flags()));

  s.set('in-manual-stop', '94');
  ck('manual price overrides structural', Math.abs(s.stop() - 94) < 0.01 && /your chart stop/.test(s.stopSub()),
     s.stop() + ' ' + s.stopSub());
  ck('manual stop: position held at the max-position cap, risk below 1R',
     s.size() === 40000 && s.riskStated() === 2400 && s.riskStated() <= 4000, s.size() + ' ' + s.riskStated());
  s.set('in-manual-unit', 'pct'); s.set('in-manual-stop', '5');
  ck('manual percent stop', Math.abs(s.stop() - 95) < 0.01, String(s.stop()));
  ck('position cap still applies to a very tight manual stop', s.size() <= 200000 * 0.20 + 1, String(s.size()));
  s.set('in-manual-stop', '120');
  ck('invalid manual stop is rejected and the technical stop is used',
     /below the entry/.test(s.flags()) && Math.abs(s.stop() - 90) < 0.01, s.flags());
  s.set('in-manual-stop', '');
  ck('clearing the manual stop returns to structural', Math.abs(s.stop() - 90) < 0.01);

  s.$('riskCheckBtn').dispatchEvent(new s.w.Event('click', { bubbles: true }));
  await wait(900);
  s.$('addPosBtn').dispatchEvent(new s.w.Event('click', { bubbles: true }));
  await wait(30);
  const post = s.w.__positionRequests.find(r => r.method === 'POST');
  ck('saved position carries the chosen stop and its source',
     post && Math.abs(post.body.initial_stop - 90) < 0.01 && post.body.stop_source === 'STRUCTURAL',
     JSON.stringify(post && post.body));

  const far = page({ stop: 70, no_support: false, extended: true, support_lo: 71, support_hi: 72,
                     support_gap_pct: 28, justification: 'below support 71-72' });
  await wait(800);
  ck('support 28% away shows the timing warning',
     /⚠️ Extended from support — poor R\/R entry timing/.test(far.flags()), far.flags());
  ck('extended entry is still sized honestly, not hidden', far.size() > 0);

  const none = page({ stop: null, no_support: true, extended: true, reasons: [], justification: 'none' });
  await wait(800);
  ck('no support at all shows the timing warning',
     /Extended from support — poor R\/R entry timing/.test(none.flags()), none.flags());
  ck('no support falls back to the ATR stop', Math.abs(none.stop() - 85) < 0.01, String(none.stop()));

  report('entry_stop');
  process.exit(0);
})();
