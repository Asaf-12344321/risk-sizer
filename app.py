"""FastAPI host for the VM-deployable Risk Sizer application."""
from __future__ import annotations

import hmac
import json
import math
import os
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import pandas as pd
import yfinance as yf
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from database import DEFAULT_SETTINGS, Database
from quant_risk_engine import QuantitativeRiskEngine
from stop_engine import build_policy_snapshot, har_parkinson_shadow, replay_post_close_stop


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


class PostCloseRequest(BaseModel):
    """The caller supplies the finalized exchange session, never an intraday timestamp."""

    as_of_session: date


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=16, max_length=1024)
    auth: str = Field(min_length=8, max_length=1024)


class PushSubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=12, max_length=4096)
    keys: PushSubscriptionKeys

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("push endpoint must be a secure HTTPS URL")
        return value


class PushSubscriptionDelete(BaseModel):
    endpoint: str = Field(min_length=12, max_length=4096)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("push endpoint must be a secure HTTPS URL")
        return value


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


def _push_config() -> dict[str, str] | None:
    """Return only a complete, explicitly configured Web Push identity.

    The private key is never sent to a browser.  Leaving any setting absent simply
    leaves the display control disabled; it must never make the stop engine fail.
    """
    private_key = os.environ.get("RISK_SIZER_VAPID_PRIVATE_KEY", "").strip()
    public_key = os.environ.get("RISK_SIZER_VAPID_PUBLIC_KEY", "").strip()
    subject = os.environ.get("RISK_SIZER_VAPID_SUBJECT", "").strip()
    if not all((private_key, public_key, subject)):
        return None
    return {"private_key": private_key, "public_key": public_key, "subject": subject}


def _deliver_web_push(payload: dict[str, Any]) -> dict[str, int]:
    """Deliver a visible push to each registered device, never affecting stops.

    A browser can invalidate an endpoint when the app is removed.  404/410 are
    expected lifecycle events, so remove only that dead endpoint and keep all other
    subscriptions.  Other delivery failures are counted for diagnostics but cannot
    make a finalized stop replay fail.
    """
    config = _push_config()
    if config is None:
        return {"sent": 0, "expired": 0, "failed": 0}
    from pywebpush import WebPushException, webpush

    summary = {"sent": 0, "expired": 0, "failed": 0}
    for subscription in db.list_push_subscriptions():
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(payload, separators=(",", ":")),
                vapid_private_key=config["private_key"],
                vapid_claims={"sub": config["subject"]},
                ttl=60 * 60 * 12,
                timeout=10,
            )
            summary["sent"] += 1
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) in {404, 410}:
                db.delete_push_subscription(subscription["endpoint"])
                summary["expired"] += 1
            else:
                summary["failed"] += 1
        except Exception:
            # A notification provider outage is not permission to alter a broker stop
            # or to fail the scheduled post-close workflow.
            summary["failed"] += 1
    return summary


def _send_stop_move_alert(position: dict[str, Any], stop: dict[str, Any]) -> dict[str, int]:
    if not stop.get("actionable_alert_needed"):
        return {"sent": 0, "expired": 0, "failed": 0}
    name = position.get("display_name") or position["ticker"]
    stop_price = float(stop["current_stop_price"])
    return _deliver_web_push({
        "title": "Risk Sizer",
        "body": f"{name}: move stop to {stop_price:.2f}",
        "tag": f"risk-sizer-stop-{position['id']}",
        "url": "/",
    })

db = Database(DATABASE_PATH)
db.initialize()
market_data = CachedDownloader()
app = FastAPI(title="Risk Sizer", version="6.0")


def _daily_ohlc(ticker: str, as_of_session: date) -> pd.DataFrame:
    """Return final adjusted daily OHLC through the requested session or fail closed."""
    raw = market_data(
        ticker, period="6y", interval="1d", auto_adjust=True, progress=False,
        group_by="ticker", threads=False,
    )
    if raw is None or raw.empty:
        raise RuntimeError(f"{ticker}: no daily OHLC returned")
    if isinstance(raw.columns, pd.MultiIndex):
        if ticker in raw.columns.get_level_values(0):
            raw = raw[ticker]
        else:
            raw = raw.xs(ticker, axis=1, level=-1)
    columns = {str(column).lower(): column for column in raw.columns}
    required = {"open", "high", "low", "close"}
    if not required.issubset(columns):
        raise RuntimeError(f"{ticker}: daily OHLC is incomplete")
    output = pd.DataFrame({name: pd.to_numeric(raw[column], errors="coerce") for name, column in columns.items() if name in required})
    output.index = pd.to_datetime(output.index, errors="raise").tz_localize(None).normalize()
    output = output.sort_index().loc[lambda frame: frame.index <= pd.Timestamp(as_of_session)]
    if output.empty or output.index[-1] != pd.Timestamp(as_of_session):
        raise RuntimeError(f"{ticker}: requested session {as_of_session.isoformat()} is not finalized")
    return output


def _snapshot_missing_positions() -> None:
    """One-time explicit migration: freeze today’s persisted policy for legacy rows."""
    settings = db.get_settings()["settings"]
    for position in db.list_active():
        if position.get("policy_snapshot") is None:
            db.set_active_policy_snapshot(position["id"], build_policy_snapshot(position["atr"], settings))


def _replay_post_close(position: dict[str, Any], as_of_session: date) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist one position's finalized EOD stop and reference-only HAR shadow record."""
    ohlc = _daily_ohlc(position["ticker"], as_of_session)
    held = ohlc.loc[ohlc.index >= pd.Timestamp(position["opened_at"])]
    if held.empty:
        raise RuntimeError("no finalized bars are available after the entry date")
    bars = [
        {"session": str(session.date()), "open": float(row.open), "high": float(row.high),
         "low": float(row.low), "close": float(row.close)}
        for session, row in held.iterrows()
    ]
    previous = db.latest_stop_update(position["id"])
    previous_stop = previous["payload"]["current_stop_price"] if previous else None
    stop = replay_post_close_stop(
        entry_price=float(position["entry_price"]), policy_snapshot=position["policy_snapshot"],
        bars=bars, previous_confirmed_stop_price=previous_stop,
    )
    shadow = har_parkinson_shadow(ohlc, as_of_session=as_of_session)
    db.append_stop_update(position["id"], stop, shadow["source_hash"])
    db.append_har_shadow_record(position["id"], shadow)
    return stop, shadow

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


@app.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest() -> FileResponse:
    return FileResponse(ROOT / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/service-worker.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(ROOT / "service-worker.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/risk-sizer-icon.svg", include_in_schema=False)
def app_icon() -> FileResponse:
    return FileResponse(ROOT / "risk-sizer-icon.svg", media_type="image/svg+xml")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "storage": "sqlite"}


@app.get("/api/notifications/config")
def notification_config(_: Protected) -> dict[str, Any]:
    config = _push_config()
    return {"enabled": config is not None, "public_key": config["public_key"] if config else None}


@app.post("/api/notifications/subscribe", status_code=201)
def subscribe_notifications(request: PushSubscriptionRequest, _: Protected) -> dict[str, bool]:
    if _push_config() is None:
        raise HTTPException(status_code=503, detail="Stop alerts are not configured yet")
    db.upsert_push_subscription(request.model_dump())
    return {"subscribed": True}


@app.delete("/api/notifications/subscribe", status_code=204)
def unsubscribe_notifications(request: PushSubscriptionDelete, _: Protected) -> Response:
    db.delete_push_subscription(request.endpoint)
    return Response(status_code=204)


@app.post("/api/notifications/test")
def test_notification(_: Protected) -> dict[str, int]:
    if _push_config() is None:
        raise HTTPException(status_code=503, detail="Stop alerts are not configured yet")
    if not db.list_push_subscriptions():
        raise HTTPException(status_code=409, detail="Enable stop alerts on this phone first")
    return _deliver_web_push({
        "title": "Risk Sizer",
        "body": "Alerts are on. You will be notified when a stop needs to move.",
        "tag": "risk-sizer-test",
        "url": "/",
    })


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
    _snapshot_missing_positions()
    return db.list_active()


@app.post("/api/positions", status_code=201)
def create_position(request: ActiveCreate, _: Protected) -> dict[str, Any]:
    values = request.model_dump()
    values["opened_at"] = values["opened_at"].isoformat()
    # The browser does not send a policy. The server freezes its current persisted
    # settings together with the entered ATR, so a later Settings edit cannot rewrite
    # this position's live stop rule.
    values["policy_snapshot"] = build_policy_snapshot(values["atr"], db.get_settings()["settings"])
    try:
        return db.create_active(values)
    except sqlite3.IntegrityError as exc:
        raise _conflict(exc) from exc


@app.put("/api/positions/{item_id}")
def update_position(item_id: int, request: ActiveUpdate, _: Protected) -> dict[str, Any]:
    values = request.model_dump(exclude_unset=True)
    if {"entry_price", "atr"}.intersection(values):
        raise HTTPException(status_code=409, detail="entry price and entry ATR are immutable after position creation")
    if isinstance(values.get("opened_at"), date):
        values["opened_at"] = values["opened_at"].isoformat()
    try:
        item = db.update_active(item_id, values)
    except sqlite3.IntegrityError as exc:
        raise _conflict(exc) from exc
    if item is None:
        raise HTTPException(status_code=404, detail="Active position not found")
    return item


@app.get("/api/positions/shadow")
def list_position_shadows(_: Protected) -> dict[str, Any]:
    """Reference-only records for the display card; they never reach a risk gate."""
    return {str(position_id): record for position_id, record in db.latest_har_shadow_records().items()}


@app.get("/api/positions/stops")
def list_position_stops(_: Protected) -> dict[str, Any]:
    """Latest frozen-policy EOD replay for comparison with the browser tracker."""
    return {str(position["id"]): record["payload"] for position in db.list_active()
            if (record := db.latest_stop_update(position["id"])) is not None}


@app.post("/api/positions/post-close")
def process_post_close_positions(request: PostCloseRequest, _: Protected) -> dict[str, Any]:
    """Idempotently replay every open position after a finalized exchange session.

    A non-trading session is reported as ``not_finalized`` rather than silently using a
    prior close.  The scheduled caller can therefore distinguish a market holiday from
    a genuine provider or data error while the stop engine always fails closed.
    """
    _snapshot_missing_positions()
    processed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    notifications = {"sent": 0, "expired": 0, "failed": 0}
    for position in db.list_active():
        try:
            stop, _ = _replay_post_close(position, request.as_of_session)
            delivery = _send_stop_move_alert(position, stop)
            for key, value in delivery.items():
                notifications[key] += value
            processed.append({
                "position_id": position["id"], "ticker": position["ticker"],
                "current_stop_price": stop["current_stop_price"],
                "actionable_alert_needed": stop["actionable_alert_needed"],
            })
        except (RuntimeError, ValueError) as exc:
            message = str(exc)
            failures.append({
                "position_id": position["id"], "ticker": position["ticker"],
                "code": "not_finalized" if "is not finalized" in message else "replay_error",
                "detail": message,
            })
    return {
        "as_of_session": request.as_of_session.isoformat(),
        "processed": processed,
        "failures": failures,
        "notifications": notifications,
        "sizing_or_var_inputs_changed": False,
    }


@app.post("/api/positions/{item_id}/post-close")
def process_post_close_position(item_id: int, request: PostCloseRequest, _: Protected) -> dict[str, Any]:
    """Run one finalized-session replay plus isolated shadow volatility calculation."""
    _snapshot_missing_positions()
    position = db.get_active(item_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Active position not found")
    try:
        stop, shadow = _replay_post_close(position, request.as_of_session)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "position_id": item_id,
        "stop_update": stop,
        "har_parkinson_shadow": shadow,
        "shadow_reference_only": True,
        "sizing_or_var_inputs_changed": False,
    }


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
