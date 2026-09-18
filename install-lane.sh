#!/usr/bin/env bash
# install-lane.sh - the mac-lane fallback, in one command.
#
# Installs the lane wrapper, applies the local result-block patch to the
# poller, sets the allowlist to exactly ["uname", <this checkout's lane>],
# restarts the poller, then files a real sentinel job through the wrapper
# and waits for it. SUCCESS prints only when the sentinel came back
# exec-done with a result block in the issue body.
#
# Run from the checkout:   ./install-lane.sh
# Safe to run twice. Re-run it after any `git pull` of this repo: the three
# lane files are untracked and survive a pull, but the patch to
# bridge-poller.py must be re-applied if upstream rewrote that file (the
# patch script tells you when its anchors drifted).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HOME/.config/issue-bridge"
TARGET="gui/$(id -u)/bridge.poller"
WRAPPER="$HERE/lane"
PY="$(command -v python3 || true)"

[ -n "$PY" ] || { echo "python3 not found on PATH"; exit 1; }
command -v curl >/dev/null || { echo "curl not found on PATH"; exit 1; }
[ -f "$CONF/config.json" ] || { echo "$CONF/config.json missing - run ./install.sh first"; exit 1; }
[ -f "$CONF/github-token" ] || { echo "$CONF/github-token missing - run ./install.sh first"; exit 1; }
[ -f "$HERE/bridge-poller.py" ] || { echo "run me from the issue-bridge checkout"; exit 1; }

echo "checking the wrapper ..."
bash -n "$WRAPPER"
if command -v shellcheck >/dev/null 2>&1; then
  shellcheck -S warning "$WRAPPER"
  echo "  shellcheck clean"
else
  echo "  shellcheck not installed - bash -n passed (install shellcheck for the full check)"
fi
chmod 755 "$WRAPPER"

echo "verifying the local deployment patch (result block + writeback journal) ..."
"$PY" "$HERE/patch-result-block.py"
"$PY" -m py_compile "$HERE/bridge-poller.py"
"$PY" "$HERE/bridge-poller.py" --self-test | tail -1

echo "setting the allowlist ..."
# Exactly ["uname", <wrapper>]: the wrapper is the only lane surface, and
# anything more general (bash, sh, python, ssh, tmux, herdr itself) would
# hand the whole machine to anyone who can label an issue. Nothing else in
# the file is touched; a pre-change backup lands next to it.
"$PY" - "$CONF/config.json" "$WRAPPER" <<'CFG_EOF'
import json, pathlib, sys, time
conf, wrapper = pathlib.Path(sys.argv[1]), sys.argv[2]
c = json.loads(conf.read_text())
want = ["uname", wrapper]
if c.get("allow") == want:
    print("  allow already exact: %s" % want)
else:
    backup = conf.with_name("config.json.pre-lane-%d" % int(time.time()))
    backup.write_text(conf.read_text())
    c["allow"] = want
    conf.write_text(json.dumps(c, indent=2) + "\n")
    print("  allow set to %s (old config: %s)" % (want, backup))
CFG_EOF

echo "restarting the poller ..."
launchctl print "$TARGET" >/dev/null 2>&1 || {
  echo "$TARGET is not loaded - run ./install.sh first"; exit 1; }
launchctl kickstart -k "$TARGET"
echo "  $TARGET restarted"

# The sentinel: a health check filed through the whole loop - issue, poll,
# allowlist match on the wrapper path, wrapper run, comment, result block,
# exec-done, close. Token rides stdin to curl, never the command line.
LABEL="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("label", "exec-job"))' "$CONF/config.json")"
REPO="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["repo"])' "$CONF/config.json")"
api() {
  local method="$1" path="$2"
  local args=(-sS -K - -X "$method"
              -H "Accept: application/vnd.github+json"
              -H "X-GitHub-Api-Version: 2022-11-28")
  if [ "$#" -gt 2 ]; then
    args+=(-H "Content-Type: application/json" --data "$3")
  fi
  printf 'header = "Authorization: Bearer %s"\n' "$(cat "$CONF/github-token")" |
    curl "${args[@]}" "https://api.github.com/repos/$REPO$path"
}

echo "filing the sentinel (lane health) on $REPO ..."
BODY="$("$PY" -c 'import json,sys; print(json.dumps({
 "title": "lane install check",
 "labels": [sys.argv[1]],
 "body": "---\nargv: [%s, \"health\"]\nrule: drain-on-wake\n---\n" % json.dumps(sys.argv[2])}))' "$LABEL" "$WRAPPER")"
NUM="$(api POST /issues "$BODY" | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
if "number" not in d:
    sys.exit("could not file the issue: %s" % d.get("message", d))
print(d["number"])')"
echo "  issue #$NUM filed; the poller runs on a 60s cycle, so allow a few minutes"

for _ in $(seq 1 48); do
  sleep 5
  VERDICT="$(api GET "/issues/$NUM" | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
names = [l["name"] for l in d.get("labels", [])]
body = d.get("body") or ""
if d.get("state") == "closed" and d.get("comments", 0) > 0 and "exec-done" in names:
    if "<!-- issue-bridge:result" in body and chr(34)+"exit"+chr(34)+":0" in body:
        print("done")
    else:
        print("no-block")
elif "exec-failed" in names:
    print("failed")
else:
    print("waiting")')"
  case "$VERDICT" in
    done)
      echo
      echo "SUCCESS - sentinel issue #$NUM ran the wrapper, commented, carried a"
      echo "result block in its body, and closed exec-done. The mac-lane fallback is live."
      echo
      echo "File lane jobs as issues labelled $LABEL on $REPO with argv:"
      echo "  [\"$WRAPPER\", \"up\", \"<task-name>\"]"
      echo "  [\"$WRAPPER\", \"prompt\", \"<task-name>\", \"<issue-number>\", \"<prompt text>\"]"
      echo "  [\"$WRAPPER\", \"read\", \"<task-name>\", \"120\"]"
      echo "  [\"$WRAPPER\", \"stop\", \"<task-name>\"]"
      echo "Lane names: lowercase, digits, dashes, 31 chars max. The prompt job id"
      echo "(use the issue number) is what stops a retried job sending twice."
      exit 0 ;;
    no-block)
      echo "FAILED - issue #$NUM closed exec-done but its body has no result block."
      echo "The poller patch did not take: read $CONF/poller.log and re-run this script."
      exit 1 ;;
    failed)
      echo "FAILED - issue #$NUM closed exec-failed. Read its comment: it says why."
      exit 1 ;;
  esac
done

echo "TIMED OUT after 4 minutes - issue #$NUM never came back."
echo "Check: launchctl print $TARGET, $CONF/poller.log, $CONF/status.json"
exit 1
