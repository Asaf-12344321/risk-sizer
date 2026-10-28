// Positions tab shows the server-recorded stop (shared with the Telegram job), not an ATR replay.
const { boot, ck, report } = require('./lib');
const wait = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
  const t = boot({ positions: [
    { ticker: 'REC', entry_price: 100, atr: 5, quantity: 10, value_ils: 5000, currency: 'ILS',
      opened_at: '2026-09-01', stop: 112.5, stop_source: 'NORMAL', stop_asof: '2026-10-16',
      stop_why: 'trailing below support 112.80 (3 touches, volume node)', stop_last_action: 'HOLD' },
    { ticker: 'OLD', entry_price: 50, atr: 2, quantity: 10, value_ils: 5000, currency: 'ILS',
      opened_at: '2026-09-01' },
  ] });
  await wait(300);
  t.w.document.getElementById('tab-pos').dispatchEvent(new t.w.Event('click', { bubbles: true }));
  await wait(300);
  const html = t.$('posList').innerHTML, text = t.$('posList').textContent;
  ck('recorded stop is shown, not the ATR ladder value', /112\.50/.test(text), text.slice(0, 200));
  ck('source and justification are shown',
     /daily review \(support trail\)/.test(text) && /trailing below support 112\.80/.test(text) && /2026-10-16/.test(text));
  ck('position without a record shows no source line', (text.match(/Stop source:/g) || []).length === 1, text.slice(0, 400));
  // with live bars: only the latest bar can breach the current stop
  const b = boot({ positions: [{ ticker: 'REC', entry_price: 100, atr: 5, quantity: 10, value_ils: 5000,
      currency: 'ILS', opened_at: '2026-09-01', stop: 112.5, stop_source: 'STRUCTURAL', stop_asof: '2026-10-16',
      stop_why: 'x' }],
    bars: { REC: [['2026-09-02', 108, 99, 105, 100], ['2026-10-15', 125, 120, 124, 121], ['2026-10-16', 123, 110, 115, 122]] } });
  await wait(300);
  b.w.document.getElementById('tab-pos').dispatchEvent(new b.w.Event('click', { bubbles: true }));
  await wait(400);
  const bt = b.$('posList').textContent;
  ck('a breached chart stop offers the hold-on-purpose button', /data-act="hold"/.test(b.$('posList').innerHTML));
  ck('earlier lows below the current stop do not false-alarm; the latest bar can', /Your chart stop was reached/.test(bt), bt.slice(0, 300));
  const c = boot({ positions: [{ ticker: 'REC', entry_price: 100, atr: 5, quantity: 10, value_ils: 5000,
      currency: 'ILS', opened_at: '2026-09-01', stop: 112.5, stop_source: 'STRUCTURAL', stop_asof: '2026-10-16' }],
    bars: { REC: [['2026-09-02', 108, 99, 105, 100], ['2026-10-16', 125, 120, 124, 121]] } });
  await wait(300);
  c.w.document.getElementById('tab-pos').dispatchEvent(new c.w.Event('click', { bubbles: true }));
  await wait(400);
  ck('stop above old lows but latest bar clear: no breach warning', !/Your chart stop was reached/.test(c.$('posList').textContent),
     c.$('posList').textContent.slice(0, 300));
  // The recorded stop wins over the post-close engine and the browser ladder, everywhere on the card.
  const d = boot({ positions: [
      { id: 1, ticker: 'REC', entry_price: 100, atr: 5, quantity: 10, value_ils: 5000, currency: 'ILS',
        opened_at: '2026-09-01', stop: 112.5, stop_source: 'NORMAL', stop_asof: '2026-10-16', stop_last_action: 'RAISE',
        stop_prev: 100, stop_why: 'Following the price up: the stop trails a comfortable distance below its highest close ($130.00).' },
      { id: 2, ticker: 'NEW', entry_price: 100, atr: 5, quantity: 10, value_ils: 5000, currency: 'ILS',
        opened_at: '2026-09-01' }],
    stops: { 1: { current_stop_price: 99, actionable_alert_needed: false, position_exit_detected: true },
             2: { current_stop_price: 97.25, actionable_alert_needed: false } } });
  await wait(300);
  d.w.document.getElementById('tab-pos').dispatchEvent(new d.w.Event('click', { bubbles: true }));
  await wait(400);
  const cards = d.$('posList').innerHTML.split('<div class="pos">').slice(1);
  const rec = cards.find(c => /REC/.test(c)), fresh = cards.find(c => /NEW/.test(c));
  const stopOf = c => (/Current stop<\/span><strong>([^<]+)</.exec(c) || [])[1];
  ck('recorded stop is shown even when the post-close engine disagrees', stopOf(rec) === '₪112.50', stopOf(rec));
  ck('post-close engine exit flag cannot contradict the recorded stop', !/Your chart stop was reached/.test(rec));
  ck('a RAISE from the daily job is presented as the move-your-broker-stop action',
     /Move your broker stop up to 112\.50/.test(rec));
  ck('a raised stop shows a green up-arrow with the previous stop', /stop-up">▲ up from ₪100\.00/.test(rec) || /stop-up">▲ up from/.test(rec), rec.slice(0, 400));
  ck('the plain-language reason is shown with the raise', /Following the price up/.test(rec), rec.slice(0, 600));
  ck('a position that did not move has no green arrow', !/stop-up/.test(fresh));
  ck('no ladder guidance next to a recorded stop', !/Next: at/.test(rec) && /only moves when the daily review/.test(rec));
  ck('position with no record falls back to the post-close engine stop', stopOf(fresh) === '₪97.25', stopOf(fresh));

  // --- no chart support: no stop, a flag, and the holding-on-purpose decision --------------------
  const n = boot({ positions: [
      { id: 1, ticker: 'LOW', entry_price: 100, atr: 5, quantity: 10, value_ils: 5000, currency: 'USD',
        opened_at: '2026-09-01', stop: null, stop_source: 'NONE', stop_asof: '2026-10-16', stop_last_action: 'NO_SUPPORT',
        stop_why: 'The price is at or near its lowest level in about 7 months, so there is no proven support underneath to anchor a stop to. No chart support below, manual decision required.' },
      { id: 2, ticker: 'HELD', entry_price: 100, atr: 5, quantity: 10, value_ils: 5000, currency: 'USD',
        opened_at: '2026-09-01', stop: null, stop_source: 'NONE', stop_asof: '2026-10-16', stop_last_action: 'HOLD',
        held_since: '2026-10-20', hold_count: 1 },
      { id: 3, ticker: 'OK', entry_price: 100, atr: 5, quantity: 10, value_ils: 5000, currency: 'USD',
        opened_at: '2026-09-01', stop: 90, stop_source: 'CHART', stop_asof: '2026-10-16', stop_last_action: 'HOLD' }],
    holdPreview: { ticker: 'LOW', price: 60, new_stop: null, no_support: true, extra_risk_ils: 2190, loss_so_far_ils: 1460,
                   position_value_ils: 2190, prior_holds: 0, reasons_allowed: { thesis: 'my reasons for owning it still hold', other: 'another reason' } } });
  await wait(300);
  n.w.document.getElementById('tab-pos').dispatchEvent(new n.w.Event('click', { bubbles: true }));
  await wait(400);
  const nc = n.$('posList').innerHTML.split('<div class="pos">').slice(1);
  const low = nc.find(c => />LOW</.test(c) || /position-ticker">LOW/.test(c)), held = nc.find(c => /position-ticker">HELD/.test(c)), okc = nc.find(c => /position-ticker">OK/.test(c));
  ck('no support shows "No chart stop" instead of a number', /Current stop<\/span><strong>No chart stop</.test(low), low.slice(0, 300));
  ck('no support shows the manual-decision flag', /No chart support below — manual decision required/.test(low));
  ck('no support offers the hold-on-purpose button', /data-act="hold"/.test(low));
  ck('a position already held on purpose says so and has no button', /Holding on purpose since 2026-10-20/.test(held) && !/data-act="hold"/.test(held));
  ck('a position with a chart stop and no breach has no hold button', !/data-act="hold"/.test(okc));
  n.w.document.querySelector('button[data-act="hold"]').dispatchEvent(new n.w.Event('click', { bubbles: true }));
  await wait(300);
  const panel = n.w.document.querySelector('.hold-panel:not([hidden])');
  ck('the hold panel shows the cost before confirming', panel && /no stop/.test(panel.textContent) && /₪2,190/.test(panel.textContent) && /₪1,460/.test(panel.textContent),
     panel && panel.textContent);
  ck('the hold panel asks why', panel && /Why are you holding/.test(panel.textContent) && /my reasons for owning it still hold/.test(panel.textContent));
  n.w.document.querySelector('button[data-act="hold-confirm"]').dispatchEvent(new n.w.Event('click', { bubbles: true }));
  await wait(300);
  ck('confirming sends the reason to the server', (n.w.__holdRequests || []).length === 1 && n.w.__holdRequests[0].reason === 'thesis',
     JSON.stringify(n.w.__holdRequests));

  report('positions_stop');
  process.exit(0);
})();
