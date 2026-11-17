#!/usr/bin/env bash
# Structural sync + TLC for verify-ofac-settle.
# Usage: run_check.sh [--expect-fail-broken]
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# skill → skills → .cursor → repo
REPO="$(cd "$SKILL_DIR/../../.." && pwd)"
MODEL="$SKILL_DIR/model"
OUT="${DESK_FORMAL_OUT:-/tmp/disbursement-desk-formal-ofac}"
mkdir -p "$OUT"

echo "== structural sync =="
python3 "$SKILL_DIR/scripts/check_structure.py" "$REPO"

if [[ -z "${TLA2TOOLS:-}" && -f /tmp/tla/tla2tools.jar ]]; then
  export TLA2TOOLS=/tmp/tla/tla2tools.jar
fi
export JAVA="${JAVA:-$(command -v java || true)}"

run_tlc() {
  local cfg="$1" label="$2"
  local dest="$OUT/$label"
  rm -rf "$dest"
  mkdir -p "$dest"
  echo "== TLC: $label ($cfg) =="
  local tlc_rc=0
  if [[ -n "${TLA2TOOLS:-}" && -n "${JAVA:-}" ]]; then
    set +e
    (cd "$MODEL" && "$JAVA" -cp "$TLA2TOOLS" tlc2.TLC -config "$cfg" OfacSettle.tla) \
      >"$dest/tlc.out" 2>&1
    tlc_rc=$?
    cat "$dest/tlc.out"
    echo "$tlc_rc" >"$dest/tlc.exit"
    return "$tlc_rc"
  elif command -v tlc >/dev/null 2>&1; then
    set +e
    (cd "$MODEL" && tlc -config "$cfg" OfacSettle.tla) >"$dest/tlc.out" 2>&1
    tlc_rc=$?
    cat "$dest/tlc.out"
    echo "$tlc_rc" >"$dest/tlc.exit"
    return "$tlc_rc"
  else
    echo "FAIL: set TLA2TOOLS=/path/to/tla2tools.jar and JAVA=$(command -v java)" >&2
    exit 2
  fi
}

set +e
run_tlc OfacSettle.cfg correct
correct_rc=$?
set -e
if [[ "$correct_rc" -ne 0 ]] || grep -q "Invariant Inv_NoSettleWithoutClear is violated" "$OUT/correct/tlc.out" 2>/dev/null; then
  echo "FAIL: GateCorrect=TRUE model did not pass Inv_NoSettleWithoutClear (rc=$correct_rc)" >&2
  exit 1
fi
echo "OK: GateCorrect=TRUE model satisfies Inv_NoSettleWithoutClear"

if [[ "${1:-}" == "--expect-fail-broken" ]]; then
  set +e
  run_tlc OfacSettleBroken.cfg broken
  broken_rc=$?
  set -e
  if grep -q "Invariant Inv_NoSettleWithoutClear is violated" "$OUT/broken/tlc.out" 2>/dev/null \
     || [[ "$broken_rc" -ne 0 ]]; then
    echo "OK: broken model shows Inv_NoSettleWithoutClear can fail (see $OUT/broken/tlc.out)"
    echo "Read that counterexample with /statechart (do not hard-code that skill's CLI flags here)."
  else
    echo "FAIL: expected broken model to violate Inv_NoSettleWithoutClear" >&2
    exit 1
  fi
fi

echo "OK: formal check finished; artifacts in $OUT"
