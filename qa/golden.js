// GOLDEN SNAPSHOT — freeze the answers for a fixed set of stocks. Any logic change that
// moves them fails here, so a shift is always a deliberate decision rather than a surprise.
// Regenerate on purpose with:  node golden.js --update
const { boot, ck, report } = require('./lib');
const fs = require('fs');
const FILE = __dirname + '/golden.json';
const CASES = [
  ['very calm',  100,   1.0],   ['calm',      100,   2.0],
  ['active',     100,   3.25],  ['volatile',  100,   5.0],
  ['wild',       100,   8.0],   ['saturated', 100,  15.0],
  ['AAPL-like',  338.19, 8.02], ['MSFT-like', 500,  15.2],
  ['QBTS-like',  16.18,  1.71], ['ASTS-like', 53.03, 6.48],
  ['KO-like',    89.08,  2.05], ['TSLA-like', 298.32, 17.43],
  ['penny',      4.5,    0.4],  ['high-price', 1200, 30],
];
const t = boot();
const got = {};
for (const [name, p, a] of CASES) {
  t.price(p, a, 1.5);
  got[name] = { size: t.size(), stop: t.stop(), stopPct: t.stopPctStated(),
                risk: t.riskStated(), rungs: t.ladder().length };
}
if (process.argv.includes('--update')) {
  fs.writeFileSync(FILE, JSON.stringify(got, null, 2));
  console.log(`golden.json written with ${Object.keys(got).length} cases`);
  process.exit(0);
}
if (!fs.existsSync(FILE)) { console.log('no golden.json — run: node golden.js --update'); process.exit(1); }
const want = JSON.parse(fs.readFileSync(FILE, 'utf8'));
for (const k of Object.keys(want)) {
  const g = got[k], e = want[k];
  const same = g && ['size','stop','stopPct','risk','rungs'].every(f => Math.abs(g[f] - e[f]) < 0.02);
  ck(`${k} unchanged`, same,
     same ? `₪${e.size.toLocaleString()} stop ${e.stop}` :
     `expected ₪${e.size} stop ${e.stop} risk ₪${e.risk} — got ₪${g && g.size} stop ${g && g.stop} risk ₪${g && g.risk}`);
}
report('GOLDEN SNAPSHOT');
