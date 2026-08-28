// Browser-to-Python contract: server persistence, fail-closed approval, and
// dashboard rendering of correlation/variance/incremental VaR.
const { boot, ck, report } = require('./lib');
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));

(async () => {
  const approved = boot({
    positions: [{
      tick: 'COREA', entry: 100, atr: 3, rung: 0, added: '2026-01-01',
      value_ils: 20000, currency: 'ILS', quantity: 200
    }]
  });
  approved.price(100, 5, 1.2);
  approved.set('in-ticker', 'NEW');
  await wait(850);
  ck('quant gate sends only the proposal; the server owns existing holdings',
     approved.w.__riskRequests.length === 1 &&
       !Object.prototype.hasOwnProperty.call(approved.w.__riskRequests[0], 'active_positions') &&
       !Object.prototype.hasOwnProperty.call(approved.w.__riskRequests[0], 'max_daily_var_ils'),
     JSON.stringify(approved.w.__riskRequests[0] || {}));
  ck('approved verdict is shown in green state',
     approved.$('riskGate').dataset.state === 'approved' &&
       approved.$('riskVerdict').textContent === 'APPROVED');
  ck('incremental 99% VaR is rendered in ILS',
     /1,000/.test(approved.$('riskIncrementalVar').textContent),
     approved.$('riskIncrementalVar').textContent);
  ck('server-calculated portfolio heat is rendered',
     approved.$('riskPortfolioHeat').textContent === '1 / 5R',
     approved.$('riskPortfolioHeat').textContent);
  ck('Track remains disabled until the matching risk request is approved',
     approved.$('addPosBtn').disabled === false);

  approved.$('addPosBtn').dispatchEvent(new approved.w.Event('click', { bubbles: true }));
  await wait(20);
  const savedPost = approved.w.__positionRequests.find(request => request.method === 'POST');
  const newest = savedPost && savedPost.body;
  ck('POSTed position contains actual value_ils', newest && newest.value_ils > 0, JSON.stringify(newest));
  ck('POSTed position contains quantity, currency, and risk status',
     newest && newest.quantity > 0 && newest.currency === 'ILS' && newest.risk_status === 'RISK_ON',
     JSON.stringify(newest));
  ck('new positions are never grandfathered by the browser', newest && newest.legacy === false,
     JSON.stringify(newest));

  approved.$('tab-settings').dispatchEvent(new approved.w.Event('click', { bubbles: true }));
  ck('Settings navigation opens the SQLite-backed settings modal',
     approved.$('settingsPanel').open === true &&
       approved.$('tab-settings').getAttribute('aria-selected') === 'true');
  approved.set('cfg-maxriskonr', '7');
  await wait(300);
  ck('Risk-On budget changes persist through /api/settings',
     approved.w.__settingsRequest && approved.w.__settingsRequest.settings.maxriskonr === 7,
     JSON.stringify(approved.w.__settingsRequest || {}));
  ck('desktop result CSS keeps gate, explanation, and inputs in document flow',
     !/\.result-card \.why\s*\{[^}]*position:\s*absolute/.test(approved.w.document.documentElement.innerHTML) &&
       !/\.result-card \.add-row\s*\{[^}]*position:\s*absolute/.test(approved.w.document.documentElement.innerHTML));
  const css = [...approved.w.document.querySelectorAll('style')].map(node => node.textContent).join('\n');
  ck('mobile breakpoint stacks result inputs and settings into one column',
     /@media\s*\(max-width:\s*779px\)[\s\S]*\.add-fields,\s*\.settings-grid,\s*\.row2\s*\{\s*grid-template-columns:\s*1fr/.test(css));
  ck('desktop breakpoint renders Settings as a fixed centered modal',
     /details\.settings\[open\]\s*\{\s*position:\s*fixed;[\s\S]*transform:\s*translate\(-50%,\s*-50%\)/.test(css));
  const activeActions = approved.w.document.querySelector('.pos .row-actions');
  ck('Active Edit and Delete actions share a compact trailing group',
     activeActions && activeActions.querySelectorAll('button').length === 2 &&
       /Edit/.test(activeActions.textContent) && /Delete/.test(activeActions.textContent));

  approved.set('coreTicker', 'LUMI.TA');
  approved.set('coreDisplayName', 'Leumi / לאומי');
  approved.set('coreValue', '100000');
  approved.$('coreForm').dispatchEvent(new approved.w.Event('submit', { bubbles: true, cancelable: true }));
  await wait(20);
  const coreActions = approved.w.document.querySelector('.core-row .row-actions');
  ck('Core friendly name and compact Edit/Delete group render together',
     /Leumi \/ לאומי/.test(approved.$('coreList').textContent) &&
       coreActions && coreActions.querySelectorAll('button').length === 2);

  const rejected = boot({ riskResponse: {
    is_trade_approved: false,
    risk_metrics: {
      correlation: { new_ticker_to_weighted_portfolio: 0.88 },
      variance: { relative_increase_pct: 0.31 },
      historical_var_99: { incremental_var_ils: 27000 },
      warnings: ['Correlation Warning', 'VaR REJECTION']
    }
  }});
  rejected.price(100, 5, 1.2);
  rejected.set('in-ticker', 'HOT');
  await wait(850);
  ck('rejected verdict is shown in red state',
     rejected.$('riskGate').dataset.state === 'rejected' &&
       rejected.$('riskVerdict').textContent === 'REJECTED');
  ck('specific correlation/VaR reasons are visible',
     /Correlation Warning/.test(rejected.$('riskReasons').textContent) &&
       /VaR REJECTION/.test(rejected.$('riskReasons').textContent));
  ck('rejected trade cannot be tracked', rejected.$('addPosBtn').disabled === true);

  const source = approved.w.document.documentElement.innerHTML;
  const storageCalls = [...source.matchAll(/localStorage\.(?:getItem|setItem|removeItem)\(([^)]*)\)/g)];
  ck('browser persistence is limited to the API credential',
     storageCalls.length > 0 && storageCalls.every(match => /^API_KEY_STORE\b/.test(match[1])));

  report('QUANTITATIVE RISK INTEGRATION');
})();
