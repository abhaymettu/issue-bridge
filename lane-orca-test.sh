#!/usr/bin/env bash
# lane-orca-test.sh - checks on the Orca lane backend.
#
# Offline by default: it exercises the parts that decide whether a job runs,
# duplicates, or reaches the wrong backend, without starting an agent.
# LANE_TEST_LIVE=1 adds a real round trip through Orca (creates a throwaway
# lane, sends one prompt, reads the answer back, archives the lane).
set -uo pipefail

LANE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lane"
pass=0; fail=0

check() { # <name> <expected-exit> <cmd...>
  local name="$1" want="$2"; shift 2
  "$@" >/dev/null 2>&1
  local got=$?
  if [ "$got" = "$want" ]; then pass=$((pass+1)); printf 'PASS  %s\n' "$name"
  else fail=$((fail+1)); printf 'FAIL  %s (exit %s, wanted %s)\n' "$name" "$got" "$want"; fi
}

grep_check() { # <name> <needle> <cmd...>
  local name="$1" needle="$2"; shift 2
  # Capture, then match. Piping into `grep -q` makes grep exit on the first
  # hit, SIGPIPEs the command, and pipefail then reports the find as a failure.
  local out; out="$("$@" 2>&1)"
  if printf '%s' "$out" | grep -F -- "$needle" >/dev/null; then
    pass=$((pass+1)); printf 'PASS  %s\n' "$name"
  else
    fail=$((fail+1)); printf 'FAIL  %s (no %q in output)\n' "$name" "$needle"
  fi
}

echo "--- argument validation (nothing runs) ---"
check "bare invocation is usage"            2 "$LANE"
check "unknown subcommand is usage"         2 "$LANE" nonsense
check "uppercase lane name rejected"        2 "$LANE" up BadName
check "lane name with slash rejected"       2 "$LANE" up ../escape
check "lane name with dot rejected"         2 "$LANE" up a.b
check "empty prompt rejected"               2 "$LANE" prompt canary job1 ""
check "prompt without job id rejected"      2 "$LANE" prompt canary
check "bad job id rejected"                 2 "$LANE" prompt canary 'job id' hi
check "unknown control command rejected"    2 "$LANE" control shutdown job1 canary
check "control without a lane rejected"     2 "$LANE" control mute job1
check "read with non-numeric lines"         2 "$LANE" read canary lots

echo
echo "--- backend selection ---"
grep_check "default backend is orca" "orca:" "$LANE" health
# The rollback backend must still be reachable and must still be the tmux one.
if [ -x "$(dirname "$LANE")/lane-herdr" ]; then
  pass=$((pass+1)); printf 'PASS  %s\n' "lane-herdr present for rollback"
else
  fail=$((fail+1)); printf 'FAIL  %s\n' "lane-herdr missing"
fi
grep_check "herdr backend reachable by env" "tmux:" env ISSUE_BRIDGE_LANE_BACKEND=herdr "$LANE" health
# Herdr must be rollback only. Naming it in a comment or in the backend
# variable is fine; running the binary is not, so this looks for herdr in
# command position rather than for the word.
if grep -nE '(^|[;&|(]|\$\()[[:space:]]*herdr[[:space:]]' "$LANE" >/dev/null; then
  fail=$((fail+1)); printf 'FAIL  %s\n' "orca lane invokes the herdr binary"
else
  pass=$((pass+1)); printf 'PASS  %s\n' "orca lane never invokes the herdr binary"
fi

echo
echo "--- health ---"
grep_check "health names the allowlist entry" "config:" "$LANE" health
grep_check "health reports the orca runtime"  "orca:"   "$LANE" health
grep_check "health reports rollback"          "rollback:" "$LANE" health

echo
echo "--- deduplication (ledger, no agent needed) ---"
TMPHOME="$(mktemp -d)"
mkdir -p "$TMPHOME/lanes/dedupe/jobs"
printf 'active\n' > "$TMPHOME/lanes/dedupe/state"
printf 'job: j1\naccepted: 2026-01-01T00:00:00Z\nsubmitted: 2026-01-01T00:00:01Z\n' \
  > "$TMPHOME/lanes/dedupe/jobs/j1"
grep_check "an accepted job is never re-sent" "duplicate suppressed" \
  env ISSUE_BRIDGE_HOME="$TMPHOME" "$LANE" prompt dedupe j1 "this must not be sent"
printf 'job: j2\naccepted: 2026-01-01T00:00:00Z\n' > "$TMPHOME/lanes/dedupe/jobs/j2"
grep_check "a job with no submitted stamp warns" "the send may never have happened" \
  env ISSUE_BRIDGE_HOME="$TMPHOME" "$LANE" prompt dedupe j2 "this must not be sent"
grep_check "an unknown lane sends nothing" "does not exist" \
  env ISSUE_BRIDGE_HOME="$TMPHOME" "$LANE" prompt nosuchlane j3 "x"
printf 'archived\n' > "$TMPHOME/lanes/dedupe/state"
grep_check "an archived lane refuses work" "archived" \
  env ISSUE_BRIDGE_HOME="$TMPHOME" "$LANE" prompt dedupe j4 "x"
check "archived lane exits non-zero" 1 env ISSUE_BRIDGE_HOME="$TMPHOME" "$LANE" prompt dedupe j5 "x"
rm -rf "$TMPHOME"

echo
echo "--- live Orca round trip ---"
if [ "${LANE_TEST_LIVE:-0}" != 1 ]; then
  echo "SKIP  set LANE_TEST_LIVE=1 to run the live round trip"
else
  L="orca-selftest"
  marker="ORCA_SELFTEST_OK_$$"
  grep_check "lane up reports an orca terminal" "orca terminal" "$LANE" up "$L"
  grep_check "lane list shows the live terminal" "orca term_" "$LANE" list
  grep_check "prompt is submitted" "submitted: job" \
    "$LANE" prompt "$L" "t$$" "Reply with exactly $marker and nothing else."
  # The agent needs a moment; read until the marker shows or the budget runs out.
  found=no
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if "$LANE" read "$L" 2>/dev/null | grep -qF "$marker"; then found=yes; break; fi
    sleep 5
  done
  if [ "$found" = yes ]; then pass=$((pass+1)); printf 'PASS  %s\n' "the lane answered in its Orca pane"
  else fail=$((fail+1)); printf 'FAIL  %s\n' "no answer in the Orca pane within 60s"; fi
  grep_check "a resumed lane is not duplicated" "resumed:" "$LANE" up "$L"
  grep_check "stop archives the lane" "archived:" "$LANE" stop "$L"
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
