"""FastAPI host for the VM-deployable Risk Sizer application."""
from __future__ import annotations

import hmac
import math
import os
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
import yfinance as yf
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from database import DEFAULT_SETTINGS, Database
from quant_risk_engine import QuantitativeRiskEngine


ROOT = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.environ.get("RISK_SIZER_DB_PATH", ROOT / "data" / "risk_sizer.db"))


def _normalise_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError("ticker cannot be blank")
    return symbol


def _normalise_currency(value: str) -> str:
    currency = value.strip().upper()
    if currency == "NIS":
        currency = "ILS"
    if currency not in {"ILS", "USD", "AUTO"}:
        raise ValueError("currency must be ILS, USD, or AUTO")
    return currency


class CoreCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    ticker: str = Field(min_length=1, max_length=32)
    value_ils: float = Field(gt=0)
    currency: str = "AUTO"
    fx_ticker: str | None = None
    display_name: str | None = Field(default=None, max_length=120)

    _ticker = field_validator("ticker")(_normalise_symbol)
    _currency = field_validator("currency")(_normalise_currency)

    @field_validator("fx_ticker")
    @classmethod
    def normalise_fx_ticker(cls, value: str | None) -> str | None:
        return _normalise_symbol(value) if value else None


class CoreUpdate(BaseModel):
    ticker: str | None = Field(default=None, min_length=1, max_length=32)
    value_ils: float | None = Field(default=None, gt=0)
    currency: str | None = None
    fx_ticker: str | None = None
    display_name: str | None = Field(default=None, max_length=120)

    @field_validator("ticker")
    @classmethod
    def normalise_ticker(cls, value: str | None) -> str | None:
        return _normalise_symbol(value) if value is not None else None

    @field_validator("currency")
    @classmethod
    def normalise_currency(cls, value: str | None) -> str | None:
        return _normalise_currency(value) if value is not None else None

    @field_validator("fx_ticker")
    @classmethod
    def normalise_fx_ticker(cls, value: str | None) -> str | None:
        return _normalise_symbol(value) if value else None


class ActiveCreate(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)
    entry_price: float = Field(gt=0)
    atr: float = Field(gt=0)
    quantity: float = Field(gt=0)
    value_ils: float = Field(gt=0)
    currency: str = "AUTO"
    fx_to_ils: float = Field(default=1, gt=0)
    fx_ticker: str | None = None
    display_name: str | None = Field(default=None, max_length=120)
    legacy: bool = False
    risk_status: str = "RISK_ON"
    rung: int = Field(default=0, ge=0)
    opened_at: date = Field(default_factory=date.today)

    _ticker = field_validator("ticker")(_normalise_symbol)
    _currency = field_validator("currency")(_normalise_currency)

    @field_validator("fx_ticker")
    @classmethod
    def normalise_fx_ticker(cls, value: str | None) -> str | None:
        return _normalise_symbol(value) if value else None

    @field_validator("risk_status")
    @classmethod
    def normalise_risk_status(cls, value: str) -> str:
        status_value = value.strip().upper()
        if status_value not in {"RISK_ON", "ARMED_ZERO_RISK"}:
            raise ValueError("risk_status must be RISK_ON or ARMED_ZERO_RISK")
        return status_value


class ActiveUpdate(BaseModel):
    ticker: str | None = Field(default=None, min_length=1, max_length=32)
    entry_price: float | None = Field(default=None, gt=0)
    atr: float | None = Field(default=None, gt=0)
    quantity: float | None = Field(default=None, gt=0)
    value_ils: float | None = Field(default=None, gt=0)
    currency: str | None = None
    fx_to_ils: float | None = Field(default=None, gt=0)
    fx_ticker: str | None = None
    display_name: str | None = Field(default=None, max_length=120)
    legacy: bool | None = None
    risk_status: str | None = None
    rung: int | None = Field(default=None, ge=0)
    opened_at: date | None = None

    @field_validator("ticker")
    @classmethod
    def normalise_ticker(cls, value: str | None) -> str | None:
        return _normalise_symbol(value) if value is not None else None

    @field_validator("currency")
    @classmethod
    def normalise_currency(cls, value: str | None) -> str | None:
        return _normalise_currency(value) if value is not None else None

    @field_validator("fx_ticker")
    @classmethod
    def normalise_fx_ticker(cls, value: str | None) -> str | None:
        return _normalise_symbol(value) if value else None

    @field_validator("risk_status")
    @classmethod
    def normalise_risk_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        status_value = value.strip().upper()
        if status_value not in {"RISK_ON", "ARMED_ZERO_RISK"}:
            raise ValueError("risk_status must be RISK_ON or ARMED_ZERO_RISK")
        return status_value


class SettingsUpdate(BaseModel):
    settings: dict[str, float] = Field(default_factory=dict)
    setup_complete: bool | None = None

    @field_validator("settings")
    @classmethod
    def validate_settings(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = set(value) - set(DEFAULT_SETTINGS)
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        if any(not math.isfinite(number) for number in value.values()):
            raise ValueError("settings must contain finite numbers")
        non_negative = {"capital", "riskabs", "riskpct", "breakbudget"}
        if any(value.get(key, 0) < 0 for key in non_negative):
            raise ValueError("capital and risk budgets cannot be negative")
        if "maxdailyvar" in value and value["maxdailyvar"] <= 0:
            raise ValueError("maxdailyvar must be greater than zero")
        if "maxriskonr" in value and value["maxriskonr"] < 1:
            raise ValueError("maxriskonr must be at least 1R")
        return value


class TradeEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_ticker: str = Field(min_length=1, max_length=32)
    proposed_entry_size_ils: float = Field(gt=0)
    new_ticker_currency: str = "AUTO"
    new_ticker_fx_ticker: str | None = None

    _ticker = field_validator("new_ticker")(_normalise_symbol)
    _currency = field_validator("new_ticker_currency")(_normalise_currency)


class CachedDownloader:
    """Process-local TTL cache so repeated UI checks reuse the same history."""

    def __init__(self, ttl_seconds: int = 900) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[tuple[Any, ...], tuple[float, pd.DataFrame]] = {}
        self._lock = threading.Lock()

    def __call__(self, tickers, **kwargs) -> pd.DataFrame:
        symbols = tuple(sorted(tickers if isinstance(tickers, (list, tuple)) else [tickers]))
        key = (symbols, tuple(sorted((key, str(value)) for key, value in kwargs.items())))
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] <= self.ttl_seconds:
                return cached[1].copy()
        result = yf.download(tickers, **kwargs)
        # Do not turn a transient provider failure into a 15-minute outage. Partial
        # batch frames are still useful because the risk engine retries absent symbols
        # individually; wholly empty responses should be attempted again next time.
        if result is not None and not result.empty:
            with self._lock:
                self._cache[key] = (now, result.copy())
        return result


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    expected = os.environ.get("RISK_SIZER_API_KEY", "")
    if not expected:
        if os.environ.get("RISK_SIZER_ENV", "development").lower() == "production":
            raise HTTPException(status_code=503, detail="RISK_SIZER_API_KEY is not configured")
        return
    if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid API key")


Protected = Annotated[None, Depends(require_api_key)]

db = Database(DATABASE_PATH)
db.initialize()
market_data = CachedDownloader()
app = FastAPI(title="Risk Sizer", version="6.0")

cors_origins = [origin.strip() for origin in os.environ.get(
    "RISK_SIZER_CORS_ORIGINS",
    "http://localhost,http://127.0.0.1,http://localhost:8000,http://127.0.0.1:8000",
).split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key"],
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "storage": "sqlite"}


def _conflict(exc: sqlite3.IntegrityError) -> HTTPException:
    return HTTPException(status_code=409, detail=f"Database constraint failed: {exc}")


@app.get("/api/core")
def list_core(_: Protected) -> list[dict[str, Any]]:
    return db.list_core()


@app.post("/api/core", status_code=201)
def create_core(request: CoreCreate, _: Protected) -> dict[str, Any]:
    try:
        return db.create_core(request.model_dump())
    except sqlite3.IntegrityError as exc:
        raise _conflict(exc) from exc


@app.put("/api/core/{item_id}")
def update_core(item_id: int, request: CoreUpdate, _: Protected) -> dict[str, Any]:
    try:
        item = db.update_core(item_id, request.model_dump(exclude_unset=True))
    except sqlite3.IntegrityError as exc:
        raise _conflict(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Core holding not found")
    return item


@app.delete("/api/core/{item_id}", status_code=204)
def delete_core(item_id: int, _: Protected) -> Response:
    if not db.delete_core(item_id):
        raise HTTPException(status_code=404, detail="Core holding not found")
    return Response(status_code=204)


@app.get("/api/positions")
def list_positions(_: Protected) -> list[dict[str, Any]]:
    return db.list_active()


@app.post("/api/positions", status_code=201)
def create_position(request: ActiveCreate, _: Protected) -> dict[str, Any]:
    values = request.model_dump()
    values["opened_at"] = values["opened_at"].isoformat()
    try:
        return db.create_active(values)
    except sqlite3.IntegrityError as exc:
        raise _conflict(exc) from exc


@app.put("/api/positions/{item_id}")
def update_position(item_id: int, request: ActiveUpdate, _: Protected) -> dict[str, Any]:
    values = request.model_dump(exclude_unset=True)
    if isinstance(values.get("opened_at"), date):
        values["opened_at"] = values["opened_at"].isoformat()
    try:
        item = db.update_active(item_id, values)
    except sqlite3.IntegrityError as exc:
        raise _conflict(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Active position not found")
    return item


@app.delete("/api/positions/{item_id}", status_code=204)
def delete_position(item_id: int, _: Protected) -> Response:
    if not db.delete_active(item_id):
        raise HTTPException(status_code=404, detail="Active position not found")
    return Response(status_code=204)


@app.get("/api/settings")
def get_settings(_: Protected) -> dict[str, Any]:
    return db.get_settings()


@app.put("/api/settings")
def update_settings(request: SettingsUpdate, _: Protected) -> dict[str, Any]:
    return db.update_settings(request.settings, request.setup_complete)


@app.post("/api/risk/evaluate")
async def evaluate_trade(request: TradeEvaluationRequest, _: Protected) -> dict[str, Any]:
    # Portfolio membership is server-owned: a client cannot omit a position to make
    # the risk gate look safer.
    combined = await run_in_threadpool(db.combined_portfolio)
    if not combined:
        raise HTTPException(
            status_code=422,
            detail="Add at least one Core or Active holding before running holistic risk",
        )
    settings_state, active_positions = await run_in_threadpool(
        lambda: (db.get_settings(), db.list_active())
    )
    settings = settings_state["settings"]
    max_risk_on = float(settings["maxriskonr"])
    risk_on_positions = [
        position for position in active_positions
        if position["risk_status"] == "RISK_ON" and not bool(position["legacy"])
    ]
    legacy_risk_on = [
        position for position in active_positions
        if position["risk_status"] == "RISK_ON" and bool(position["legacy"])
    ]
    current_heat = float(len(risk_on_positions))
    proposed_heat = current_heat + 1.0
    heat_breach = proposed_heat > max_risk_on

    engine = QuantitativeRiskEngine(
        current_positions=combined,
        new_ticker=request.new_ticker,
        proposed_entry_size_ils=request.proposed_entry_size_ils,
        max_daily_var_ils=float(settings["maxdailyvar"]),
        correlation_threshold=0.75,
        max_variance_increase_pct=0.20,
        block_on_warnings=True,
        new_ticker_currency=request.new_ticker_currency,
        new_ticker_fx_ticker=request.new_ticker_fx_ticker,
        downloader=market_data,
    )
    try:
        quant_approved, metrics = await run_in_threadpool(engine.evaluate_trade)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    metrics["portfolio_heat"] = {
        "current_r": current_heat,
        "proposed_r": proposed_heat,
        "max_r": max_risk_on,
        "remaining_r": max(0.0, max_risk_on - current_heat),
        "budget_breach": heat_breach,
        "legacy_risk_on_excluded": len(legacy_risk_on),
    }
    if heat_breach:
        metrics["warnings"].append(
            f"Portfolio Heat REJECTION: opening this trade would raise Risk-On heat "
            f"to {proposed_heat:g}R, above the {max_risk_on:g}R limit."
        )
    approved = quant_approved and not heat_breach
    core_count = len(await run_in_threadpool(db.list_core))
    active_count = len(active_positions)
    return {
        "is_trade_approved": approved,
        "risk_metrics": metrics,
        "portfolio": {
            "core_positions": core_count,
            "active_positions": active_count,
            "combined_instruments": len(combined),
        },
    }
