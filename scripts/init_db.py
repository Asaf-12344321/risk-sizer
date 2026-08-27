#!/usr/bin/env python3
"""Create or upgrade the Risk Sizer SQLite schema."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import Database  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "risk_sizer.db")
    args = parser.parse_args()
    database = Database(args.db)
    database.initialize()
    print(f"Initialized {database.path}")


if __name__ == "__main__":
    main()
