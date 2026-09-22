#!/usr/bin/env python3
"""issue-bridge poller - turns GitHub issues into commands on this machine.

Any agent that can open a GitHub issue files one labelled `exec-job` on a single
designated repo. This polls that repo, checks the argv against a local
allowlist, runs it, comments the output back, and closes the issue.

Stdlib only. The only network it needs is outbound https to api.github.com:
no inbound port, no tunnel, no VPN.

Protocol: AGENTS.md. Setup: README.md.
"""
import calendar, json, os, pathlib, re, shlex, shutil, subprocess, sys, tempfile, threading, time
import urllib.error, urllib.parse, urllib.request

import capabilities

VERSION = "1.2.0"
HOME = pathlib.Path(os.environ.get("ISSUE_BRIDGE_HOME")
                    or pathlib.Path.home() / ".config/issue-bridge")
CONFIG, TOKEN, STATUS = HOME / "config.json", HOME / "github-token", HOME / "status.json"
RECEIPTS = HOME / "receipts"
# The full digest, not a prefix: eight hex chars is a 32-bit target that a body
# edit could be searched against; sixty-four is not.
APPROVE_RE = re.compile(r"\s*/approve\s+([0-9a-f]{64})\s*")
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
           "running": None, "disabled": False, "rate_limited": None, "approval_asked": []}


def config():
    cfg = json.loads(CONFIG.read_text())
    cfg.setdefault("label", "exec-job")
    cfg.setdefault("poll_interval", 60)
    cfg.setdefault("allow", [])
    cfg.setdefault("owner", None)
    # jobs_per_hour caps everything; privileged_per_hour additionally caps the three
    # gated classes. Defaults match spec.md; either is overridable per machine.
    cfg["rate"] = dict({"jobs_per_hour": 30, "privileged_per_hour": 5}, **(cfg.get("rate") or {}))
    # "orca" runs each job in a visible Orca terminal; "subprocess" is the pre-Orca
    # hidden child, kept as the rollback. ISSUE_BRIDGE_RUNNER overrides for one run.
    cfg["runner"] = os.environ.get("ISSUE_BRIDGE_RUNNER") or cfg.get("runner") or "orca"
    cfg.setdefault("orca_lane", "~/lanes/bridge-jobs")
    # A bad capabilities table fails closed: the poller keeps running (legacy argv
    # jobs still work), but no `capability:` job can resolve, and the reason is
    # visible in status.json rather than only in a log nobody reads.
    try:
        cfg["_capabilities"] = capabilities.load(cfg)
    except ValueError as e:
        cfg["_capabilities"] = {}
        _status["last_error"] = "capabilities: %s" % e
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

def run(argv, timeout=None):
    """The hidden runner: a child of the poller with its output on pipes. It is
    the rollback (`"runner": "subprocess"`) and the fallback when Orca cannot be
    reached, so the queue drains either way. `timeout=None` reads the module's
    TIMEOUT at call time rather than binding it at def time, so a test (or a
    capability) that changes it still takes effect."""
    timeout = TIMEOUT if timeout is None else timeout
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        r = {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
    except subprocess.TimeoutExpired:
        r = {"exit": None, "stdout": "", "stderr": "timeout after %ds" % timeout}
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


def hidden(argv, why, timeout=None):
    r = run(argv, timeout=timeout)
    r["stderr"] += ("\n" if r["stderr"] else "") + "[issue-bridge] ran as a hidden subprocess: " + why
    return r


def await_job(cfg, jobdir, handle, started, timeout):
    """Wait for the tab to exit, then read the files. The wait is re-issued in
    bounded slices so an Orca restart mid-job costs one slice, not the job. Past
    the timeout the tab is closed, which hangs up the PTY and everything the command
    started under it; whatever it printed before that is kept."""
    deadline = started + timeout
    while True:
        if (jobdir / "exit.json").exists():
            return job_result(jobdir)
        left = deadline - time.time()
        if left <= 0:
            break
        w = orca(cfg, "terminal", "wait", "--terminal", handle, "--for", "exit",
                 "--timeout-ms", str(int(min(left, 60) * 1000)), timeout=min(left, 60) + 30)
        if w is None:
            # F8: `wait` failing to answer is not proof the job ended, whether
            # `show` confirms the terminal, fails outright, or reports it is not
            # connected. Keep polling in the same bounded slices until exit.json
            # appears or the deadline passes, instead of falling through to
            # "ended without recording a result" on the first comms hiccup.
            orca(cfg, "terminal", "show", "--terminal", handle)
            time.sleep(5)
            continue
        if not w.get("wait", {}).get("satisfied"):
            continue
        time.sleep(1)  # the runner writes exit.json before the PTY closes; give the fs a beat
        return job_result(jobdir, None if (jobdir / "exit.json").exists() else
                          "the Orca terminal ended without recording a result")
    orca(cfg, "terminal", "close", "--terminal", handle, "--tab")
    r = job_result(jobdir, "timeout after %ds; the Orca terminal was closed" % timeout)
    r["exit"], r["duration"] = None, round(time.time() - started, 3)
    return r


def run_visible(cfg, issue, argv, rec, timeout=None):
    """Run one job in its own visible Orca terminal. `rec` is the job's writeback
    journal entry: the terminal handle and job directory go into it as soon as
    they exist, so a restarted poller can find the tab instead of the label."""
    timeout = TIMEOUT if timeout is None else timeout
    n = issue["number"]
    lane = job_lane(cfg)
    if lane is None:
        return hidden(argv, "Orca is not reachable (orca CLI missing, runtime down, or the job lane could not be adopted)",
                      timeout=timeout)
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
        return hidden(argv, "orca terminal create failed", timeout=timeout)
    rec.update({"jobdir": str(jobdir), "terminal": handle, "started": started, "timeout": timeout})
    save_pending(rec)
    _status["running"] = {"issue": n, "terminal": handle, "lane": str(lane),
                          "started_at": now(), "title": title}
    write_status()
    try:
        return await_job(cfg, jobdir, handle, started, timeout=timeout)
    finally:
        _status["running"] = None


def resume_job(cfg, rec):
    """A journal entry that names a terminal: the poller restarted while that job
    ran in Orca. Wait for it if it is still running, recover its result from the
    job directory, and never run the command again."""
    return await_job(cfg, pathlib.Path(rec["jobdir"]), rec["terminal"],
                     rec.get("started") or time.time(), timeout=rec.get("timeout") or TIMEOUT)



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


def not_run(cfg, issue, body, done=False):
    """Journal and answer a job that never ran (unparseable, stale, or denied):
    no result block, since there is no result. Shared by the legacy `argv:`
    path and the `capability:` path so both dead ends look the same on the
    issue."""
    n = issue["number"]
    rec = {"issue": n, "done": done, "comment": legacy_note(cfg, issue) + body,
           "steps": {}, "attempts": 0, "created": now(), "body": None}
    save_pending(rec)
    return writeback(cfg, rec)


def actor(cfg, issue):
    """Who filed it and who labelled it, for the receipt. A failed lookup reads
    as "unknown" rather than raising: a capability job should not lose its
    result over an events-endpoint hiccup."""
    author = ((issue.get("user") or {}).get("login")) or "unknown"
    labelled_by = "unknown"
    try:
        events = gh(cfg, "GET", "/issues/%d/events?per_page=100" % issue["number"]) or []
        wanted = set([cfg["label"]] + legacy_aliases(cfg))
        for e in events:
            if e.get("event") == "labeled" and (e.get("label") or {}).get("name") in wanted:
                labelled_by = ((e.get("actor") or {}).get("login")) or "unknown"  # last one wins
    except Exception:
        labelled_by = "unknown"
    return {"author": author, "labelled_by": labelled_by}


def find_approval(cfg, issue, dig):
    """The id of an owner `/approve <digest>` comment on this issue that has
    not already been spent on an earlier receipt, or None."""
    n = issue["number"]
    try:
        comments = gh(cfg, "GET", "/issues/%d/comments?per_page=100" % n) or []
    except Exception:
        return None
    used = set()  # approval ids this issue already spent, from its own few receipts
    for p in pathlib.Path(RECEIPTS).glob("*-%d.json" % n):
        try:
            used.add(json.loads(p.read_text()).get("approval"))
        except (OSError, ValueError):
            pass
    found = None
    if not cfg.get("owner"):
        return None  # no owner configured means nobody can approve, not anybody
    for c in comments:
        if (c.get("user") or {}).get("login") != cfg["owner"]:
            continue
        m = APPROVE_RE.fullmatch(c.get("body") or "")
        if m and m.group(1) == dig and c.get("id") not in used:
            found = c.get("id")  # keep scanning; the most recent match wins
    return found


def class_gate(cfg, issue, job, dig):
    """(True, approval_id) once a job may run. For the three privileged classes
    that means an unused owner `/approve` comment; anything else is `read` or
    `mutate` and needs none. A gated job that lacks approval gets one comment
    (never repeated while it keeps waiting) and keeps its label."""
    if job["class"] not in capabilities.PRIVILEGED:
        return True, None
    n = issue["number"]
    approval_id = find_approval(cfg, issue, dig)
    if approval_id is not None:
        return True, approval_id
    if n not in _status["approval_asked"]:
        gh(cfg, "POST", "/issues/%d/comments" % n, {"body": (
            "**owner approval needed** - capability `%s` is class `%s`.\n\n"
            "To approve this exact request, comment:\n\n```\n/approve %s\n```\n"
            % (job["name"], job["class"], dig))})
        _status["approval_asked"].append(n)
    return False, None


def run_capability(cfg, issue, name, params, job, act, approval_id):
    """Run a resolved capability job through the existing runner, then write
    its receipt. Mirrors the legacy `argv:` run path: label dropped and a
    pre-run journal entry saved before anything executes, so a crash mid-run
    is survivable and never re-runs the command."""
    n = issue["number"]
    relabel(cfg, n, None)
    pre = {"issue": n, "done": False, "body": None, "steps": {}, "attempts": 0,
           "created": now(), "capability": name, "timeout": job["timeout"],
           "comment": "**result lost** - the poller stopped between starting capability "
                      "`%s` and recording its output. The command may have run; it will "
                      "not be run again. Refile if you still need it.\n" % name}
    save_pending(pre)

    started_at = now()
    # The receipt exists before the command does. Its approval id is therefore
    # spent even if the poller dies mid-run and the issue is later relabelled.
    fields = {"issue": n, "actor": act, "capability": name, "class": job["class"],
              "params_sha256": capabilities.digest(params), "approval": approval_id,
              "exit": None, "duration": None, "changed_paths": [], "changed_truncated": False,
              "runner": cfg.get("runner"), "started": started_at, "finished": None}
    rid = capabilities.receipt(RECEIPTS, fields)
    if cfg.get("runner") == "orca":
        # Inside the job dir for orca: run_visible re-creates the dir and clears
        # stdout/stderr/exit.json, but leaves any other file alone.
        jobdir = JOBS / str(n)
        jobdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = jobdir / "marker"
        marker.write_text("")
        r = run_visible(cfg, issue, job["argv"], pre, timeout=job["timeout"])
    else:
        fd, marker_name = tempfile.mkstemp(prefix="issue-bridge-marker-")
        os.close(fd)
        marker = pathlib.Path(marker_name)
        r = run(job["argv"], timeout=job["timeout"])
    finished_at = now()

    try:
        changed_paths, truncated = capabilities.changed(job["changed_roots"], str(marker))
    except OSError:
        changed_paths, truncated = [], False
    finally:
        if cfg.get("runner") != "orca":
            marker.unlink(missing_ok=True)

    capabilities.receipt(RECEIPTS, dict(
        fields, exit=r["exit"], duration=r["duration"], changed_paths=changed_paths,
        changed_truncated=truncated, finished=finished_at), rid)

    out, err = budget(r["stdout"], r["stderr"])
    body = ("receipt: %s  changed: %d%s\n\n"
            "exit: `%s`  duration: `%ss`\n\n**stdout**\n```\n%s\n```\n"
            "**stderr**\n```\n%s\n```\n"
            % (rid, len(changed_paths), "+" if truncated else "", r["exit"], r["duration"], out, err))
    rec = {"issue": n, "done": r["exit"] == 0, "comment": legacy_note(cfg, issue) + body,
           "steps": {}, "attempts": 0, "created": now(),
           "body": _rb_body(issue.get("body") or "", r), "receipt": rid}
    save_pending(rec)
    return writeback(cfg, rec)


def handle_capability(cfg, issue, fm, rule):
    """The `capability:` body path: resolve through the table, then the kill
    switch, the rate cap and the class gate, in that order, before anything
    runs."""
    n = issue["number"]
    name = fm.get("capability", "")
    try:
        params = json.loads(fm.get("params", "{}"))
    except ValueError:
        params = None
    if not isinstance(params, dict):
        return not_run(cfg, issue, "**not run** - `params` must be a JSON object on one line.\n")
    if stale(fm, rule):
        return not_run(cfg, issue, "**not run** - missed its window (rule `%s`, ttl `%s`s, queued `%s`).\n"
                       % (rule, fm.get("ttl"), fm.get("queued_at")))

    job, reason = capabilities.resolve(cfg["_capabilities"], name, params)
    if job is None:
        # `reason` names the capability and the field, never a parameter value:
        # capabilities.resolve() guarantees that, so it is safe to post verbatim.
        return not_run(cfg, issue,
            "**denied** - %s\n\nThis is final for this exact request: refiling the same "
            "capability and parameters gets the same answer.\n" % reason)

    rate = cfg["rate"]
    over_total = capabilities.recent_count(RECEIPTS, 3600) >= rate["jobs_per_hour"]
    over_privileged = (job["class"] in capabilities.PRIVILEGED and
                       capabilities.recent_count(RECEIPTS, 3600, classes=capabilities.PRIVILEGED)
                       >= rate["privileged_per_hour"])
    if over_total or over_privileged:
        # Over the cap keeps the label and waits; it is never dropped.
        _status["rate_limited"] = n
        return None
    _status["rate_limited"] = None

    # The approval names the whole request, capability and params together, so
    # editing the body to another capability with the same params does not reuse it.
    dig = capabilities.digest({"capability": name, "params": params})
    ok, approval_id = class_gate(cfg, issue, job, dig)
    if not ok:
        return None

    return run_capability(cfg, issue, name, params, job, actor(cfg, issue), approval_id)


def handle(cfg, issue):
    n = issue["number"]
    rec = load_pending(n)
    if rec:
        # A journal entry means this job already executed (or was already answered).
        # Finish the paperwork; do not parse, and above all do not run, again.
        return writeback(cfg, rec)
    # Kill switch, before the body is even parsed: nothing runs, nothing is
    # commented, the label stays, and the job is picked up once the file goes.
    # Journal writebacks above still finish, since their command already ran.
    _status["disabled"] = capabilities.disabled(HOME)
    if _status["disabled"]:
        return None
    fm = parse(issue.get("body"))
    rule = fm.get("rule", "drain-on-wake")

    if "capability" in fm:
        return handle_capability(cfg, issue, fm, rule)

    try:
        argv = json.loads(fm.get("argv", "null"))
    except ValueError:
        argv = None

    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        return not_run(cfg, issue,
            "**not run** - the body did not parse.\n\n`argv` must be a JSON array "
            "of strings on one line:\n\n```\n---\nargv: [\"uname\", \"-a\"]\n"
            "rule: drain-on-wake\nqueued_at: %s\nttl: 3600\n---\n```\n" % now())
    if stale(fm, rule):
        return not_run(cfg, issue, "**not run** - missed its window (rule `%s`, ttl `%s`s, queued `%s`).\n"
                       % (rule, fm.get("ttl"), fm.get("queued_at")))
    if not cfg["allow"]:
        # Migration compat (spec.md): once `allow` is emptied, an `argv:` body is
        # not merely unmatched, it is off the map entirely. Point at the door
        # that is actually open now, instead of the generic allowlist denial.
        return not_run(cfg, issue,
            "**denied** - this machine no longer accepts a plain `argv:` body; "
            "`allow` is empty. File the job as `capability: <name>` with `params:` "
            "instead. See AGENTS.md.\n")
    if not allowed(argv, cfg["allow"]):
        # The whole argv, not argv[0]: an entry like `git -C /srv/repo` denies on a
        # later token, and naming only `git` would read as a lie.
        return not_run(cfg, issue,
            "**denied** - `%s` is not on this machine's allowlist, so nothing ran.\n\n"
            "This is final: refiling the same argv gets the same answer. Ask the "
            "machine's owner to add it to `allow` in the poller config.\n"
            % clip(shlex.join(argv), 200))

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
    issues.sort(key=lambda i: i.get("created_at") or "")
    return issues


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

    # --- F8: `wait` failing to answer must not read as "the job ended" ---
    with tempfile.TemporaryDirectory() as d:
        real_orca, real_sleep = orca, time.sleep
        calls_f8, n_waits = [], {"n": 0}
        jobdir = pathlib.Path(d) / "jobs" / "55"
        jobdir.mkdir(parents=True)

        def fake_orca_f8(cfg, *args, timeout=60):
            calls_f8.append(args[:2])
            if args[:2] == ("terminal", "wait"):
                n_waits["n"] += 1
                if n_waits["n"] == 1:
                    return None  # comms hiccup: wait itself fails to answer
                (jobdir / "exit.json").write_text(json.dumps({"exit": 0, "duration": 0.01, "ts": now()}))
                return {"wait": {"satisfied": True}}
            if args[:2] == ("terminal", "show"):
                return None  # show ALSO fails: this is exactly the F8 trap
            return {}

        globals()["orca"], time.sleep = fake_orca_f8, lambda s: None
        try:
            r = await_job({"repo": "x/y"}, jobdir, "term_f8", time.time(), timeout=5)
            check("F8 recovers the result after wait and show both fail once", r["exit"] == 0)
            check("F8 re-checks terminal show on a failed wait", ("terminal", "show") in calls_f8)
            check("F8 does not give up after a single comms hiccup", n_waits["n"] >= 2)
        finally:
            globals()["orca"], time.sleep = real_orca, real_sleep

    # --- capability path: kill switch, rate cap, class gate, receipts ---
    with tempfile.TemporaryDirectory() as d:
        real_home, real_receipts = HOME, RECEIPTS
        globals()["HOME"] = pathlib.Path(d) / "home"
        globals()["HOME"].mkdir()
        globals()["RECEIPTS"] = globals()["HOME"] / "receipts"
        real_status, real_pending = STATUS, PENDING
        globals()["STATUS"] = pathlib.Path(d) / "status.json"
        globals()["PENDING"] = pathlib.Path(d) / "pending"
        real_gh2, real_run2 = gh, run
        _status["approval_asked"], _status["disabled"], _status["rate_limited"] = [], False, None

        # capabilities.resolve() checks changed_roots against the real $HOME, not
        # ISSUE_BRIDGE_HOME; patch os.path.expanduser (shared with capabilities.py,
        # same os module) so the check runs entirely inside this tempdir instead of
        # touching the real home directory.
        real_expanduser = os.path.expanduser
        os.path.expanduser = lambda p, _h=globals()["HOME"]: str(_h) if p == "~" else real_expanduser(p)

        target = globals()["HOME"] / "target"
        target.mkdir()
        gh_state = {}

        def issue_state(n):
            return gh_state.setdefault(n, {"closed": False, "labels": set(), "body": "",
                                           "comments": [], "events": [], "next_id": 1})

        calls2 = []

        def fake_gh2(cfg, method, path, body=None):
            calls2.append((method, path))
            m = re.match(r"^/issues/(\d+)(/.*)?$", path.split("?")[0])
            n = int(m.group(1))
            rest = m.group(2) or ""
            st = issue_state(n)
            if method == "GET" and rest.startswith("/comments"):
                return [{"id": c["id"], "user": {"login": c["login"]}, "body": c["body"]}
                        for c in st["comments"]]
            if method == "GET" and rest.startswith("/events"):
                return st["events"]
            if method == "POST" and rest == "/comments":
                cid = st["next_id"]
                st["next_id"] += 1
                st["comments"].append({"id": cid, "login": "poller-bot", "body": body["body"]})
                return {"id": cid}
            if method == "POST" and rest == "/labels":
                st["labels"] |= set(body["labels"])
                return {}
            if method == "DELETE" and rest.startswith("/labels/"):
                st["labels"].discard(urllib.parse.unquote(rest.split("/labels/", 1)[1]))
                return {}
            if method == "PATCH" and rest == "":
                if body and "body" in body:
                    st["body"] = body["body"]
                if body and body.get("state") == "closed":
                    st["closed"] = True
                return {}
            return {}

        write_prog = "import sys, pathlib; pathlib.Path(sys.argv[1]).write_text('hi')"
        cap_cfg = {"capabilities": {
            "write-file": {"argv": [sys.executable, "-c", write_prog, "{path}"],
                           "params": {"path": r"^%s/[a-z0-9]{1,20}\.txt$" % re.escape(str(target))},
                           "class": "mutate", "timeout": 30, "changed_roots": [str(target)]},
            "danger": {"argv": [sys.executable, "-c", "print('danger-ran')"], "params": {},
                       "class": "destructive", "timeout": 30, "changed_roots": []},
        }}
        cfg2 = {"repo": "x/y", "label": "exec-job", "allow": [], "owner": "abhaymettu",
                "rate": {"jobs_per_hour": 30, "privileged_per_hour": 5}, "runner": "subprocess"}
        cfg2["_capabilities"] = capabilities.load(cap_cfg)

        globals()["gh"], globals()["run"] = fake_gh2, real_run2
        try:
            # A: a mutate capability runs, writes a receipt with actor and changed path.
            n = 201
            issue_state(n)["labels"] = {"exec-job"}
            issue_state(n)["events"] = [{"event": "labeled", "label": {"name": "exec-job"},
                                         "actor": {"login": "labeller1"}}]
            issue = {"number": n, "user": {"login": "filer1"},
                     "body": '---\ncapability: write-file\nparams: {"path": "%s/out.txt"}\n---\n' % target}
            handle(cfg2, issue)
            receipts = sorted(pathlib.Path(globals()["RECEIPTS"]).glob("*-%d.json" % n))
            check("capability run writes exactly one receipt", len(receipts) == 1)
            data = json.loads(receipts[0].read_text()) if receipts else {}
            check("receipt records the actor", data.get("actor") == {"author": "filer1", "labelled_by": "labeller1"})
            check("receipt records the changed path",
                  os.path.realpath(target / "out.txt") in (data.get("changed_paths") or []))
            check("receipt exit is 0", data.get("exit") == 0)
            check("result comment carries the receipt id",
                  data.get("id", "\0") in issue_state(n)["comments"][-1]["body"])
            check("mutate capability closes the issue done", issue_state(n)["closed"]
                  and "exec-done" in issue_state(n)["labels"])

            # B: unknown capability is denied, never echoing the parameter value.
            n = 202
            issue_state(n)["labels"] = {"exec-job"}
            issue = {"number": n, "body":
                     '---\ncapability: does-not-exist\nparams: {"secret": "SECRETVALUE123"}\n---\n'}
            handle(cfg2, issue)
            check("unknown capability is denied", "denied" in issue_state(n)["comments"][-1]["body"])
            check("unknown-capability denial never carries a parameter value",
                  "SECRETVALUE123" not in issue_state(n)["comments"][-1]["body"])

            # C: a regex miss is denied, never echoing the parameter value.
            n = 203
            issue_state(n)["labels"] = {"exec-job"}
            issue = {"number": n, "body":
                     '---\ncapability: write-file\nparams: {"path": "SECRETPATHVALUE"}\n---\n'}
            handle(cfg2, issue)
            check("a param regex miss is denied", "denied" in issue_state(n)["comments"][-1]["body"])
            check("regex-miss denial never carries the parameter value",
                  "SECRETPATHVALUE" not in issue_state(n)["comments"][-1]["body"])

            # D: DISABLED leaves the label and posts nothing.
            n = 204
            issue_state(n)["labels"] = {"exec-job"}
            (globals()["HOME"] / "DISABLED").write_text("")
            before = len(calls2)
            issue = {"number": n, "body":
                     '---\ncapability: write-file\nparams: {"path": "%s/d.txt"}\n---\n' % target}
            result = handle(cfg2, issue)
            check("DISABLED runs no job", result is None)
            check("DISABLED makes no GitHub call at all", len(calls2) == before)
            check("DISABLED leaves the label untouched", issue_state(n)["labels"] == {"exec-job"})
            # The switch is above the body parse, so a legacy argv job stops too.
            cfg2["allow"] = ["uname"]
            result = handle(cfg2, {"number": n, "body": '---\nargv: ["uname"]\n---\n'})
            check("DISABLED stops a legacy argv job as well", result is None and len(calls2) == before)
            cfg2["allow"] = []
            (globals()["HOME"] / "DISABLED").unlink()

            # E: over the rate cap leaves the label and posts nothing.
            n = 205
            issue_state(n)["labels"] = {"exec-job"}
            cfg_rate = dict(cfg2, rate={"jobs_per_hour": 0, "privileged_per_hour": 5})
            before = len(calls2)
            issue = {"number": n, "body":
                     '---\ncapability: write-file\nparams: {"path": "%s/e.txt"}\n---\n' % target}
            result = handle(cfg_rate, issue)
            check("over the rate cap runs no job", result is None)
            check("rate cap makes no GitHub call", len(calls2) == before)
            check("rate cap leaves the label untouched", issue_state(n)["labels"] == {"exec-job"})
            check("rate cap is visible in status", _status["rate_limited"] == n)

            # F: a privileged class without approval posts exactly one comment
            # across two cycles, and leaves the label.
            n = 206
            issue_state(n)["labels"] = {"exec-job"}
            issue = {"number": n, "body": '---\ncapability: danger\nparams: {}\n---\n'}
            handle(cfg2, issue)
            handle(cfg2, issue)
            check("privileged class without approval posts exactly one comment",
                  len(issue_state(n)["comments"]) == 1)
            check("the approval comment names the capability and its class",
                  "danger" in issue_state(n)["comments"][0]["body"]
                  and "destructive" in issue_state(n)["comments"][0]["body"])
            check("a pending-approval job leaves the label", issue_state(n)["labels"] == {"exec-job"})

            # G: a matching owner /approve comment lets it run, and the receipt
            # carries that comment's id.
            digest8 = capabilities.digest({"capability": "danger", "params": {}})
            approval_id = issue_state(n)["next_id"]
            issue_state(n)["comments"].append(
                {"id": approval_id, "login": "abhaymettu", "body": "/approve %s" % digest8})
            issue_state(n)["next_id"] += 1
            handle(cfg2, issue)
            receipts206 = sorted(pathlib.Path(globals()["RECEIPTS"]).glob("*-%d.json" % n))
            check("approved privileged job writes one receipt", len(receipts206) == 1)
            data206 = json.loads(receipts206[0].read_text()) if receipts206 else {}
            check("the receipt carries the approving comment's id", data206.get("approval") == approval_id)
            check("approved privileged job closes the issue",
                  issue_state(n)["closed"] and "exec-done" in issue_state(n)["labels"])

            # H: the same approval comment id is not reused for a second run.
            _status["approval_asked"] = []
            before_receipts = len(receipts206)
            before_comments = len(issue_state(n)["comments"])
            handle(cfg2, issue)  # journal was retired by G's success; this re-parses the body
            receipts206b = sorted(pathlib.Path(globals()["RECEIPTS"]).glob("*-%d.json" % n))
            check("a spent approval id does not run the job again", len(receipts206b) == before_receipts)
            check("a spent approval id asks for a fresh approval instead",
                  len(issue_state(n)["comments"]) == before_comments + 1)
            check("waiting on a fresh approval leaves the label where G left it",
                  issue_state(n)["labels"] == {"exec-done"})

            # I: an `argv:` body still works while `allow` is non-empty (migration compat).
            n = 207
            issue_state(n)["labels"] = {"exec-job"}
            cfg_allow = dict(cfg2, allow=["uname"])
            issue = {"number": n, "body": '---\nargv: ["uname", "-a"]\n---\n'}
            handle(cfg_allow, issue)
            check("argv body still runs while allow is non-empty",
                  issue_state(n)["closed"] and "exec-done" in issue_state(n)["labels"])

            # J: an `argv:` body is denied once `allow` is empty, pointing at `capability:`.
            n = 208
            issue_state(n)["labels"] = {"exec-job"}
            issue = {"number": n, "body": '---\nargv: ["uname", "-a"]\n---\n'}
            handle(cfg2, issue)
            check("argv body is denied once allow is empty",
                  "denied" in issue_state(n)["comments"][-1]["body"])
            check("the empty-allow denial points at capability:",
                  "capability:" in issue_state(n)["comments"][-1]["body"])
        finally:
            globals()["gh"], globals()["run"] = real_gh2, real_run2
            globals()["HOME"], globals()["RECEIPTS"] = real_home, real_receipts
            globals()["STATUS"], globals()["PENDING"] = real_status, real_pending
            os.path.expanduser = real_expanduser

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
        cycle(cfg)
        if once:
            break
        time.sleep(cfg["poll_interval"])
