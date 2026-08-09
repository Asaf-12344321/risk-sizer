// POSITION TRACKER — the replay path (`liveState`), driven through real bar fixtures.
//
// This suite exists because the feed used to publish [date, high, low, close] with no
// OPEN, so the tracker could only ask `low <= stop` and then report the fill AT the stop.
// That is false on any bar that opened below the stop: you were filled lower, at the
// open. It overstated the exit on precisely the days the stop exists for, and no
// assertion touched it — parity.js drives this path but only ever checks arming.
//
// Entry 100 / ATR 5 throughout: initial stop = clamp(2.5x5/100) = 12.5% -> 87.50,
// arm trigger = max(15%, 3.0 x 5%) = +15% -> 115.00.
const { boot, ck, report } = require('./lib');

const ENTRY = 100, ATR = 5, STOP = 87.5, ARM = 115;
const ADDED = '2026-01-01';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// Renders the position tab and returns the breach line's reported fill price, or NaN.
async function replay(bars) {
  const inst = boot({
    bars: { TRK: bars },
    positions: [{ tick: 'TRK', entry: ENTRY, atr: ATR, rung: 0, added: ADDED }]
  });
  await wait(60);
  inst.$('tab-pos').dispatchEvent(new inst.w.Event('click', { bubbles: true }));
  await wait(250);
  const txt = inst.$('posList').textContent.replace(/\s+/g, ' ');
  const m = /stop was breached on ([\d-]+) at ([\d.]+)/.exec(txt);
  return { price: m ? parseFloat(m[2]) : NaN, date: m ? m[1] : null,
           gapped: /gapped through/.test(txt), txt };
}

// A quiet bar that neither breaches nor arms, so a fixture can end without side effects.
const calm = (d) => [d, 101, 99, 100, 100];

(async () => {
  // 1. Ordinary fill: the bar opens above the stop and trades down through it. The stop
  //    order was live and marketable, so the fill IS the stop price.
  let r = await replay([['2026-01-02', 96, 87.0, 94, 95], calm('2026-01-05')]);
  ck('a bar that opens above the stop and trades through it fills AT the stop',
     Math.abs(r.price - STOP) < 0.01 && !r.gapped, `reported ${r.price}, gap=${r.gapped}`);

  // 2. Gap fill: the bar OPENS below the stop. The stop price never traded — the fill is
  //    the open, and it is worse than the stop. This is the case the old feed could not see.
  r = await replay([['2026-01-02', 82, 78, 81, 80], calm('2026-01-05')]);
  ck('a bar that opens BELOW the stop fills at the open, not the stop',
     Math.abs(r.price - 80) < 0.01, `reported ${r.price}, expected 80.00 (stop was ${STOP})`);
  ck('the gap fill is reported as worse than the stop, never better',
     r.price < STOP - 0.01, `fill ${r.price} vs stop ${STOP}`);
  ck('a gap fill is labelled as a gap, so the number is not read as a stop fill',
     r.gapped === true, r.gapped ? 'says "gapped through"' : 'no gap wording in the breach line');

  // 3. Stop-first ordering. This bar breaches the stop AND clears the arm trigger. Daily
  //    OHLC cannot say which came first, so the conservative and reproducible reading is
  //    that the already-active stop wins: it cannot be retroactively replaced by a stop
  //    that only exists later in the same bar.
  r = await replay([['2026-01-02', 120, 87.0, 118, 95], calm('2026-01-05')]);
  ck('a bar that both breaches the stop and clears the arm trigger exits at the OLD stop',
     Math.abs(r.price - STOP) < 0.01,
     `reported ${r.price}, expected ${STOP} (bar high ${120} cleared arm ${ARM})`);

  // 4. Same bar, but it gaps below the stop before doing anything else. Opening under the
  //    stop must beat arming too, or a crash day that later rallies reads as protected.
  r = await replay([['2026-01-02', 130, 78, 128, 80], calm('2026-01-05')]);
  ck('opening below the stop wins over arming on the same bar',
     Math.abs(r.price - 80) < 0.01 && r.gapped, `reported ${r.price}, gap=${r.gapped}`);

  // 5. Backward compatibility. A page loaded against a feed published before opens were
  //    added receives 4-element bars. It must still report the breach, at the old price —
  //    degrading to the previous answer, never dropping the breach.
  r = await replay([['2026-01-02', 82, 78, 81], ['2026-01-05', 101, 99, 100]]);
  ck('a legacy 4-field bar still reports the breach at the stop price',
     Math.abs(r.price - STOP) < 0.01 && !r.gapped,
     `reported ${r.price}, gap=${r.gapped}`);

  // 6. No breach at all: the stop must not fire on a bar that stayed above it.
  r = await replay([['2026-01-02', 105, 88.0, 104, 100], calm('2026-01-05')]);
  ck('a bar whose low holds above the stop reports no breach',
     Number.isNaN(r.price), r.txt.slice(0, 90));

  // 7. The breach date is the bar it happened on, not the last bar replayed.
  r = await replay([['2026-01-02', 82, 78, 81, 80], calm('2026-01-05'), calm('2026-01-06')]);
  ck('the breach is dated to its own bar, not the end of the replay',
     r.date === '2026-01-02', `reported ${r.date}`);

  report('POSITION TRACKER');
})();
