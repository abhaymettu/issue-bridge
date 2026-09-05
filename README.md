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

The bridge itself only ever makes outbound https requests to `api.github.com`.
No inbound port, no tunnel, no VPN, no dynamic DNS, nothing to forward on your
router. It does need working GitHub API access and valid credentials, which is
more than "the laptop can browse the web", and the commands you allow may make
network connections of their own.

Any agent that can open a GitHub issue can drive it. There is no SDK: the
protocol is an issue body, and it is written down in [AGENTS.md](AGENTS.md).

## Quickstart

1. **Pick a repo to use as the lane.** A dedicated private repo is best. Anyone
   who can put the job label on an issue there can run your allowlisted
   commands, and anyone who can edit an eligible issue's body can change what
   runs. See [Security and delivery limits](#security-and-delivery-limits).

2. **Create a fine-grained personal access token.** GitHub → Settings →
   Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token. Scope it to **only that one repository**, with:

   | Permission | Access |
   | ---------- | ------ |
   | Issues     | Read and write |
   | Metadata   | Read (GitHub forces this on) |

   The bridge never touches repository contents; do not grant Contents.

   Set an expiry you will actually notice. The poller records the expiry date in
   `~/.config/issue-bridge/status.json` so you can check it before it bites.

3. **Install.**

   ```sh
   git clone https://github.com/abhaymettu/issue-bridge.git
   cd issue-bridge
   ./install.sh
   ```

   It asks for the repo and the token, writes `~/.config/issue-bridge/`
   (token at mode 600), loads the LaunchAgent, creates the `exec-job`,
   `exec-done` and `exec-failed` labels, then files a real `uname -a` job and
   waits up to three minutes for it to come back. `SUCCESS` means the installer
   saw the `exec-done` label land, which is the round trip working. Open the
   issue and confirm it also has an output comment and is closed; the installer
   does not check those.

   Reinstalling keeps an existing `config.json` but replaces the token, and the
   check job always uses the repo you just typed and the `exec-job` label. If
   the kept config points somewhere else, the check tests a lane the poller is
   not watching.

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
   every `herdr` subcommand. Matching is on whole tokens, so `uname` never
   matches `unamex`. The config is re-read at the top of every cycle, so a change
   needs no restart, but it does not reach a batch already being processed.

   `"git -C /Users/you/code/site"` matches only argvs opening with those exact
   tokens, so `git -C /etc pull` is denied. It does not confine git to that
   checkout: later options, subcommands, hooks and config still apply. When an
   operation needs real constraints, allowlist a wrapper script you wrote, not a
   prefix of a general-purpose tool.

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
stops. To survive an unattended reboot, either enable automatic login and accept
what that costs, or run it as a LaunchDaemon instead. A LaunchDaemon is a
separate deployment this installer does not write; it can be pointed at a
non-root `UserName`, so it does not have to mean running commands as root.

## Security and delivery limits

**The boundary is an open issue carrying the job label on the designated repo.**
The poller checks neither who opened the issue nor who applied the label. Anyone
who can label an issue there, or edit the body of one waiting to run, can run
anything your allowlist permits. Treat it exactly like handing out shell access,
scoped to the allowlist.

- **Use a dedicated private repo** as the lane, especially if the allowlisted
  commands are powerful. Some tools grant far more than they look like they do:
  a terminal-driving CLI on your allowlist is effectively full access to your
  drive and everything logged into on it.
- **The allowlist caps accidents, not attackers.** It stops a confused agent
  from running the wrong thing. It is not a sandbox: commands run as you, with
  your files and your credentials, and a command that itself takes arbitrary
  input (a shell, an interpreter, `ssh`) hands the whole boundary away. Do not
  allowlist those.
- **Denials are final and visible.** A denied job gets a comment saying so and
  closes `exec-failed`, so a wrong allowlist shows up as a closed issue rather
  than as a mystery.
- **Side effects are unlikely to run twice, not guaranteed not to.** The poller
  drops the label before running, which is what stops a restart from re-running
  a command. It is not a lock: two pollers on one lane, a relabel, or a refiled
  issue will each run the command again. Run one poller per lane.
- **A crash can leave no result at all.** The order is comment, then result
  label, then close. A crash or an API failure part way through leaves an open,
  unlabelled issue and possibly no record of what happened, even though the
  command ran. Do not retry a side-effecting job whose outcome is unknown.
- **The token is scoped to one repo**, kept at mode 600, and never printed,
  logged, or passed on a command line. Revoking it stops all further API calls,
  but not a command already running. Commands running as you can read the file.
- **Issues are a good work record, not an audit log.** They are editable, and
  the crash case above means a job can run without leaving one. Do not put
  secrets in an issue body or print them into a result.

## Requirements

macOS with `python3` and `curl` (both ship with the Xcode command line tools).
No pip installs, no dependencies. Python standard library only.
