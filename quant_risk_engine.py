"""Quantitative pre-trade risk gate for the combined Core + Active portfolio.

The engine uses adjusted daily prices, converts foreign-asset returns into ILS returns,
and fetches every required ticker/FX pair in one yfinance request. It intentionally does
not forward-fill across exchange holidays because invented zero returns bias correlation,
covariance, and historical VaR downward.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any, Callable, Iterable, Mapping

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ModuleNotFoundError:  # Pure calculations/tests can use an injected downloader.
    yf = None


@dataclass(frozen=True)
class PositionValue:
    ticker: str
    value_ils: float
    currency: str = "AUTO"
    fx_ticker: str | None = None

    @classmethod
    def from_input(cls, raw: "PositionValue | Mapping[str, Any]") -> "PositionValue":
        if isinstance(raw, cls):
            return raw
        return cls(
            ticker=str(raw["ticker"]).strip().upper(),
            value_ils=float(raw["value_ils"]),
            currency=str(raw.get("currency", "AUTO")).strip().upper(),
            fx_ticker=(
                str(raw["fx_ticker"]).strip().upper()
                if raw.get("fx_ticker")
                else None
            ),
        )


class QuantitativeRiskEngine:
    """Estimate marginal concentration, variance, and 1-day historical VaR.

    ``current_positions`` accepts dictionaries such as
    ``{"ticker": "MSFT", "value_ils": 150000, "currency": "USD"}``.
    ``currency`` is optional: ``.TA`` symbols default to ILS and all other symbols to
    USD. For another currency, pass ``fx_ticker`` whose quote is ILS per unit of that
    currency (for example ``EURILS=X``).

    Correlation and covariance use the most recent 90 aligned return observations.
    Historical 99% VaR defaults to 500 observations, because 90 observations contain
    fewer than one observation in the 1% tail and cannot support a meaningful 99% VaR.
    """

    def __init__(
        self,
        current_positions: Iterable[PositionValue | Mapping[str, Any]],
        new_ticker: str,
        proposed_entry_size_ils: float,
        *,
        max_daily_var_ils: float = 25_000.0,
        correlation_threshold: float = 0.75,
        max_variance_increase_pct: float = 0.20,
        correlation_lookback_days: int = 90,
        var_lookback_days: int = 500,
        min_correlation_observations: int = 60,
        min_var_observations: int = 250,
        block_on_warnings: bool = True,
        new_ticker_currency: str = "AUTO",
        new_ticker_fx_ticker: str | None = None,
        downloader: Callable[..., pd.DataFrame] | None = None,
    ) -> None:
        positions = [PositionValue.from_input(item) for item in current_positions]
        if not positions:
            raise ValueError("at least one current Core or Active position is required")
        if proposed_entry_size_ils <= 0:
            raise ValueError("proposed_entry_size_ils must be positive")
        if max_daily_var_ils <= 0:
            raise ValueError("max_daily_var_ils must be positive")
        if not 0 < correlation_threshold <= 1:
            raise ValueError("correlation_threshold must be in (0, 1]")
        if max_variance_increase_pct < 0:
            raise ValueError("max_variance_increase_pct cannot be negative")
        if correlation_lookback_days < min_correlation_observations:
            raise ValueError("correlation lookback is shorter than its minimum sample")
        if var_lookback_days < min_var_observations:
            raise ValueError("VaR lookback is shorter than its minimum sample")

        self.current_positions = tuple(self._validate_position(p) for p in positions)
        self.new_position = self._validate_position(
            PositionValue(
                ticker=new_ticker.strip().upper(),
                value_ils=float(proposed_entry_size_ils),
                currency=new_ticker_currency.upper(),
                fx_ticker=(new_ticker_fx_ticker.upper() if new_ticker_fx_ticker else None),
            )
        )
        self.max_daily_var_ils = float(max_daily_var_ils)
        self.correlation_threshold = float(correlation_threshold)
        self.max_variance_increase_pct = float(max_variance_increase_pct)
        self.correlation_lookback_days = int(correlation_lookback_days)
        self.var_lookback_days = int(var_lookback_days)
        self.min_correlation_observations = int(min_correlation_observations)
        self.min_var_observations = int(min_var_observations)
        # 250 is the preferred sample for 99% historical VaR. A slightly shorter
        # history (common for newer listings) is still usable, but is disclosed as
        # degraded. Below 200 observations the tail estimate is too fragile to use.
        self.minimum_usable_var_observations = min(self.min_var_observations, 200)
        self.block_on_warnings = bool(block_on_warnings)
        if downloader is None and yf is None:
            raise RuntimeError("yfinance is required unless a downloader is injected")
        self._downloader = downloader or yf.download
        self._ils_prices: pd.DataFrame | None = None
        self._returns: pd.DataFrame | None = None
        self._excluded_positions: dict[str, str] = {}

    def fetch_historical_data(self, force_refresh: bool = False) -> pd.DataFrame:
        """Fetch and cache ILS-valued adjusted closes for all holdings in one batch."""
        if self._ils_prices is not None and not force_refresh:
            return self._ils_prices.copy()
        self._excluded_positions = {}

        specifications = self._ticker_specifications()
        market_tickers = sorted(specifications)
        fx_tickers = sorted(
            {
                fx
                for currency, fx in specifications.values()
                if currency != "ILS" and fx is not None
            }
        )
        symbols = market_tickers + [fx for fx in fx_tickers if fx not in market_tickers]
        period = "3y" if self.var_lookback_days <= 700 else "5y"
        try:
            raw = self._downloader(
                symbols,
                period=period,
                interval="1d",
                auto_adjust=True,
                progress=False,
                group_by="ticker",
                threads=True,
            )
        except Exception:  # Provider/network failure: retry each symbol below.
            raw = pd.DataFrame()
        if raw is None:
            raw = pd.DataFrame()

        close: dict[str, pd.Series] = {}
        missing: dict[str, str] = {}
        for symbol in symbols:
            try:
                close[symbol] = self._extract_close(raw, symbol)
            except RuntimeError as exc:
                missing[symbol] = str(exc)

        # Yahoo occasionally returns a structurally valid batch frame with one empty
        # ticker.  An individual retry uses a different endpoint/code path in
        # yfinance and commonly recovers that ticker without failing the whole book.
        for symbol in tuple(missing):
            try:
                retry = self._downloader(
                    symbol,
                    period=period,
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                    group_by="ticker",
                    threads=False,
                )
            except Exception:
                continue
            if retry is None or retry.empty:
                continue
            try:
                close[symbol] = self._extract_close(retry, symbol)
            except RuntimeError:
                continue
            missing.pop(symbol, None)

        if self.new_position.ticker in missing:
            raise RuntimeError(
                f"{self.new_position.ticker}: no valid closing prices for the proposed ticker"
            )

        converted: dict[str, pd.Series] = {}
        for ticker, (currency, fx_ticker) in specifications.items():
            if ticker in missing:
                # Existing instruments unsupported by Yahoo (for example local fund
                # security numbers) must not make the endpoint unavailable. They are
                # omitted from market-risk calculations and disclosed in the result.
                self._excluded_positions[ticker] = missing[ticker]
                continue
            local_price = close[ticker]
            if currency == "ILS":
                ils_price = local_price
            else:
                if not fx_ticker:
                    raise ValueError(f"{ticker}: no ILS FX ticker configured for {currency}")
                if fx_ticker in missing:
                    raise RuntimeError(
                        f"{ticker}: no valid closing prices for required FX ticker {fx_ticker}"
                    )
                # Multiplication makes pct_change include both the asset and FX return.
                ils_price = local_price.mul(close[fx_ticker], fill_value=np.nan)
            converted[ticker] = ils_price.rename(ticker)

        current_tickers = {position.ticker for position in self.current_positions}
        if not current_tickers.intersection(converted):
            raise RuntimeError("no current holdings have usable closing-price history")

        prices = pd.concat(converted.values(), axis=1, join="outer").sort_index()
        prices = prices.replace([np.inf, -np.inf], np.nan)
        returns = prices.pct_change(fill_method=None).dropna(how="any")
        required = max(self.correlation_lookback_days, self.var_lookback_days)
        if len(returns) < min(required, self.minimum_usable_var_observations):
            raise RuntimeError(
                f"only {len(returns)} aligned returns are available; "
                f"at least {self.minimum_usable_var_observations} are required"
            )
        self._ils_prices = prices
        self._returns = returns
        return prices.copy()

    def calculate_correlation_matrix(self) -> tuple[pd.DataFrame, float]:
        """Return the Pearson matrix and rho(new ticker, weighted existing portfolio)."""
        returns = self._get_returns().tail(self.correlation_lookback_days)
        if len(returns) < self.min_correlation_observations:
            raise RuntimeError("insufficient aligned observations for correlation")
        matrix = returns.corr(method="pearson")
        existing_amounts = self._current_amounts()
        existing_weights = existing_amounts / existing_amounts.sum()
        existing_return = returns[existing_weights.index].dot(existing_weights)
        new_return = returns[self.new_position.ticker]
        rho = float(new_return.corr(existing_return, method="pearson"))
        if not np.isfinite(rho):
            raise RuntimeError("new-ticker correlation is undefined")
        return matrix, rho

    def calculate_portfolio_variance(self) -> dict[str, float | int | bool]:
        """Compute daily MPT variance before and after the proposed position."""
        returns = self._get_returns().tail(self.correlation_lookback_days)
        covariance = returns.cov()
        current_amounts = self._current_amounts()
        proposed_amounts = current_amounts.copy()
        proposed_amounts.loc[self.new_position.ticker] = (
            proposed_amounts.get(self.new_position.ticker, 0.0)
            + self.new_position.value_ils
        )
        current_variance = self._weighted_variance(covariance, current_amounts)
        proposed_variance = self._weighted_variance(covariance, proposed_amounts)
        if current_variance <= 0:
            relative_increase = np.inf if proposed_variance > 0 else 0.0
        else:
            relative_increase = proposed_variance / current_variance - 1.0
        warning = bool(relative_increase > self.max_variance_increase_pct)
        return {
            "lookback_observations": int(len(returns)),
            "current_daily_variance": float(current_variance),
            "proposed_daily_variance": float(proposed_variance),
            "relative_increase_pct": float(relative_increase),
            "warning_threshold_pct": self.max_variance_increase_pct,
            "variance_warning": warning,
            "current_annualized_volatility_pct": sqrt(max(current_variance, 0.0) * 252),
            "proposed_annualized_volatility_pct": sqrt(max(proposed_variance, 0.0) * 252),
        }

    def calculate_historical_var_99(self) -> dict[str, float | int | bool]:
        """Calculate conservative empirical 1-day 99% VaR from ILS daily P&L."""
        returns = self._get_returns().tail(self.var_lookback_days)
        if len(returns) < self.minimum_usable_var_observations:
            raise RuntimeError("insufficient aligned observations for historical VaR")
        current_amounts = self._current_amounts()
        proposed_amounts = current_amounts.copy()
        proposed_amounts.loc[self.new_position.ticker] = (
            proposed_amounts.get(self.new_position.ticker, 0.0)
            + self.new_position.value_ils
        )
        current_pnl = returns[current_amounts.index].dot(current_amounts)
        proposed_pnl = returns[proposed_amounts.index].dot(proposed_amounts)
        current_var = self._historical_var(current_pnl)
        proposed_var = self._historical_var(proposed_pnl)
        current_budget_breach = current_var > self.max_daily_var_ils
        incremental_breach = (
            proposed_var > self.max_daily_var_ils
            and proposed_var > current_var + 1e-9
        )
        return {
            "confidence": 0.99,
            "horizon_days": 1,
            "lookback_observations": int(len(returns)),
            "current_var_ils": current_var,
            "proposed_var_ils": proposed_var,
            "incremental_var_ils": proposed_var - current_var,
            "max_daily_risk_budget_ils": self.max_daily_var_ils,
            "current_budget_breach": current_budget_breach,
            "budget_breach": incremental_breach,
        }

    def evaluate_trade(self) -> tuple[bool, dict[str, Any]]:
        """Return ``(is_trade_approved, metrics_for_the_daily_dashboard)``."""
        self.fetch_historical_data()
        matrix, weighted_correlation = self.calculate_correlation_matrix()
        variance = self.calculate_portfolio_variance()
        historical_var = self.calculate_historical_var_99()
        correlation_warning = weighted_correlation > self.correlation_threshold
        variance_warning = bool(variance["variance_warning"])
        budget_breach = bool(historical_var["budget_breach"])

        warnings: list[str] = []
        if correlation_warning:
            warnings.append(
                f"Correlation Warning: rho {weighted_correlation:.3f} exceeds "
                f"{self.correlation_threshold:.2f}."
            )
        if variance_warning:
            warnings.append(
                "Variance Warning: proposed daily variance rises by "
                f"{float(variance['relative_increase_pct']):.1%}, above the "
                f"{self.max_variance_increase_pct:.1%} limit."
            )
        if budget_breach:
            warnings.append(
                f"VaR REJECTION: proposed 99% 1-day VaR is ILS "
                f"{float(historical_var['proposed_var_ils']):,.0f}, above the ILS "
                f"{self.max_daily_var_ils:,.0f} budget."
            )

        warning_block = self.block_on_warnings and (
            correlation_warning or variance_warning
        )
        is_trade_approved = not budget_breach and not warning_block
        metrics: dict[str, Any] = {
            "correlation": {
                "new_ticker_to_weighted_portfolio": weighted_correlation,
                "threshold": self.correlation_threshold,
                "correlation_warning": correlation_warning,
                "matrix": matrix.round(6).to_dict(),
            },
            "variance": variance,
            "historical_var_99": historical_var,
            "data": {
                "as_of": self._get_returns().index[-1].date().isoformat(),
                "correlation_lookback": self.correlation_lookback_days,
                "var_lookback": self.var_lookback_days,
                "prices_are_adjusted": True,
                "returns_are_ils_adjusted": True,
                "missing_dates_forward_filled": False,
                "degraded": bool(self._excluded_positions) or (
                    len(self._get_returns()) < self.min_var_observations
                ),
                "excluded_current_positions": sorted(self._excluded_positions),
                "preferred_var_observations": self.min_var_observations,
                "minimum_usable_var_observations": self.minimum_usable_var_observations,
            },
            "warnings": warnings,
            "warnings_block_trade": self.block_on_warnings,
        }
        if self._excluded_positions:
            omitted = ", ".join(sorted(self._excluded_positions))
            metrics["warnings"].append(
                "Market-data warning: excluded current holding(s) with no usable "
                f"closing-price history: {omitted}."
            )
        available_observations = len(self._get_returns())
        if available_observations < self.min_var_observations:
            metrics["warnings"].append(
                "Market-data warning: historical VaR used a limited but usable "
                f"sample of {available_observations} aligned returns; "
                f"{self.min_var_observations} are preferred."
            )
        return is_trade_approved, metrics

    def _ticker_specifications(self) -> dict[str, tuple[str, str | None]]:
        specs: dict[str, tuple[str, str | None]] = {}
        for position in (*self.current_positions, self.new_position):
            currency = self._resolve_currency(position)
            fx = self._resolve_fx_ticker(position, currency)
            previous = specs.get(position.ticker)
            if previous and previous != (currency, fx):
                raise ValueError(
                    f"{position.ticker} has conflicting currency/FX specifications"
                )
            specs[position.ticker] = (currency, fx)
        return specs

    def _current_amounts(self) -> pd.Series:
        amounts: dict[str, float] = {}
        for position in self.current_positions:
            amounts[position.ticker] = amounts.get(position.ticker, 0.0) + position.value_ils
        if self._returns is not None:
            amounts = {
                ticker: amount for ticker, amount in amounts.items()
                if ticker in self._returns.columns
            }
        return pd.Series(amounts, dtype=float)

    def _get_returns(self) -> pd.DataFrame:
        if self._returns is None:
            self.fetch_historical_data()
        assert self._returns is not None
        return self._returns

    @staticmethod
    def _weighted_variance(covariance: pd.DataFrame, amounts: pd.Series) -> float:
        weights = amounts / amounts.sum()
        selected = covariance.loc[weights.index, weights.index]
        return float(weights.to_numpy() @ selected.to_numpy() @ weights.to_numpy())

    @staticmethod
    def _historical_var(pnl: pd.Series) -> float:
        # ``lower`` selects an observed loss at or below the 1% quantile instead of
        # interpolating a less severe synthetic value between tail observations.
        tail_pnl = float(np.quantile(pnl.to_numpy(), 0.01, method="lower"))
        return max(0.0, -tail_pnl)

    @staticmethod
    def _validate_position(position: PositionValue) -> PositionValue:
        if not position.ticker:
            raise ValueError("position ticker is required")
        if not np.isfinite(position.value_ils) or position.value_ils <= 0:
            raise ValueError(f"{position.ticker}: value_ils must be positive and finite")
        return position

    @staticmethod
    def _resolve_currency(position: PositionValue) -> str:
        if position.currency != "AUTO":
            return position.currency
        return "ILS" if position.ticker.endswith(".TA") else "USD"

    @staticmethod
    def _resolve_fx_ticker(position: PositionValue, currency: str) -> str | None:
        if currency == "ILS":
            return None
        if position.fx_ticker:
            return position.fx_ticker
        if currency == "USD":
            return "ILS=X"
        raise ValueError(
            f"{position.ticker}: provide fx_ticker for non-ILS/non-USD currency {currency}"
        )

    @staticmethod
    def _extract_close(raw: pd.DataFrame, symbol: str) -> pd.Series:
        if isinstance(raw.columns, pd.MultiIndex):
            level0 = raw.columns.get_level_values(0)
            level1 = raw.columns.get_level_values(1)
            if symbol in level0 and "Close" in level1:
                series = raw[symbol]["Close"]
            elif "Close" in level0 and symbol in level1:
                series = raw["Close"][symbol]
            else:
                raise RuntimeError(f"{symbol}: Close column missing from yfinance data")
        else:
            if "Close" not in raw.columns:
                raise RuntimeError(f"{symbol}: Close column missing from yfinance data")
            series = raw["Close"]
        if isinstance(series, pd.DataFrame):
            series = series.iloc[:, 0]
        series = pd.to_numeric(series, errors="coerce").dropna()
        if series.empty:
            raise RuntimeError(f"{symbol}: no valid closing prices")
        return series.rename(symbol)


if __name__ == "__main__":
    engine = QuantitativeRiskEngine(
        current_positions=[
            {"ticker": "QQQ", "value_ils": 300_000, "currency": "USD"},
            {"ticker": "MSFT", "value_ils": 200_000, "currency": "USD"},
        ],
        new_ticker="NVDA",
        proposed_entry_size_ils=75_000,
        max_daily_var_ils=25_000,
    )
    approved, risk_metrics = engine.evaluate_trade()
    print({"is_trade_approved": approved, "risk_metrics": risk_metrics})
