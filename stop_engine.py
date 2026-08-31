"""Pure post-close stop replay and isolated HAR-Parkinson shadow forecasts.

This module deliberately has no imports from the calculator, sizing, or portfolio-VaR
code.  It can recommend a next-session stop and write a reference volatility record,
but it cannot change an order, size, or risk gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from math import floor, log, sqrt
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


POLICY_VERSION = "risk-sizer-stop-policy-v1"
STOP_ENGINE_VERSION = "post-close-stop-replay-v1"
HAR_SHADOW_VERSION = "har-parkinson-shadow-v1"
HAR_MIN_TRAIN = 252
HAR_MAX_TRAIN = 1260


def _hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def build_policy_snapshot(entry_atr: float, settings: Mapping[str, float]) -> dict[str, Any]:
    """Capture every value that can affect an entered position's ladder."""
    if not entry_atr > 0:
        raise ValueError("entry_atr must be positive")
    keys = ("initmult", "trailmult", "armpct", "armatrmult", "minstop", "maxstop")
    snapshot = {
        "policy_version": POLICY_VERSION,
        "entry_atr": float(entry_atr),
        "price_basis": "adjusted_daily_ohlc",
        "arming_reference": "intraday_high",
        "trailing_reference": "highest_close",
        "tick_size": 0.01,
    }
    for key in keys:
        value = float(settings[key])
        if not np.isfinite(value):
            raise ValueError(f"policy setting {key} must be finite")
        snapshot[key] = value
    if snapshot["trailmult"] <= 0 or snapshot["initmult"] <= 0 or snapshot["tick_size"] <= 0:
        raise ValueError("policy multipliers and tick size must be positive")
    if snapshot["minstop"] <= 0 or snapshot["maxstop"] < snapshot["minstop"]:
        raise ValueError("policy stop bounds are invalid")
    return snapshot


def validate_policy_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "policy_version", "entry_atr", "price_basis", "arming_reference",
        "trailing_reference", "tick_size", "initmult", "trailmult", "armpct",
        "armatrmult", "minstop", "maxstop",
    }
    missing = required - set(snapshot)
    if missing:
        raise ValueError(f"policy snapshot missing: {', '.join(sorted(missing))}")
    if snapshot["policy_version"] != POLICY_VERSION:
        raise ValueError("unsupported policy snapshot version")
    if snapshot["arming_reference"] != "intraday_high" or snapshot["trailing_reference"] != "highest_close":
        raise ValueError("policy snapshot has unsupported stop references")
    normalized = dict(snapshot)
    for key in ("entry_atr", "tick_size", "initmult", "trailmult", "armpct", "armatrmult", "minstop", "maxstop"):
        normalized[key] = float(normalized[key])
        if not np.isfinite(normalized[key]):
            raise ValueError(f"policy snapshot {key} must be finite")
    if normalized["entry_atr"] <= 0 or normalized["tick_size"] <= 0:
        raise ValueError("policy snapshot has invalid ATR or tick")
    if normalized["minstop"] <= 0 or normalized["maxstop"] < normalized["minstop"]:
        raise ValueError("policy snapshot has invalid stop bounds")
    return normalized


@dataclass(frozen=True)
class DailyBar:
    session: str
    high: float
    low: float
    close: float
    open: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DailyBar":
        session = str(value["session"])
        high, low, close = (float(value[key]) for key in ("high", "low", "close"))
        opening = value.get("open")
        opening = float(opening) if opening is not None else None
        if not high > 0 or not low > 0 or not close > 0 or high < low:
            raise ValueError(f"invalid OHLC for {session}")
        return cls(session=session, high=high, low=low, close=close, open=opening)


def _round_down_to_tick(value: float, tick_size: float) -> float:
    return floor((value + 1e-12) / tick_size) * tick_size


def _initial_stop(entry_price: float, snapshot: Mapping[str, Any]) -> float:
    raw_pct = snapshot["initmult"] * snapshot["entry_atr"] / entry_price * 100.0
    placed_pct = min(max(raw_pct, snapshot["minstop"]), snapshot["maxstop"])
    return _round_down_to_tick(entry_price * (1.0 - placed_pct / 100.0), snapshot["tick_size"])


def _arm_price(entry_price: float, snapshot: Mapping[str, Any]) -> float:
    trigger_pct = max(snapshot["armpct"], snapshot["armatrmult"] * snapshot["entry_atr"] / entry_price * 100.0)
    return entry_price * (1.0 + trigger_pct / 100.0)


def replay_post_close_stop(
    *,
    entry_price: float,
    policy_snapshot: Mapping[str, Any],
    bars: Iterable[DailyBar | Mapping[str, Any]],
    previous_confirmed_stop_price: float | None = None,
) -> dict[str, Any]:
    """Replay final bars and produce a next-session, never-lower stop payload."""
    if not entry_price > 0:
        raise ValueError("entry_price must be positive")
    snapshot = validate_policy_snapshot(policy_snapshot)
    history = [item if isinstance(item, DailyBar) else DailyBar.from_mapping(item) for item in bars]
    if not history:
        raise ValueError("at least one finalized daily bar is required")
    sessions = [item.session for item in history]
    if sessions != sorted(sessions) or len(set(sessions)) != len(sessions):
        raise ValueError("bars must have unique ascending sessions")
    initial_stop = _initial_stop(entry_price, snapshot)
    prior = initial_stop if previous_confirmed_stop_price is None else max(initial_stop, float(previous_confirmed_stop_price))
    if not np.isfinite(prior) or prior <= 0:
        raise ValueError("previous confirmed stop is invalid")
    stop, highest_close, armed, exit_fill = prior, entry_price, False, None
    arm_price = _arm_price(entry_price, snapshot)
    for bar in history:
        if bar.open is not None and bar.open <= stop:
            exit_fill = {"session": bar.session, "price": bar.open, "gap": True}
            break
        if bar.low <= stop:
            exit_fill = {"session": bar.session, "price": stop, "gap": False}
            break
        highest_close = max(highest_close, bar.close)
        if not armed and max(highest_close, bar.high) >= arm_price:
            armed, stop = True, max(stop, entry_price)
        if armed:
            candidate = _round_down_to_tick(highest_close - snapshot["trailmult"] * snapshot["entry_atr"], snapshot["tick_size"])
            stop = max(stop, candidate, entry_price)
    current = float(stop)
    delta = current - prior
    ticks = int(floor(max(0.0, delta) / snapshot["tick_size"] + 1e-9))
    moved = bool(exit_fill is None and ticks >= 1)
    return {
        "engine_version": STOP_ENGINE_VERSION,
        "as_of_session": history[-1].session,
        "current_stop_price": current,
        "previous_confirmed_stop_price": float(prior),
        "stop_moved_up": moved,
        "delta_ticks": ticks,
        "actionable_alert_needed": moved,
        "position_exit_detected": exit_fill is not None,
        "exit_fill": exit_fill,
        "armed": armed,
        "highest_close": highest_close,
        "initial_stop_price": initial_stop,
        "policy_hash": _hash(snapshot),
    }


def _canonical_ohlc(frame: pd.DataFrame, as_of_session: pd.Timestamp) -> pd.DataFrame:
    required = {"high", "low", "close"}
    columns = {str(column).lower(): column for column in frame.columns}
    if not required.issubset(columns):
        raise ValueError("OHLC frame must contain high, low, and close")
    output = pd.DataFrame({key: pd.to_numeric(frame[value], errors="coerce") for key, value in columns.items() if key in required})
    output.index = pd.to_datetime(frame.index, errors="raise").tz_localize(None).normalize()
    output = output.loc[output.index <= as_of_session].sort_index()
    output = output.loc[output["high"].gt(0) & output["low"].gt(0) & output["close"].gt(0) & output["high"].ge(output["low"])]
    if output.empty or output.index[-1] != as_of_session:
        raise ValueError("finalized OHLC data is unavailable for requested as-of session")
    if output.index.has_duplicates:
        raise ValueError("OHLC frame has duplicate sessions")
    return output


def har_parkinson_shadow(frame: pd.DataFrame, *, as_of_session: date | str | pd.Timestamp, horizons: tuple[int, ...] = (21, 31)) -> dict[str, Any]:
    """Strictly causal per-asset HAR-Parkinson forecasts for post-close display."""
    as_of = pd.Timestamp(as_of_session).tz_localize(None).normalize()
    ohlc = _canonical_ohlc(frame, as_of)
    variance = np.square(np.log(ohlc["high"] / ohlc["low"])) / (4.0 * log(2.0))
    features = pd.DataFrame(index=ohlc.index)
    for lookback in (1, 5, 22):
        rv = np.sqrt(variance.rolling(lookback, min_periods=lookback).sum())
        features[f"log_rv_{lookback}"] = np.log(rv.where(rv.gt(0)))
    records: dict[str, Any] = {}
    for horizon in horizons:
        # At a post-close t forecast, each fitted target ends no later than t. The
        # last usable feature session is t-horizon, so no future bar leaks in.
        target = np.log(np.sqrt(variance.rolling(horizon, min_periods=horizon).sum().shift(-horizon)))
        train = pd.concat([features, target.rename("target")], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
        # Index arithmetic alone cannot express trading sessions; target availability
        # (the canonical frame ends at as_of) guarantees the forward horizon completed
        # before the post-close forecast is made.
        train = train.loc[train.index < as_of].tail(HAR_MAX_TRAIN)
        point = features.loc[as_of]
        if len(train) < HAR_MIN_TRAIN or not np.isfinite(point.to_numpy(dtype="float64")).all():
            records[str(horizon)] = {"status": "unavailable", "reason": "insufficient_causal_training_history", "training_labels": int(len(train))}
            continue
        x = np.column_stack([np.ones(len(train)), train.iloc[:, :3].to_numpy(dtype="float64")])
        coefficients = np.linalg.lstsq(x, train["target"].to_numpy(dtype="float64"), rcond=None)[0]
        predicted_log = float(np.r_[1.0, point.to_numpy(dtype="float64")] @ coefficients)
        records[str(horizon)] = {
            "status": "available",
            "forecast_log_total_volatility": predicted_log,
            "forecast_total_volatility": float(np.exp(predicted_log)),
            "training_labels": int(len(train)),
            "coefficients": [float(value) for value in coefficients],
        }
    source_hash = _hash({"as_of": str(as_of.date()), "ohlc": ohlc.loc[:, ["high", "low", "close"]].round(12).to_dict(orient="split")})
    return {
        "model_version": HAR_SHADOW_VERSION,
        "as_of_session": str(as_of.date()),
        "source_hash": source_hash,
        "feature_values": {key: float(value) if np.isfinite(value) else None for key, value in features.loc[as_of].items()},
        "forecasts": records,
        "model_hash": _hash({"version": HAR_SHADOW_VERSION, "minimum_train": HAR_MIN_TRAIN, "maximum_train": HAR_MAX_TRAIN, "lookbacks": [1, 5, 22]}),
    }
