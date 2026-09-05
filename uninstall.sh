#!/usr/bin/env bash
# Removes the poller, its LaunchAgent and its config. Leaves the GitHub repo,
# its labels and its issues alone - those are yours to keep or delete.
set -euo pipefail

CONF="$HOME/.config/issue-bridge"
PLIST="$HOME/Library/LaunchAgents/bridge.poller.plist"
TARGET="gui/$(id -u)/bridge.poller"

launchctl bootout "$TARGET" 2>/dev/null || true
rm -f "$PLIST"
rm -rf "$CONF"

echo "removed the LaunchAgent, $PLIST and $CONF"
echo "the repo, its labels and its issues are untouched; revoke the PAT on GitHub."
