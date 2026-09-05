# issue-bridge

Let a cloud AI agent run commands on your Mac, without opening a port.

The agent files a GitHub issue. A small poller on your Mac picks it up, runs the
command if it is on your allowlist, comments the output back, and closes the
issue. That is the whole system: one Python file, one LaunchAgent, one repo.

```
   agent (anywhere)                 GitHub                  your Mac
  ------------------            --------------        ---------------------
   files an issue      ------>   issue #42
   labelled exec-job             label: exec-job
                                        |
                                        |  poll every 60s (outbound https)
                                        |<---------------------  bridge-poller
                                        |                             |
                                        |                     allowlist check
                                        |                             |
                                        |                        run argv
   reads the result    <------   comment: stdout/stderr  <-----  post + close
                                 label: exec-done                     |
                                 issue closed                    status.json
```

The Mac only ever makes outbound https requests to `api.github.com`. No inbound
port, no tunnel, no VPN, no dynamic DNS, nothing to forward on your router. If
your laptop can browse the web, the bridge works.

Any agent that can open a GitHub issue can drive it. There is no SDK: the
protocol is an issue body, and it is written down in [AGENTS.md](AGENTS.md).

## Quickstart

1. **Pick a repo to use as the lane.** A dedicated private repo is best. Anyone
   who can file an issue on it can run your allowlisted commands, so the repo's
   collaborator list *is* the guest list. See [Security](#security).

2. **Create a fine-grained personal access token.** GitHub → Settings →
   Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token. Scope it to **only that one repository**, with:

   | Permission | Access |
   | ---------- | ------ |
   | Issues     | Read and write |
   | Contents   | Read and write |
   | Metadata   | Read (GitHub forces this on) |

   Set an expiry you will actually notice. The poller records the expiry date in
   `~/.config/issue-bridge/status.json` so you can check it before it bites.

3. **Install.**

   ```sh
   git clone <this repo> issue-bridge && cd issue-bridge
   ./install.sh
   ```

   It asks for the repo and the token, writes `~/.config/issue-bridge/`
   (token at mode 600), loads the LaunchAgent, creates the `exec-job`,
   `exec-done` and `exec-failed` labels, then files a real `uname -a` job and
   waits for it to come back. It prints `SUCCESS` when the round trip closes
   `exec-done`. That success line is the verification test - if you see it, the
   bridge works end to end.

4. **Widen the allowlist.** It ships as `["uname"]` and nothing else runs. Edit
   `allow` in `~/.config/issue-bridge/config.json`:

   ```json
   {
     "repo": "you/your-lane-repo",
     "label": "exec-job",
     "allow": ["herdr", "git -C /Users/you/code/site", "uname"],
     "poll_interval": 60
   }
   ```

   Each entry is a **token prefix** of the argv it permits. `"herdr"` allows
   every `herdr` subcommand. `"git -C /Users/you/code/site"` allows `git` only
   against that one checkout - `git -C /etc pull` is denied. Matching is on
   whole tokens, so `uname` never matches `unamex`. The config is re-read every
   cycle; no restart needed.

5. **Hand your agent [AGENTS.md](AGENTS.md).** Paste it into the agent's system
   prompt, drop it in its repo, or just link it. Tell the agent which repo is
   the lane. That is the entire integration.

## Operating it

- **Is it alive?** `cat ~/.config/issue-bridge/status.json` - written every
  cycle, with `last_poll_ok`, `queue_depth`, `last_error` and `pat_expiry`.
- **Logs:** `~/.config/issue-bridge/poller.log`.
- **Restart:** `launchctl kickstart -k gui/$(id -u)/bridge.poller`.
- **Remove it:** `./uninstall.sh` (leaves the repo and its issues alone).

### Crashes and reboots

`KeepAlive` plus `ThrottleInterval 10` is the whole crash story: kill the
poller and launchd restarts it about ten seconds later. You do not need a
supervisor.

Reboots are different, and worth being blunt about. This is a **LaunchAgent in
the gui domain**, so it starts **at login, not at boot**. A Mac that reboots and
sits at the login window is not running the bridge. If you log out, the bridge
stops. If you want it to survive an unattended reboot, enable automatic login,
or move the job to a LaunchDaemon and accept that it then runs as root - which
is a much larger blast radius than this design assumes.

## Security

**The boundary is "can file an issue on the designated repo."** That is it.
Anyone with issue-write on that repo - collaborators, org members with access,
any bot or agent holding a token for it - can run anything your allowlist
permits. Treat it exactly like handing out shell access, scoped to the
allowlist.

- **Use a dedicated private repo** as the lane, especially if the allowlisted
  commands are powerful. Some tools grant far more than they look like they do:
  a terminal-driving CLI on your allowlist is effectively full access to your
  drive and everything logged into on it.
- **The allowlist caps accidents, not attackers.** It stops a confused agent
  from running the wrong thing. It is not a sandbox, and a command that itself
  takes arbitrary input (a shell, an interpreter, `ssh`) hands the whole boundary
  away. Do not allowlist those.
- **Denials are final and visible.** A denied job gets a comment saying it was
  denied and closes `exec-failed`. Nothing is ever dropped silently, so a wrong
  allowlist shows up as a closed issue, not as a mystery.
- **Side effects never run twice.** The poller removes the `exec-job` label
  before running the command. If it crashes mid-job you may lose the *output*,
  but the command does not re-run when it restarts.
- **The token is write-scoped to one repo**, kept at mode 600, and never
  printed, logged, or passed on a command line. Revoke it on GitHub to kill the
  lane instantly.
- **Everything is auditable after the fact.** Every job is an issue with a
  comment: what was asked, what ran, what came back, when it closed.

## Requirements

macOS with `python3` and `curl` (both ship with the Xcode command line tools).
No pip installs, no dependencies. Python standard library only.
