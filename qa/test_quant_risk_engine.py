import unittest

import numpy as np
import pandas as pd

from quant_risk_engine import QuantitativeRiskEngine


def synthetic_download(symbols, **_kwargs):
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2023-01-02", periods=650)
    factor = rng.normal(0.0003, 0.010, len(dates))
    a_return = factor + rng.normal(0, 0.002, len(dates))
    b_return = 0.6 * factor + rng.normal(0, 0.006, len(dates))
    c_return = factor + rng.normal(0, 0.001, len(dates))
    diverse_return = rng.normal(0.0002, 0.006, len(dates))
    # Keep tail losses inside the 500-day VaR sample but outside the 90-day
    # correlation/covariance window, so each model is tested independently.
    c_return[-200:-188] = -0.10
    returns = {"COREA": a_return, "COREB": b_return, "NEW": c_return, "DIVERSE": diverse_return}
    columns = {}
    for symbol in symbols:
        series = returns.get(symbol, np.zeros(len(dates)))
        columns[(symbol, "Close")] = 100 * np.cumprod(1 + series)
    return pd.DataFrame(columns, index=dates)


class QuantitativeRiskEngineTests(unittest.TestCase):
    def make_engine(self, **overrides):
        kwargs = dict(
            current_positions=[
                {"ticker": "COREA", "value_ils": 100_000, "currency": "ILS"},
                {"ticker": "COREB", "value_ils": 100_000, "currency": "ILS"},
            ],
            new_ticker="NEW",
            new_ticker_currency="ILS",
            proposed_entry_size_ils=300_000,
            max_daily_var_ils=25_000,
            downloader=synthetic_download,
        )
        kwargs.update(overrides)
        return QuantitativeRiskEngine(**kwargs)

    def test_correlation_is_against_weighted_existing_portfolio(self):
        engine = self.make_engine()
        engine.fetch_historical_data()
        matrix, rho = engine.calculate_correlation_matrix()
        self.assertEqual(set(matrix.columns), {"COREA", "COREB", "NEW"})
        self.assertGreater(rho, 0.75)

    def test_var_budget_breach_rejects_trade(self):
        approved, metrics = self.make_engine().evaluate_trade()
        self.assertFalse(approved)
        self.assertTrue(metrics["historical_var_99"]["budget_breach"])
        self.assertGreater(metrics["historical_var_99"]["proposed_var_ils"], 25_000)

    def test_warnings_can_be_advisory_but_var_still_blocks(self):
        approved, metrics = self.make_engine(
            proposed_entry_size_ils=10_000,
            max_daily_var_ils=1_000_000,
            block_on_warnings=False,
        ).evaluate_trade()
        self.assertTrue(metrics["correlation"]["correlation_warning"])
        self.assertTrue(approved)

    def test_variance_metrics_are_mpt_quadratic_forms(self):
        engine = self.make_engine(max_daily_var_ils=1_000_000)
        engine.fetch_historical_data()
        metrics = engine.calculate_portfolio_variance()
        self.assertGreater(metrics["current_daily_variance"], 0)
        self.assertGreater(metrics["proposed_daily_variance"], 0)
        self.assertEqual(metrics["lookback_observations"], 90)

    def test_existing_over_budget_var_is_grandfathered_if_trade_reduces_it(self):
        engine = QuantitativeRiskEngine(
            current_positions=[{"ticker": "COREA", "value_ils": 100_000, "currency": "ILS"}],
            new_ticker="DIVERSE", new_ticker_currency="ILS",
            proposed_entry_size_ils=100_000, max_daily_var_ils=5_000,
            downloader=synthetic_download,
        )
        dates = pd.bdate_range("2025-01-01", periods=300)
        core = np.zeros(300)
        hedge = np.zeros(300)
        core[:5] = -0.10
        hedge[:5] = 0.10
        engine._returns = pd.DataFrame({"COREA": core, "DIVERSE": hedge}, index=dates)
        metrics = engine.calculate_historical_var_99()
        self.assertTrue(metrics["current_budget_breach"])
        self.assertLess(metrics["proposed_var_ils"], metrics["current_var_ils"])
        self.assertFalse(metrics["budget_breach"])


if __name__ == "__main__":
    unittest.main()
