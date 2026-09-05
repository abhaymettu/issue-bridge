#!/usr/bin/env bash
# issue-bridge installer: config + token + LaunchAgent + labels + a live check.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="$HOME/.config/issue-bridge"
PLIST="$HOME/Library/LaunchAgents/bridge.poller.plist"
TARGET="gui/$(id -u)/bridge.poller"
PY="$(command -v python3 || true)"

[ -n "$PY" ] || { echo "python3 not found on PATH"; exit 1; }
command -v curl >/dev/null || { echo "curl not found on PATH"; exit 1; }

read -r -p "Designated repo (owner/name): " REPO
[ -n "$REPO" ] || { echo "a repo is required"; exit 1; }
echo "Paste a fine-grained PAT scoped to $REPO only"
echo "  (Issues: read/write, Metadata: read - nothing else)"
read -r -s -p "PAT: " PAT
echo
[ -n "$PAT" ] || { echo "a token is required"; exit 1; }

# The token goes to disk once, at 0600, and is never echoed or logged.
umask 077
mkdir -p "$CONF"
printf '%s\n' "$PAT" > "$CONF/github-token"
chmod 600 "$CONF/github-token"

if [ -f "$CONF/config.json" ]; then
  echo "keeping the existing $CONF/config.json (edit 'allow' by hand)"
else
  cat > "$CONF/config.json" <<JSON
{
  "repo": "$REPO",
  "label": "exec-job",
  "allow": ["uname"],
  "poll_interval": 60
}
JSON
  echo "wrote $CONF/config.json - allow starts at [\"uname\"] until you widen it"
fi

# curl reads the credential from stdin, so the token never appears in `ps`.
api() {
  local method="$1" path="$2"
  local args=(-sS -K - -X "$method"
              -H "Accept: application/vnd.github+json"
              -H "X-GitHub-Api-Version: 2022-11-28")
  if [ "$#" -gt 2 ]; then
    args+=(-H "Content-Type: application/json" --data "$3")
  fi
  printf 'header = "Authorization: Bearer %s"\n' "$PAT" |
    curl "${args[@]}" "https://api.github.com/repos/$REPO$path"
}

echo "checking the token against $REPO ..."
api GET "" | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
if "full_name" not in d:
    sys.exit("token cannot read the repo: %s" % d.get("message", d))
print("  ok:", d["full_name"], "(private)" if d.get("private") else "(PUBLIC - see README security)")'

echo "creating labels ..."
for l in exec-job exec-done exec-failed; do
  api POST /labels "{\"name\":\"$l\",\"color\":\"ededed\"}" >/dev/null || true
done

echo "installing the LaunchAgent ..."
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>bridge.poller</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$HERE/bridge-poller.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$CONF/poller.log</string>
  <key>StandardErrorPath</key><string>$CONF/poller.log</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "$TARGET" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "$TARGET" 2>/dev/null || true
echo "  loaded: $TARGET"

echo "filing a verification job (uname -a) ..."
BODY="$("$PY" -c 'import json; print(json.dumps({
 "title": "issue-bridge install check",
 "labels": ["exec-job"],
 "body": "---\nargv: [\"uname\", \"-a\"]\nrule: drain-on-wake\n---\n"}))')"
NUM="$(api POST /issues "$BODY" | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
if "number" not in d:
    sys.exit("could not file the issue: %s" % d.get("message", d))
print(d["number"])')"
echo "  issue #$NUM filed; the poller runs on a 60s cycle, so allow a minute"

for _ in $(seq 1 36); do
  sleep 5
  VERDICT="$(api GET "/issues/$NUM" | "$PY" -c 'import json,sys
d = json.load(sys.stdin)
names = [l["name"] for l in d.get("labels", [])]
print("done" if "exec-done" in names else "failed" if "exec-failed" in names else "waiting")')"
  case "$VERDICT" in
    done)
      echo
      echo "SUCCESS - issue #$NUM ran and closed exec-done. The bridge is live."
      echo "Next: widen \"allow\" in $CONF/config.json, then hand your agent AGENTS.md."
      exit 0 ;;
    failed)
      echo "FAILED - issue #$NUM closed exec-failed. Read its comment: it says why."
      echo "If it was denied, add the command to \"allow\" in $CONF/config.json."
      exit 1 ;;
  esac
done

echo "TIMED OUT after 3 minutes - issue #$NUM never came back."
echo "Check: launchctl print $TARGET, $CONF/poller.log, $CONF/status.json"
exit 1
