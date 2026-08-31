"""SQLite persistence for Risk Sizer.

Every method opens a short-lived connection.  This is a good fit for FastAPI's
thread-pool execution model and avoids sharing sqlite3 connections across threads.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping


DEFAULT_SETTINGS: dict[str, float] = {
    "capital": 0,
    "riskpct": 2,
    "riskabs": 0,
    "maxdailyvar": 25_000,
    "maxriskonr": 5,
    "breakbudget": 0,
    "maxpct": 20,
    "betaexpct": 25,
    "gapt1": 22,
    "gapt2": 25,
    "gapt3": 34,
    "gapt4": 44,
    "gapt5": 51,
    "gapt6": 56,
    "gapt7": 60,
    "gapt8": 63,
    "liqpct": 5,
    "initmult": 2.5,
    "trailmult": 3.5,
    "armpct": 15,
    "armatrmult": 3.0,
    "minstop": 8,
    "maxstop": 30,
    "drawdown": 40,
    "holddays": 90,
    "fx": 3.65,
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    """Small repository layer around a single SQLite database file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS core_portfolio (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL CHECK(length(trim(ticker)) > 0),
                    value_ils REAL NOT NULL CHECK(value_ils > 0),
                    currency TEXT NOT NULL DEFAULT 'AUTO',
                    fx_ticker TEXT,
                    display_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(ticker, currency)
                );

                CREATE TABLE IF NOT EXISTS active_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL CHECK(length(trim(ticker)) > 0),
                    entry_price REAL NOT NULL CHECK(entry_price > 0),
                    atr REAL NOT NULL CHECK(atr > 0),
                    quantity REAL NOT NULL CHECK(quantity > 0),
                    value_ils REAL NOT NULL CHECK(value_ils > 0),
                    currency TEXT NOT NULL DEFAULT 'AUTO',
                    fx_to_ils REAL NOT NULL DEFAULT 1 CHECK(fx_to_ils > 0),
                    fx_ticker TEXT,
                    display_name TEXT,
                    legacy INTEGER NOT NULL DEFAULT 0 CHECK(legacy IN (0, 1)),
                    risk_status TEXT NOT NULL DEFAULT 'RISK_ON'
                        CHECK(risk_status IN ('RISK_ON', 'ARMED_ZERO_RISK')),
                    rung INTEGER NOT NULL DEFAULT 0 CHECK(rung >= 0),
                    policy_snapshot TEXT,
                    opened_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    settings_json TEXT NOT NULL,
                    setup_complete INTEGER NOT NULL DEFAULT 0
                        CHECK(setup_complete IN (0, 1)),
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS post_close_stop_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id INTEGER NOT NULL,
                    as_of_session TEXT NOT NULL,
                    engine_version TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(position_id, as_of_session, engine_version)
                );

                CREATE TABLE IF NOT EXISTS har_parkinson_shadow_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    position_id INTEGER NOT NULL,
                    as_of_session TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    model_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(position_id, as_of_session, model_version)
                );

                -- A subscription is an opaque browser-issued endpoint plus public
                -- encryption material. It grants delivery only; it cannot read or
                -- change any Risk Sizer data.
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    endpoint TEXT PRIMARY KEY,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Existing VM databases predate friendly names and grandfathering.
            # SQLite's additive migration keeps every holding and is safe to rerun.
            core_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(core_portfolio)")
            }
            if "display_name" not in core_columns:
                connection.execute("ALTER TABLE core_portfolio ADD COLUMN display_name TEXT")
            active_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(active_positions)")
            }
            if "display_name" not in active_columns:
                connection.execute("ALTER TABLE active_positions ADD COLUMN display_name TEXT")
            if "legacy" not in active_columns:
                connection.execute(
                    "ALTER TABLE active_positions ADD COLUMN legacy INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(legacy IN (0, 1))"
                )
            if "policy_snapshot" not in active_columns:
                connection.execute("ALTER TABLE active_positions ADD COLUMN policy_snapshot TEXT")
            connection.execute(
                """INSERT OR IGNORE INTO app_settings
                   (id, settings_json, setup_complete, updated_at)
                   VALUES (1, ?, 0, ?)""",
                (json.dumps(DEFAULT_SETTINGS, sort_keys=True), _now()),
            )

    @staticmethod
    def _rows(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        output = [dict(row) for row in rows]
        for row in output:
            if "policy_snapshot" in row and row["policy_snapshot"]:
                row["policy_snapshot"] = json.loads(row["policy_snapshot"])
        return output

    def list_core(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM core_portfolio ORDER BY ticker, id"
            ).fetchall()
        return self._rows(rows)

    def create_core(self, values: Mapping[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO core_portfolio
                   (ticker, value_ils, currency, fx_ticker, display_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    values["ticker"], values["value_ils"], values["currency"],
                    values.get("fx_ticker"), values.get("display_name"), now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM core_portfolio WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._rows([row])[0]

    def update_core(self, item_id: int, values: Mapping[str, Any]) -> dict[str, Any] | None:
        fields = ("ticker", "value_ils", "currency", "fx_ticker", "display_name")
        return self._update("core_portfolio", item_id, values, fields)

    def delete_core(self, item_id: int) -> bool:
        return self._delete("core_portfolio", item_id)

    def list_active(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM active_positions ORDER BY opened_at DESC, id DESC"
            ).fetchall()
        return self._rows(rows)

    def get_active(self, item_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM active_positions WHERE id = ?", (item_id,)).fetchone()
        return self._rows([row])[0] if row is not None else None

    def set_active_policy_snapshot(self, item_id: int, snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
        return self._update("active_positions", item_id, {"policy_snapshot": json.dumps(snapshot, sort_keys=True)}, ("policy_snapshot",))

    def create_active(self, values: Mapping[str, Any]) -> dict[str, Any]:
        # API creation always supplies a server-captured snapshot.  This fallback
        # preserves the repository's direct seeding/test API while still freezing the
        # default policy at creation rather than reading it at replay time.
        snapshot = values.get("policy_snapshot")
        if snapshot is None:
            from stop_engine import build_policy_snapshot
            snapshot = build_policy_snapshot(float(values["atr"]), DEFAULT_SETTINGS)
        now = _now()
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO active_positions
                   (ticker, entry_price, atr, quantity, value_ils, currency,
                    fx_to_ils, fx_ticker, display_name, legacy, risk_status, rung, policy_snapshot,
                    opened_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    values["ticker"], values["entry_price"], values["atr"],
                    values["quantity"], values["value_ils"], values["currency"],
                    values.get("fx_to_ils", 1), values.get("fx_ticker"),
                    values.get("display_name"), int(bool(values.get("legacy", False))),
                    values.get("risk_status", "RISK_ON"), values.get("rung", 0),
                    json.dumps(snapshot, sort_keys=True),
                    values["opened_at"], now, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM active_positions WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._rows([row])[0]

    def update_active(self, item_id: int, values: Mapping[str, Any]) -> dict[str, Any] | None:
        fields = (
            "ticker", "quantity", "value_ils", "currency",
            "fx_to_ils", "fx_ticker", "display_name", "legacy", "risk_status", "rung",
            "opened_at",
        )
        return self._update("active_positions", item_id, values, fields)

    def latest_stop_update(self, position_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM post_close_stop_updates WHERE position_id = ?
                   ORDER BY as_of_session DESC, id DESC LIMIT 1""",
                (position_id,),
            ).fetchone()
        if row is None:
            return None
        output = dict(row)
        output["payload"] = json.loads(output.pop("payload_json"))
        return output

    def append_stop_update(self, position_id: int, payload: Mapping[str, Any], source_hash: str) -> dict[str, Any]:
        now = _now()
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO post_close_stop_updates
                   (position_id, as_of_session, engine_version, source_hash, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (position_id, payload["as_of_session"], payload["engine_version"], source_hash,
                 json.dumps(payload, sort_keys=True), now),
            )
        return self.latest_stop_update(position_id) or {}

    def append_har_shadow_record(self, position_id: int, payload: Mapping[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO har_parkinson_shadow_records
                   (position_id, as_of_session, model_version, source_hash, model_hash, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (position_id, payload["as_of_session"], payload["model_version"], payload["source_hash"],
                 payload["model_hash"], json.dumps(payload, sort_keys=True), _now()),
            )

    def latest_har_shadow_records(self) -> dict[int, dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT record.position_id, record.payload_json FROM har_parkinson_shadow_records AS record
                   INNER JOIN (
                       SELECT position_id, MAX(id) AS id
                       FROM har_parkinson_shadow_records GROUP BY position_id
                   ) AS latest ON latest.id = record.id"""
            ).fetchall()
        return {int(row["position_id"]): json.loads(row["payload_json"]) for row in rows}

    def list_push_subscriptions(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT endpoint, p256dh, auth FROM push_subscriptions ORDER BY created_at"
            ).fetchall()
        return [
            {"endpoint": row["endpoint"], "keys": {"p256dh": row["p256dh"], "auth": row["auth"]}}
            for row in rows
        ]

    def upsert_push_subscription(self, subscription: Mapping[str, Any]) -> None:
        now = _now()
        keys = subscription["keys"]
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(endpoint) DO UPDATE SET
                       p256dh = excluded.p256dh, auth = excluded.auth, updated_at = excluded.updated_at""",
                (subscription["endpoint"], keys["p256dh"], keys["auth"], now, now),
            )

    def delete_push_subscription(self, endpoint: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        return cursor.rowcount > 0

    def delete_active(self, item_id: int) -> bool:
        return self._delete("active_positions", item_id)

    def _update(
        self, table: str, item_id: int, values: Mapping[str, Any], fields: tuple[str, ...]
    ) -> dict[str, Any] | None:
        changes = {key: values[key] for key in fields if key in values}
        if not changes:
            with self.connect() as connection:
                row = connection.execute(
                    f"SELECT * FROM {table} WHERE id = ?", (item_id,)
                ).fetchone()
            return self._rows([row])[0] if row else None
        changes["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in changes)
        parameters = [*changes.values(), item_id]
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE {table} SET {assignments} WHERE id = ?", parameters
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                f"SELECT * FROM {table} WHERE id = ?", (item_id,)
            ).fetchone()
        return self._rows([row])[0]

    def _delete(self, table: str, item_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(f"DELETE FROM {table} WHERE id = ?", (item_id,))
        return cursor.rowcount > 0

    def get_settings(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT settings_json, setup_complete, updated_at FROM app_settings WHERE id = 1"
            ).fetchone()
        stored = json.loads(row["settings_json"])
        settings = dict(DEFAULT_SETTINGS)
        settings.update(
            {key: value for key, value in stored.items() if key in DEFAULT_SETTINGS}
        )
        return {
            "settings": settings,
            "setup_complete": bool(row["setup_complete"]),
            "updated_at": row["updated_at"],
        }

    def update_settings(
        self, settings: Mapping[str, float], setup_complete: bool | None = None
    ) -> dict[str, Any]:
        current = self.get_settings()
        merged = current["settings"]
        merged.update(settings)
        complete = current["setup_complete"] if setup_complete is None else setup_complete
        with self.connect() as connection:
            connection.execute(
                """UPDATE app_settings
                   SET settings_json = ?, setup_complete = ?, updated_at = ?
                   WHERE id = 1""",
                (json.dumps(merged, sort_keys=True), int(complete), _now()),
            )
        return self.get_settings()

    def combined_portfolio(self) -> list[dict[str, Any]]:
        """Return Core + invested Active capital, aggregating identical instruments."""
        entries: list[dict[str, Any]] = []
        for row in self.list_core():
            entries.append({
                "ticker": row["ticker"], "value_ils": row["value_ils"],
                "currency": row["currency"], "fx_ticker": row["fx_ticker"],
            })
        for row in self.list_active():
            entries.append({
                "ticker": row["ticker"], "value_ils": row["value_ils"],
                "currency": row["currency"], "fx_ticker": row["fx_ticker"],
            })
        aggregate: dict[tuple[str, str, str | None], dict[str, Any]] = {}
        for entry in entries:
            key = (entry["ticker"], entry["currency"], entry["fx_ticker"])
            if key not in aggregate:
                aggregate[key] = entry
            else:
                aggregate[key]["value_ils"] += entry["value_ils"]
        return list(aggregate.values())

    def backup_to(self, destination: str | Path) -> Path:
        """Create a transactionally consistent backup, including WAL contents."""
        target = Path(destination).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source:
            with closing(sqlite3.connect(target)) as backup:
                source.backup(backup)
        return target
