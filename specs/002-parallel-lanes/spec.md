# 002 - Parallel lanes

## Problem
The bridge runs one job at a time. A long job (a `claude -p` run, up to the 900 s timeout)
holds every job behind it, including canaries, health checks and quick reads
(2026-09-26: a notary-letter check waited ~10 min behind a long job).

## Goal
Jobs filed under different lane labels never wait on each other. Order within a lane stays
serial, so a filer that needs ordering keeps it by using one label.

## Design
- Config `lanes`: the labels to serve, e.g. `exec-job, exec-fast, exec-deep-1..3`.
  Default is `[label]`, which is today's behaviour.
- One poller process, one GitHub poll per cycle across all lane labels (plus the doorbell's
  directly fetched issues). Each issue belongs to the lane whose label it carries; the
  legacy `am-exec` label belongs to `exec-job`.
- Dispatch: the main loop hands an issue to its lane's worker thread only if that lane is
  idle and the issue is not already in flight. A worker runs the existing `handle()`
  unchanged, with that lane's label, then rings the loop so the lane's next job starts
  immediately.
- No claim step: a label is read by exactly one lane and a lane runs one job at a time, so
  two workers cannot take the same issue. Two poller processes on one repo still can; that
  was already true and still is not supported.
- Config `lane_timeouts` (seconds per label). `exec-fast: 60` keeps long work out of the
  fast lane: past it the tab is closed like any timeout.
- Journal recovery (`drain_pending`) runs on the main loop and skips issues that are in
  flight, so it never resumes a job a worker is still running.
- `status.json`: `running` becomes a map of lane -> job; `queue_depth` stays the total.

## Out of scope
Assigning labels (the filer does that, e.g. round-robin across deep lanes); more than one
poller process; changing the allowlist.

## Tasks
1. Poll across all lane labels; tag each issue with its lane.
2. Dispatcher with per-lane worker threads and an in-flight set; drain skips in-flight.
3. Per-lane timeout; thread-safe status writes; `running` per lane.
4. Self-test: two lanes run concurrently, one lane stays serial, drain skips in-flight.
5. Config: the five lanes, `exec-fast: 60`. Create the labels on the repo.
6. Verify live: a long job in one lane does not delay a job in another.
