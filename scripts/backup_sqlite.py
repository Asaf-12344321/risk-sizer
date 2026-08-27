#!/usr/bin/env python3
"""Create a consistent SQLite backup and expire old backup files."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--retention-days", type=int, default=30)
    args = parser.parse_args()

    if args.retention_days < 1:
        parser.error("--retention-days must be at least 1")
    if not args.db.exists():
        parser.error(f"database does not exist: {args.db}")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = args.backup_dir / f"risk_sizer-{stamp}.db"
    Database(args.db).backup_to(destination)

    cutoff = datetime.now(UTC) - timedelta(days=args.retention_days)
    removed = 0
    for candidate in args.backup_dir.glob("risk_sizer-*.db"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
        if candidate != destination and modified < cutoff:
            candidate.unlink()
            removed += 1
    print(f"Created {destination}; expired {removed} old backup(s)")


if __name__ == "__main__":
    main()
