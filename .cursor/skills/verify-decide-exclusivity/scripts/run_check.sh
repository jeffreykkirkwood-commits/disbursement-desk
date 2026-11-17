#!/usr/bin/env bash
# Structural sync + TLC for verify-decide-exclusivity.
# Usage: run_check.sh [--expect-fail-unlocked]
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$SKILL_DIR/../../.." && pwd)"
MODEL="$SKILL_DIR/model"
OUT="${DESK_FORMAL_OUT:-/tmp/disbursement-desk-formal-decide}"
mkdir -p "$OUT"

echo "== structural sync =="
python3 "$SKILL_DIR/scripts/check_structure.py" "$REPO"

STATECHART=""
shopt -s nullglob
for c in \
  "$HOME"/.cursor/plugins/cache/cursor-public/*/skills/statechart/scripts/statechart \
  "$HOME"/.cursor/plugins/cache/cursor-public/*/*/skills/statechart/scripts/statechart \
  "$HOME"/.cursor/plugins/pstack/skills/statechart/scripts/statechart
 do
  if [[ -x "$c" ]]; then STATECHART="$c"; break; fi
done
shopt -u nullglob
if [[ -z "$STATECHART" && -n "${PSTACK_STATECHART:-}" && -x "${PSTACK_STATECHART}" ]]; then
  STATECHART="$PSTACK_STATECHART"
fi

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
    (cd "$MODEL" && "$JAVA" -cp "$TLA2TOOLS" tlc2.TLC -config "$cfg" DecideExclusivity.tla) \
      >"$dest/tlc.out" 2>&1
    tlc_rc=$?
    cat "$dest/tlc.out"
    echo "$tlc_rc" >"$dest/tlc.exit"
    return "$tlc_rc"
  elif command -v tlc >/dev/null 2>&1; then
    set +e
    (cd "$MODEL" && tlc -config "$cfg" DecideExclusivity.tla) >"$dest/tlc.out" 2>&1
    tlc_rc=$?
    cat "$dest/tlc.out"
    echo "$tlc_rc" >"$dest/tlc.exit"
    return "$tlc_rc"
  elif [[ -n "$STATECHART" ]]; then
    "$STATECHART" "$MODEL/DecideExclusivity.tla" --config "$MODEL/$cfg" --out "$dest" \
      | tee "$OUT/${label}.statechart.log"
  else
    echo "FAIL: set TLA2TOOLS=/path/to/tla2tools.jar and JAVA=$(command -v java)" >&2
    exit 2
  fi
}

set +e
run_tlc DecideExclusivity.cfg locked
locked_rc=$?
set -e
if [[ "$locked_rc" -ne 0 ]] || grep -q "Invariant Inv_AtMostOneSettle is violated" "$OUT/locked/tlc.out" 2>/dev/null; then
  echo "FAIL: Locked=TRUE model did not pass Inv_AtMostOneSettle (rc=$locked_rc)" >&2
  exit 1
fi
echo "OK: Locked=TRUE model satisfies Inv_AtMostOneSettle"

if [[ "${1:-}" == "--expect-fail-unlocked" ]]; then
  set +e
  run_tlc DecideExclusivityUnlocked.cfg unlocked
  unlocked_rc=$?
  set -e
  if grep -q "Invariant Inv_AtMostOneSettle is violated" "$OUT/unlocked/tlc.out" 2>/dev/null \
     || [[ "$unlocked_rc" -ne 0 ]]; then
    echo "OK: unlocked model shows Inv_AtMostOneSettle can fail (see $OUT/unlocked/tlc.out)"
  else
    echo "FAIL: expected unlocked model to violate Inv_AtMostOneSettle" >&2
    exit 1
  fi
fi

echo "OK: formal check finished; artifacts in $OUT"
