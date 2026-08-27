#!/usr/bin/env python3
"""Import a private portfolio snapshot into Risk Sizer's SQLite database.

The input JSON stays local and is gitignored. Values are market values in ILS;
``currency`` describes the instrument's quote/exposure currency for FX-adjusted
returns. Active rows without an ``atr`` receive a live Wilder ATR(14) from Yahoo.
Imported Active holdings default to ``legacy=true`` because this script is intended
for grandfathering a pre-existing book, not approving a new trade.
"""
from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping, Sequence

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402


def load_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Load and minimally validate a private Core/Active JSON snapshot."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read portfolio snapshot {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Portfolio snapshot must be a JSON object")
    snapshot: dict[str, list[dict[str, Any]]] = {}
    for sleeve in ("core", "active"):
        rows = payload.get(sleeve, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError(f"'{sleeve}' must be an array of objects")
        snapshot[sleeve] = rows
    if not snapshot["core"] and not snapshot["active"]:
        raise RuntimeError("Portfolio snapshot contains no holdings")
    return snapshot


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = float(
        true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    )
    if not np.isfinite(atr) or atr <= 0:
        raise RuntimeError("ATR is missing or non-positive")
    return atr


def fetch_active_atr(
    active_holdings: Sequence[Mapping[str, Any]],
    downloader: Callable[..., pd.DataFrame] = yf.download,
) -> dict[str, float]:
    """Return provided ATRs and download only symbols whose ATR is missing."""
    atr_by_ticker: dict[str, float] = {}
    missing: list[str] = []
    for holding in active_holdings:
        ticker = str(holding.get("ticker", "")).strip().upper()
        if not ticker:
            raise RuntimeError("Every Active holding requires a ticker")
        supplied = holding.get("atr")
        if supplied is not None and np.isfinite(float(supplied)) and float(supplied) > 0:
            atr_by_ticker[ticker] = float(supplied)
        else:
            missing.append(ticker)
    if not missing:
        return atr_by_ticker

    history = downloader(
        missing,
        period="1y",
        interval="1d",
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    if history is None or history.empty:
        raise RuntimeError("Yahoo returned no history; portfolio was not seeded")
    for ticker in missing:
        try:
            frame = history[ticker][["High", "Low", "Close"]].dropna()
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"{ticker}: Yahoo history is unavailable") from exc
        if len(frame) < 60:
            raise RuntimeError(
                f"{ticker}: only {len(frame)} daily bars are available; at least 60 are required"
            )
        atr_by_ticker[ticker] = wilder_atr(
            frame["High"], frame["Low"], frame["Close"]
        )
    return atr_by_ticker


def seed_database(
    database: Database,
    snapshot: Mapping[str, Sequence[Mapping[str, Any]]],
    atr_by_ticker: Mapping[str, float],
    *,
    opened_at: str,
    replace: bool = False,
) -> dict[str, int]:
    """Create or update snapshot holdings using the Database repository methods."""
    core_holdings = snapshot.get("core", [])
    active_holdings = snapshot.get("active", [])
    missing_atr = [
        ticker
        for holding in active_holdings
        for ticker in [str(holding.get("ticker", "")).strip().upper()]
        if ticker not in atr_by_ticker
        or not np.isfinite(atr_by_ticker[ticker])
        or atr_by_ticker[ticker] <= 0
    ]
    if missing_atr:
        raise RuntimeError(
            f"Missing valid ATR for {', '.join(missing_atr)}; portfolio was not seeded"
        )

    database.initialize()
    counts = {"core_created": 0, "core_updated": 0, "active_created": 0, "active_updated": 0}
    if replace:
        for row in database.list_active():
            database.delete_active(row["id"])
        for row in database.list_core():
            database.delete_core(row["id"])

    existing_core = {
        (row["ticker"], row["currency"]): row for row in database.list_core()
    }
    for source in core_holdings:
        ticker = str(source["ticker"]).strip().upper()
        currency = str(source.get("currency", "AUTO")).strip().upper()
        values = {
            "ticker": ticker,
            "value_ils": float(source["value_ils"]),
            "currency": currency,
            "fx_ticker": source.get("fx_ticker"),
            "display_name": source.get("display_name") or ticker,
        }
        current = existing_core.get((ticker, currency))
        if current:
            database.update_core(current["id"], values)
            counts["core_updated"] += 1
        else:
            database.create_core(values)
            counts["core_created"] += 1

    active_rows: dict[str, list[dict[str, Any]]] = {}
    for row in database.list_active():
        active_rows.setdefault(row["ticker"], []).append(row)
    for source in active_holdings:
        ticker = str(source["ticker"]).strip().upper()
        values = {
            "ticker": ticker,
            "entry_price": float(source["entry_price"]),
            "atr": float(atr_by_ticker[ticker]),
            "quantity": float(source["quantity"]),
            "value_ils": float(source["value_ils"]),
            "currency": str(source.get("currency", "AUTO")).strip().upper(),
            "fx_to_ils": float(source.get("fx_to_ils", 1)),
            "fx_ticker": source.get("fx_ticker"),
            "display_name": source.get("display_name") or ticker,
            "legacy": bool(source.get("legacy", True)),
            "risk_status": str(source.get("risk_status", "RISK_ON")).strip().upper(),
            "rung": int(source.get("rung", 0)),
            "opened_at": str(source.get("opened_at") or opened_at),
        }
        matches = active_rows.get(ticker, [])
        if len(matches) > 1:
            raise RuntimeError(f"{ticker}: multiple Active rows exist; resolve duplicates first")
        if matches:
            database.update_active(matches[0]["id"], values)
            counts["active_updated"] += 1
        else:
            database.create_active(values)
            counts["active_created"] += 1
    return counts


def print_snapshot(
    snapshot: Mapping[str, Sequence[Mapping[str, Any]]],
    atr_by_ticker: Mapping[str, float],
) -> None:
    for sleeve in ("core", "active"):
        print(f"{sleeve.title()} holdings: {len(snapshot.get(sleeve, []))}")
    total = sum(
        float(row["value_ils"])
        for sleeve in snapshot.values()
        for row in sleeve
    )
    print(f"Total invested market value: ILS {total:,.2f}")
    if atr_by_ticker:
        print(f"Active ATR values available: {len(atr_by_ticker)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", type=Path, required=True,
        help="private JSON snapshot; keep this file outside source control",
    )
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "risk_sizer.db")
    parser.add_argument(
        "--opened-at", default=date.today().isoformat(),
        help="migration date used when an Active row has no opened_at",
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="delete existing Core and Active rows before inserting this snapshot",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    snapshot = load_snapshot(args.input)
    atr_by_ticker = fetch_active_atr(snapshot["active"])
    print_snapshot(snapshot, atr_by_ticker)
    if args.dry_run:
        print("Dry run: database was not changed")
        return
    database = Database(args.db)
    if args.replace and args.db.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = args.db.with_name(f"risk_sizer.pre-seed-{stamp}.db")
        database.backup_to(backup)
        print(f"Safety backup: {backup.resolve()}")
    counts = seed_database(
        database, snapshot, atr_by_ticker,
        opened_at=args.opened_at, replace=args.replace,
    )
    print(f"Seeded {args.db.resolve()}: {counts}")


if __name__ == "__main__":
    main()
