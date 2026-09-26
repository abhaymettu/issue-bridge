# 001 - Webhook doorbell

## Problem
Instinct's direct-line tool is a per-environment grant and disappears when Instinct's
environment is rebuilt (2026-09-26 12:16 CT; also Sep 15-16). Its GitHub connection is
account-level and survives rebuilds, so the issue bridge is the durable route. Its cost is
latency: a job waits up to one poll interval before pickup.

## Goal
Pick up an `exec-job` issue within ~1-2 s of it being opened, without widening what the
bridge will run or trusting anything new.

## Design
The webhook is a doorbell, not a job channel.

- GitHub sends `issues` events for the bridge repo to `https://hook.abhaymettu.com/github-hook`.
- Cloudflare tunnel routes that hostname to `127.0.0.1:<webhook_port>` (not behind Access:
  GitHub cannot pass Access; the HMAC is the gate).
- The poller runs a tiny HTTP listener on that port. A POST with a valid
  `X-Hub-Signature-256` (HMAC-SHA256 over the raw body, secret in
  `~/.config/issue-bridge/webhook-secret`, mode 0600) sets a wake event and returns 204.
  Anything else returns 401/404. The payload is never parsed for commands.
- GitHub's label-filtered issue list lags a new issue by 10 s or more, so the only thing
  read from a signed payload is `issue.number`; poll() fetches that issue directly and
  applies the usual label, state and allowlist checks to the API's copy.
- The main loop waits on that event with `poll_interval` as the timeout, so a ring runs the
  normal poll immediately; the existing GitHub fetch, allowlist, and staleness checks decide
  what runs. Polling continues unchanged as the backstop.

## Constraints
- Off unless `webhook_port` is set in config. No new dependencies (stdlib only).
- Listener binds 127.0.0.1 only. Body capped at 1 MiB. Constant-time signature compare.
- A flood of valid or invalid requests can at most cause back-to-back polls; wakes coalesce.
- Missing/unreadable secret: listener refuses every request, poller keeps polling.

## Out of scope
Running jobs from the payload; an MCP server; changes to the direct line.

## Tasks
1. Poller: wake event, listener thread, HMAC check; main loop waits on the event.
2. Self-test: signature accept/reject, wake is set only on a valid ring.
3. Config: `webhook_port: 8791`; generate the secret (0600).
4. Cloudflare (dashboard, Abhay): public hostname `hook.abhaymettu.com` -> `http://localhost:8791`.
5. GitHub: repo webhook, `issues` events, JSON, the secret.
6. Verify: GitHub ping delivers 204; a test issue is picked up in < 5 s.
