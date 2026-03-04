#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.preference import train_editor_choice_model


def main() -> None:
    parser = argparse.ArgumentParser(description="Train editor-choice model on clean labeled events only.")
    parser.add_argument("--days-back", type=int, default=365, help="Lookback window in days.")
    parser.add_argument("--min-samples", type=int, default=20, help="Minimum samples to allow training.")
    parser.add_argument("--min-reason-len", type=int, default=20, help="Minimum reason length for clean labels.")
    parser.add_argument("--max-rows", type=int, default=300, help="Cap dataset rows (0 = unlimited).")
    parser.add_argument("--balance", action="store_true", help="Balance classes to 50/50.")
    args = parser.parse_args()

    out = train_editor_choice_model(
        days_back=int(args.days_back),
        min_samples=int(args.min_samples),
        clean_only=True,
        min_reason_len=int(args.min_reason_len),
        balance_classes=bool(args.balance),
        max_rows=int(args.max_rows),
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
