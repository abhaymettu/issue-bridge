#!/usr/bin/env python3
"""patch-result-block.py - guard for the local deployment patch in bridge-poller.py.

NOT UPSTREAM. The deployment patch gives a handled job a machine-readable result
block in the issue BODY (an <!-- issue-bridge:result {...} --> marker with exit,
duration, ts and the last 4KB of each stream) and, since the writeback journal was
added, makes that writeback retryable: `~/.config/issue-bridge/pending/<issue>.json`
holds the captured result until the body PATCH, comment, label and close have all
landed, and a retry never re-runs the command.

This script used to insert that patch by anchor. It no longer does: the patch now
spans handle(), cycle() and status, which is more than anchored insertion can place
safely, and a half-application would silently trade the journal for the old
lose-the-marker behaviour. So it verifies instead of edits. The patched poller lives
in this repo's git history; that is the recovery path after an upstream pull.

Exit 0 when the patch is present, 1 when it is not.
"""
import pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
POLLER = HERE / "bridge-poller.py"
REQUIRED = ("issue-bridge:result", "def writeback(", "def drain_pending(",
            "def save_pending(", "pending_writebacks")


def main():
    src = POLLER.read_text()
    missing = [m for m in REQUIRED if m not in src]
    if not missing:
        print("patch present: %s carries the result block and the writeback journal"
              % POLLER)
        return 0
    print("PATCH MISSING from %s: %s\n"
          "This script no longer re-applies it (see the docstring). Restore the "
          "patched poller from this repo instead:\n"
          "  git -C %s log --oneline -- bridge-poller.py\n"
          "  git -C %s checkout <commit> -- bridge-poller.py\n"
          "then re-run this script and `bridge-poller.py --self-test`."
          % (POLLER, ", ".join(missing), HERE, HERE), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
