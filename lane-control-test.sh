#!/usr/bin/env bash
# Offline check of `lane control`: the parts that decide whether a control command
# reaches every named lane, now, without waiting for the work in flight.
#
# tmux, herdr and claude are stubbed. Nothing here touches a real lane, a real tmux
# session or GitHub. Run it from the checkout:  ./lane-control-test.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LANE="$HERE/lane"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
pass=0; fail=0
check() { if [ "$2" = 0 ]; then printf '%-52s PASS\n' "$1"; pass=$((pass+1));
          else printf '%-52s FAIL\n' "$1"; fail=$((fail+1)); fi; }

# The wrapper pins PATH to fixed system dirs, so a stub binary cannot shadow tmux.
# Source its functions instead (dispatch stripped) and stub tmux as a shell function,
# which does beat PATH. The dispatch itself is exercised by running the real script.
sed '/^case "${1:-}" in/,$d' "$LANE" > "$TMP/lanelib.sh"
# shellcheck source=/dev/null
. "$TMP/lanelib.sh"
set +eu

export TMUX_LOG="$TMP/tmux.log"
export ISSUE_BRIDGE_HOME="$TMP/conf"
export ISSUE_BRIDGE_LANES_ROOT="$TMP/lanes"
REGISTRY="$ISSUE_BRIDGE_HOME/lanes"
: > "$TMUX_LOG"

tmux() {
  echo "$*" >> "$TMUX_LOG"
  case "$1" in
    has-session)     [ "${HAVE_SESSION:-1}" = 1 ] ;;
    display-message) echo claude ;;
    paste-buffer)    sleep 1 ;;
    *)               return 0 ;;
  esac
}

for name in alpha bravo charlie; do
  mkdir -p "$ISSUE_BRIDGE_HOME/lanes/$name/jobs"
  printf 'active\n' > "$ISSUE_BRIDGE_HOME/lanes/$name/state"
done

# 1. fan-out: every named lane gets the control, and they go out at once.
: > "$TMUX_LOG"
t0=$(date +%s)
cmd_control mute job-1 alpha bravo charlie >"$TMP/out1" 2>"$TMP/err1"; rc=$?
elapsed=$(( $(date +%s) - t0 ))
check "control exits 0 when every lane takes it" "$rc"
for name in alpha bravo charlie; do
  grep -q "lanectl-job-1-$name" "$TMUX_LOG"; check "$name received the control" $?
  grep -q "^control: mute$" "$ISSUE_BRIDGE_HOME/lanes/$name/jobs/job-1"; check "$name ledgered the job" $?
done
[ "$elapsed" -lt 3 ]; check "three lanes go out in parallel (${elapsed}s, serial is 3s)" $?

# 2. the work lock does not hold a control command.
mkdir -p "$ISSUE_BRIDGE_HOME/lanes/alpha/.lock"
: > "$TMUX_LOG"
cmd_control mute job-2 alpha >"$TMP/out2" 2>"$TMP/err2"; rc=$?
check "control sends while a work lock is held" "$rc"
grep -q "lanectl-job-2-alpha" "$TMUX_LOG"; check "the paste happened anyway" $?
grep -q "control does not wait" "$TMP/err2"; check "the lock bypass is warned about" $?
rmdir "$ISSUE_BRIDGE_HOME/lanes/alpha/.lock"

# 3. the ledger still suppresses a refiled job id, per lane.
: > "$TMUX_LOG"
cmd_control mute job-1 alpha >"$TMP/out3" 2>&1
grep -q "duplicate suppressed" "$TMP/out3"; check "a refiled job id is suppressed" $?
[ ! -s "$TMUX_LOG" ]; check "a suppressed job sends nothing" $?

# 4. the vocabulary is closed, and one dead lane does not stop the others.
( "$LANE" control 'rm -rf /' job-4 alpha ) >/dev/null 2>&1; [ $? -eq 2 ]
check "an unknown control command is refused" $?
( "$LANE" control mute job-4 ) >/dev/null 2>&1; [ $? -eq 2 ]
check "control with no lane named is refused" $?
cmd_control mute job-5 nope-not-a-lane >/dev/null 2>&1; [ $? -eq 1 ]
check "a missing lane fails the command" $?
: > "$TMUX_LOG"
cmd_control mute job-6 alpha nope-not-a-lane >/dev/null 2>&1; rc=$?
[ "$rc" -eq 1 ]; check "a partial failure is reported" $?
grep -q "lanectl-job-6-alpha" "$TMUX_LOG"; check "the live lane still got it" $?

printf '\n%d/%d passed\n' "$pass" "$((pass+fail))"
[ "$fail" -eq 0 ]
