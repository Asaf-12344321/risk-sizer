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
  ck('position without a record is labelled as not yet reviewed', /not reviewed yet/.test(text));
  ck('recorded position says the stop moves only on a daily raise', /only moves when the daily review/.test(text));
  // with live bars: only the latest bar can breach the current stop
  const b = boot({ positions: [{ ticker: 'REC', entry_price: 100, atr: 5, quantity: 10, value_ils: 5000,
      currency: 'ILS', opened_at: '2026-09-01', stop: 112.5, stop_source: 'STRUCTURAL', stop_asof: '2026-10-16',
      stop_why: 'x' }],
    bars: { REC: [['2026-09-02', 108, 99, 105, 100], ['2026-10-15', 125, 120, 124, 121], ['2026-10-16', 123, 110, 115, 122]] } });
  await wait(300);
  b.w.document.getElementById('tab-pos').dispatchEvent(new b.w.Event('click', { bubbles: true }));
  await wait(400);
  const bt = b.$('posList').textContent;
  ck('earlier lows below the current stop do not false-alarm; the latest bar can', /Stop touched/.test(bt), bt.slice(0, 300));
  const c = boot({ positions: [{ ticker: 'REC', entry_price: 100, atr: 5, quantity: 10, value_ils: 5000,
      currency: 'ILS', opened_at: '2026-09-01', stop: 112.5, stop_source: 'STRUCTURAL', stop_asof: '2026-10-16' }],
    bars: { REC: [['2026-09-02', 108, 99, 105, 100], ['2026-10-16', 125, 120, 124, 121]] } });
  await wait(300);
  c.w.document.getElementById('tab-pos').dispatchEvent(new c.w.Event('click', { bubbles: true }));
  await wait(400);
  ck('stop above old lows but latest bar clear: no breach warning', !/Stop touched|should already be closed/.test(c.$('posList').textContent),
     c.$('posList').textContent.slice(0, 300));
  report('positions_stop');
  process.exit(0);
})();
