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

## Callers

Anything that can open an issue and put the label on it can drive the bridge:
Instinct, Claude Code, a ChatGPT or Gemini agent, a script, or you in the GitHub
web UI. Those are callers, not integrations. Nothing here is bundled with any of
them, and a chat session with no GitHub access cannot file a job on its own.

There is no SDK. The protocol is an issue body, and it is written down in
[AGENTS.md](AGENTS.md).

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
   waits up to three minutes for it to come back. `SUCCESS` means that job ran,
   commented its output and closed `exec-done`: the whole round trip, not one
   label.

   Reinstalling keeps an existing `config.json` and replaces the token. If that
   config names a different repo, the installer stops rather than verify a lane
   the poller is not watching.

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

## What the command runs in

There is no shell. The poller starts the argv directly, so pipes, `&&`,
redirection, globs, `~` and `$VARS` are not expanded by the bridge; they arrive
as literal argument strings.

Commands run as the logged-in user, in whatever environment launchd hands the
LaunchAgent. The installer sets no working directory and no `PATH`, so an
interactive shell's `PATH`, aliases and startup files are not there. Use
absolute paths for executables and files, and make the allowlist entry match the
argv the caller will actually send.

Each command gets 900 seconds. On a timeout the result is `exit: null` with a
`timeout after 900s` note and no partial output; anything the command already
printed is lost. The timeout kills the command, not necessarily the processes it
spawned.

### Queue timing

The poller sleeps 60 seconds between cycles, so a job waits up to about a minute
before it starts. Each cycle asks for the 30 oldest open issues carrying the
label, drops pull requests, and runs what is left one at a time. There is no
pagination: with more than 30 waiting, the rest come round on later cycles. A
long command delays every job behind it.

## Operating it

- **Is it alive?** `cat ~/.config/issue-bridge/status.json` - rewritten around
  every poll and every job.
- **Logs:** `~/.config/issue-bridge/poller.log`.
- **Restart:** `launchctl kickstart -k gui/$(id -u)/bridge.poller`.
- **Remove it:** `./uninstall.sh` (leaves the repo and its issues alone).

Read the status fields for what they are. `last_poll_ok` is the last successful
issue listing, not proof that any job finished. `queue_depth` is how many issues
came back in the last batch once pull requests were dropped, so it stops at 30
and is not the backlog. `last_drain_ts` moves when a job finishes being handled,
`last_error` holds the last failure the poller survived, and `pat_expiry` is the
token expiry GitHub reported, when it reports one. Status writes are best effort
and skipped silently if they fail. Command output goes to GitHub, not to the
log; the log is for the poller's own noise.

`./uninstall.sh` removes the LaunchAgent and `~/.config/issue-bridge`, token,
status and log included. Revoke the PAT on GitHub yourself.

### Local checks

```sh
python3 bridge-poller.py --self-test
python3 bridge-poller.py --once
```

`--self-test` is offline: parsing, allowlist matching, ttl rules, the comment
budget, subprocess handling and the status write. It says nothing about GitHub
access or launchd. `--once` runs one real cycle and will execute queued jobs, so
stop the LaunchAgent first or the two race for the same issues.
`ISSUE_BRIDGE_HOME` points both at a different config directory, which is how to
try one without touching the installed lane.

The LaunchAgent runs `bridge-poller.py` from the checkout you installed from.
Move or delete that directory and the poller stops coming back; reinstall from
the new path.

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

## FAQ

**Is this SSH or ngrok?** No. SSH gives you a session on a reachable machine and
ngrok publishes a local service through a tunnel. This queues a command and
returns its output later. Use it when a request and a reply are enough, and use
SSH when you need a shell.

**Is it a self-hosted runner or a webhook receiver?** Neither. It runs an argv
out of an issue body, not an Actions workflow, and it polls rather than listening
on a port. A self-hosted runner is also outbound-only, so "no inbound port" is
not what separates them; the difference is that this is a command queue, with no
workflow engine and no job isolation.

**Is it an MCP server?** No. The protocol is a labelled issue with a small body.
An agent may well use a GitHub MCP server to file that issue, but there is no MCP
adapter here.

**Does it work behind NAT?** Yes, that is the point. It needs outbound https to
`api.github.com`, a valid token and repo permissions, and nothing mapped on the
router. Being able to browse the web is not proof the API is reachable.

**What happens while the Mac is asleep or offline?** A `drain-on-wake` job waits
and runs once the machine is awake, logged in and able to reach GitHub. The
bridge does not wake the Mac and does not run at the login window. A
`drop-if-stale` or `alert` job whose window has passed is skipped and closed
`exec-failed`.

**How long until a job starts?** Usually under a minute: 60 seconds between
cycles, 30 issues per cycle oldest first, run one at a time. Sleep, logout, a
backlog or a long-running earlier job all add to that.

**Can I give a job a deadline?** Only a start window. `drop-if-stale` with
`queued_at` and `ttl` skips a job that was picked up too late; it does not stop
one that is already running or cap how long it takes. `alert` behaves the same
way today - a missed-window comment and `exec-failed`, no notification of any
other kind. Missing or unparseable window data means the job runs.

**Where do results go, and can I move files?** Into a comment on the issue: exit
code, duration, stdout, stderr. Output over about 58,000 characters is truncated
with a marker, and nothing is saved locally. There is no file transfer. To move
something large, have an allowlisted command write it somewhere and read it back
in bounded pieces, or use a channel of your own.

**Why is my issue still open?** Check the label first: no label, no job. Then
check that the Mac is awake and logged in, read `status.json`, and look for an
earlier job still running. An open issue whose label is gone has already been
picked up, and a crash can strand it there. Do not relabel or refile a
side-effecting job until you know whether it ran.

**Can two Macs share one lane?** No. Dropping the label is not a lock, so two
pollers can fetch and run the same issue. Give each Mac its own repo, or its own
label with one poller each.

**Linux or Windows?** The poller is stdlib Python and does not care, but the
installer, the LaunchAgent and these instructions are macOS. Nothing here sets up
a systemd unit or a Windows service.

## Requirements

A logged-in macOS session, `python3` and `curl` (both ship with the Xcode command
line tools), git to clone this, and reachable GitHub API access. No pip installs,
no dependencies. Python standard library only.
