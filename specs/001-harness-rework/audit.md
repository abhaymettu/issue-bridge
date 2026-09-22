# Mac execution harness: architecture and threat-model audit

Date: 2026-09-21, read-only. Nothing on the machine was changed to produce this.
Sources: the code paths named below, live launchd, lsof, tailscale serve and funnel
status, and the runtime state directories. Line numbers are as of the commits named.

## 1. What is running

| Surface | Process | Bound to | Reachable from | Auth | Power once through |
| --- | --- | --- | --- | --- | --- |
| issue-bridge poller | `bridge.poller` LaunchAgent, `bridge-poller.py` 1.1.0 at 8e5b471 | outbound only (api.github.com) | anyone who can label an issue on `abhaymettu/abhay-mac-lane` (private, one collaborator) | GitHub PAT held by the poller; the filer is never checked | whatever `allow` admits; today that is a full shell |
| direct-line | `com.abhay.direct-line`, `~/lanes/direct-line/direct_line.py` | 127.0.0.1:8790 | public internet via Cloudflare tunnel `cmd.abhaymettu.com`, no Access policy | one static 64-byte secret, constant-time compare | `/exec` any argv except a shell blocklist; `python3 -c` and `osascript -e` are admitted and were used 2026-09-21 |
| Atlas gateway | `com.abhay.atlas-gateway`, `tailnet-gateway/src/server.ts` at ac150e9 | 127.0.0.1:8912 | tailnet only, tailscale serve 8445 | Tailscale identity header pinned to one login, then passkey (WebAuthn) for the writable terminal | `/bin/zsh -l` as abhay in a ttyd after passkey; fixed-argv lane start, prompt, stop |
| Orca runtime | `dev.onorca.serve`, `Orca --serve --serve-pairing-address 100.66.246.74` | `*:6768`, all interfaces | LAN and tailnet (macOS firewall is off; `orca serve` has no bind option, only `--port`) | pairing token per device; HTTP root serves the web app unauthenticated; WS auth not probed | paired device: terminal send, computer click/type, browser eval, worktree create/rm, orchestration |
| atlas-aqua publisher | `com.abhay.atlas-aqua-publisher`, signed Swift wrapper + `adapter/publisher.py` | local queue directory, launchd QueueDirectories | any process running as abhay | keychain item `life-dashboard-writer`, ACL bound to the wrapper's code signature | runs the unpinned, currently dirty `nightly-learning` main working tree with keychain and TCC, then one PUT to the Worker |
| sshd | system | 22 | public internet via Funnel 8443 | pubkey only, 10 keys | shell; sudo is `(ALL) NOPASSWD: ALL` (`/etc/sudoers.d/abhay-agent`) |
| Hammerspoon instinct-daemon | Hammerspoon | 127.0.0.1:8399 | public internet via Funnel 10000 | device token, separate shell token for `run`, constant-time compare, fail-silence | device verbs; shell with the shell token |
| dead routes | none | 8787, 8788 | Funnel 10001, tailnet 8444 | none | nothing today; whatever binds 8787 next becomes public with no further step |
| ssh-key watcher | `com.abhay.instinct-ssh-key-watcher`, every 30 s | outbound | private repo `abhaymettu/numen-deadman`, label `ssh-key` | repo access | appends to `~/.ssh/authorized_keys` |

Rollback switches that exist today and still work: poller `"runner": "subprocess"` or
`ISSUE_BRIDGE_RUNNER=subprocess`; lane `ISSUE_BRIDGE_LANE_BACKEND=herdr` to `lane-herdr`;
gateway `ATLAS_LANE_RUNTIME=herdr` (fails closed while Herdr is stopped; `herdr status`
says not running, state under `~/.config/herdr` and the retired plist are intact);
producer `git revert a337ac9` on nightly-learning main.

FirstMate: `~/code/firstmate/config/backend` reads `herdr` (checked 2026-09-21 23:00 CDT).
The morning memory note that said `orca` was wrong.

## 2. Assets

- The abhay login session: home directory, keychain, TCC grants (Accessibility,
  Screen Recording, Reminders, Drive), and passwordless sudo. Every surface above ends
  here, so "unbounded user shell" and "root" are the same thing on this machine.
- Bearer secrets, each alone sufficient for code execution: the bridge PAT
  (`~/.config/issue-bridge/github-token`), the direct-line secret, the perch device and
  shell tokens, the Orca runtime authToken and two device tokens, `gh` auth on this Mac.
  All are 0600 files or keychain items. None is rotated by any script.
- The published dashboard (external-send): one Worker PUT, no local copy of the last body.
- Live agent sessions (Claude Code lanes) that hold their own tool permissions.

## 3. Threat model

Actors, most likely first:

- **A1. An agent on this Mac, prompt-injected or simply wrong.** It runs as abhay, so it
  can read every token file, call `gh issue create` with a label, drop a file in the aqua
  queue, edit `publisher.py`, or POST to direct-line on loopback. Today each of those is
  a full shell with no human in the loop and no record of which agent did it.
- **A2. Whoever holds one leaked secret.** The direct-line secret alone gives internet
  reachable RCE. The PAT alone gives issue filing on a private repo, which is a shell.
- **A3. A LAN neighbour.** Port 6768 answers on every interface with the firewall off.
- **A4. Bugs that re-run side effects.** The label drop is documented as not a lock
  (README:264-266); `await_job` (bridge-poller.py:296-312) can report "no result" while
  the job is still running; a refiled issue re-runs a command.

Trust boundary the harness should have but does not: a single point where every remote
request is turned into a named capability with typed parameters, checked against a table,
rate-limited, recorded with its actor, and only then executed with a fixed argv. Today
the gateway has most of this for its own three actions; the poller and direct-line have
none of it; aqua has the table but not the actor or the pin.

## 4. Findings, ranked

| # | Sev | Where | Defect | Evidence |
| --- | --- | --- | --- | --- |
| F1 | High | poller config | `/bin/zsh` on `allow`; prefix match makes it any command | `allowed()` bridge-poller.py:137-147; jobs 476, 477 ran `/bin/zsh -lc`; README:283-286 forbids exactly this |
| F2 | High | direct-line | public ingress, one static secret, `/exec` admits interpreters | direct_line.py:41,52,199-215; REPORT.md "no Access policy"; log entries 20:30 and 21:40 |
| F3 | High | lane wrapper | `lane` prefix admits `prompt`, which types caller text into a tool-using Claude session | lane:263-317 |
| F4 | High | Orca serve | `*:6768`, firewall off, root route unauthenticated, paired device is console-equivalent, no CLI revoke | lsof; `socketfilterfw --getglobalstate`; orca-devices.json has 2 devices |
| F5 | Med | all | no surface records the actor; none records changed paths | poller never reads `issue["user"]`; aqua jobId is caller-chosen; direct-line has one shared secret |
| F6 | Med | aqua | full-publish runs an unpinned dirty checkout under keychain and TCC; adapter is unsigned and user-writable; no last-body rollback | publisher.py:73,356-359,576-578 |
| F7 | Med | tailscale | Funnel 10001 and tailnet 8444 point at nothing | lsof on 8787, 8788 empty |
| F8 | Med | poller | `await_job` falls through when `wait` and `show` both fail | bridge-poller.py:296-312 |
| F9 | Med | gateway | identity is a header trusted because only serve can reach loopback; a same-user local process can forge it | auth.ts:35-45 |
| F10 | Med | ssh | automated authorized_keys writer beside a public SSH funnel | instinct-ssh-key-watcher; Funnel 8443 |
| F11 | Low | direct-line | log redaction is heuristic; bare non-hex argv secrets land in the log | direct_line.py:65-86,374 |
| F12 | Low | gateway | prompt text is an argv element of `orca terminal send`, visible in `ps` | orca.ts:206 |
| F13 | Low | gateway | no route to revoke a passkey; keystrokes in the writable shell are not audited | webauthn.ts:109-122 |
| F14 | Low | poller | issue title becomes an Orca tab title | bridge-poller.py:336 |
| F15 | Low | aqua | stale `atlas-aqua.adhoc-backup-20260921` beside the live binary | bin/ |

Things already right and worth keeping: the poller is outbound-only and stdlib; the PAT
never reaches argv, logs, status or comments; the writeback journal makes the result
retryable without re-running; the gateway's session, CSRF, origin, rate-limit and
`DISABLED` file design; aqua's keychain ACL bound to a signature and the key allowlist
in the wrapper; the 8399 daemon's two-tier tokens and fail-silence.
