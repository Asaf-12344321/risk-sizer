// Shared harness: boot the real page in jsdom and drive it like a user.
const { JSDOM } = require('jsdom'); const fs = require('fs');
// The tool ships as index.html. The risk-sizer.html fallback dates from when a second
// working copy lived in ~/trading-tools; that directory was consolidated away on
// 2026-08-04, but the fallback stays so a clone under either name still runs rather than
// crashing on a missing path — a crash the runner would report as zero tests.
const HTML_PATH = process.env.QA_HTML || [
  __dirname + '/../risk-sizer.html',
  __dirname + '/../index.html',
].find(p => fs.existsSync(p));
if (!HTML_PATH) throw new Error('no risk-sizer.html or index.html found next to qa/');
const HTML = fs.readFileSync(HTML_PATH, 'utf8');
const QUOTES = process.env.QA_QUOTES || '/tmp/q.json';

// Neutral defaults so this suite carries no personal financial parameters.
// Override with QA_CAPITAL / QA_MAXSPEC / QA_RISK to exercise your own settings.
const D_CAPITAL = +(process.env.QA_CAPITAL || 100000);
const D_MAXSPEC = +(process.env.QA_MAXSPEC || 6000);
const D_RISK    = +(process.env.QA_RISK    || 1300);

function boot({ capital = D_CAPITAL, maxspec = D_MAXSPEC, riskabs = D_RISK,
                quotes = QUOTES, positions = null, bars = {}, riskResponse = null } = {}) {
  const feed = fs.existsSync(quotes) ? fs.readFileSync(quotes, 'utf8') : null;
  const barsFixture = bars;
  const serverPositions = (positions || []).map((p, i) => ({
    id: p.id || i + 1, ticker: p.ticker || p.tick, entry_price: p.entry_price || p.entry,
    atr: p.atr, quantity: p.quantity || 1, value_ils: p.value_ils || 1,
    currency: p.currency || 'ILS', fx_to_ils: p.fx_to_ils || 1,
    risk_status: p.risk_status || 'RISK_ON', rung: p.rung || 0,
    display_name: p.display_name || '', legacy: Boolean(p.legacy),
    opened_at: p.opened_at || p.added || new Date().toISOString().slice(0, 10)
  }));
  const dom = new JSDOM(HTML, { runScripts: 'dangerously', url: 'https://qa.local/',
    beforeParse(w) {
      // bars/<SYM>.json is served from `barsFixture` when a test sets one. Without this the
      // position tracker's replay path (liveState) is unreachable, which is how the
      // close-vs-high arming reference went untested on both sides.
      w.__riskRequests = [];
      w.__positionRequests = [];
      w.__serverPositions = serverPositions;
      w.__serverCore = [];
      const response = (body, status = 200) => Promise.resolve({
        ok: status >= 200 && status < 300, status,
        json: () => Promise.resolve(body)
      });
      w.fetch = (u, options = {}) => {
        const url = String(u), method = (options.method || 'GET').toUpperCase();
        if (/\/api\/settings$/.test(url)) {
          if (method === 'PUT') w.__settingsRequest = JSON.parse(options.body || '{}');
          return response({
            settings: Object.assign({}, {
              capital, riskpct: 2, riskabs, maxdailyvar: 25000, maxriskonr: 5, breakbudget: maxspec * .63,
              maxpct: 20, betaexpct: 25, gapt1: 22, gapt2: 25, gapt3: 34,
              gapt4: 44, gapt5: 51, gapt6: 56, gapt7: 60, gapt8: 63,
              liqpct: 5, initmult: 2.5, trailmult: 3.5, armpct: 15,
              armatrmult: 3, minstop: 8, maxstop: 30, drawdown: 40,
              holddays: 90, fx: 3.65
            }, w.__settingsRequest && w.__settingsRequest.settings),
            setup_complete: true
          });
        }
        if (/\/api\/core(?:\/\d+)?$/.test(url)) {
          if (method === 'GET') return response(w.__serverCore);
          const body = options.body ? JSON.parse(options.body) : {};
          if (method === 'POST') { body.id = w.__serverCore.length + 1; w.__serverCore.push(body); return response(body, 201); }
          const id = +(url.match(/(\d+)$/) || [])[1], ix = w.__serverCore.findIndex(p => p.id === id);
          if (method === 'PUT' && ix >= 0) { Object.assign(w.__serverCore[ix], body); return response(w.__serverCore[ix]); }
          if (method === 'DELETE' && ix >= 0) { w.__serverCore.splice(ix, 1); return response(null, 204); }
        }
        if (/\/api\/positions(?:\/\d+)?$/.test(url)) {
          if (method === 'GET') return response(w.__serverPositions);
          const body = options.body ? JSON.parse(options.body) : {};
          w.__positionRequests.push({ method, body });
          if (method === 'POST') { body.id = w.__serverPositions.length + 1; w.__serverPositions.push(body); return response(body, 201); }
          const id = +(url.match(/(\d+)$/) || [])[1], ix = w.__serverPositions.findIndex(p => p.id === id);
          if (method === 'PUT' && ix >= 0) { Object.assign(w.__serverPositions[ix], body); return response(w.__serverPositions[ix]); }
          if (method === 'DELETE' && ix >= 0) { w.__serverPositions.splice(ix, 1); return response(null, 204); }
        }
        if (/\/api\/risk\/evaluate/.test(String(u))) {
          w.__riskRequests.push(JSON.parse(options.body || '{}'));
          const body = riskResponse || {
            is_trade_approved: true,
            risk_metrics: {
              correlation: { new_ticker_to_weighted_portfolio: 0.30 },
              variance: { relative_increase_pct: 0.05 },
              historical_var_99: { incremental_var_ils: 1000 },
              portfolio_heat: { current_r: 1, max_r: 5, legacy_risk_on_excluded: 0 },
              warnings: []
            }
          };
          return response(body);
        }
        if (feed && /quotes\.json/.test(u))
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(feed)) });
        const m = /bars\/([^./]+)\.json/.exec(String(u));
        if (m && barsFixture[m[1]])
          return Promise.resolve({ ok: true, status: 200,
            json: () => Promise.resolve({ s: m[1], bars: barsFixture[m[1]] }) });
        return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) });
      };
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
module.exports = { boot, ck, report, HTML, HTML_PATH };
