#!/usr/bin/env python3
"""Fail if payouts.decide no longer serializes under BEGIN IMMEDIATE + UNIQUE keys."""
from __future__ import annotations

import re
import sys
from pathlib import Path


def function_body(text: str, name: str) -> str:
    m = re.search(rf"^def {name}\(.*?:\n(.*?)(?=^def |\Z)", text, re.M | re.S)
    if not m:
        raise SystemExit(f"payouts.py: {name}() not found")
    return m.group(1)


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    path = root / "payouts.py"
    text = path.read_text()
    decide = function_body(text, "decide")

    if "BEGIN IMMEDIATE" not in decide:
        raise SystemExit(
            "FAIL: decide() no longer uses BEGIN IMMEDIATE — model Locked=TRUE is out of sync"
        )
    if "idempotency_key" not in decide:
        raise SystemExit("FAIL: decide() missing idempotency_key lookup/insert")
    if "UNIQUE" not in text and "idempotency_key TEXT UNIQUE" not in text:
        # SCHEMA may live above decide; require UNIQUE on the column somewhere in the module.
        if "idempotency_key TEXT UNIQUE" not in text:
            raise SystemExit("FAIL: payout_events.idempotency_key UNIQUE missing from payouts.py")
    for needle in (
        "FROM payout_events WHERE idempotency_key",
        "record_transition",
        "DECIDABLE",
    ):
        if needle not in decide:
            raise SystemExit(f"FAIL: decide() missing {needle!r}")

    print("OK: decide() keeps BEGIN IMMEDIATE + idempotency_key exclusivity")


if __name__ == "__main__":
    main()
