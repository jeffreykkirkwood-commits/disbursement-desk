#!/usr/bin/env python3
"""Fail if acquire_stream/release_stream no longer keep count updates under _stream_lock."""
from __future__ import annotations

import re
import sys
from pathlib import Path


def function_body(text: str, name: str) -> str:
    m = re.search(rf"^def {name}\(.*?:\n(.*?)(?=^def |\Z)", text, re.M | re.S)
    if not m:
        raise SystemExit(f"payout_events.py: {name}() not found")
    return m.group(1)


def lock_block(body: str) -> str:
    if "with _stream_lock:" not in body:
        raise SystemExit(
            "FAIL: function no longer takes _stream_lock — model Locked=TRUE is out of sync"
        )
    after = body.split("with _stream_lock:", 1)[1]
    block_lines = []
    for line in after.splitlines()[1:]:
        if line.startswith("    ") and not line.startswith("     "):
            break
        if line.strip() == "" or line.startswith("        "):
            block_lines.append(line)
        else:
            break
    return "\n".join(block_lines)


def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "payout_events.py")
    text = path.read_text()
    if "MAX_STREAMS" not in text:
        raise SystemExit("FAIL: MAX_STREAMS missing from payout_events.py")

    acquire = function_body(text, "acquire_stream")
    acquire_block = lock_block(acquire)
    for needle in ("_stream_count >= MAX_STREAMS", "_stream_count += 1"):
        if needle not in acquire_block:
            raise SystemExit(f"FAIL: under _stream_lock in acquire_stream, missing {needle!r}")

    release = function_body(text, "release_stream")
    release_block = lock_block(release)
    if "_stream_count -= 1" not in release_block and "_stream_count > 0" not in release_block:
        raise SystemExit("FAIL: under _stream_lock in release_stream, missing decrement guard")

    print("OK: acquire_stream/release_stream keep slot updates under _stream_lock")


if __name__ == "__main__":
    main()
