#!/usr/bin/env python3
"""issue-bridge poller - turns GitHub issues into commands on this machine.

Any agent that can open a GitHub issue files one labelled `exec-job` on a single
designated repo. This polls that repo, checks the argv against a local
allowlist, runs it, comments the output back, and closes the issue.

Stdlib only. The only network it needs is outbound https to api.github.com:
no inbound port, no tunnel, no VPN.

Protocol: AGENTS.md. Setup: README.md.
"""
import calendar, json, os, pathlib, shlex, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request

VERSION = "1.0.0"
HOME = pathlib.Path(os.environ.get("ISSUE_BRIDGE_HOME")
                    or pathlib.Path.home() / ".config/issue-bridge")
CONFIG, TOKEN, STATUS = HOME / "config.json", HOME / "github-token", HOME / "status.json"
API = "https://api.github.com/repos/"
COMMENT_BUDGET = 58000  # GitHub caps a single comment at 65536 chars
TIMEOUT = 900           # per-command wall clock

_status = {"version": VERSION, "ts": 0, "last_poll_ok": None, "last_drain_ts": None,
           "queue_depth": 0, "last_error": None, "pat_expiry": None}


def config():
    cfg = json.loads(CONFIG.read_text())
    cfg.setdefault("label", "exec-job")
    cfg.setdefault("poll_interval", 60)
    cfg.setdefault("allow", [])
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


def relabel(cfg, n, add):
    try:
        gh(cfg, "DELETE", "/issues/%d/labels/%s" % (n, urllib.parse.quote(cfg["label"])))
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


def handle(cfg, issue):
    n = issue["number"]
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
        body = ("**denied** - `%s` is not on this machine's allowlist, so nothing ran.\n\n"
                "This is final: refiling the same argv gets the same answer. Ask the "
                "machine's owner to add it to `allow` in the poller config.\n"
                % clip(argv[0], 200))
    else:
        # Drop the label BEFORE running. A crash between here and the comment
        # loses the result, which is survivable; keeping the label would re-run
        # a command with side effects, which is not.
        relabel(cfg, n, None)
        r = run(argv)
        out, err = budget(r["stdout"], r["stderr"])
        done = r["exit"] == 0
        body = ("exit: `%s`  duration: `%ss`\n\n**stdout**\n```\n%s\n```\n"
                "**stderr**\n```\n%s\n```\n" % (r["exit"], r["duration"], out, err))

    # Comment first: the result is the part worth keeping if the next call fails.
    gh(cfg, "POST", "/issues/%d/comments" % n, {"body": body})
    relabel(cfg, n, "exec-done" if done else "exec-failed")
    gh(cfg, "PATCH", "/issues/%d" % n,
       {"state": "closed", "state_reason": "completed" if done else "not_planned"})


# --- loop ---------------------------------------------------------------

def cycle(cfg):
    q = urllib.parse.quote(cfg["label"])
    try:
        issues = gh(cfg, "GET", "/issues?state=open&labels=%s&sort=created"
                                "&direction=asc&per_page=30" % q)
        _status["last_poll_ok"], _status["last_error"] = time.time(), None
    except (urllib.error.HTTPError, OSError) as e:
        # Transient by construction: the issues keep their label and the next
        # cycle picks them up. A dead PAT looks the same and is visible in
        # last_error rather than in a log nobody reads.
        _status["last_error"] = "poll: %s" % e
        return write_status()

    issues = [i for i in (issues or []) if "pull_request" not in i]
    _status["queue_depth"] = len(issues)
    write_status()
    for issue in issues:
        try:
            handle(cfg, issue)
            _status["last_drain_ts"] = time.time()
        except Exception as e:  # one bad job must not stop the lane
            _status["last_error"] = "issue #%d: %r" % (issue["number"], e)
        write_status()


def self_test():
    """Offline check of the parts that decide whether a command runs."""
    global STATUS
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

    for name, ok in results:
        print("%-32s %s" % (name, "PASS" if ok else "FAIL"))
    bad = sum(1 for _, ok in results if not ok)
    print("\n%d/%d passed" % (len(results) - bad, len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
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
