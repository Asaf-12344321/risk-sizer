// The /regime dashboard: layout, plain-language wording, red flags first, collapsibles, refresh, key prompt.
const { JSDOM } = require('jsdom'); const fs = require('fs');
const { ck, report } = require('./lib');
const HTML = fs.readFileSync(__dirname + '/../regime.html', 'utf8');
const wait = (ms) => new Promise(r => setTimeout(r, ms));

const vm = (over = {}) => Object.assign({
  title: 'Nasdaq-100 (NDX / QQQ) Market Regime', as_of: '2026-10-30', label: 'Systemic Stress / Bear Regime', tone: 'bad',
  summary: 'Several statistical red flags of a deeper bear market are flashing at the same time.', flags_on: 2, flags_known: 6,
  watch_count: 2,
  kpis: [
    { key: 'drawdown', title: 'Pullback from 52w High', value: '-24.0%', value_tone: 'bad', pill: 'Bear-market territory', pill_tone: 'bad', scale: 'Normal pullback: 5–10% | Bear market: 20%+' },
    { key: 'trend', title: 'Long-Term Trend (200d SMA)', value: '+1.6% Above & Rising', value_tone: 'good', pill: 'Thin cushion: watch', pill_tone: 'warn', scale: 'Bullish above 200d SMA | Bearish below' },
    { key: 'vix', title: 'Market Fear (VIX)', value: '27.0', value_tone: 'orange', pill: 'High Stress', pill_tone: 'orange', scale: '<18 Calm | 18–25 Caution | 25–30 High Stress | 30+ Panic' },
    { key: 'today', title: "Today's Move", value: '-3.46%', value_tone: 'bad', pill: 'Rare shock', pill_tone: 'bad', scale: 'Shock rarity: ~1 in 115 trading days', raw: 'Raw size alone: ~1 in 40 days' }],
  checks: [
    { key: 'trend', title: 'Long-Term Trend', level: 'red', ok: false, badge: 'RED FLAG', status: 'Price is 6.0% below the 200-day average', gauge: 1, limit: 'Red flag if price drops below the 200-day line (0%)' },
    { key: 'drawdown', title: 'Drawdown Depth', level: 'red', ok: false, badge: 'RED FLAG', status: '-24.0% from the 52-week high: past the -20% bear-market line', gauge: 1, limit: 'Red flag at -20% (official bear market)' },
    { key: 'speed', title: 'Drop Speed', level: 'watch', ok: false, badge: 'WATCH', status: '-9.1% over the last 20 days: close to the -10% panic-speed line', gauge: 0.91, limit: 'Red flag at -10% within 20 days (panic speed)' },
    { key: 'vix', title: 'Market Fear (VIX)', level: 'watch', ok: false, badge: 'WATCH', status: 'VIX is 27.0: high stress, close to panic (30)', gauge: 0.83, limit: 'Red flag at 30+ (institutional panic)' },
    { key: 'term', title: 'Fear Term Structure', level: 'na', ok: null, badge: 'NO DATA', status: 'No data for this date', gauge: 0, limit: 'Red flag at 1.00 or above (inverted)' },
    { key: 'slope', title: '200-Day Direction', level: 'caution', ok: false, badge: 'CAUTION', status: 'The 200-day line is only flattening upward (+0.30% in 20 days)', gauge: 0.8, limit: 'Red flag if the 200-day line turns down (below 0%)' },
    { key: 'breadth', title: 'Market Breadth', level: 'ok', ok: true, badge: 'OK', status: 'Equal-weight vs QQQ: -0.7% over 60 days (not narrowing)', gauge: 0.54, limit: 'Red flag if equal-weight lags cap-weight by 3% over 60 days' }],
  analogs: { sets: [
    { key: 'full', title: 'Full History (1990+)', level: 'Tight match', days: 90, episodes: 32, low_confidence: false,
      spread: 'Matched 32 episodes (90 days): drawdown -12.7% to -6.2%, 200d cushion +0.0% to +5.4%, 20d speed -10.7% to -5.7%, VIX 14.6 to 24.1, VIX vs 60d average 1.04 to 1.39',
      rows: [
        { horizon: 20, timeframe: '1 Month (20d)', win_rate: 0.74, win_lo: 0.58, win_hi: 0.88, typical: 0.034, worst_dip: -0.076, crash_risk: 0.0, n: 145 },
        { horizon: 60, timeframe: '3 Months (60d)', win_rate: 0.8, win_lo: 0.62, win_hi: 0.93, typical: -0.012, worst_dip: -0.088, crash_risk: 0.09, n: 145 },
        { horizon: 120, timeframe: '6 Months (120d)', win_rate: 0.66, win_lo: 0.5, win_hi: 0.8, typical: 0.1, worst_dip: -0.101, crash_risk: 0.03, n: 139 }],
      raw: { horizons: [{ horizon: 20, n: 145, ret_p10: -0.032, ret_p50: 0.034, ret_p90: 0.088, dd_p10: -0.076, dd_p50: -0.021, dd_p90: 0, p_down10: 0.03, p_crash20: 0, p_negative: 0.26, base_p_crash20: 0.014 }],
        episodes: [{ start: '2008-06-05', end: '2008-06-05', days: 1, ret_60d: -0.089, worst_dd_120d: -0.496 }] } },
    { key: 'modern', title: 'Modern Era (2006+, real VIX/VIX3M & QQEW breadth)', level: 'Widened match (x1.3)', days: 90, episodes: 19, low_confidence: false,
      spread: 'Matched 19 episodes (90 days): drawdown -11.0% to -5.4%, 200d cushion +0.1% to +6.8%, 20d speed -9.8% to -5.4%, VIX 15.9 to 25.5, VIX vs 60d average 1.02 to 1.45, VIX/VIX3M 0.87 to 1.03, breadth -5.1% to +2.1%',
      rows: [
        { horizon: 20, timeframe: '1 Month (20d)', win_rate: 0.71, win_lo: 0.5, win_hi: 0.9, typical: 0.026, worst_dip: -0.07, crash_risk: 0.0, n: 90 },
        { horizon: 60, timeframe: '3 Months (60d)', win_rate: 0.88, win_lo: 0.71, win_hi: 1.0, typical: 0.074, worst_dip: -0.05, crash_risk: 0.0, n: 90 },
        { horizon: 120, timeframe: '6 Months (120d)', win_rate: 0.88, win_lo: 0.74, win_hi: 0.98, typical: 0.126, worst_dip: -0.08, crash_risk: 0.07, n: 80 }],
      raw: { horizons: [{ horizon: 20, n: 90, ret_p10: -0.03, ret_p50: 0.026, ret_p90: 0.08, dd_p10: -0.07, dd_p50: -0.02, dd_p90: 0, p_down10: 0.02, p_crash20: 0, p_negative: 0.29, base_p_crash20: 0.014 }],
        episodes: [{ start: '2022-01-10', end: '2022-01-19', days: 8, ret_60d: -0.2, worst_dd_120d: -0.287 }] } }],
    compare: { text: '3 months later: Full History 80% higher, typical -1.2%, severe crash risk 9% | Modern Era 88% higher, typical +7.4%, severe crash risk 0%. The two eras disagree: severe falls show up more in the older years (for example 1990).', differs: true } },
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
  ck('regime label and plain-English summary are shown', /Systemic Stress \/ Bear Regime · 2 of 6 red flags/.test(text) && /red flags of a deeper bear market/.test(text));
  const cards = [...d.querySelectorAll('.kpi')];
  ck('exactly four KPI cards', cards.length === 4);
  ck('each card carries its benchmark scale', cards.every(c => c.querySelector('.s').textContent.length > 10) &&
     /Normal pullback: 5–10% \| Bear market: 20%\+/.test(cards[0].textContent) && /<18 Calm \| 18–25 Caution \| 25–30 High Stress \| 30\+ Panic/.test(cards[2].textContent));
  ck('shock rarity sits under Today\'s Move', /Shock rarity: ~1 in 115 trading days/.test(cards[3].textContent));
  ck('the banner counts red flags and watch items', /2 of 6 red flags · 2 watch/.test(text));
  ck('a negative number is red', cards[0].querySelector('.v').classList.contains('bad') && cards[3].querySelector('.v').classList.contains('bad'));
  ck('a positive number is green and VIX 25-30 is deep orange', cards[1].querySelector('.v').classList.contains('good') && cards[2].querySelector('.v').classList.contains('orange'));
  ck('the VIX card shows only the number, with the word in a pill like the other cards',
     cards[2].querySelector('.v').textContent === '27.0' && cards[2].querySelector('.pill').textContent === 'High Stress' &&
     cards.every(c => c.querySelector('.pill')) && !/-/.test(cards[2].querySelector('.v').textContent));
  ck('no em dash or unicode minus anywhere on the page', !/\u2014|\u2212/.test(d.body.textContent) && !/\u2014|\u2212/.test(HTML));
  ck('the status verdict is a separate pill with its own colour', cards[0].querySelector('.pill').textContent === 'Bear-market territory' &&
     cards[1].querySelector('.pill').classList.contains('warn') && cards[1].querySelector('.pill').textContent === 'Thin cushion: watch');
  const pillBeside = boot([{ status: 200, body: vm({ kpis: [{ key: 'drawdown', title: 'Pullback from 52w High', value: '-9.1%', value_tone: 'bad', pill: 'Normal pullback', pill_tone: 'good', scale: 's' }] }) }], { storedKey: 'k' });
  await wait(150);
  const dk = pillBeside.d.querySelector('.kpi');
  ck('-9.1% is red while "Normal pullback" is green', dk.querySelector('.v').classList.contains('bad') && dk.querySelector('.pill').classList.contains('good'));

  const grid = d.querySelector('.checks');
  const rows = [...d.querySelectorAll('.check')];
  ck('all seven checks are visible on the main screen, not collapsed', rows.length === 7 && !grid.closest('details'));
  ck('the checks sit in a two-column grid', /grid-template-columns:\s*1fr 1fr/.test(d.querySelector('style').textContent));
  ck('red cards come first, then watch, then caution, no-data and ok', rows.map(r => r.className.replace('check ', '')).join(',') === 'red,red,watch,orange,na,caution,ok');
  ck('badges use the right words', /RED FLAG/.test(rows[0].textContent) && /WATCH/.test(rows[2].textContent) && /CAUTION/.test(rows[5].textContent) &&
     /NO DATA/.test(rows[4].textContent) && /OK/.test(rows[6].textContent));
  ck('a green tick never appears next to a problem or a warning', rows.filter(r => !/ok/.test(r.className.replace('check ', ''))).every(r => !/\u2705/.test(r.textContent)));
  ck('only the OK card has a green tick', /\u2705/.test(rows[6].textContent));
  ck('every card has a gauge sized to how close the value is to its red line',
     rows.map(r => r.querySelector('.gauge i').style.width).join(',') === '100%,100%,91%,83%,0%,80%,54%');
  ck('every card says where its red flag is', rows.every(r => /^Red flag/.test(r.querySelector('.lim').textContent)));
  ck('the stored API key is sent', t.calls[0].key === 'k1' && t.calls[0].url === '/api/regime');

  const summaries = [...d.querySelectorAll('details > summary')].map(s => s.textContent);
  ck('explainer for the 7 checks is collapsed by default (the checks themselves are not)', summaries.includes('ℹ️ What do these 7 checks & VIX levels mean?') &&
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
  const heads = [...table.querySelectorAll('th')].filter(h => !h.closest('tr').classList.contains('era')).map(h => h.textContent);
  ck('analogs table keeps the five simple columns', heads.length === 5 && /Timeframe/.test(heads[0]) && /Win Rate/.test(heads[1]) && /Typical Return/.test(heads[2]) &&
     /Worst-Case Dip/.test(heads[3]) && /Severe Crash Risk/.test(heads[4]), heads.join('|'));
  const eraRows = [...table.querySelectorAll('tr.era')].map(r => r.textContent);
  ck('Full History and the Modern Era sit next to each other in the same table', eraRows.length === 2 && /Full History \(1990\+\)/.test(eraRows[0]) &&
     /Modern Era \(2006\+, real VIX\/VIX3M & QQEW breadth\)/.test(eraRows[1]));
  ck('each era header shows its episode count and Tight/Widened label', /32 episodes/.test(eraRows[0]) && /Tight match/.test(eraRows[0]) &&
     /19 episodes/.test(eraRows[1]) && /Widened match \(x1\.3\)/.test(eraRows[1]));
  const dataRows = [...table.querySelectorAll('tr')].filter(r => !r.classList.contains('era') && r.querySelector('td'));
  const body = dataRows.map(r => [...r.children].map(c => c.textContent));
  ck('three timeframes per era', dataRows.length === 6 && body[0][0] === '1 Month (20d)' && body[3][0] === '1 Month (20d)', JSON.stringify(body.map(r => r[0])));
  ck('win rate is the bold number with the 90% range as small grey subtext underneath', dataRows[1].children[1].firstChild.textContent === '80%' &&
     dataRows[1].children[1].querySelector('small').textContent === '~62-93%' && dataRows[4].children[1].querySelector('small').textContent === '~71-100%');
  ck('win rate is tinted green from 70% up only', dataRows[0].children[1].className === 'win hi' && dataRows[2].children[1].className === 'win' &&
     /fit/.test(table.closest('.wrap').className));
  ck('typical return is green when positive and red when negative', dataRows[0].children[2].className === 'pos' && dataRows[1].children[2].className === 'neg');
  ck('severe crash risk differs between the eras and is shown per era', dataRows[1].children[4].textContent === '9%' && dataRows[4].children[4].textContent === '0%');
  const spreads = [...d.querySelectorAll('.spread')].map(p => p.textContent);
  ck('the real spread of the matched days is shown above the table for both eras', spreads.length >= 2 &&
     /Full History \(1990\+\): Matched 32 episodes \(90 days\): drawdown -12\.7% to -6\.2%, 200d cushion \+0\.0% to \+5\.4%, 20d speed -10\.7% to -5\.7%, VIX 14\.6 to 24\.1/.test(spreads[0]) &&
     /Modern Era .*VIX\/VIX3M 0\.87 to 1\.03, breadth -5\.1% to \+2\.1%/.test(spreads[1]));
  ck('the spread lines come before the table', d.querySelector('.spread').compareDocumentPosition(table) & 4);
  ck('a one-line comparison of the eras sits below the table', /Full History 80% higher.*Modern Era 88% higher/.test(d.querySelector('.compare').textContent) &&
     (table.compareDocumentPosition(d.querySelector('.compare')) & 4));
  ck('the median explainer names the episode count and how the matching works', /Out of 32 similar historical episodes since 1990, the Median \(Typical\) Return sorts all outcomes from worst to best/.test(text) &&
     /half of historical times did better than this number, and half did worse/.test(text) && /same side of the 200d line/.test(text));
  ck('raw data for both eras is collapsed at the bottom', summaries.includes('📂 Show raw percentiles & all 51 historical episodes') &&
     /2008-06-05/.test(d.body.innerHTML) && /2022-01-10/.test(d.body.innerHTML) && /Return p10/.test(d.body.innerHTML));
  ck('the main table is not inside a collapsed section', table.closest('details') === null);

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
