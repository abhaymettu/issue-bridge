# AGENTS.md - run a command on the operator's Mac through a GitHub issue

You can run one allowlisted command on a specific person's Mac by creating a
labelled GitHub issue. A poller on that machine watches one designated repo,
runs the command, and answers in the issue. This is an asynchronous request and
reply, not a shell: one command per issue, one round trip.

## Before you file anything

Ask the operator for:

- the lane repo, as `owner/name`,
- the job label, normally `exec-job`,
- the command argv or wrapper scripts they have allowlisted,
- any absolute paths those commands need.

You need GitHub access that can create issues, **apply the label**, and read
comments on that repo. Creating an issue without successfully applying the label
does not queue anything.

There is no API for reading the machine's allowlist. Do not probe for one, and
do not look for ways around a denial.

## File one command job

Create an issue on the lane with the label `exec-job`, a short title, and a body
that is YAML frontmatter:

```
---
argv: ["uname", "-a"]
---
```

That example matches a fresh install's allowlist.

| Field | Required | Meaning |
| ----- | -------- | ------- |
| `argv` | yes | The command as a **JSON array of strings**, one line. Not a shell string. |
| `rule` | no | `drain-on-wake` (default), `drop-if-stale`, or `alert`. |
| `queued_at` | with `ttl` | UTC, `YYYY-MM-DDTHH:MM:SSZ`. |
| `ttl` | no | Seconds from `queued_at`. Governs whether the job *starts*, not its runtime. |

There is no shell, so pipes, `&&`, globs, `$VARS` and `~` are not expanded by
the bridge. Use the absolute paths the operator gave you.

The body looks like YAML but is parsed as plain `key: value` lines, not real
YAML. Do not use multi-line arrays or YAML-quote the scheduling values. The same
lines are also accepted inside a code fence or bare with no `---` delimiters;
frontmatter is the form to write.

Over the REST API:

```
POST /repos/OWNER/REPO/issues
```

```json
{
  "title": "check Mac",
  "labels": ["exec-job"],
  "body": "---\nargv: [\"uname\", \"-a\"]\n---"
}
```

Keep the returned issue number. The title is for the human reading the queue;
nothing is named after it.

### The label is the doorbell

**An issue without the job label is invisible to the poller.** It sits open
forever and nobody tells you. If you create the issue and add the label in a
second call, check that the second call succeeded. Most "the bridge is broken"
reports are a missing label.

## Scheduling fields, if the job is time-sensitive

Omitted, `rule` is `drain-on-wake`: the job runs whenever the poller next
reaches it, however late, and `ttl` is ignored. That is the right default for
anything idempotent.

For a job that is worse late than not at all, add before the closing `---`:

```
rule: drop-if-stale
queued_at: CURRENT_UTC_TIMESTAMP
ttl: 1800
```

Replace `CURRENT_UTC_TIMESTAMP` with the real time when you file it, as
`YYYY-MM-DDTHH:MM:SSZ`. Never submit the placeholder and never copy a timestamp
from an example or an earlier job: a past `queued_at` makes the job dead on
arrival.

`alert` is currently identical to `drop-if-stale`: it posts a missed-window
comment and closes `exec-failed`. There is no notification, escalation, or
attention label beyond ordinary GitHub behaviour. Do not rely on one.

**Bad window data does not fail closed.** A missing, malformed, or unparseable
`queued_at`, and an unrecognised `rule` name, all leave the job eligible to run.
Validate time-sensitive fields yourself before you file.

## Read the result

Poll the issue you created, not the whole lane:

```
GET /repos/OWNER/REPO/issues/NUMBER
GET /repos/OWNER/REPO/issues/NUMBER/comments
```

Normal completion, in this order:

1. the poller comments the exit code, duration, stdout and stderr,
2. it adds `exec-done` (exit 0) or `exec-failed` (anything else),
3. it closes the issue.

Read the poller's result comment, not simply the newest one, and look for the
result label. A closed issue is not by itself proof of execution: people close
issues too.

The default cycle is 60 seconds between polls, not a promise that your job
finishes within a minute. Jobs run one at a time in the order they were created,
at most 30 per cycle, so a long earlier command delays yours. A single command
may run for up to 900 seconds.

A timeout or a failure to start the program reports `exit: null` with the reason
in stderr. Timeouts keep no partial output, and processes the command spawned
are not guaranteed to have stopped.

Combined stdout and stderr are truncated near 58,000 characters with a marker
saying how much was dropped. There is no artifact, no file download, and no
second-job trick that gets around it: printing the same file again hits the same
budget. If you need more, ask the operator for an allowlisted way to write the
output somewhere and read it back in bounded pieces.

## When it does not run, and when you cannot tell

- **denied** - the argv did not match any allowed token prefix. The operator
  allowlists whole prefixes, so this can be about any token, not only the
  program name. It is **final** for that config: refiling the same argv gets the
  same answer. Ask the operator to widen `allow`. Do not retry and do not look
  for a bypass.
- **did not parse** - `argv` was not a non-empty JSON array of strings on one
  line. Fix it and file a new issue.
- **missed its window** - the command did not run. Confirm the work is still
  wanted before refiling it with a fresh deadline.
- **non-zero exit, timeout, or start failure** - read stderr and check the
  relevant state. A failure does not prove nothing happened.

**An open issue with the label removed is ambiguous.** The poller drops the
label before it starts the command, so that issue is either running now or was
stranded by a crash or an API failure after the command already ran. Do not
reapply the label and do not file a replacement: ask the operator to look at the
issue and at `~/.config/issue-bridge/status.json`.

An issue still open **with** its label means the poller has not reached it: the
Mac is asleep, logged out or off, there is a backlog, or the poller is stopped
or cannot reach GitHub. Those are the operator's to fix.

## Multi-step work and stateful tools

One issue is one command. The bridge cannot chain commands or feed one
command's output into the next inside a job.

Sequence independent steps as separate issues, waiting for each result. When
several steps must share state, or when state has to be checked immediately
before it is used, ask the operator for an allowlisted wrapper that does the
whole sequence in one invocation.

**One workspace per job.** If a command drives a terminal multiplexer, pane
manager or interactive agent, that job should create its own fresh workspace and
capture the id from the tool's own output, inside the one invocation. Pane ids
do not survive a reboot: one saved yesterday may be gone, or may now belong to
something else. Never carry a pane id between jobs and never write one into an
issue body. The create, act, and read-back steps belong inside one wrapper
invocation, because the bridge cannot sequence them for you.

**No output proves nothing.** Some pane-driving commands (for example
`herdr pane run`) print nothing on success. Exit 0 means the process you invoked
succeeded, not that the work it handed off elsewhere did. Use the tool's own
read-back or status operation, in the same wrapper, when it matters.

## If your own browser is capped

When your browsing is rate-limited or unavailable, ask the operator whether the
lane allows a browser command on the Mac. If it does, one job is one page: the
argv drives their browser wrapper and the text comes back in the result comment.

Do not assume such a command exists, and if the argv is denied, do not look for
another way to reach that browser. Send the URL and say what to extract, keep
the output well under the 58,000-character budget, and treat the browser as
signed in as the operator: nothing you would not want in the lane's issue
history should be visited or printed.
