# Orca Harness Rework - Active Minimum Plan

**Revised September 21, 2026 after scope cut**

## Goal

Prove the smallest complete path first:

`durable event + Orca gate -> visible Orca lane -> stop/input hooks + repo verifier -> terminal state -> delivery -> cleanup -> task_done`

The runtime remains Orca. H1A-min bundles the ledger and `orca-gate` as the first working slice. The gate canary runs the day H1A-min through H1D land. Atlas verification builds in parallel. Everything else is frozen unless a failure in this path proves it is needed.

## Principles that remain binding

1. **Build the smallest working path first.** Prove the door, then harden it.
2. **No prose-only completion.** `turn-ended`, idle prompt, worker text saying "done," shell return, or missing heartbeat cannot terminalize or notify.
3. **Delivery is completion.** Only a verified delivery receipt can emit `task_done`.
4. **Evidence is append-only and source-attributed.** Backfills and imports cannot overwrite history or impersonate authoritative sources.
5. **Repository identity is pinned.** Every event includes canonical repo realpath and delivery remote. Branch names are never searched in an assumed repo.
6. **Every patch gets a regression fixture.** Discovered failures become tests rather than reminders.

All substantive work remains visible in Orca. Fixes remain source-controlled, restart-safe, and tested. Do not report a start until a lane is visibly running. Do not report completion before delivery.

## State machines

Runtime:

`accepted -> dispatched -> running -> stop_observed -> verifying -> terminal_succeeded | terminal_failed | terminal_blocked`

Delivery:

`undelivered -> delivery_pending -> delivered | delivery_failed`

Cleanup:

`not_eligible -> eligible -> cleaned | cleanup_blocked`

These remain separate. Terminal does not mean delivered; delivered does not silently imply cleaned.

# Active build

## 1. H1A-min - Event ledger plus Orca gate

Build on the existing FirstMate journal substrate at commit `e029b73ec7424beb61f3c57ba3a8cc11bb5f08bf`. Keep its append writer, global monotonic sequence, hash-chain substrate, transition idempotency, and non-consuming reads. Add only what the first path needs.

### Minimum event contract

Every event has:

- `event_id`
- `task_id`
- `correlation_id`
- `seq`
- `event_type`
- `writer_source`
- `source_ts`
- `ingest_ts`
- canonical repository realpath
- delivery remote
- Orca native IDs
- branch
- HEAD
- artifact
- verification
- supersedes

No trust classes, quarantine cohorts, checkpoint system, tamper audit, replay-from-zero work, storage immutability project, signatures, or broader precedence framework in the active build.

### Minimum Orca gate

The gate is part of H1A-min, not a later transport project. It exposes only:

- `health`
- `dispatch`
- `status`
- `wait`
- `reply`
- `cancel`

Requirements:

- `dispatch` returns a real durable task ID in under five seconds.
- The lane starts with `/goal` set from the task.
- Stop and notification hooks append `needs_input` events when worker input is required.
- `wait` returns on `needs_input` as well as verified terminal outcomes.
- `reply <task_id> "text"` sends the next prompt to that task's lane and appends the reply to the ledger.
- Every reply is logged with task/correlation identity and timestamps.
- `needs_input` reaches Instinct through `wait`; it is not sent directly to the user by the worker or gate. Instinct decides what to ask and routes the answer back with `reply`.
- No arbitrary shell or unregistered task/repository routing is accepted.

### Terminal rule

The trusted stop hook plus attached repository-verifier output is the only route to terminal state.

Terminalization requires:

- matching task/correlation ID;
- matching repository, delivery remote, Orca IDs, branch, and HEAD;
- real runtime stop evidence;
- artifact attached and valid;
- verification output attached, passing, and bound to the same repo/HEAD/artifact.

The stop hook proposes `stop_observed`; the verifier completes the terminal transaction. Missing, failed, stale, unrelated, or unattached verification blocks terminal success. Worker prose, turn-ended, shell return, UI labels, and missing heartbeat cannot terminalize.

### Minimum fixtures

- stop plus matching artifact and verifier can terminalize;
- stop without verifier cannot terminalize;
- verifier without matching stop cannot terminalize;
- wrong repo, branch, HEAD, Orca IDs, or artifact fails closed;
- duplicate transition deduplicates;
- supersession appends and does not overwrite the original;
- `turn-ended` and `done_reported` cannot terminalize or notify.

### Branch

Active branch: `abhaymettu/orca-harness-rework`

Active worktree: `/Users/abhay/orca/workspaces/issue-bridge/orca-harness-rework`

## 2. H1B-min - Resume and park

On controller, Orca, login-session, or Mac restart:

1. Load nonterminal tasks.
2. Reconcile exact project/worktree/terminal/run/PID against native Orca state.
3. Resume monitoring if the worker is verified alive.
4. If the endpoint is missing or dead, park it with a snapshot.

The parking snapshot includes:

- repository realpath and delivery remote;
- Orca IDs;
- branch and HEAD;
- dirty and untracked state;
- diff summary and commits versus base;
- remote reachability;
- artifact and verification status;
- last heartbeat/activity;
- recovery attempts observed before parking.

That is all for the active build. There is no policy-driven recreate or automatic resume/rebuild path. Dead lanes park and remain visible.

### Minimum fixtures

- controller restart retains nonterminal tasks;
- verified live worker resumes monitoring;
- dead lane parks with the full snapshot;
- restart causes neither duplicate dispatch nor false terminal state.

## 3. H1C-min - Preserve and clean

When a worker emits `done_reported`:

1. Append it as a nonterminal event.
2. Snapshot repo/worktree/artifact/verification state.
3. Push current work to a task-namespaced remote ref.
4. Verify that the pushed commit is reachable from that remote ref.

If push or reachability fails, keep the task blocked and the worktree parked. Do not terminalize, deliver, clean, or notify.

No protection leases, repository rulesets, dynamic protected-ref framework, or long-term preservation service in the active build.

Cleanup is allowed only after `delivered`. It also requires matching identity, retained artifact and proof, a clean/captured worktree, and verified remote reachability. The cleanup receipt records the exact terminal/tab closed, worktree removed, branch/ref disposition, and independent Orca plus Git readback.

### Minimum fixtures

- `done_reported` pushes a task ref and remains nonterminal;
- push or reachability failure blocks and parks;
- cleanup before delivered is denied;
- delivered work with safe reachability can clean exactly once;
- dirty or identity-mismatched work refuses cleanup.

## 4. H1D - Delivery is the only done source

A delivery contract is declared at task creation:

- code: PR/merge and required CI/release state;
- site: deployment, live URL, HTTP and pixel proof;
- read-only work: explicit report/file handoff;
- other artifact: named retained destination and receipt.

Rules:

- A branch or worker report is not delivery.
- Delivery writes real destination IDs/URLs, hashes, commit/merge/deploy identity, and proof references.
- Done text carries the PR or delivery URL plus proof.
- Only `delivered` may emit `task_done`.
- Terminal-but-undelivered emits no done.
- Delivery failure remains visible and emits no success.
- Replay emits exactly one done event.

### Minimum fixtures

- unmerged code cannot emit done;
- terminal and undelivered cannot emit done;
- verified delivery emits one done event with URL and proof;
- stale or unrelated verifier proof cannot satisfy delivery.

## 5. Gate canary - The day H1A-min through H1D land

Run one harmless registered-repository task from the scratch side through the real `orca-gate` path.

One correlation chain must contain:

1. **Dispatch receipt** - durable task ID returned in under five seconds, measured start latency, `/goal` set from the task, and real Orca native IDs independently readable through status.
2. **Wait receipt** - returns only after stop hook, artifact, and repository verifier produce terminal state; includes artifact and proof.
3. **Delivery/cleanup/done receipt** - delivered destination, safe cleanup with independent readback, and one Mac-originated `task_done` carrying the same task/correlation ID, delivery URL, and proof.

Controls:

- duplicate dispatch deduplicates;
- disconnect after submit recovers by task ID;
- timeout does not mark failure;
- `needs_input` wakes `wait`, does not contact the user directly, and a logged `reply` resumes the same task;
- wrong task/repo/caller is denied;
- dirty worktree refuses cleanup;
- no Mac click or focus steal is needed.

The canary is the first full receipt. It is not deferred behind H2-H7 work.

## 6. Atlas verifier - Parallel track

Add a checked-in deterministic verifier CLI and versioned feature map to Atlas.

The feature map names every dashboard feature/pane, how to reach it, prerequisites, expected state, and required proof.

The CLI must:

- load the real dashboard;
- enumerate every pane from the feature map;
- capture and inspect a screenshot of every pane;
- run colorblind-accessible contrast checks;
- check expected data and freshness indicators, not only shell rendering;
- emit machine-readable results and a manifest linking each feature to proof;
- bind output to repo, branch, HEAD, invocation, timestamps, and artifact hashes.

This output feeds the H1A-min `verification` field. Required skipped checks fail unless the task contract explicitly permits the skip.

abhaymettu.com verification and agentSEM verification remain in the verification track as principles, but they are not active before the first canary. Add them after the canary or when the active path requires them; agentSEM still waits until the simulator is runnable.

# Migration section

Migration remains part of the plan but is limited to what the first path needs.

- Orca-native lifecycle becomes authoritative.
- Existing FirstMate `e029b73e` journal code is reused for H1A-min rather than rewritten.
- Historical Herdr/FirstMate rows are evidence only and cannot set current terminal or delivered state.
- Sep 20 16:11 synthetic busy-state files remain untrusted and are not consumed as task truth.
- Sep 20 23:42 mass disappearance is treated as a server shutdown/restart incident, not task completion.
- Seven historical prose-done rows remain nonterminal.
- Corrected repository-pinned branch finding is the fixture: 7/7 named branches exist; four are local-only/unpublished. Do not push or merge those historical branches without a separate delivery decision.
- Existing surfaces may be replaced later only after the minimum path has receipts and no task/artifact/branch becomes unreachable.

No full historical importer, replay parity system, quarantine framework, or component deletion project is active before the canary.

# Verification track principles

Each supported repository will eventually receive:

1. a checked-in deterministic verification CLI;
2. a versioned feature map describing each feature and how to reach it;
3. machine-readable results plus retained human-inspectable proof;
4. a stable invocation usable by workers, hooks, CI, and delivery.

`verification` remains adjacent to `artifact` in ledger events. Proof must bind to the exact task, repo, branch/HEAD, artifact, and verifier/feature-map version. Visual work requires pixel inspection, not file or DOM existence alone. Done text carries delivery URL and proof.

Only Atlas is active in parallel before the canary.

# Corrections-as-code registry

Repeated user corrections become enforcement rather than chat memory.

When the user complains twice about the same agent behavior, create an enforcement ticket. Keep a short checked-in registry containing:

- the behavior and rule;
- first and second occurrence evidence;
- scope;
- enforcement ticket;
- hook, lint, policy check, or verifier that prevents recurrence;
- regression fixture;
- status and any exact exception.

A ticket closes only when the behavior is mechanically blocked or reliably detected, the persistent enforcement is installed, and a regression fixture passes. A reminder or prompt edit is not closure. `comment-gate` is the model.

Do not convert a single in-the-moment design opinion into a permanent rule without the required repeated evidence.

# Runtime and usage policy

- Orca remains the runtime. The scope cut changes how much is built around it, not where work runs.
- No hard spending or lane cap. Spend by judgment.
- Run one harness implementation lane at a time.
- Use the cheapest adequate model for read-only inventory and verifier steps.
- Keep agentSEM, applications, and Tsenta moving; the harness may not crowd them out.
- Atlas verifier may run in parallel as specified.

# Later, only if a failure demands it

The following are frozen until the gate canary passes. After that, add one only when an observed failure or concrete requirement demands it:

- H2 policy/capability/sandbox expansion;
- H3 generalized signed secret broker;
- H4 dynamic admission, lane doctor, and resource governor;
- H5 generalized verification/release platform beyond the minimum delivery path;
- H6 Jev decision kernel and all further Jev work;
- H7 transport comparison, hardening, and extended soak beyond the minimum Orca gate;
- trust classes and source-precedence framework;
- quarantine cohorts;
- checkpoint/backup/tamper-audit system;
- replay-from-zero and broad historical importer;
- protection leases, dynamic rulesets, and preservation service;
- policy-driven lane recreation;
- abhaymettu.com and agentSEM verifier implementations before the first canary.

The existing Jev 41/41 result is recorded but does not justify more work. No Jev work resumes until the canary passes.

# Immediate receipts owed

1. Completed H0 inventory receipt, not merely a live-lane receipt.
2. Live interview-prep page URL and its deployment/proof receipt.
3. H1A-min completion time and branch receipt.

Known H1A-min branch: `abhaymettu/orca-harness-rework`.

A clock completion time must come from the live worker's current state; it is not guessed from a stale screen.
