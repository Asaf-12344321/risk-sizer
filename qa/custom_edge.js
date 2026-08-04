// qa/custom_edge.js — Extended Edge Case Suite
const { boot, ck, report } = require('./lib');

const t = boot();

// 1. Extreme Volatility (ATR = 80% of price)
t.price(100, 80);
ck('Extreme ATR (80%) -> Stop clamped to maxstop (30%)', t.stopPctStated() === 30, `Stop: ${t.stopPctStated()}%`);
ck('Extreme ATR (80%) -> Flags warn user about clean stop breach', t.$('out-flags').textContent.includes('Too volatile'), t.$('out-flags').textContent);

// 2. Micro Volatility (ATR = 0.01% of price)
t.price(100, 0.01);
ck('Micro ATR (0.01%) -> Stop floored at minstop (8%)', t.stopPctStated() === 8, `Stop: ${t.stopPctStated()}%`);

// 3. Beta Zero Division Guard
t.set('in-price', '100');
t.set('in-atr', '3');
t.set('in-beta', '0');
ck('Beta = 0 -> Does not cause Infinity or crash', !/Infinity|NaN/.test(t.$('out-size-formula').textContent), t.$('out-size-formula').textContent);

// 4. Zero/Negative FX Guard
t.set('in-ccy', 'USD');
t.set('in-fx', '0');
t.price(100, 3);
ck('FX = 0 -> Falls back to default FX without NaN', !t.$('out-size').textContent.includes('NaN'), t.$('out-size').textContent);

// 5. Input Reset & Blanking
t.set('in-price', '');
ck('Empty Price -> Resets output cleanly to placeholder', t.$('out-size').textContent.includes('—'), t.$('out-size').textContent);

report('CUSTOM EDGE CASES');