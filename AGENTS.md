# AGENTS.md - how to run a command on the operator's Mac

You can run commands on a specific person's Mac by filing a GitHub issue. A
poller on that machine watches one designated repo, runs allowlisted commands,
and answers in the issue. You never get a shell; you get one round trip per
issue.

Ask the operator which repo is the lane. Everything below happens on that repo.

## File the job

Create an issue with **the label `exec-job`**, a short title, and a body that is
YAML frontmatter:

```
---
argv: ["uname", "-a"]
rule: drain-on-wake
queued_at: 2026-01-15T09:30:00Z
ttl: 3600
---
```

| Field | Required | Meaning |
| ----- | -------- | ------- |
| `argv` | yes | The command, as a **JSON array of strings, on one line**. Not a shell string. `argv[0]` is the program; there is no shell, so no pipes, globs, `&&`, or `~`. |
| `rule` | no | `drain-on-wake` (default), `drop-if-stale`, or `alert`. |
| `queued_at` | with `ttl` | UTC, `YYYY-MM-DDTHH:MM:SSZ`. |
| `ttl` | no | Seconds. Combined with `queued_at`, defines the window. |

Rules:

- `drain-on-wake` - runs whenever the Mac next comes back, however late. Use
  this for anything idempotent. It is the default and usually the right answer.
- `drop-if-stale` - if `ttl` seconds have passed since `queued_at`, it is not
  run. Use this for anything time-sensitive, where doing it late is worse than
  not doing it.
- `alert` - same window as `drop-if-stale`; a miss is marked for the operator's
  attention rather than quietly skipped.

The body is also accepted inside a code fence, or bare with no `---` delimiters.
Frontmatter written normally is the safe form.

### The label is the doorbell

**An issue without the `exec-job` label is invisible to the poller.** It will sit
open forever and nobody will tell you. If you file the issue and add the label in
a second API call, check that the second call succeeded. Most "the bridge is
broken" reports are a missing label.

## Get the result

Within about **60 seconds** (one poll cycle; longer if the Mac is asleep or
logged out), the poller:

1. comments on your issue with the exit code, duration, stdout and stderr,
2. adds `exec-done` (exit 0) or `exec-failed` (anything else),
3. closes the issue.

So: poll your own issue. It is done when it is closed. Read the label for the
verdict and the last comment for the output. Output is truncated near 58,000
characters, with a marker saying how much was dropped - if you need more, have
the command write a file and fetch it in a second job.

### When it does not run

You get a comment and an `exec-failed` close in every case, never silence:

- **denied** - `argv[0]` is not on the operator's allowlist. This is **final**.
  Refiling the identical command gets the identical denial. Ask the operator to
  widen the allowlist; do not retry, and do not go looking for a way around it.
- **not run, did not parse** - `argv` was not a JSON array of strings.
- **not run, missed its window** - a `drop-if-stale` or `alert` job past its ttl.

If the issue is still open after several minutes: the Mac is off, asleep, or
logged out, or the label is missing. All of those are the operator's to fix.

## Things that will bite you

**Terminal panes do not survive a reboot.** If your command drives a terminal
multiplexer or pane manager, pane ids are not stable across reboots - the id you
saved yesterday may not exist, or may now be someone else's pane. Never hardcode
a pane id across jobs. Create-or-reuse the workspace **inside the same job** that
uses it, capture the id from that job's output, and use it only within that job.

**A command that returns no output proves nothing.** Some pane-driving commands
(for example `herdr pane run`) print nothing at all on success. Do not read an
empty stdout as "it worked". Follow it with an explicit read (`herdr pane read`)
in the same job, and check that.

**Keep titles short.** Result artifacts are named from the issue title and get
truncated around 48 characters. Long titles collide with each other. A short
title plus a descriptive body is better in every way.

**One command per issue.** There is no shell, so there is no chaining. Sequence
work as separate issues, or as one script that is itself on the allowlist.

**Check before you assume state.** The Mac may have rebooted, slept, or had
files change since your last job. If a job depends on state from an earlier job,
verify that state in the same job that relies on it.

## Full example

Title: `check site build`

Label: `exec-job`

Body:

```
---
argv: ["git", "-C", "/Users/operator/code/site", "pull"]
rule: drop-if-stale
queued_at: 2026-01-15T09:30:00Z
ttl: 1800
---
```

Then poll issue #N until it closes. `exec-done` plus the comment is your answer.
