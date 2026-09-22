# Spec: remote drive without an unbounded shell

Feature: replace the poller's token-prefix allowlist with named capabilities, add
receipts, a kill switch, rate limits, action-specific owner authority, and secret
rotation, so that a GitHub issue (or later a direct-line or gateway request) can drive
this Mac without any path to a general shell. Lightweight spec for an existing codebase.

## Goals

1. **No general shell from any remote intake.** After deploy, no issue body, however
   labelled, can start `zsh`, `sh`, `python -c`, `osascript -e`, or type free text into
   an agent session without an owner approval bound to that exact request.
2. **Every action is a named capability** with a fixed argv template, typed parameters,
   a class (`read`, `mutate`, `destructive`, `spend`, `external-send`), a timeout, a
   rate, and the roots it may change. Callers name the capability and give parameters.
   They never give argv.
3. **Receipts.** Every run writes one JSON receipt: actor (issue author and who applied
   the label), capability, parameter digest, approval id if any, exit, duration, and the
   paths that changed under the capability's declared roots. The issue result comment
   carries the receipt id.
4. **Owner authority for `destructive`, `spend`, `external-send`.** Such a job waits
   until a comment `/approve <digest>` by the configured owner login exists on the
   issue, where the digest covers the capability name and the parameters together.
   An approval is single-use. A passkey-gateway approval route is P4.
5. **Kill switch.** One file, `~/.config/issue-bridge/DISABLED`, stops the poller from
   starting any job while leaving labels in place; `status.json` says so. A `harness`
   helper touches the same file for every intake it knows about.
6. **Rate limits.** A global cap on jobs per hour and a smaller cap for the three
   privileged classes; jobs over the cap keep their label and wait, they are not dropped.
7. **No secrets in argv, logs, forms, or comments.** Parameters travel in the 0700 job
   directory; receipts and comments carry digests, never parameter values.
8. **Token rotation** as a documented, scripted step per secret this repo owns (the PAT)
   with a verify call, plus a README table of every other secret the harness touches and
   where it is rotated.
9. **Narrow privileged helper.** An enumerated-action script that is the only thing
   sudo should ever admit, so `NOPASSWD: ALL` can be replaced by one line naming it.
   Writing the helper is in scope; changing sudoers is the owner's step.
10. **Existing working paths and rollbacks are preserved.** `runner: subprocess`,
    `ISSUE_BRIDGE_LANE_BACKEND=herdr`, and the writeback journal stay as they are. The
    old `allow` list keeps working until the owner switches the config, so rollback is
    "put the old config back".

## Non-goals

- Changing TCC, signing, keychain, sudoers, tailscale serve, the Cloudflare tunnel, the
  macOS firewall, or Orca pairing. Each is listed in plan.md as an owner decision.
- Rewriting direct-line or the gateway. They adopt the shared capability module in
  follow-up lanes; this lane ships the module and the poller.
- Auditing keystrokes inside the gateway's passkey-granted shell.

## Interface

Issue body, frontmatter form, one `key: value` per line as today:

```
---
capability: git-pull
params: {"repo": "issue-bridge"}
---
```

`argv:` bodies are still accepted while `allow` is non-empty in the config, so the
migration is a config change, not a flag day.

Config, new keys beside the existing ones:

```
"owner": "abhaymettu",
"rate": {"jobs_per_hour": 30, "privileged_per_hour": 5},
"capabilities": {
  "git-pull": {"argv": ["git", "-C", "{repo}", "pull", "--ff-only"],
               "params": {"repo": "^/Users/abhay/src/[a-z0-9-]{1,40}$"},
               "class": "mutate", "timeout": 120,
               "changed_roots": ["{repo}"]},
  "lane-prompt": {"argv": ["/Users/abhay/src/issue-bridge/lane", "prompt", "{lane}", "{job}", "{text}"],
                  "params": {"lane": "^[a-z0-9-]{1,32}$", "job": "^[0-9]{1,8}$", "text": "^[\\s\\S]{1,12000}$"},
                  "class": "destructive", "timeout": 60, "changed_roots": []}
}
```

Rules: a `{name}` placeholder must be a whole argv token, is substituted once, and must
match its regex in full. Unknown parameters are refused. Parameter values that contain
a NUL, or that resolve a path outside the declared roots when the template says a
path, are refused.

Receipt, `~/.config/issue-bridge/receipts/<utc>-<issue>.json`:

```
{"id": "20260922T041500Z-481", "issue": 481, "actor": {"author": "abhaymettu", "labelled_by": "abhaymettu"},
 "capability": "git-pull", "params_sha256": "...", "approval": null, "class": "mutate",
 "exit": 0, "duration": 3.2, "changed_paths": ["/Users/abhay/src/issue-bridge/README.md"],
 "changed_truncated": false, "runner": "orca", "started": "...", "finished": "..."}
```

## Acceptance

- `bridge-poller.py --self-test` covers: capability match and refusal (unknown name,
  unknown param, regex miss, placeholder not a whole token), class gating without and
  with a used approval, single-use approval, rate cap leaves the label, DISABLED leaves
  the label and comments nothing, receipt written with actor and changed paths, no
  parameter value in any comment or log line.
- A sentinel job through the deployed poller returns exec-done with a receipt id in the
  comment and a receipt file on disk. That step is the owner's.

## Owner decisions needed before deploy

See plan.md, section "Owner decisions". None of the code work depends on them.
