#!/usr/bin/env python3
"""issue-bridge poller - turns GitHub issues into commands on this machine.

Any agent that can open a GitHub issue files one labelled `exec-job` on a single
designated repo. This polls that repo, checks the argv against a local
allowlist, runs it, comments the output back, and closes the issue.

Stdlib only. The only network it needs is outbound https to api.github.com:
no inbound port, no tunnel, no VPN.

Protocol: AGENTS.md. Setup: README.md.
"""
import calendar, hashlib, hmac, http.server, json, os, pathlib, shlex, shutil, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request

VERSION = "1.1.0"
HOME = pathlib.Path(os.environ.get("ISSUE_BRIDGE_HOME")
                    or pathlib.Path.home() / ".config/issue-bridge")
CONFIG, TOKEN, STATUS = HOME / "config.json", HOME / "github-token", HOME / "status.json"
API = "https://api.github.com/repos/"
# Labels this bridge was filed under before the rename. Agents, saved snippets and old
# runbooks still use them, and an issue carrying only the old label used to sit open
# forever with nobody saying why. Fixed table, mapped to the label it was renamed to:
# it is deliberately not read from config, because the label is the one place
# instructions enter this machine (TRUST-BOUNDARY.md) and widening it by configuration
# would widen that.
LEGACY_LABELS = {"am-exec": "exec-job"}
CONTROL_SUBCOMMAND = "control"   # argv[1] of a job that may jump the queue
COMMENT_BUDGET = 58000  # GitHub caps a single comment at 65536 chars
TIMEOUT = 900           # per-command wall clock

_status = {"version": VERSION, "ts": 0, "last_poll_ok": None, "last_drain_ts": None,
           "queue_depth": 0, "last_error": None, "pat_expiry": None,
           "pending_writebacks": [], "legacy_label_jobs": [], "runner": None,
           "running": None, "doorbell": None}


def config():
    cfg = json.loads(CONFIG.read_text())
    cfg.setdefault("label", "exec-job")
    cfg.setdefault("poll_interval", 60)
    cfg.setdefault("allow", [])
    # "orca" runs each job in a visible Orca terminal; "subprocess" is the pre-Orca
    # hidden child, kept as the rollback. ISSUE_BRIDGE_RUNNER overrides for one run.
    cfg["runner"] = os.environ.get("ISSUE_BRIDGE_RUNNER") or cfg.get("runner") or "orca"
    cfg.setdefault("orca_lane", "~/lanes/bridge-jobs")
    return cfg


def write_status():
    """Never fatal. A poller that dies because it could not write its own
    status file is worse than one whose status file is stale."""
    try:
        _status["ts"] = time.time()
        tmp = STATUS.with_suffix(".tmp")
        tmp.write_text(json.dumps(_status, indent=1) + "\n")
        tmp.replace(STATUS)
    except OSError:
        pass


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- GitHub -------------------------------------------------------------

def gh(cfg, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Authorization": "Bearer " + TOKEN.read_text().strip(),
         "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "issue-bridge/" + VERSION}
    if data:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(API + cfg["repo"] + path, data=data,
                                 headers=h, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        # GitHub returns the PAT's expiry on every authenticated response.
        # Parking it in status.json is how an operator sees the lane's death
        # coming instead of discovering it the morning after.
        _status["pat_expiry"] = (r.headers.get("github-authentication-token-expiration")
                                 or _status["pat_expiry"])
        return json.loads(r.read() or b"null")


def legacy_aliases(cfg):
    """Old labels that mean this lane's label. Empty unless the lane runs under the
    default, so a lane with a custom label never inherits another lane's old queue."""
    return [old for old, new in LEGACY_LABELS.items() if new == cfg["label"]]


def relabel(cfg, n, add):
    # Every label that could re-queue this issue comes off, aliases included: the label is
    # dropped before the command runs, and one left behind means a second execution.
    for label in [cfg["label"]] + legacy_aliases(cfg):
        try:
            gh(cfg, "DELETE", "/issues/%d/labels/%s" % (n, urllib.parse.quote(label)))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
    if add:
        gh(cfg, "POST", "/issues/%d/labels" % n, {"labels": [add]})


# --- job parsing --------------------------------------------------------

def parse(body):
    """Accept the frontmatter verbatim, inside a code fence, or bare - an issue
    typed on a phone will not have the --- delimiters."""
    b = (body or "").replace("\r\n", "\n").strip()
    if b.startswith("```"):
        b = b.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if not b.startswith("---\n"):
        b = "---\n%s\n---\n" % b
    fm = {}
    for line in b.split("\n---", 1)[0][4:].split("\n"):
        k, sep, v = line.partition(":")
        if sep:
            fm[k.strip()] = v.strip()
    return fm


def stale(fm, rule):
    """True if a TTL'd job missed its window. Only drop-if-stale and alert have
    windows; drain-on-wake runs however late, by design."""
    if not fm.get("ttl") or rule == "drain-on-wake":
        return False
    try:
        t0 = calendar.timegm(time.strptime(
            fm.get("queued_at", "").replace("Z", ""), "%Y-%m-%dT%H:%M:%S"))
        return time.time() > t0 + float(fm["ttl"])
    except ValueError:
        return False  # an unparseable window is not a licence to skip the job


def allowed(argv, allow):
    """Each allow entry is a token prefix of the argv it permits: `herdr` allows
    every herdr subcommand, `git -C /srv/repo` allows only that repo. Matching
    is on whole tokens, so `uname` never matches `unamex`."""
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        return False
    for entry in allow:
        pfx = shlex.split(entry)
        if pfx and argv[:len(pfx)] == pfx:
            return True
    return False


# --- running ------------------------------------------------------------

def run(argv):
    """The hidden runner: a child of the poller with its output on pipes. It is
    the rollback (`"runner": "subprocess"`) and the fallback when Orca cannot be
    reached, so the queue drains either way."""
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT)
        r = {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired:
        r = {"exit": None, "stdout": "", "stderr": "timeout after %ds" % TIMEOUT}
    except OSError as e:
        r = {"exit": None, "stdout": "", "stderr": str(e)}
    r["duration"] = round(time.time() - t0, 3)
    return r


def clip(s, n):
    return s if len(s) <= n else s[:n] + "\n...[truncated, %d chars dropped]" % (len(s) - n)


def budget(out, err):
    if len(out) + len(err) <= COMMENT_BUDGET:
        return out, err
    err = clip(err, max(COMMENT_BUDGET // 4, COMMENT_BUDGET - len(out)))
    return clip(out, COMMENT_BUDGET - len(err)), err


# --- visible runner: one Orca terminal per job ---------------------------
# A job runs in a terminal tab Orca shows on its board, under a workspace of its
# own (`orca_lane`), instead of as a hidden child of the poller. The tab runs
# `bridge-poller.py --run-job <dir>`: a fixed command whose only variable is the
# job directory, so the text typed into that shell never carries anything from
# the issue. The runner tees the command's output to the tab and to files in the
# directory and records the exit code there. The poller reads those files, not
# Orca: Orca's own exit code and scrollback are not reliable once a tab has
# exited, and the files survive a poller restart while the job keeps running.
JOBS = HOME / "jobs"
# launchd starts the poller with /usr/bin:/bin:/usr/sbin:/sbin; Orca is a brew tool.
ORCA_PATH = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"


def orca(cfg, *args, timeout=60):
    """One `orca ... --json` call. The parsed `result`, or None when the CLI is
    missing, fails, times out or answers ok:false. Callers treat None as "Orca
    is not there right now", never as a verdict about the job."""
    exe = shutil.which("orca", path=ORCA_PATH)
    if not exe:
        return None
    try:
        p = subprocess.run([exe, *args, "--json"], capture_output=True, text=True,
                           timeout=timeout)
        d = json.loads(p.stdout or "null")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return d.get("result") if isinstance(d, dict) and d.get("ok") else None


def job_lane(cfg):
    """The Orca workspace the job tabs live under. Orca adopts a directory only if
    it is a git repo; an empty init is enough, and adopting twice is a no-op."""
    lane = pathlib.Path(os.path.expanduser(cfg["orca_lane"]))
    try:
        lane.mkdir(parents=True, exist_ok=True)
        if not (lane / ".git").exists():
            subprocess.run(["git", "init", "-q", str(lane)], capture_output=True,
                           check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return lane if orca(cfg, "repo", "add", "--path", str(lane)) else None


def run_job(jobdir):
    """`--run-job <dir>`: the process inside the Orca tab. Runs the argv in
    job.json with stdin closed, tees each stream to the tab and to a file, and
    writes exit.json last, so its presence means the record is complete."""
    jobdir = pathlib.Path(jobdir)
    job = json.loads((jobdir / "job.json").read_text())
    argv = job["argv"]
    print("issue-bridge job #%s\n$ %s\n" % (job["issue"], shlex.join(argv)), flush=True)
    t0 = time.time()

    def tee(src, path, mirror):
        with open(path, "wb") as dst:
            for chunk in iter(lambda: src.read1(65536), b""):
                dst.write(chunk)
                dst.flush()
                mirror.write(chunk)
                mirror.flush()

    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    except OSError as e:
        (jobdir / "stdout").write_bytes(b"")
        (jobdir / "stderr").write_text(str(e))
        code = None
        print("could not start: %s" % e, flush=True)
    else:
        threads = [threading.Thread(target=tee, args=(p.stdout, jobdir / "stdout", sys.stdout.buffer)),
                   threading.Thread(target=tee, args=(p.stderr, jobdir / "stderr", sys.stderr.buffer))]
        for t in threads:
            t.start()
        code = p.wait()
        for t in threads:
            t.join()
    duration = round(time.time() - t0, 3)
    tmp = jobdir / "exit.tmp"
    tmp.write_text(json.dumps({"exit": code, "duration": duration, "ts": now()}))
    tmp.replace(jobdir / "exit.json")
    print("\nexit: %s  duration: %ss" % (code, duration), flush=True)
    return 0 if code == 0 else 1


def job_result(jobdir, note=None):
    """The result as the runner left it. A missing exit.json reads as exit None,
    which is how a killed or crashed job reports."""
    def read(name):
        try:
            return (jobdir / name).read_bytes().decode("utf-8", "replace")
        except OSError:
            return ""
    try:
        meta = json.loads((jobdir / "exit.json").read_text())
    except (OSError, ValueError):
        meta = {}
    err = read("stderr")
    if note:
        err = (err + "\n" if err else "") + "[issue-bridge] " + note
    return {"exit": meta.get("exit"), "stdout": read("stdout"), "stderr": err,
            "duration": meta.get("duration")}


def hidden(argv, why):
    r = run(argv)
    r["stderr"] += ("\n" if r["stderr"] else "") + "[issue-bridge] ran as a hidden subprocess: " + why
    return r


def await_job(cfg, jobdir, handle, started):
    """Wait for the tab to exit, then read the files. The wait is re-issued in
    bounded slices so an Orca restart mid-job costs one slice, not the job. Past
    TIMEOUT the tab is closed, which hangs up the PTY and everything the command
    started under it; whatever it printed before that is kept."""
    deadline = started + TIMEOUT
    while True:
        if (jobdir / "exit.json").exists():
            return job_result(jobdir)
        left = deadline - time.time()
        if left <= 0:
            break
        w = orca(cfg, "terminal", "wait", "--terminal", handle, "--for", "exit",
                 "--timeout-ms", str(int(min(left, 60) * 1000)), timeout=min(left, 60) + 30)
        if w is None:
            show = orca(cfg, "terminal", "show", "--terminal", handle)
            if show and show.get("terminal", {}).get("connected"):
                time.sleep(5)
                continue
        elif not w.get("wait", {}).get("satisfied"):
            continue
        time.sleep(1)  # the runner writes exit.json before the PTY closes; give the fs a beat
        return job_result(jobdir, None if (jobdir / "exit.json").exists() else
                          "the Orca terminal ended without recording a result")
    orca(cfg, "terminal", "close", "--terminal", handle, "--tab")
    r = job_result(jobdir, "timeout after %ds; the Orca terminal was closed" % TIMEOUT)
    r["exit"], r["duration"] = None, round(time.time() - started, 3)
    return r


def run_visible(cfg, issue, argv, rec):
    """Run one job in its own visible Orca terminal. `rec` is the job's writeback
    journal entry: the terminal handle and job directory go into it as soon as
    they exist, so a restarted poller can find the tab instead of the label."""
    n = issue["number"]
    lane = job_lane(cfg)
    if lane is None:
        return hidden(argv, "Orca is not reachable (orca CLI missing, runtime down, or the job lane could not be adopted)")
    jobdir = JOBS / str(n)
    jobdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("stdout", "stderr", "exit.json"):
        (jobdir / name).unlink(missing_ok=True)
    started = time.time()
    (jobdir / "job.json").write_text(json.dumps({"issue": n, "argv": argv}))
    cmd = "exec " + shlex.join([sys.executable, str(pathlib.Path(__file__).resolve()),
                                "--run-job", str(jobdir)])
    title = ("#%d %s" % (n, issue.get("title") or ""))[:60]
    res = orca(cfg, "terminal", "create", "--worktree", "path:%s" % lane,
               "--title", title, "--command", cmd)
    handle = ((res or {}).get("terminal") or {}).get("handle")
    if not handle:
        return hidden(argv, "orca terminal create failed")
    rec.update({"jobdir": str(jobdir), "terminal": handle, "started": started})
    save_pending(rec)
    _status["running"] = {"issue": n, "terminal": handle, "lane": str(lane),
                          "started_at": now(), "title": title}
    write_status()
    try:
        return await_job(cfg, jobdir, handle, started)
    finally:
        _status["running"] = None


def resume_job(cfg, rec):
    """A journal entry that names a terminal: the poller restarted while that job
    ran in Orca. Wait for it if it is still running, recover its result from the
    job directory, and never run the command again."""
    return await_job(cfg, pathlib.Path(rec["jobdir"]), rec["terminal"],
                     rec.get("started") or time.time())



# --- local deployment patch: result block + retryable writeback
# (not upstream; re-apply after a pull)
RESULT_BLOCK_TAIL = 4096  # chars kept per stream, from the END of the stream
MARKER = "<!-- issue-bridge:result"
PENDING = HOME / "pending"  # one <issue>.json per writeback that is not finished
WRITEBACK_STEPS = ("body", "comment", "label", "close")


def _rb_tail(s):
    return s if len(s) <= RESULT_BLOCK_TAIL else (
        "...[%d chars dropped from the start]\n%s"
        % (len(s) - RESULT_BLOCK_TAIL, s[-RESULT_BLOCK_TAIL:]))


def _rb_body(orig_body, r):
    """The issue body with the bounded result block appended, or None if the body
    already carries a block.

    The comment stays the human record. The body block gives a caller that polls
    the issue a stable machine-readable record without fetching the comments
    endpoint: an <!-- issue-bridge:result {...} --> marker whose JSON carries exit,
    duration and ts, then the last 4KB of each stream in fences. A second append is
    refused, so a re-handled issue keeps one block.
    """
    if MARKER in orig_body:
        return None
    meta = json.dumps({"exit": r["exit"], "duration": r["duration"],
                       "ts": now()}, separators=(",", ":"))
    block = ("\n\n<!-- %s %s -->\n\n"
             "**stdout (last %d chars)**\n```\n%s\n```\n"
             "**stderr (last %d chars)**\n```\n%s\n```\n"
             % ("issue-bridge:result", meta,
                RESULT_BLOCK_TAIL, _rb_tail(r["stdout"]),
                RESULT_BLOCK_TAIL, _rb_tail(r["stderr"])))
    if len(orig_body) + len(block) > 60000:  # GitHub caps a body at 65536
        orig_body = orig_body[:60000 - len(block)]
    return orig_body + block


# --- writeback journal --------------------------------------------------
# The label is dropped before the command runs, so the label cannot say whether a
# job already executed. This journal can: one file per job, written before the
# first writeback attempt and deleted only when every step is done. run() is not
# reachable from any retry path, so a retry can never execute a command twice.

def pending_path(n):
    return PENDING / ("%d.json" % n)


def save_pending(rec):
    PENDING.mkdir(parents=True, exist_ok=True, mode=0o700)  # holds command output
    tmp = pending_path(rec["issue"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1) + "\n")
    tmp.replace(pending_path(rec["issue"]))


def load_pending(n):
    try:
        rec = json.loads(pending_path(n).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("issue") == n else None


def iter_pending():
    """(path, record) per journal entry; record is None when the file is unusable."""
    for path in sorted(PENDING.glob("*.json")):
        try:
            rec = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            _status["last_error"] = "unreadable writeback journal %s: %r" % (path.name, e)
            rec = None
        yield path, rec if isinstance(rec, dict) and isinstance(rec.get("issue"), int) else None


def pending_status():
    """What an operator or a monitor reads. Survives restarts because it is derived
    from the journal files, not from a scalar that the next poll overwrites."""
    out = []
    for path, rec in iter_pending():
        if rec is None:
            out.append({"file": path.name, "unreadable": True})
            continue
        steps = rec.get("steps") or {}
        out.append({"issue": rec["issue"], "attempts": rec.get("attempts", 0),
                    "steps_left": [k for k in WRITEBACK_STEPS if not steps.get(k)],
                    "created": rec.get("created"), "last_error": rec.get("last_error")})
    return out


def writeback(cfg, rec):
    """Finish, or resume, the writeback for one job that has already executed.

    Every step is idempotent (the body PATCH is guarded by the marker, relabel
    tolerates a missing label, closing a closed issue is a no-op) and each
    completion is journalled, so a retry repeats no step. Returns True when the
    journal entry is retired.
    """
    n, steps = rec["issue"], rec.setdefault("steps", {})
    try:
        if not steps.get("body"):
            if rec.get("body") is not None:
                gh(cfg, "PATCH", "/issues/%d" % n, {"body": rec["body"]})
            steps["body"] = True
            save_pending(rec)
        if not steps.get("comment"):
            gh(cfg, "POST", "/issues/%d/comments" % n, {"body": rec["comment"]})
            steps["comment"] = True
            save_pending(rec)
        if not steps.get("label"):
            relabel(cfg, n, "exec-done" if rec.get("done") else "exec-failed")
            steps["label"] = True
            save_pending(rec)
        if not steps.get("close"):
            gh(cfg, "PATCH", "/issues/%d" % n,
               {"state": "closed",
                "state_reason": "completed" if rec.get("done") else "not_planned"})
            steps["close"] = True
    except Exception as e:  # retried next cycle; the journal keeps the result
        rec["attempts"] = rec.get("attempts", 0) + 1
        rec["last_error"] = "%s %r" % (now(), e)
        save_pending(rec)
        _status["last_error"] = "writeback issue #%d: %r" % (n, e)
        print("writeback issue #%d incomplete (attempt %d, steps left %s): %r"
              % (n, rec["attempts"], [k for k in WRITEBACK_STEPS if not steps.get(k)], e),
              flush=True)
        return False
    pending_path(n).unlink(missing_ok=True)
    return True


def drain_pending(cfg):
    """Retry unfinished writebacks from earlier cycles. These issues have already
    lost the label, so the poll query never returns them again."""
    for _, rec in list(iter_pending()):
        if not rec:
            continue
        if rec.get("jobdir"):  # the pre-run record of a job that ran in Orca
            # body=None: the original issue body is not at hand, and a PATCH without it
            # would drop it; the comment carries the result.
            rec = dict(result_record(cfg, {"number": rec["issue"]}, resume_job(cfg, rec)), body=None)
            save_pending(rec)
        writeback(cfg, rec)
# --- end local deployment patch ---


def legacy_note(cfg, issue):
    """The warning the filer actually reads. Says it ran, and what to file next time."""
    old = issue.get("legacy_label")
    if not old:
        return ""
    return ("> **legacy label.** This was filed as `%s`, renamed to `%s`. It was handled "
            "anyway and the old label has been removed. File the next one with `%s`.\n\n"
            % (old, cfg["label"], cfg["label"]))


def handle(cfg, issue):
    n = issue["number"]
    rec = load_pending(n)
    if rec:
        # A journal entry means this job already executed (or was already answered).
        # Finish the paperwork; do not parse, and above all do not run, again.
        return writeback(cfg, rec)
    r = None
    fm = parse(issue.get("body"))
    rule = fm.get("rule", "drain-on-wake")
    try:
        argv = json.loads(fm.get("argv", "null"))
    except ValueError:
        argv = None

    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        done = False
        body = ("**not run** - the body did not parse.\n\n`argv` must be a JSON array "
                "of strings on one line:\n\n```\n---\nargv: [\"uname\", \"-a\"]\n"
                "rule: drain-on-wake\nqueued_at: %s\nttl: 3600\n---\n```\n" % now())
    elif stale(fm, rule):
        done = False
        body = ("**not run** - missed its window (rule `%s`, ttl `%s`s, queued `%s`).\n"
                % (rule, fm.get("ttl"), fm.get("queued_at")))
    elif not allowed(argv, cfg["allow"]):
        done = False
        # The whole argv, not argv[0]: an entry like `git -C /srv/repo` denies on a
        # later token, and naming only `git` would read as a lie.
        body = ("**denied** - `%s` is not on this machine's allowlist, so nothing ran.\n\n"
                "This is final: refiling the same argv gets the same answer. Ask the "
                "machine's owner to add it to `allow` in the poller config.\n"
                % clip(shlex.join(argv), 200))
    else:
        # Drop the label BEFORE running. A crash between here and the comment
        # loses the result, which is survivable; keeping the label would re-run
        # a command with side effects, which is not.
        relabel(cfg, n, None)
        # Journal before running. If the poller dies mid-command the next cycle
        # finds this entry, says so on the issue, and does not run it again.
        pre = {"issue": n, "done": False, "body": None, "steps": {},
               "attempts": 0, "created": now(), "argv": shlex.join(argv),
               "comment": "**result lost** - the poller stopped between starting "
                          "`%s` and recording its output. The command may have run; "
                          "it will not be run again. Refile if you still need it.\n"
                          % clip(shlex.join(argv), 200)}
        save_pending(pre)
        r = run_visible(cfg, issue, argv, pre) if cfg.get("runner") == "orca" else run(argv)
        rec = result_record(cfg, issue, r, legacy_note(cfg, issue))
        save_pending(rec)
        return writeback(cfg, rec)

    # --- local deployment patch: journalled writeback (not upstream)
    # denied, unparseable and stale jobs never ran, so they carry no result block
    rec = {"issue": n, "done": done, "comment": legacy_note(cfg, issue) + body,
           "steps": {}, "attempts": 0, "created": now(), "body": None}
    save_pending(rec)
    return writeback(cfg, rec)
    # --- end local deployment patch ---


def result_record(cfg, issue, r, note=""):
    """The journal entry for a job that ran: the comment, the body result block,
    and the label to close under."""
    out, err = budget(r["stdout"], r["stderr"])
    body = ("exit: `%s`  duration: `%ss`\n\n**stdout**\n```\n%s\n```\n"
            "**stderr**\n```\n%s\n```\n" % (r["exit"], r["duration"], out, err))
    return {"issue": issue["number"], "done": r["exit"] == 0, "comment": note + body,
            "steps": {}, "attempts": 0, "created": now(),
            "body": _rb_body(issue.get("body") or "", r)}


# --- loop ---------------------------------------------------------------

def is_control(issue):
    """Parsing only; nothing here runs or writes. A malformed body is not control."""
    try:
        argv = json.loads(parse(issue.get("body")).get("argv", "null"))
    except ValueError:
        return False
    return isinstance(argv, list) and len(argv) > 1 and argv[1] == CONTROL_SUBCOMMAND


def control_first(issues):
    """Control jobs (`lane control mute ...`) jump the queue; everything else keeps its
    oldest-first order, because sorted() is stable.

    The queue is serial and a lane prompt can hold it for the full 900s timeout. A mute
    that arrives behind one is useless by the time it runs. Order is not a permission:
    a control job still goes through allowed() like any other, and the wrapper accepts a
    closed set of control words with fixed text.
    """
    return sorted(issues, key=lambda i: 0 if is_control(i) else 1)


def poll(cfg):
    """Open jobs for this lane, oldest first, including any filed under a legacy label.

    A legacy-labelled issue is warned about (on the issue, in the log and in status.json)
    and then run as if it carried the current label, rather than sitting open forever
    because nothing was listening on the name the filer used.
    """
    seen, issues = set(), []
    labels = [cfg["label"]] + legacy_aliases(cfg)
    for label in labels:
        got = gh(cfg, "GET", "/issues?state=open&labels=%s&sort=created"
                             "&direction=asc&per_page=30" % urllib.parse.quote(label)) or []
        for i in got:
            if "pull_request" in i or i["number"] in seen:
                continue
            seen.add(i["number"])
            if label != cfg["label"]:
                i["legacy_label"] = label
            issues.append(i)
    # The label-filtered list lags a new issue by 10 s or more; the single-issue
    # endpoint does not. Label and state still come from the API, never the payload.
    wanted = set(labels)
    while RUNG:
        n = RUNG.pop()
        try:
            i = gh(cfg, "GET", "/issues/%d" % n)
        except urllib.error.HTTPError:
            continue
        names = {l["name"] for l in (i or {}).get("labels", [])}
        if (not i or i.get("state") != "open" or "pull_request" in i
                or n in seen or not names & wanted):
            continue
        seen.add(n)
        if cfg["label"] not in names:
            i["legacy_label"] = sorted(names & wanted)[0]
        issues.append(i)
    issues.sort(key=lambda i: i.get("created_at") or "")
    return issues


# Doorbell (specs/001-webhook-doorbell): a signed GitHub webhook only wakes the poll loop
# early. The payload is never read for commands; the normal poll decides what runs, so
# this adds latency savings and nothing to what the bridge will execute.
WAKE = threading.Event()
RUNG = set()  # issue numbers named by signed webhooks; poll() fetches them directly
SECRET = HOME / "webhook-secret"


def ring_ok(body, sig):
    try:
        key = SECRET.read_bytes().strip()
    except OSError:
        return False
    want = "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()
    return bool(key) and hmac.compare_digest(want, sig or "")


class Doorbell(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if self.path != "/github-hook":
            return self.send_error(404)
        if not 0 <= n <= 1 << 20:
            return self.send_error(413)
        body = self.rfile.read(n)
        ok = ring_ok(body, self.headers.get("X-Hub-Signature-256"))
        if ok:
            try:  # only the number is used, to pick which issue to re-fetch from the API
                num = json.loads(body)["issue"]["number"]
                if isinstance(num, int):
                    RUNG.add(num)
            except (ValueError, KeyError, TypeError):
                pass
            WAKE.set()
        self.send_response(204 if ok else 401)
        self.end_headers()

    def log_message(self, *a):
        pass


def start_doorbell(port):
    """Bound once at startup; a changed webhook_port needs a poller restart."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Doorbell)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def cycle(cfg):
    _status["runner"] = cfg.get("runner")
    drain_pending(cfg)  # unfinished writebacks first: their issues lost the label
    _status["pending_writebacks"] = pending_status()
    try:
        issues = poll(cfg)
        _status["last_poll_ok"], _status["last_error"] = time.time(), None
    except (urllib.error.HTTPError, OSError) as e:
        # Transient by construction: the issues keep their label and the next
        # cycle picks them up. A dead PAT looks the same and is visible in
        # last_error rather than in a log nobody reads.
        _status["last_error"] = "poll: %s" % e
        return write_status()

    _status["queue_depth"] = len(issues)
    _status["legacy_label_jobs"] = [{"issue": i["number"], "label": i["legacy_label"]}
                                    for i in issues if i.get("legacy_label")]
    for i in _status["legacy_label_jobs"]:
        print("issue #%d carries the legacy label %r; running it as %r. Refile future jobs "
              "with %r." % (i["issue"], i["label"], cfg["label"], cfg["label"]), flush=True)
    write_status()
    for issue in control_first(issues):
        try:
            handle(cfg, issue)
            _status["last_drain_ts"] = time.time()
        except Exception as e:  # one bad job must not stop the lane
            _status["last_error"] = "issue #%d: %r" % (issue["number"], e)
        _status["pending_writebacks"] = pending_status()
        write_status()


def self_test():
    """Offline check of the parts that decide whether a command runs, and of the
    writeback journal that decides whether a result can be lost."""
    global STATUS, PENDING
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    allow = ["herdr", "git -C /srv/repo", "uname"]
    check("allow bare command", allowed(["herdr", "pane", "list"], allow))
    check("allow token prefix", allowed(["git", "-C", "/srv/repo", "pull"], allow))
    check("deny other path", not allowed(["git", "-C", "/etc", "pull"], allow))
    check("deny unlisted command", not allowed(["rm", "-rf", "/"], allow))
    check("deny partial token", not allowed(["unamex"], allow))
    check("deny non-list argv", not allowed("uname", allow))
    check("deny empty argv", not allowed([], allow))
    check("deny with empty allowlist", not allowed(["uname"], []))

    check("parse frontmatter",
          json.loads(parse('---\nargv: ["uname", "-a"]\nrule: drain-on-wake\n---\n')["argv"])
          == ["uname", "-a"])
    check("parse fenced", json.loads(parse('```\nargv: ["uname"]\n```')["argv"]) == ["uname"])
    check("parse bare", parse('argv: ["uname"]\nrule: alert').get("rule") == "alert")
    check("parse empty body", parse(None) == {})

    old = {"ttl": "1", "queued_at": "2000-01-01T00:00:00Z"}
    check("drop-if-stale expires", stale(old, "drop-if-stale"))
    check("alert expires", stale(old, "alert"))
    check("drain-on-wake never stale", not stale(old, "drain-on-wake"))
    check("no ttl never stale", not stale({"queued_at": "2000-01-01T00:00:00Z"}, "alert"))
    check("bad queued_at not stale", not stale({"ttl": "1", "queued_at": "nonsense"}, "alert"))

    cfg_default = {"repo": "x/y", "label": "exec-job", "allow": []}
    check("legacy alias maps to the default label",
          legacy_aliases(cfg_default) == ["am-exec"])
    check("a custom label inherits no alias",
          legacy_aliases({"repo": "x/y", "label": "my-lane", "allow": []}) == [])
    check("legacy note names both labels",
          "am-exec" in legacy_note(cfg_default, {"legacy_label": "am-exec"})
          and "exec-job" in legacy_note(cfg_default, {"legacy_label": "am-exec"}))
    check("no note without a legacy label", legacy_note(cfg_default, {}) == "")

    ctl = {"number": 2, "body": '---\nargv: ["/x/lane", "control", "mute", "j", "a"]\n---\n'}
    work = {"number": 1, "body": '---\nargv: ["/x/lane", "prompt", "a", "j", "hi"]\n---\n'}
    work2 = {"number": 3, "body": '---\nargv: ["uname", "-a"]\n---\n'}
    check("control job is recognised", is_control(ctl))
    check("prompt job is not control", not is_control(work))
    check("unparseable job is not control", not is_control({"number": 4, "body": "junk"}))
    check("control jumps the queue",
          [i["number"] for i in control_first([work, ctl, work2])] == [2, 1, 3])
    check("work order is otherwise untouched",
          [i["number"] for i in control_first([work, work2])] == [1, 3])

    out, err = budget("x" * 60000, "y" * 60000)
    check("comment budget", len(out) + len(err) < 65536)
    check("short output untouched", budget("a", "b") == ("a", "b"))

    r = run([sys.executable, "-c", "import sys; print('out'); sys.exit(3)"])
    check("run captures exit", r["exit"] == 3)
    check("run captures stdout", r["stdout"].strip() == "out")
    check("run survives missing binary", run(["/nonexistent/binary"])["exit"] is None)

    with tempfile.TemporaryDirectory() as d:
        STATUS = pathlib.Path(d) / "status.json"
        write_status()
        check("status.json written", json.loads(STATUS.read_text())["version"] == VERSION)
        STATUS = pathlib.Path(d) / "no-such-dir" / "status.json"
        write_status()
        check("status failure is not fatal", True)

    # --- writeback journal (local deployment patch) ---
    with tempfile.TemporaryDirectory() as d:
        STATUS = pathlib.Path(d) / "status.json"
        PENDING = pathlib.Path(d) / "pending"
        real_gh, real_run = gh, run
        calls, runs, state = [], [], {"body": "", "closed": False, "labels": []}
        fail = {"body_patch": True}

        def fake_gh(cfg, method, path, body=None):
            calls.append((method, path))
            if method == "POST" and path.endswith("/comments"):
                state.setdefault("comments", []).append(body["body"])
            if method == "PATCH" and body and "body" in body:
                if fail["body_patch"]:
                    raise OSError("simulated body PATCH failure")
                state["body"] = body["body"]
            if method == "PATCH" and body and body.get("state") == "closed":
                state["closed"] = True
            if method == "POST" and path.endswith("/labels"):
                state["labels"] += body["labels"]
            return {}

        def fake_run(argv):
            runs.append(argv)
            return {"exit": 0, "stdout": "out", "stderr": "", "duration": 0.01}

        globals()["gh"], globals()["run"] = fake_gh, fake_run
        try:
            cfg = {"repo": "x/y", "label": "exec-job", "allow": ["uname"]}
            issue = {"number": 11,
                     "body": '---\nargv: ["uname", "-m"]\nrule: drain-on-wake\n---\n'}
            handle(cfg, dict(issue))
            comments = lambda: len([c for c in calls if c[0] == "POST"
                                    and c[1].endswith("/comments")])
            check("failed writeback keeps a journal entry", load_pending(11) is not None)
            check("failed writeback is observable in status",
                  pending_status()[0]["issue"] == 11 and pending_status()[0]["attempts"] == 1)
            check("marker absent while the PATCH fails", MARKER not in state["body"])

            handle(cfg, dict(issue))  # same issue seen again, label race or restart
            check("resume does not re-run the command", len(runs) == 1)
            check("resume does not duplicate the comment", comments() <= 1)

            fail["body_patch"] = False
            drain_pending(cfg)
            check("retry lands the result marker", MARKER in state["body"])
            check("retry closes the issue", state["closed"])
            check("retry posts exactly one comment", comments() == 1)
            check("retry labels once", state["labels"] == ["exec-done"])
            check("journal retired after success", load_pending(11) is None
                  and pending_status() == [])
            check("command ran exactly once", len(runs) == 1)

            # a denied job never runs, so it carries no result block and no journal
            handle(cfg, {"number": 12,
                         "body": '---\nargv: ["rm", "-rf", "/"]\nrule: drain-on-wake\n---\n'})
            check("denied job does not run", len(runs) == 1)
            check("denied job leaves no journal", load_pending(12) is None)

            # a job filed under the legacy label: runs, warns, and loses both labels
            del calls[:]
            handle(cfg, {"number": 13, "legacy_label": "am-exec",
                         "body": '---\nargv: ["uname", "-m"]\nrule: drain-on-wake\n---\n'})
            deletes = [c[1] for c in calls if c[0] == "DELETE"]
            check("legacy job runs", len(runs) == 2)
            check("legacy label is removed too",
                  any(d.endswith("/labels/am-exec") for d in deletes)
                  and any(d.endswith("/labels/exec-job") for d in deletes))
            check("legacy job is warned about on the issue",
                  "legacy label" in state["comments"][-1]
                  and "am-exec" in state["comments"][-1])
        finally:
            globals()["gh"], globals()["run"] = real_gh, real_run

    # --- visible runner: the tab-side runner, its result files, and the fallbacks ---
    with tempfile.TemporaryDirectory() as d:
        jobdir = pathlib.Path(d) / "jobs" / "7"
        jobdir.mkdir(parents=True)
        jobdir.joinpath("job.json").write_text(json.dumps({"issue": 7, "argv": [
            sys.executable, "-c", "import sys; print('vis-out'); print('vis-err', file=sys.stderr); sys.exit(4)"]}))
        p = subprocess.run([sys.executable, __file__, "--run-job", str(jobdir)],
                           capture_output=True, text=True, timeout=60)
        r = job_result(jobdir)
        check("run-job records the exit code", r["exit"] == 4)
        check("run-job records stdout", r["stdout"].strip() == "vis-out")
        check("run-job records stderr", r["stderr"].strip() == "vis-err")
        check("run-job mirrors output to the tab", "vis-out" in p.stdout and "vis-err" in p.stderr)
        check("run-job names the job in the tab", "job #7" in p.stdout)
        check("missing exit.json reads as exit None",
              job_result(pathlib.Path(d) / "nope")["exit"] is None)
        check("job_result appends the note to stderr",
              job_result(jobdir, "why")["stderr"].endswith("[issue-bridge] why"))

        # no orca binary: the job still runs, hidden, and the result says so
        real_jobs, real_path = JOBS, ORCA_PATH
        globals()["JOBS"], globals()["ORCA_PATH"] = pathlib.Path(d) / "jobs2", "/nonexistent"
        cfg_v = {"repo": "x/y", "label": "exec-job", "allow": ["uname"], "runner": "orca",
                 "orca_lane": str(pathlib.Path(d) / "lane")}
        r = run_visible(cfg_v, {"number": 8, "title": "t"}, [sys.executable, "-c", "print('fb')"], {"issue": 8})
        check("fallback runs the command when orca is missing", r["exit"] == 0 and r["stdout"].strip() == "fb")
        check("fallback says it ran hidden", "ran as a hidden subprocess" in r["stderr"])

        # timeout: a fake orca that never reports exit -> the tab is closed, partial output kept
        real_orca, real_timeout = orca, TIMEOUT
        calls = []

        def fake_orca(cfg, *args, timeout=60):
            calls.append(args[:2])
            if args[:2] == ("repo", "add"):
                return {"repo": {}}
            if args[:2] == ("terminal", "create"):
                jd = pathlib.Path(args[args.index("--command") + 1].split("--run-job ")[1])
                jd.joinpath("stdout").write_text("partial")
                return {"terminal": {"handle": "term_fake"}}
            if args[:2] == ("terminal", "wait"):
                time.sleep(0.05)
                return {"wait": {"satisfied": False}}
            if args[:2] == ("terminal", "close"):
                return {}
            return None

        globals()["orca"], globals()["TIMEOUT"], globals()["ORCA_PATH"] = fake_orca, 0.2, real_path
        try:
            rec = {"issue": 9}
            r = run_visible(cfg_v, {"number": 9, "title": "slow"}, ["sleep", "5"], rec)
            check("timeout closes the orca tab", ("terminal", "close") in calls)
            check("timeout reports exit None", r["exit"] is None and "timeout" in r["stderr"])
            check("timeout keeps partial output", r["stdout"] == "partial")
            check("journal learns the terminal before the run ends",
                  rec.get("terminal") == "term_fake" and rec.get("jobdir"))
            check("command never typed into the tab shell",
                  all("sleep" not in " ".join(c) for c in calls))
            # resume after a poller restart: exit.json present -> recovered, never re-run
            jd = pathlib.Path(rec["jobdir"])
            jd.joinpath("exit.json").write_text(json.dumps({"exit": 0, "duration": 1.5}))
            jd.joinpath("stdout").write_text("done-out")
            r = resume_job(cfg_v, rec)
            check("resume recovers the recorded result", r and r["exit"] == 0 and r["stdout"] == "done-out")
        finally:
            globals()["orca"], globals()["TIMEOUT"] = real_orca, real_timeout
            globals()["JOBS"] = real_jobs

    # doorbell: only a correctly signed ring wakes the loop
    real_secret = SECRET
    with tempfile.TemporaryDirectory() as td:
        globals()["SECRET"] = pathlib.Path(td) / "secret"
        check("doorbell refuses without a secret", not ring_ok(b"{}", "sha256=00"))
        SECRET.write_text("k\n")
        good = "sha256=" + hmac.new(b"k", b"{}", hashlib.sha256).hexdigest()
        check("doorbell accepts a valid signature", ring_ok(b"{}", good))
        check("doorbell rejects a tampered body", not ring_ok(b"{ }", good))
        check("doorbell rejects a missing signature", not ring_ok(b"{}", None))
        srv = start_doorbell(0)
        url = "http://127.0.0.1:%d/github-hook" % srv.server_address[1]

        def post(sig, data=b"{}"):
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"X-Hub-Signature-256": sig})
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code
        WAKE.clear()
        check("bad ring is 401 and does not wake", post("sha256=00") == 401 and not WAKE.is_set())
        check("good ring is 204 and wakes the loop", post(good) == 204 and WAKE.is_set())
        named = b'{"issue": {"number": 7}}'
        sig = "sha256=" + hmac.new(b"k", named, hashlib.sha256).hexdigest()
        RUNG.clear()
        post("sha256=00", named)
        check("unsigned ring names no issue", not RUNG)
        check("signed ring names its issue", post(sig, named) == 204 and RUNG == {7})
        RUNG.clear()
        srv.shutdown()
        WAKE.clear()
    globals()["SECRET"] = real_secret

    for name, ok in results:
        print("%-42s %s" % (name, "PASS" if ok else "FAIL"))
    bad = sum(1 for _, ok in results if not ok)
    print("\n%d/%d passed" % (len(results) - bad, len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    if "--run-job" in sys.argv:
        sys.exit(run_job(sys.argv[sys.argv.index("--run-job") + 1]))
    once = "--once" in sys.argv
    doorbell = None
    while True:
        try:
            cfg = config()
        except (OSError, ValueError) as e:
            _status["last_error"] = "config: %s" % e
            write_status()
            if once:
                sys.exit(1)
            time.sleep(60)
            continue
        if not once and doorbell is None and cfg.get("webhook_port"):
            try:
                doorbell = start_doorbell(int(cfg["webhook_port"]))
                _status["doorbell"] = "listening 127.0.0.1:%s" % cfg["webhook_port"]
            except OSError as e:  # port taken: keep polling, say why
                doorbell = False
                _status["doorbell"] = "off: %s" % e
        cycle(cfg)
        if once:
            break
        WAKE.wait(cfg["poll_interval"])  # a signed webhook ends the wait early
        WAKE.clear()
