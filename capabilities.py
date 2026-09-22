#!/usr/bin/env python3
"""capabilities.py - named capabilities for issue-bridge and, later, direct-line
and the gateway.

A capability is a fixed argv template with typed parameters, a class, a
timeout and the roots it may change. Callers give a name and parameters,
never argv, so nothing that reaches this module can start an arbitrary
program or type free text into a shell.

Stdlib only. Imported by bridge-poller.py; not run as a daemon itself.
"""
import calendar, hashlib, json, os, pathlib, re, sys, tempfile, time

CLASSES = ("read", "mutate", "destructive", "spend", "external-send")
PRIVILEGED = ("destructive", "spend", "external-send")

# A whole argv token that is exactly one placeholder, e.g. "{repo}".
PLACEHOLDER = re.compile(r"^\{([a-zA-Z0-9_]+)\}$")
# Any placeholder name embedded in a larger string, for changed_roots entries
# like "/Users/abhay/src/{repo}" where the placeholder is one path segment,
# not the whole entry.
EMBEDDED = re.compile(r"\{([a-zA-Z0-9_]+)\}")


# --- table loading, fails closed --------------------------------------

def load(cfg):
    """Validate cfg["capabilities"] and return a table name -> capability.

    Any bad entry raises ValueError naming the capability and the field, so a
    single config typo fails the whole table rather than admitting the rest
    of it silently.
    """
    caps = cfg.get("capabilities")
    if caps is None:
        caps = {}
    if not isinstance(caps, dict):
        raise ValueError("capabilities: must be an object")
    return {name: _load_one(name, spec) for name, spec in caps.items()}


def _load_one(name, spec):
    if not isinstance(spec, dict):
        raise ValueError("capability %s: must be an object" % name)

    argv = spec.get("argv")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        raise ValueError("capability %s: argv must be a non-empty list of strings" % name)

    raw_params = spec.get("params") if spec.get("params") is not None else {}
    if not isinstance(raw_params, dict):
        raise ValueError("capability %s: params must be an object" % name)
    patterns = {}
    for pname, pat in raw_params.items():
        if not isinstance(pname, str) or not isinstance(pat, str):
            raise ValueError("capability %s: params.%s: name and pattern must be strings" % (name, pname))
        try:
            patterns[pname] = re.compile(pat)
        except re.error as e:
            raise ValueError("capability %s: params.%s: bad regex: %s" % (name, pname, e))

    # Every {x} in argv must be a whole token, and x must be a declared param.
    # A token that merely contains "{" without being a clean whole-token
    # placeholder is a config error: it would otherwise substitute silently
    # wrong or not at all.
    for tok in argv:
        if "{" in tok:
            m = PLACEHOLDER.match(tok)
            if not m:
                raise ValueError("capability %s: argv token %r is not a whole-token placeholder" % (name, tok))
            if m.group(1) not in patterns:
                raise ValueError("capability %s: argv placeholder {%s} has no matching params entry"
                                 % (name, m.group(1)))

    cls = spec.get("class")
    if cls not in CLASSES:
        raise ValueError("capability %s: class must be one of %s" % (name, ", ".join(CLASSES)))

    timeout = spec.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("capability %s: timeout must be a positive integer" % name)

    roots = spec.get("changed_roots") if spec.get("changed_roots") is not None else []
    if not (isinstance(roots, list) and all(isinstance(r, str) for r in roots)):
        raise ValueError("capability %s: changed_roots must be a list of strings" % name)
    for r in roots:
        for pname in EMBEDDED.findall(r):
            if pname not in patterns:
                raise ValueError("capability %s: changed_roots placeholder {%s} has no matching params entry"
                                 % (name, pname))

    return {"argv": argv, "params": patterns, "class": cls, "timeout": timeout, "changed_roots": roots}


# --- resolving one request ----------------------------------------------

def resolve(table, name, params):
    """(job, None) on success, (None, reason) on refusal. A refusal names the
    capability and the field; it never carries a parameter value, so a deny
    comment or log line is safe to post verbatim."""
    cap = table.get(name)
    if cap is None:
        return None, "unknown capability %r" % name
    if not isinstance(params, dict):
        return None, "capability %s: params must be an object" % name

    declared = cap["params"]
    unknown = sorted(set(params) - set(declared))
    if unknown:
        return None, "capability %s: unknown parameter(s) %s" % (name, ", ".join(unknown))
    missing = sorted(set(declared) - set(params))
    if missing:
        return None, "capability %s: missing parameter(s) %s" % (name, ", ".join(missing))

    for pname, val in params.items():
        if not isinstance(val, str):
            return None, "capability %s: parameter %s must be a string" % (name, pname)
        if "\x00" in val:
            return None, "capability %s: parameter %s is refused" % (name, pname)
        if not declared[pname].fullmatch(val):
            return None, "capability %s: parameter %s does not match its pattern" % (name, pname)

    argv = []
    for tok in cap["argv"]:
        m = PLACEHOLDER.match(tok) if "{" in tok else None
        argv.append(params[m.group(1)] if m else tok)

    home = os.path.realpath(os.path.expanduser("~"))
    roots = []
    for entry in cap["changed_roots"]:
        # One pass, so a value that itself contains "{x}" is never re-expanded.
        expanded = EMBEDDED.sub(lambda m: params[m.group(1)], entry)
        if not expanded.startswith("/"):
            return None, "capability %s: changed_roots resolves to a non-absolute path" % name
        real = os.path.realpath(expanded)
        # Strictly under the home: the home itself would walk everything on every run.
        if not real.startswith(home + os.sep):
            return None, "capability %s: changed_roots resolves outside $HOME" % name
        roots.append(real)

    return {"argv": argv, "class": cap["class"], "timeout": cap["timeout"],
            "changed_roots": roots, "name": name}, None


# --- what a run touched ---------------------------------------------------

def changed(roots, marker):
    """Files and dirs under roots whose st_mtime_ns is newer than marker's,
    capped at 200 paths. Symlinked dirs are skipped, not descended into and
    not reported, so a capability cannot be fooled into "changing" whatever a
    symlink under its root points at."""
    cutoff = os.stat(marker).st_mtime_ns
    found = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            keep = []
            for d in dirnames:
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    continue
                keep.append(d)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if st.st_mtime_ns > cutoff:
                    found.append(full)
            dirnames[:] = keep
            for f in filenames:
                full = os.path.join(dirpath, f)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if st.st_mtime_ns > cutoff:
                    found.append(full)
    found.sort()
    if len(found) > 200:
        return found[:200], True
    return found, False


# --- receipts, the ledger this whole module answers to --------------------

def receipt(receipts_dir, fields, rid=None):
    """Write one receipt, 0600, atomically. Returns its id (the filename
    without .json), which callers embed in the result comment. Passing an
    existing id overwrites that receipt: the poller writes one before the run,
    so an approval is spent the moment the job starts, and rewrites it after."""
    receipts_dir = pathlib.Path(receipts_dir)
    receipts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    rid = rid or "%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), fields.get("issue"))
    path = receipts_dir / (rid + ".json")
    tmp_path = receipts_dir / (rid + ".tmp")
    data = dict(fields, id=rid)
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=1) + "\n")
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    tmp_path.replace(path)
    return rid


def recent_count(receipts_dir, seconds, classes=None):
    """How many receipts started within the last `seconds`. With `classes`
    given, only those whose recorded class is in it; that path reads each
    file, the rest is a filename scan."""
    receipts_dir = pathlib.Path(receipts_dir)
    if not receipts_dir.is_dir():
        return 0
    cutoff = time.time() - seconds
    n = 0
    for p in receipts_dir.glob("*.json"):
        try:  # the filename's leading timestamp; no need to open the file for this
            ts = calendar.timegm(time.strptime(p.name.split("-", 1)[0], "%Y%m%dT%H%M%SZ"))
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if classes is None:
            n += 1
            continue
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if data.get("class") in classes:
            n += 1
    return n


# --- kill switch and approval digests --------------------------------------

def disabled(home):
    return (pathlib.Path(home) / "DISABLED").exists()


def digest(params):
    return hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- self-test -------------------------------------------------------------

def self_test():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))

    # --- load(): good table ---
    # argv placeholders must be a whole token, so a capability whose path has a
    # fixed prefix names the parameter after the full path, not after the
    # variable segment alone; changed_roots may still embed it (below).
    good_cfg = {"capabilities": {
        "git-pull": {"argv": ["git", "-C", "{repo_path}", "pull", "--ff-only"],
                     "params": {"repo_path": r"^/Users/abhay/src/[a-z0-9-]{1,40}$"},
                     "class": "mutate", "timeout": 120,
                     "changed_roots": ["{repo_path}"]},
        "uname": {"argv": ["uname", "-a"], "params": {}, "class": "read", "timeout": 10,
                  "changed_roots": []},
        "write-note": {"argv": ["touch", "{note}"], "params": {"note": "^[a-z0-9]{1,20}$"},
                       "class": "mutate", "timeout": 10,
                       "changed_roots": ["/Users/abhay/notes/{note}.txt"]},
    }}
    table = load(good_cfg)
    check("load accepts a good table", set(table) == {"git-pull", "uname", "write-note"})

    def bad(cfg):
        try:
            load(cfg)
            return None
        except ValueError as e:
            return str(e)

    check("load rejects non-dict capabilities", bad({"capabilities": []}) is not None)
    check("load rejects missing argv",
          (bad({"capabilities": {"x": {"class": "read", "timeout": 1}}}) or "").find("x: argv") >= 0)
    check("load rejects empty argv",
          "argv" in (bad({"capabilities": {"x": {"argv": [], "class": "read", "timeout": 1}}}) or ""))
    check("load rejects non-string argv element",
          bad({"capabilities": {"x": {"argv": ["ok", 1], "class": "read", "timeout": 1}}}) is not None)
    check("load rejects bad param regex",
          "bad regex" in (bad({"capabilities": {"x": {"argv": ["a"], "params": {"p": "("},
                                                  "class": "read", "timeout": 1}}}) or ""))
    check("load rejects a partial-token placeholder",
          "whole-token" in (bad({"capabilities": {"x": {
              "argv": ["git", "-C", "/x/{repo}-extra"], "params": {"repo": "^[a-z]+$"},
              "class": "read", "timeout": 1}}}) or ""))
    check("load rejects an argv placeholder with no matching param",
          "no matching params" in (bad({"capabilities": {"x": {
              "argv": ["{missing}"], "params": {}, "class": "read", "timeout": 1}}}) or ""))
    check("load rejects an unknown class",
          "class" in (bad({"capabilities": {"x": {"argv": ["a"], "class": "nuke", "timeout": 1}}}) or ""))
    check("load rejects a zero timeout",
          "timeout" in (bad({"capabilities": {"x": {"argv": ["a"], "class": "read", "timeout": 0}}}) or ""))
    check("load rejects a bool timeout",
          "timeout" in (bad({"capabilities": {"x": {"argv": ["a"], "class": "read", "timeout": True}}}) or ""))
    check("load rejects non-list changed_roots",
          "changed_roots" in (bad({"capabilities": {"x": {
              "argv": ["a"], "class": "read", "timeout": 1, "changed_roots": "nope"}}}) or ""))
    check("load rejects a changed_roots placeholder with no matching param",
          "changed_roots placeholder" in (bad({"capabilities": {"x": {
              "argv": ["a"], "class": "read", "timeout": 1,
              "changed_roots": ["/Users/abhay/{repo}"]}}}) or ""))
    check("load error names the capability", "capability x:" in (bad({"capabilities": {
        "x": {"argv": [], "class": "read", "timeout": 1}}}) or ""))

    # --- resolve(): the good path ---
    repo_path = "/Users/abhay/src/issue-bridge"
    job, reason = resolve(table, "git-pull", {"repo_path": repo_path})
    check("resolve substitutes a whole-token argv placeholder",
          job is not None and job["argv"] == ["git", "-C", repo_path, "pull", "--ff-only"])
    check("resolve carries class and timeout through",
          job["class"] == "mutate" and job["timeout"] == 120)
    check("resolve substitutes a whole-entry changed_roots placeholder",
          job["changed_roots"] == [os.path.realpath(repo_path)])
    check("resolve on a no-param capability needs no params",
          resolve(table, "uname", {})[0]["argv"] == ["uname", "-a"])

    job, reason = resolve(table, "write-note", {"note": "todo"})
    check("resolve substitutes an embedded changed_roots placeholder",
          job["changed_roots"] == [os.path.realpath("/Users/abhay/notes/todo.txt")])

    # --- resolve(): refusals, none of which may echo a parameter value ---
    job, reason = resolve(table, "does-not-exist", {})
    check("resolve refuses an unknown capability", job is None and "does-not-exist" in reason)

    job, reason = resolve(table, "git-pull", {"repo_path": repo_path, "extra": "SECRETVALUE"})
    check("resolve refuses an unknown parameter", job is None and "extra" in reason)
    check("unknown-parameter refusal never carries the value", "SECRETVALUE" not in reason)

    job, reason = resolve(table, "git-pull", {})
    check("resolve refuses a missing parameter", job is None and "repo_path" in reason)

    job, reason = resolve(table, "git-pull", {"repo_path": "/Users/abhay/src/SECRETVALUE!not-allowed"})
    check("resolve refuses a regex miss", job is None)
    check("regex-miss refusal never carries the value", "SECRETVALUE" not in reason)

    job, reason = resolve(table, "git-pull", {"repo_path": "/Users/abhay/src/a\x00b"})
    check("resolve refuses a NUL byte", job is None)

    job, reason = resolve(table, "git-pull", {"repo_path": 7})
    check("resolve refuses a non-string parameter value", job is None)

    escape_table = load({"capabilities": {"esc": {
        "argv": ["cat", "{p}"], "params": {"p": "^[a-zA-Z0-9/_.-]+$"}, "class": "read",
        "timeout": 5, "changed_roots": ["/Users/abhay/{p}"]}}})
    job, reason = resolve(escape_table, "esc", {"p": "../../etc/passwd"})
    check("resolve refuses a changed_roots escape outside $HOME", job is None)

    # --- changed(): the marker walk ---
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d) / "root"
        root.mkdir()
        old = root / "old.txt"
        old.write_text("old")
        marker = pathlib.Path(d) / "marker"
        marker.write_text("m")
        time.sleep(0.02)
        new = root / "new.txt"
        new.write_text("new")
        sub = root / "sub"
        sub.mkdir()
        (sub / "inner.txt").write_text("inner")
        paths, truncated = changed([str(root)], str(marker))
        check("changed reports a new file", str(new) in paths)
        check("changed reports a new dir", str(sub) in paths)
        check("changed reports a new file inside a new dir", str(sub / "inner.txt") in paths)
        check("changed does not report an untouched file", str(old) not in paths)
        check("changed is not truncated under the cap", not truncated)

        symtarget = pathlib.Path(d) / "outside"
        symtarget.mkdir()
        (symtarget / "later.txt").write_text("later")
        symlink = root / "linked"
        try:
            symlink.symlink_to(symtarget)
            paths2, _ = changed([str(root)], str(marker))
            check("changed skips a symlinked dir", str(symlink) not in paths2
                  and not any(p.startswith(str(symtarget)) for p in paths2))
        except OSError:
            check("changed skips a symlinked dir", True)  # no symlink permission in this sandbox

        many = pathlib.Path(d) / "many"
        many.mkdir()
        for i in range(210):
            (many / ("f%03d" % i)).write_text("x")
        paths3, truncated3 = changed([str(many)], str(marker))
        check("changed caps at 200", len(paths3) == 200)
        check("changed reports truncated past the cap", truncated3)

    # --- receipt() and recent_count() ---
    with tempfile.TemporaryDirectory() as d:
        rdir = pathlib.Path(d) / "receipts"
        rid = receipt(rdir, {"issue": 481, "capability": "git-pull", "class": "mutate",
                             "actor": {"author": "abhaymettu", "labelled_by": "abhaymettu"}})
        check("receipt id embeds the issue number", rid.endswith("-481"))
        stored = json.loads((rdir / (rid + ".json")).read_text())
        check("receipt round-trips its fields",
              stored["capability"] == "git-pull" and stored["id"] == rid)
        mode = os.stat(rdir / (rid + ".json")).st_mode & 0o777
        check("receipt file is 0600", mode == 0o600)
        dmode = os.stat(rdir).st_mode & 0o777
        check("receipt dir is 0700", dmode == 0o700)

        check("recent_count sees a fresh receipt", recent_count(rdir, 3600) == 1)
        check("recent_count respects the window", recent_count(rdir, 0) == 0)
        check("recent_count with a class filter matches", recent_count(rdir, 3600, classes=("mutate",)) == 1)
        check("recent_count with a non-matching class filter excludes it",
              recent_count(rdir, 3600, classes=("destructive",)) == 0)

        old_name = "20000101T000000Z-1.json"
        (rdir / old_name).write_text(json.dumps({"class": "read"}))
        check("recent_count ignores an old receipt", recent_count(rdir, 3600) == 1)
        check("recent_count on a missing dir is zero", recent_count(pathlib.Path(d) / "nope", 3600) == 0)

    # --- disabled() ---
    with tempfile.TemporaryDirectory() as d:
        check("disabled is false without the file", not disabled(d))
        (pathlib.Path(d) / "DISABLED").write_text("")
        check("disabled is true with the file", disabled(d))

    # --- receipt overwrite by id ---
    with tempfile.TemporaryDirectory() as d:
        rid = receipt(d, {"issue": 7, "exit": None})
        check("receipt with the same id overwrites, not duplicates",
              receipt(d, {"issue": 7, "exit": 0}, rid) == rid
              and len(list(pathlib.Path(d).glob("*.json"))) == 1
              and json.loads((pathlib.Path(d) / (rid + ".json")).read_text())["exit"] == 0)

    # --- digest ---
    check("digest is stable across key order",
          digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1}))

    for name, ok in results:
        print("%-58s %s" % (name, "PASS" if ok else "FAIL"))
    bad_n = sum(1 for _, ok in results if not ok)
    print("\n%d/%d passed" % (len(results) - bad_n, len(results)))
    return 1 if bad_n else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    print("capabilities.py is a library; run with --self-test", file=sys.stderr)
    sys.exit(2)
