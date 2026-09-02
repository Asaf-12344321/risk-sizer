// Frozen policy must win over current global calculator settings for an open position.
const { boot, ck, report } = require('./lib');
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  const inst = boot({
    positions: [{
      tick: 'SNAP', entry: 100, atr: 5, rung: 0, added: '2026-01-01',
      policy_snapshot: {
        entry_atr: 5, initmult: 2.5, trailmult: 1.0, armpct: 15,
        armatrmult: 3, minstop: 8, maxstop: 30,
      },
    }],
    bars: { SNAP: [
      ['2026-01-02', 116, 99, 110, 100],
      ['2026-01-05', 121, 110, 120, 110],
    ] },
    shadows: { '1': {
      as_of_session: '2026-01-05', forecasts: {
        '21': { status: 'available', forecast_total_volatility: 0.20 },
        '31': { status: 'available', forecast_total_volatility: 0.25 },
      },
    } },
    stops: { '1': {
      as_of_session: '2026-01-05', current_stop_price: 116.5,
      delta_ticks: 150, actionable_alert_needed: true, position_exit_detected: false,
    } },
  });
  await wait(60);
  inst.$('tab-pos').dispatchEvent(new inst.w.Event('click', { bubbles: true }));
  await wait(250);
  const text = inst.$('posList').textContent.replace(/\s+/g, ' ');
  // Snapshot multiplier 1x gives 120 - 1x5 = 115; the global 3.5x would have given 102.5.
  ck('frozen policy still drives the browser-tracker value held in Details',
     /Browser tracker\s*115\.00/.test(text), text);
  ck('primary guidance separates the current stop from the next ladder action',
     /Current stop\s*₪116\.50/.test(text) &&
       /Move your broker stop to 116\.50/.test(text) &&
       /Next: at ₪125\.00, move stop to ₪120\.00/.test(text) &&
       /EOD status: completed/.test(text), text);
  const details = inst.$('posList').querySelector('details.position-details');
  ck('research volatility is hidden inside the collapsed Details section',
     details && !details.open && /Research volatility outlook/.test(details.textContent), text);
  report('POLICY SNAPSHOT');
})();
