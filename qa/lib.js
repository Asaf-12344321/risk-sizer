// Shared harness: boot the real page in jsdom and drive it like a user.
const { JSDOM } = require('jsdom'); const fs = require('fs');
const HTML = fs.readFileSync(__dirname + '/../risk-sizer.html', 'utf8');
const QUOTES = process.env.QA_QUOTES || '/tmp/q.json';

// Neutral defaults so this suite carries no personal financial parameters.
// Override with QA_CAPITAL / QA_MAXSPEC / QA_RISK to exercise your own settings.
const D_CAPITAL = +(process.env.QA_CAPITAL || 100000);
const D_MAXSPEC = +(process.env.QA_MAXSPEC || 6000);
const D_RISK    = +(process.env.QA_RISK    || 1300);

function boot({ capital = D_CAPITAL, maxspec = D_MAXSPEC, riskabs = D_RISK,
                quotes = QUOTES, positions = null } = {}) {
  const feed = fs.existsSync(quotes) ? fs.readFileSync(quotes, 'utf8') : null;
  const store = {};
  if (positions) { store['riskSizerPositions_v4'] = JSON.stringify(positions); }
  const dom = new JSDOM(HTML, { runScripts: 'dangerously', url: 'https://qa.local/',
    beforeParse(w) {
      for (const k in store) w.localStorage.setItem(k, store[k]);
      w.fetch = (u) => (feed && /quotes\.json/.test(u))
        ? Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(feed)) })
        : Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) });
    } });
  const w = dom.window, $ = (id) => w.document.getElementById(id);
  const set = (id, v) => { const n = $(id); n.value = v;
    n.dispatchEvent(new w.Event('input', { bubbles: true })); };
  set('setup-capital', String(capital)); set('setup-maxspec', String(maxspec));
  $('setupSave').dispatchEvent(new w.Event('click', { bubbles: true }));
  if (riskabs) set('cfg-riskabs', String(riskabs));
  const money = (s) => parseFloat(String(s).replace(/[^\d.-]/g, '')) || 0;
  return {
    w, $, set,
    price(p, atr, beta) { set('in-price', String(p)); set('in-atr', String(atr));
                          set('in-beta', beta === undefined ? '' : String(beta)); },
    size:      () => money($('out-size').textContent),
    stop:      () => money($('out-stopprice').textContent),
    stopSub:   () => $('out-stopsub').textContent,
    // The tool's OWN figures are authoritative. Re-deriving them from rounded display
    // strings introduces error large enough to look like a cap breach — that mistake
    // produced 123 phantom failures the first time this suite ran.
    riskStated: () => { const m = /risking ₪([\d,]+)/.exec($('out-stopsub').textContent);
                        return m ? parseFloat(m[1].replace(/,/g, '')) : NaN; },
    stopPctStated: () => { const m = /([\d.]+)% below entry/.exec($('out-stopsub').textContent);
                           return m ? parseFloat(m[1]) : NaN; },
    formula:   () => $('out-size-formula').textContent,
    stockLine: () => $('stockLine').textContent,
    flags:     () => $('out-flags').textContent,
    ladder:    () => [...$('out-ladder').querySelectorAll('tr')].slice(1)
                       .map(r => [...r.children].map(c => c.textContent.trim()))
                       .map(([px, st]) => ({ px: money(px), stop: money(st) })),
    lookup(t)  { $('in-lookup').value = t;
                 $('lookupBtn').dispatchEvent(new w.Event('click', { bubbles: true })); },
    quotes:    () => JSON.parse(fs.readFileSync(quotes, 'utf8')).tickers,
  };
}

let pass = 0; const fails = [];
function ck(name, cond, detail = '') {
  if (cond) { pass++; } else { fails.push(name + (detail ? '  — ' + detail : '')); }
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${detail ? '  ' + detail : ''}`);
}
function report(suite) {
  console.log(`\n${suite}: ${pass} passed, ${fails.length} failed`);
  if (fails.length) { console.log('FAILURES:'); fails.forEach(f => console.log('  · ' + f)); }
  process.exit(fails.length ? 1 : 0);
}
module.exports = { boot, ck, report };
