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

    def test_missing_current_holding_is_retried_then_reported_and_excluded(self):
        calls = []

        def partial_download(symbols, **kwargs):
            requested = [symbols] if isinstance(symbols, str) else list(symbols)
            calls.append((requested, kwargs["threads"]))
            available = [symbol for symbol in requested if symbol != "5140918"]
            return synthetic_download(available) if available else pd.DataFrame()

        engine = self.make_engine(
            current_positions=[
                {"ticker": "COREA", "value_ils": 100_000, "currency": "ILS"},
                {"ticker": "5140918", "value_ils": 50_000, "currency": "ILS"},
            ],
            proposed_entry_size_ils=10_000,
            max_daily_var_ils=1_000_000,
            block_on_warnings=False,
            downloader=partial_download,
        )
        approved, metrics = engine.evaluate_trade()

        self.assertTrue(approved)
        self.assertIn((["5140918"], False), calls)
        self.assertTrue(metrics["data"]["degraded"])
        self.assertEqual(metrics["data"]["excluded_current_positions"], ["5140918"])
        self.assertIn("5140918", metrics["warnings"][-1])

    def test_missing_proposed_ticker_still_fails_closed(self):
        def partial_download(symbols, **_kwargs):
            requested = [symbols] if isinstance(symbols, str) else list(symbols)
            available = [symbol for symbol in requested if symbol != "NEW"]
            return synthetic_download(available) if available else pd.DataFrame()

        with self.assertRaisesRegex(RuntimeError, "proposed ticker"):
            self.make_engine(downloader=partial_download).evaluate_trade()

    def test_empty_batch_is_recovered_by_individual_retries(self):
        calls = []

        def delayed_download(symbols, **_kwargs):
            requested = [symbols] if isinstance(symbols, str) else list(symbols)
            calls.append(requested)
            if len(calls) == 1:
                return pd.DataFrame()
            return synthetic_download(requested)

        approved, metrics = self.make_engine(
            proposed_entry_size_ils=10_000,
            max_daily_var_ils=1_000_000,
            block_on_warnings=False,
            downloader=delayed_download,
        ).evaluate_trade()

        self.assertTrue(approved)
        self.assertGreaterEqual(len(calls), 4)
        self.assertFalse(metrics["data"]["degraded"])

    def test_244_aligned_returns_complete_with_degraded_warning(self):
        def short_download(symbols, **kwargs):
            return synthetic_download(symbols, **kwargs).iloc[-245:]

        approved, metrics = self.make_engine(
            proposed_entry_size_ils=10_000,
            max_daily_var_ils=1_000_000,
            block_on_warnings=False,
            downloader=short_download,
        ).evaluate_trade()

        self.assertTrue(approved)
        self.assertEqual(metrics["historical_var_99"]["lookback_observations"], 244)
        self.assertTrue(metrics["data"]["degraded"])
        self.assertIn("244 aligned returns", metrics["warnings"][-1])

    def test_history_below_200_aligned_returns_still_fails_closed(self):
        def too_short_download(symbols, **kwargs):
            return synthetic_download(symbols, **kwargs).iloc[-200:]

        with self.assertRaisesRegex(RuntimeError, "at least 200"):
            self.make_engine(downloader=too_short_download).evaluate_trade()


if __name__ == "__main__":
    unittest.main()
