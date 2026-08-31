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
  });
  await wait(60);
  inst.$('tab-pos').dispatchEvent(new inst.w.Event('click', { bubbles: true }));
  await wait(250);
  const text = inst.$('posList').textContent.replace(/\s+/g, ' ');
  // Snapshot multiplier 1x gives 120 - 1x5 = 115; the global 3.5x would have given 102.5.
  ck('open position resolves stop from frozen policy snapshot, not global trail setting',
     /stop now\s*115\.00/.test(text), text);
  ck('HAR card is explicitly reference-only',
     /21\/31-Session Volatility Outlook \(HAR Shadow\) — Reference Only/.test(text)
       && /Not used for stop, size, crash curve, or VaR/.test(text), text);
  report('POLICY SNAPSHOT');
})();
