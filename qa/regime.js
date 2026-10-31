// The /regime dashboard: layout, plain-language wording, red flags first, collapsibles, refresh, key prompt.
const { JSDOM } = require('jsdom'); const fs = require('fs');
const { ck, report } = require('./lib');
const HTML = fs.readFileSync(__dirname + '/../regime.html', 'utf8');
const wait = (ms) => new Promise(r => setTimeout(r, ms));

const vm = (over = {}) => Object.assign({
  title: 'Nasdaq-100 (NDX / QQQ) Market Regime', as_of: '2026-10-30', label: 'Systemic Stress / Bear Regime', tone: 'bad',
  summary: 'Several statistical red flags of a deeper bear market are flashing at the same time.', flags_on: 2, flags_known: 7,
  kpis: [
    { key: 'drawdown', title: 'Pullback from 52w High', value: '-24.0%', note: 'Bear-market territory', tone: 'bad', scale: 'Normal pullback: 5–10% | Bear market: 20%+' },
    { key: 'trend', title: 'Long-Term Trend (200d SMA)', value: '+1.6% Below & Falling', note: '', tone: 'bad', scale: 'Bullish above 200d SMA | Bearish below' },
    { key: 'vix', title: 'Market Fear (VIX)', value: '34.0 — Panic', note: '', tone: 'bad', scale: '<18 Calm | 18–25 Caution | 25–30 High Stress | 30+ Panic' },
    { key: 'today', title: "Today's Move", value: '-3.46%', note: '', tone: 'warn', scale: 'Shock rarity: ~1 in 115 trading days', raw: 'Raw size alone: ~1 in 40 days' }],
  checks: [
    { key: 'below_sma', title: 'Long-Term Trend', ok: false, badge: 'RED FLAG', status: 'Price is 6.0% below the 200-day average' },
    { key: 'high_vix', title: 'Market Fear (VIX)', ok: false, badge: 'RED FLAG', status: 'VIX is 34.0: institutional panic territory (30+)' },
    { key: 'inverted', title: 'Fear Term Structure', ok: null, badge: 'NO DATA', status: 'No data for this date' },
    { key: 'falling_sma', title: '200-Day Direction', ok: true, badge: 'OK', status: 'The 200-day line is rising (+1.63% in 20 days)' },
    { key: 'narrow', title: 'Market Breadth', ok: true, badge: 'OK', status: 'Equal-weight vs QQQ: -1.7% over 60 days (not narrowing)' }],
  analogs: { rows: [
      { horizon: 20, timeframe: '1 Month (20d)', win_rate: 0.74, typical: 0.034, worst_dip: -0.076, crash_risk: 0.0, n: 145 },
      { horizon: 60, timeframe: '3 Months (60d)', win_rate: 0.8, typical: -0.012, worst_dip: -0.088, crash_risk: 0.01, n: 145 },
      { horizon: 120, timeframe: '6 Months (120d)', win_rate: 0.81, typical: 0.1, worst_dip: -0.101, crash_risk: 0.03, n: 139 }],
    days: 146, episodes: 32, level: 'exact', matched_on: 'below a falling 200d average; drawdown -30% to -20%; VIX 35+', low_confidence: false },
  raw: { horizons: [{ horizon: 20, n: 145, ret_p10: -0.032, ret_p50: 0.034, ret_p90: 0.088, dd_p10: -0.076, dd_p50: -0.021, dd_p90: 0, p_down10: 0.03, p_crash20: 0, p_negative: 0.26, base_p_crash20: 0.014 }],
    episodes: [{ start: '2008-06-05', end: '2008-06-05', days: 1, ret_60d: -0.089, worst_dd_120d: -0.496 }] },
  notes: ['65% of the matched days are from before 2006.'] }, over);

function boot(responses, opts = {}) {
  const calls = [];
  const dom = new JSDOM(HTML, { runScripts: 'dangerously', url: 'https://qa.local/regime',
    beforeParse(w) {
      w.prompt = () => { (w.__prompts = w.__prompts || []).push(1); return opts.key || null; };
      w.fetch = (u, o = {}) => {
        calls.push({ url: String(u), key: (o.headers || {})['X-API-Key'] || null });
        const r = responses.shift() || { status: 200, body: vm() };
        return Promise.resolve({ ok: r.status >= 200 && r.status < 300, status: r.status, json: () => Promise.resolve(r.body) });
      };
      if (opts.storedKey) w.localStorage.setItem('riskSizerApiKey', opts.storedKey);
    } });
  return { dom, w: dom.window, d: dom.window.document, calls };
}

(async () => {
  const t = boot([{ status: 200, body: vm() }], { storedKey: 'k1' });
  await wait(150);
  const d = t.d, text = d.body.textContent;
  ck('title is the full index name', /Nasdaq-100 \(NDX \/ QQQ\) Market Regime/.test(d.querySelector('h1').textContent));
  ck('regime label and plain-English summary are shown', /Systemic Stress \/ Bear Regime · 2 of 7 red flags/.test(text) && /red flags of a deeper bear market/.test(text));
  const cards = [...d.querySelectorAll('.kpi')];
  ck('exactly four KPI cards', cards.length === 4);
  ck('each card carries its benchmark scale', cards.every(c => c.querySelector('.s').textContent.length > 10) &&
     /Normal pullback: 5–10% \| Bear market: 20%\+/.test(cards[0].textContent) && /<18 Calm \| 18–25 Caution \| 25–30 High Stress \| 30\+ Panic/.test(cards[2].textContent));
  ck('shock rarity sits under Today\'s Move', /Shock rarity: ~1 in 115 trading days/.test(cards[3].textContent));
  ck('card tone classes follow the data', cards[0].classList.contains('bad') && cards[3].classList.contains('warn'));

  const rows = [...d.querySelectorAll('.check')];
  ck('one row per check', rows.length === 5);
  ck('red flags are listed first, in red', rows[0].classList.contains('red') && rows[1].classList.contains('red') && !rows[2].classList.contains('red'));
  ck('red rows use the RED FLAG badge, ok rows the OK badge, missing data its own badge',
     /RED FLAG/.test(rows[0].textContent) && /OK/.test(rows[3].textContent) && /NO DATA/.test(rows[2].textContent));
  ck('a green tick never appears next to a problem', rows.filter(r => r.classList.contains('red')).every(r => !/✅|✓/.test(r.textContent)) &&
     rows.filter(r => /\bOK\b/.test(r.querySelector('.badge').textContent)).every(r => /✅/.test(r.querySelector('.badge').textContent)));
  ck('the stored API key is sent', t.calls[0].key === 'k1' && t.calls[0].url === '/api/regime');

  const summaries = [...d.querySelectorAll('details > summary')].map(s => s.textContent);
  ck('explainer for the 7 checks is collapsed by default', summaries.includes('ℹ️ What do these 7 checks & VIX levels mean?') &&
     ![...d.querySelectorAll('details')].some(x => x.open));
  const explain = [...d.querySelectorAll('details')].find(x => /7 checks/.test(x.querySelector('summary').textContent));
  const etext = explain.textContent;
  ck('all seven explanations are present', ['1. Price vs. 200-Day Average', '2. 200-Day Average Slope', '3. Drawdown Depth (-20% Threshold)',
     '4. Drop Velocity (-10% in 20 Days)', '5. Fear Level (VIX >= 30)', '6. Fear Term Structure (VIX vs. VIX3M)', '7. Market Breadth (QQEW vs. QQQ)']
     .every(h => etext.includes(h)));
  ck('explanations use the agreed plain wording', /Wall Street.s macro dividing line/.test(etext) && /official definition of a Bear Market/.test(etext) &&
     /12.18 = Calm routine; 18.25 = Normal caution\/choppy \(no systemic panic\); 25.30 = High stress; 30\+ = Full institutional panic/.test(etext) &&
     /Inversion > 1\.0/.test(etext) && /dominated by 7 mega-caps/.test(etext));

  const table = d.querySelector('table');
  const heads = [...table.querySelectorAll('th')].map(h => h.textContent);
  ck('analogs table has the five simple columns', heads.length === 5 && /Timeframe/.test(heads[0]) && /Win Rate/.test(heads[1]) && /Typical Return/.test(heads[2]) &&
     /Worst-Case Dip/.test(heads[3]) && /Severe Crash Risk/.test(heads[4]), heads.join('|'));
  const body = [...table.querySelectorAll('tr')].slice(1).map(r => [...r.children].map(c => c.textContent));
  ck('three timeframes with win rate, typical return and crash risk', body.length === 3 && body[0][0] === '1 Month (20d)' && body[1][1] === '80%' &&
     body[1][2] === '-1.2%' && body[2][4] === '3%', JSON.stringify(body));
  const typ = [...table.querySelectorAll('tr')].slice(1).map(r => r.children[2].className);
  ck('typical return is green when positive and red when negative', typ[0] === 'pos' && typ[1] === 'neg');
  ck('the median explainer names the episode count', /Out of 32 similar historical episodes since 1990, the Median \(Typical\) Return sorts all outcomes from worst to best/.test(text) &&
     /half of historical times did better than this number, and half did worse/.test(text));
  ck('raw data is collapsed at the bottom', summaries.includes('📂 Show raw percentiles & all 32 historical episodes') &&
     /2008-06-05/.test(d.body.innerHTML) && /Return p10/.test(d.body.innerHTML));
  ck('only five main columns are visible before opening the raw section', table.closest('details') === null);

  // refresh
  t.w.document.getElementById('refresh').dispatchEvent(new t.w.Event('click'));
  await wait(150);
  ck('Refresh asks for a fresh check', t.calls.length === 2 && t.calls[1].url === '/api/regime?refresh=1');

  // first visit on a new device: 401 -> prompt -> retry with the typed key
  const n = boot([{ status: 401, body: { detail: 'Missing or invalid API key' } }, { status: 200, body: vm({ label: 'Calm Uptrend / Near Highs', tone: 'good', flags_on: 0 }) }], { key: 'typed' });
  await wait(200);
  ck('a 401 prompts for the key once, stores it and retries', (n.w.__prompts || []).length === 1 && n.calls.length === 2 && n.calls[1].key === 'typed' &&
     n.w.localStorage.getItem('riskSizerApiKey') === 'typed' && /Calm Uptrend/.test(n.d.body.textContent));

  // failure keeps a readable message
  const f = boot([{ status: 503, body: { detail: 'Market data unavailable: offline' } }], { storedKey: 'k' });
  await wait(150);
  ck('an outage shows a clear message, not a blank page', /Could not load the market regime: Market data unavailable: offline/.test(f.d.body.textContent));
  ck('the page links back to the risk sizer', !!f.d.querySelector('a[href="/"]'));

  report('regime');
  process.exit(0);
})();
