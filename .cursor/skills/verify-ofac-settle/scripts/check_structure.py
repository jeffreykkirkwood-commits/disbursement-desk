#!/usr/bin/env python3
"""Fail if OFAC settle gates in payouts.py / payout_rules.py drift from the model."""
from __future__ import annotations

import re
import sys
from pathlib import Path


def function_body(text: str, name: str) -> str:
    m = re.search(rf"^def {name}\(.*?:\n(.*?)(?=^def |\Z)", text, re.M | re.S)
    if not m:
        raise SystemExit(f"{name}() not found")
    return m.group(1)


def main() -> None:
    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    payouts_path = repo / "payouts.py"
    rules_path = repo / "payout_rules.py"
    payouts = payouts_path.read_text()
    rules = rules_path.read_text()

    decide = function_body(payouts, "decide")
    settle = function_body(payouts, "_settle")
    explain = function_body(payouts, "explain_flag")
    route_one = function_body(rules, "route_one")

    # decide: Approve refused when POSSIBLE_SANCTIONS_MATCH is already flagged.
    if not re.search(
        r"action\s*==\s*APPROVE\s+and\s+flagged_sanctions",
        decide,
    ):
        raise SystemExit(
            "FAIL: decide() missing Approve refusal when flagged_sanctions "
            "(model GateCorrect Approve 409 path)"
        )
    if "use Clear or Block" not in decide:
        raise SystemExit("FAIL: decide() Approve+sanctions refusal message missing")

    # Only Clear may pass allow_sanctions_clear=True into _settle.
    if "allow_sanctions_clear=(action == CLEAR)" not in decide:
        raise SystemExit(
            "FAIL: decide() must call _settle(..., allow_sanctions_clear=(action == CLEAR)); "
            "a constant True would let Approve settle a match without Clear"
        )

    # _settle re-screens unless Clear allowed the send, and holds on match.
    if "allow_sanctions_clear" not in settle:
        raise SystemExit("FAIL: _settle() missing allow_sanctions_clear parameter")
    if "ofac.screen" not in settle:
        raise SystemExit(
            "FAIL: _settle() must call ofac.screen when not allow_sanctions_clear "
            "(SettleScreen in the model)"
        )
    if "_apply_sanctions_hold" not in settle:
        raise SystemExit("FAIL: _settle() must hold via _apply_sanctions_hold on match")

    # already-settled short-circuit must stay after the PAID+SETTLED check, not skip
    # the gate for a first-time settle. We only require the screen branch exists when
    # not allow_sanctions_clear.
    if not re.search(r"not allow_sanctions_clear", settle):
        raise SystemExit(
            "FAIL: _settle() must gate ofac.screen behind `not allow_sanctions_clear`"
        )

    # explain_flag must include OFAC so route_one never treats a match as clean.
    if "ofac.screen" not in explain:
        raise SystemExit(
            "FAIL: explain_flag() must call ofac.screen "
            "(RouteOne Match=>FLAGGED path)"
        )
    if "POSSIBLE_SANCTIONS_MATCH" not in explain:
        raise SystemExit("FAIL: explain_flag() must append POSSIBLE_SANCTIONS_MATCH")

    # route_one: only settle the clean (no flag_code) PAID path.
    if "explain_flag" not in route_one:
        raise SystemExit("FAIL: route_one() must recompute flags via explain_flag")
    if "payouts._settle" not in route_one and "_settle(" not in route_one:
        raise SystemExit("FAIL: route_one() must call _settle on the clean PAID path")
    if "if step == payouts.PAID" not in route_one and "if step == PAID" not in route_one:
        raise SystemExit(
            "FAIL: route_one() must call _settle only when step is PAID "
            "(flagged path must not settle)"
        )
    if "flag_code" not in route_one:
        raise SystemExit(
            "FAIL: route_one() must choose FLAGGED vs APPROVED/PAID from flag_code"
        )

    print(
        "OK: decide/_settle/explain_flag/route_one keep OFAC settle gates "
        "(Approve refusal, Clear-only allow_sanctions_clear, settle re-screen, route via explain_flag)"
    )


if __name__ == "__main__":
    main()
