// Regressions for the four problems that only surfaced in real use on Safari.
const { boot, ck, report } = require('./lib');
const { JSDOM } = require('jsdom'); const fs = require('fs');
const HTML = require('./lib').HTML;   // resolves index.html or risk-sizer.html

const t = boot();
// ---- empty state: no half-rendered result cards before there is a result
ck('UX-1 answer card hidden before any input', t.$('answerCard').hidden === true);
ck('UX-1 ladder card hidden before any input', t.$('ladderCard').hidden === true);
ck('UX-1 tracking row hidden before any input', t.$('addRow').hidden === true);
t.price(100, 3);
ck('UX-1 all three appear once a size exists',
   !t.$('answerCard').hidden && !t.$('ladderCard').hidden && !t.$('addRow').hidden);
t.set('in-price', '');
ck('UX-1 and disappear again when input is cleared', t.$('answerCard').hidden === true);

// ---- only one ticker box should be reachable before sizing
t.set('in-price', ''); t.set('in-atr', '');
ck('UX-2 only the lookup ticker field is visible when empty',
   t.$('in-lookup').offsetParent !== undefined && t.$('addRow').hidden === true);
ck('UX-2 lookup placeholder names an example', /AAPL/.test(t.$('in-lookup').placeholder),
   t.$('in-lookup').placeholder);
ck('UX-2 tracking field is labelled distinctly',
   /Ticker to track/.test(HTML) && !/Track it — ticker/.test(HTML));

// ---- jargon removed from the tagline
ck('UX-3 no "thesis break" jargon anywhere in the UI copy', !/thesis break/i.test(HTML));

// ---- weekend staleness must not cry wolf
function ageHours(h) {
  const iso = new Date(Date.now() - h * 3600e3).toISOString().slice(0, 19) + 'Z';
  return JSON.stringify({ updated: iso, fx: 3.07, count: 2605, tickers: {} });
}
function statusAt(hours) {
  const d = new JSDOM(HTML, { runScripts: 'dangerously', url: 'https://qa.local/', beforeParse(w) {
    w.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(JSON.parse(ageHours(hours))) });
  }});
  const g = (id) => d.window.document.getElementById(id);
  const set = (id, v) => { const n = g(id); n.value = v; n.dispatchEvent(new d.window.Event('input', { bubbles: true })); };
  set('setup-capital', '100000'); set('setup-maxspec', '6000');
  g('setupSave').dispatchEvent(new d.window.Event('click', { bubbles: true }));
  return () => g('feedStatus').textContent;
}
const dow = new Date().getUTCDay();
const weekendish = (dow === 0 || dow === 6 || dow === 1);
const s41 = statusAt(41);
setTimeout(() => {
  const txt = s41();
  if (weekendish) {
    ck('UX-4 a 41h-old feed is NOT called stale over a weekend', !/stale/.test(txt), txt);
    ck('UX-4 it is explained as a closed market instead', /market closed/.test(txt), txt);
  } else {
    ck('UX-4 a 41h-old feed IS called stale on a trading day', /stale/.test(txt), txt);
  }
  const s200 = statusAt(200);
  setTimeout(() => {
    ck('UX-4 a genuinely dead feed is still flagged stale', /stale/.test(s200()), s200());
    report('REAL-USE REGRESSIONS');
  }, 200);
}, 200);
