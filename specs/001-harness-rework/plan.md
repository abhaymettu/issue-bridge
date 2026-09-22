# Plan: harness rework

Ladder check before code: not needed (no: F1 to F5 are live), already in the codebase
(the gateway has the pattern for its three actions; the poller has the journal and
runner; reuse both), stdlib (yes, everything below is stdlib Python and bash),
no new dependencies.

## Architecture

One new module, `capabilities.py`, stdlib only, imported by `bridge-poller.py` and
importable later by direct-line and by a thin bridge for the gateway:

- `load(cfg) -> table` validates the config's `capabilities` block at startup; a bad
  entry fails the whole table closed and lands in `status.json.last_error`.
- `resolve(table, name, params) -> Job | Refusal` substitutes placeholders, checks
  regexes, whole-token placement, path roots, NUL; returns the fixed argv and the
  capability's class, timeout, roots.
- `changed(roots, marker) -> list[path]`: touch a marker file before the run, one
  `os.walk` after it, report entries whose `st_mtime_ns` is newer than the marker,
  capped at 200 paths with a truncated flag. Roots are absolute and under `$HOME`.
- `receipt(dir, **fields)`: a dict written as JSON, 0600, atomic rename.
- `RateLimiter` over the receipts directory: count receipts in the last hour by class.
  No extra state file; the receipts are the ledger.
- `disabled(home) -> bool` for the DISABLED file.

Poller changes, surgical:

- `handle()`: if the body has `capability:`, resolve through the table; else if `allow`
  is non-empty, keep the old path. Deny text names the capability, never the params.
- Before running: kill switch, rate cap, class gate (approval lookup: a comment on the
  issue by `cfg["owner"]` matching `/approve <digest8>`; the gateway-written file form
  comes with P4).
  A gated job that lacks approval gets one comment saying what to approve and keeps
  its label; the poller remembers it commented (journal) so it does not repeat.
- Actor: `issue["user"]["login"]` plus one call to `/issues/N/events` for the last
  `labeled` event's actor. If the events call fails the receipt says `unknown`.
- After running: snapshot diff, receipt, comment with receipt id and changed count.
- `await_job` fall-through (F8): loop until the deadline when both `wait` and `show`
  fail.

`harness` helper (bash, in this repo, the narrow privileged helper):

- Actions, each a fixed argv: `kill` (touch DISABLED for issue-bridge, atlas-gateway,
  direct-line), `resume`, `status` (reads the three status files), `rotate-pat`
  (reads the new PAT from stdin, verifies with a GET on the repo, swaps the file
  atomically, kickstarts the poller), `rotate-direct-line-secret` (regenerates, restarts
  that LaunchAgent), `restart <label>` for the labels it knows. No free arguments
  other than the label enum. This is what a one-line sudoers rule would name; today
  none of its actions needs sudo, so the rule is optional.

Docs: README gains a capabilities section and a secrets table (name, file or keychain
item, consumer, how to rotate); AGENTS.md gains the `capability:` body form and the
approval flow; `TRUST-BOUNDARY.md` gets committed.

## Phases

- **P0, this session.** Audit, spec, plan, tasks committed on this lane. Handoff.
- **P1, this lane, no deploy.** `capabilities.py`, poller changes, `harness` helper,
  self-tests, docs. Committed. Handoff after each commit.
- **P2, owner deploy.** Write the new config (owner login, rate, capabilities; remove
  `/bin/zsh`; keep `uname` as a capability), `launchctl kickstart -k`, file a sentinel
  capability job, confirm receipt. Rollback: restore `config.json.pre-*` and check out
  the previous poller commit.
- **P3, owner-only system changes**, each independent, listed under decisions.
- **P4, follow-up lanes.** direct-line `/cap` route over `capabilities.py` and the
  Cloudflare Access or tailnet-only ingress; gateway `POST /api/approve` writing the
  approval file; aqua pins a producer commit and keeps the last sent body.

## Owner decisions (nothing in P1 depends on these)

1. Remove `/bin/zsh` from `allow` now, ahead of P2, as a pure config edit. Rollback is
   the backup file the installer already writes.
2. Move direct-line ingress off the public Cloudflare hostname to tailscale serve
   tailnet-only, or put Cloudflare Access in front. Until then rotate its secret.
3. Delete the dead serve rules (Funnel 10001 to 8787, tailnet 8444 to 8788).
4. Turn the macOS application firewall on, or accept that 6768 answers on the LAN.
   Orca has no bind flag, so the firewall is the only local control.
5. Replace `/etc/sudoers.d/abhay-agent` `NOPASSWD: ALL` with a line naming the
   `harness` helper, once it exists and has run for a week.
6. Review the two Orca pairings and the ten authorized_keys entries; revoke what is not
   in use. The Drive-stored key decision stands as previously made.
7. Whether `lane prompt` stays `destructive` (approval each time) or gets a per-lane
   standing approval with a TTL.

## Estimate

Agent rounds: P1 implementation 2 rounds (module plus poller, then helper plus docs),
verification 1, rework 1. P2 is one owner round.
