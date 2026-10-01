#!/usr/bin/env python3
"""Kvitansiya — receipts for AI agent claims.

A Claude Code Stop hook. When the agent finishes and says "pushed", "deployed",
"tests pass", "created file X", Kvitansiya checks each claim against the real
world (git remote, live URL, file system, the session's own tool log). If a
claim is false, the stop is blocked and the agent gets the evidence.

Stdlib only. Modes:
  kvitansiya.py hook                    # Stop hook: JSON on stdin
  kvitansiya.py check "<text>" [--cwd DIR] [--transcript FILE] [--json]
  kvitansiya.py stats                   # how many false claims were caught
  kvitansiya.py install [--project DIR | --user] [--write]
"""
from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict

LOG_DIR = os.path.expanduser(os.environ.get("KVITANSIYA_HOME", "~/.kvitansiya"))
HTTP_TIMEOUT = float(os.environ.get("KVITANSIYA_HTTP_TIMEOUT", "8"))
GIT_TIMEOUT = float(os.environ.get("KVITANSIYA_GIT_TIMEOUT", "15"))

OK, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"
ICON = {OK: "✅", FAIL: "❌", WARN: "⚠️ ", SKIP: "·"}

# ---------------------------------------------------------------- claims ----

SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
URL_RE = re.compile(r"https?://[^\s)\]>`'\"«»,]+")
VERSION_RE = re.compile(r"\bv\d+\.\d+(?:\.\d+)?\b")
QUOTED_RE = re.compile(r"[\"“«]([^\"”»]{4,80})[\"”»]")
PATH_RE = re.compile(r"`([^`\s]*[A-Za-z_][^`\s]*\.[A-Za-z][A-Za-z0-9]{0,7})`|(?<![\w/.:-])((?:~?\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,7})\b")

PUSH_RE = re.compile(
    r"\b(pushed|pushlandi|push qil(?:dim|indi)|push etdim)\b|\b(?:github|gitlab|origin|remote|main|master)'?ga yukla(?:ndi|dim)\b|\bgit push\b[^.]{0,40}\b(bajarildi|succeeded|done|o'tdi)\b|\bran `?git push",
    re.I)
COMMIT_RE = re.compile(r"\b(committed|commit qil(?:dim|indi)|commit yaratdim|made a commit)\b", re.I)
DEPLOY_RE = re.compile(
    r"\b(deploy(?:ed|ment)?|is live|now live|went live|published|shipped to prod|deploy qil(?:dim|indi)|joylandi|joyladim|ishga tushdi)\b",
    re.I)
TEST_RE = re.compile(
    r"\b(all (?:the )?tests? pass(?:es|ed)?|tests? (?:are )?(?:all )?(?:passing|pass|passed|green)|\d+ passed|testlar? o'?t(?:di|yapti)|test(?:lar)? yashil)\b",
    re.I)
FILE_RE = re.compile(
    r"\b(created|wrote|saved|added (?:a |the )?(?:new )?file|generated|yaratdim|yozdim|saqladim|yaratildi|saqlandi)\b",
    re.I)

# a sentence with these is not a claim of a done action
NEG_RE = re.compile(
    r"\b(not|n't|never|didn't|haven't|hasn't|won't|will|should|could|would|can|need to|please|todo|next step|"
    r"if you|let me know|nothing|before (?:it|they|being)|no changes|qilmadim|tegmadim|tegilmadi|o'zgarmadi|qilinmadi|emas|yo'q|kerak|qilaman|qilamiz|qiling|qilsangiz|keyin|hali)\b|\?\s*$",
    re.I)
# negation reaches back over commas ("haven't committed, pushed, or deployed") but not over these
NEG_STOP_BEFORE = re.compile(r"[;—–]|\b(?:but|however|although|lekin|ammo|biroq)\b|,\s*(?:and\s+|so\s+)?(?:i|we)\b", re.I)
# negation after the claim only counts inside the same clause ("pushed nothing" vs "pushed the fix, nothing else changed")
NEG_STOP_AFTER = re.compile(r"[,;—–]|:\s|\b(?:but|however|lekin|ammo|biroq)\b", re.I)


def negated(s: str, pos: int) -> bool:
    """Is the claim word at `pos` inside the scope of a negation / plan / question?"""
    if re.search(r"\?\s*$", s):
        return True
    for m in NEG_RE.finditer(s):
        if m.start() < pos:
            if not NEG_STOP_BEFORE.search(s[m.end():pos]):
                return True
        elif not NEG_STOP_AFTER.search(s[pos:m.start()]):
            return True
    return False


@dataclass
class Claim:
    kind: str            # push | commit | deploy | tests | file
    sentence: str
    target: str = ""     # branch / url / path
    sha: str = ""
    expect: list = field(default_factory=list)  # strings that must be in the page
    status_only: bool = False  # "url → 200" is a status report, not a deploy of some version


@dataclass
class Receipt:
    claim: Claim
    status: str
    evidence: str


def _machine_block(m) -> str:
    """<<ISH>>{json}<<END>> report blocks: only the summary fields are the agent's claims;
    file lists, plans and critiques (<<REJA>>, <<TANQID>>) quote things, they don't claim them."""
    try:
        d = json.loads(m.group(1).strip())
    except ValueError:
        return " "
    if not isinstance(d, dict):
        return " "
    return "\n" + "\n".join(str(d[k]) for k in ("qisqa", "xulosa", "summary") if isinstance(d.get(k), str)) + "\n"


def sentences(text: str) -> list[str]:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)   # instructions in code fences are not claims
    text = re.sub(r"<<[A-Z_]+>>(.*?)(?:<<END>>|$)", _machine_block, text, flags=re.S)
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip(" -*•\t") for p in parts if p.strip(" -*•\t")]


def extract_claims(text: str) -> list[Claim]:
    claims: list[Claim] = []
    seen = set()

    def add(c: Claim):
        key = (c.kind, c.target, c.sha)
        if key not in seen:
            seen.add(key)
            claims.append(c)

    def hit(rx, s):
        m = rx.search(s)
        return m and not negated(s, m.start())

    for s in sentences(text):
        shas = [m for m in SHA_RE.findall(s) if re.search(r"\d", m) and re.search(r"[a-f]", m)]
        sha = shas[0] if shas else ""
        if hit(PUSH_RE, s):
            m = re.search(r"\b(?:to|->|→)\s+`?(?:(\w[\w.-]*)/)?([\w./-]+?)`?(?:\s|$|[.,;])", s)
            branch = ""
            if m and m.group(2) and not m.group(2).startswith("http"):
                branch = m.group(2)
            m2 = re.search(r"\b([\w./-]+?)'?ga (?:push (?:qil|etdim)|yukla)", s)
            if not branch and m2:
                branch = m2.group(1).split("/")[-1]
            if branch.lower() in ("the", "remote", "origin", "github", "gitlab", "repo", "repository"):
                branch = ""
            add(Claim("push", s, branch, sha))
        elif hit(COMMIT_RE, s):
            add(Claim("commit", s, "", sha))
        urls = URL_RE.findall(s)
        dm = DEPLOY_RE.search(s)
        if urls and dm and not negated(s, dm.start()):
            expect = QUOTED_RE.findall(s) + VERSION_RE.findall(s)
            for u in urls:
                add(Claim("deploy", s, u.rstrip(".,;:"), sha, expect))
        elif urls:
            for u in urls:  # no deploy word: only "https://x → HTTP 200" right after the url counts, and only its status
                m = re.search(re.escape(u) + r"[`'\")\]*]*\s*(?:→|->|:|=|—|returns|gives)?\s*(?:HTTP\s*)?[*`]*200\b", s)
                if m and not negated(s, m.start()):
                    add(Claim("deploy", s, u.rstrip(".,;:"), "", [], True))
        if hit(TEST_RE, s):
            add(Claim("tests", s))
        if hit(FILE_RE, s) and not urls:
            for a, b in PATH_RE.findall(s):
                p = a or b
                if p and not p.startswith(("http", "origin/")):
                    add(Claim("file", s, p))
    return claims

# -------------------------------------------------------------- checkers ----


def git(cwd: str, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT)
        return r.returncode, (r.stdout.strip() or r.stderr.strip())
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


CD_RE = re.compile(r"(?:^|&&|;|\|\|)\s*cd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)|\bgit\s+-C\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)")


GIT_WRITE_RE = re.compile(r"(?:^|&&|;|\|\||\n)\s*git(?:\s+-C\s+\S+)?\s+(commit|push)\b")
EXTRA_REPOS = [os.path.expanduser(p) for p in os.environ.get("KVITANSIYA_REPOS", "").split(os.pathsep) if p]


def session_repos(cwd: str, evs: list[dict], text: str = "") -> list[str]:
    """Git top-levels the agent worked in: cwd + every `cd X` / `git -C X` in its Bash calls
    + paths named in its message + KVITANSIYA_REPOS, newest first."""
    seen, out = set(), []
    dirs = EXTRA_REPOS + [os.path.expanduser(m) for m in re.findall(r"(?<![\w])(~?/[\w.@/-]+)", text)] + [cwd]
    for e in evs:
        if e["tool"] == "Bash":
            for a, b in CD_RE.findall(e["cmd"] or ""):
                d = os.path.expanduser((a or b).strip("\"'"))
                dirs.append(d if os.path.isabs(d) else os.path.join(cwd, d))
    for d in reversed(dirs):
        while d and not os.path.isdir(d) and d != os.path.dirname(d):
            d = os.path.dirname(d)
        rc, top = git(d, "rev-parse", "--show-toplevel") if d and d != "/" else (1, "")
        if rc == 0 and top not in seen:
            seen.add(top)
            out.append(top)
    return out


def repo_of_last(cmd: str, cwd: str, evs: list[dict]) -> str | None:
    for e in reversed(evs):
        if e["tool"] == "Bash" and cmd in (e["cmd"] or ""):
            rs = session_repos(cwd, [e])
            return rs[0] if rs else None
    return None


def push_repo(c: Claim, cwd: str, evs: list[dict], text: str = "") -> str | None:
    repos = session_repos(cwd, evs, text)
    if c.sha:
        for r in repos:
            if git(r, "cat-file", "-e", f"{c.sha}^{{commit}}")[0] == 0:
                return r
        return None
    last = repo_of_last("git push", cwd, evs)
    if last:
        return last
    top = git(cwd, "rev-parse", "--show-toplevel")
    return top[1] if top[0] == 0 else (repos[0] if repos else None)


def check_push(c: Claim, cwd: str, evs: list[dict] | None = None, text: str = "") -> Receipt:
    evs = evs or []
    repo = push_repo(c, cwd, evs, text)
    if not repo:
        if c.sha:
            ran = any(e["tool"] == "Bash" and GIT_WRITE_RE.search(e["cmd"] or "") for e in evs)
            return Receipt(c, FAIL if ran else WARN, f"commit {c.sha} is not in any local repo I can see"
                           + (" — although this session ran git here: the hash looks invented" if ran else " — cannot confirm the push"))
        return Receipt(c, SKIP, "not a git repo")
    cwd = repo
    last_push = next((e for e in reversed(evs) if e["tool"] == "Bash" and "git push" in (e["cmd"] or "")), None)
    if last_push and (last_push["error"] or re.search(r"\[rejected\]|failed to push|error: ", last_push["out"])):
        m = re.search(r"(\[rejected\].*|failed to push.*|error: .*)", last_push["out"])
        return Receipt(c, FAIL, f"the last `git push` in this session failed: {(m.group(0) if m else last_push['out'][:100]).strip()[:120]}")
    _, head = git(cwd, "rev-parse", "HEAD")
    _, cur = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    branch = c.target or cur
    rc, up = git(cwd, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
    remote = up.split("/")[0] if rc == 0 else "origin"
    rc, out = git(cwd, "ls-remote", remote, f"refs/heads/{branch}")
    if rc:
        first = next((l.strip() for l in out.splitlines() if l.strip()), "")
        return Receipt(c, WARN, f"could not reach remote '{remote}': {first[:160]}")
    if not out:
        return Receipt(c, FAIL, f"branch '{branch}' does not exist on {remote}")
    remote_sha = out.split()[0]
    want = c.sha or head
    rc, full = git(cwd, "rev-parse", "--verify", "--quiet", f"{want}^{{commit}}")
    want_full = full if rc == 0 else want
    if remote_sha.startswith(want) or remote_sha == want_full:
        dirty = _dirty(cwd)
        note = f" (but {dirty} tracked file(s) still uncommitted)" if dirty else ""
        return Receipt(c, WARN if dirty else OK, f"{remote}/{branch} = {remote_sha[:7]} = your commit{note}")
    if git(cwd, "cat-file", "-e", f"{remote_sha}^{{commit}}")[0] and not os.environ.get("KVITANSIYA_NO_FETCH"):
        git(cwd, "fetch", "--quiet", remote, branch)
    if git(cwd, "merge-base", "--is-ancestor", want_full, remote_sha)[0] == 0:
        return Receipt(c, OK, f"{want[:7]} is contained in {remote}/{branch} ({remote_sha[:7]})")
    _, ahead = git(cwd, "rev-list", "--count", f"{remote_sha}..{want_full}")
    ahead_txt = f"; local is {ahead} commit(s) ahead" if ahead.isdigit() else ""
    return Receipt(c, FAIL, f"{remote}/{branch} is at {remote_sha[:7]}, not {want[:7]}{ahead_txt} — the push did not land")


def _dirty(cwd: str) -> int:
    rc, out = git(cwd, "status", "--porcelain", "--untracked-files=no")
    return len([l for l in out.splitlines() if l.strip()]) if rc == 0 else 0


def session_start(transcript: str) -> int | None:
    try:
        with open(transcript, encoding="utf-8") as fh:
            for line in fh:
                ts = re.search(r'"timestamp":\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)', line)
                if ts:
                    return calendar.timegm(time.strptime(ts.group(1), "%Y-%m-%dT%H:%M:%S"))
    except OSError:
        pass
    return None


def check_commit(c: Claim, cwd: str, evs: list[dict] | None = None, text: str = "", transcript: str | None = None) -> Receipt:
    repos = session_repos(cwd, evs or [], text)
    if not repos:
        return Receipt(c, SKIP, "not a git repo")
    if c.sha:
        home = next((r for r in repos if git(r, "cat-file", "-e", f"{c.sha}^{{commit}}")[0] == 0), None)
        if not home:
            ran = any(e["tool"] == "Bash" and GIT_WRITE_RE.search(e["cmd"] or "") for e in evs or [])
            return Receipt(c, FAIL if ran else WARN, f"commit {c.sha} is not in any repo I can see ({', '.join(os.path.basename(r) for r in repos)})"
                           + (" although this session ran git there: the hash looks invented" if ran else ""))
        cwd = home
        _, subj = git(cwd, "log", "-1", "--format=%s", c.sha)
        return Receipt(c, OK, f"{c.sha[:7]} exists: “{subj[:60]}”")
    if not c.sha:
        cwd = repo_of_last("git commit", cwd, evs or []) or repos[0]
    _, ts = git(cwd, "log", "-1", "--format=%ct|%h|%s")
    try:
        t, h, subj = ts.split("|", 2)
        age = (time.time() - int(t)) / 60
    except ValueError:
        return Receipt(c, FAIL, "no commits in this repo")
    dirty = _dirty(cwd)
    start = session_start(transcript) if transcript else None
    if (start and int(t) < start - 60) or (not start and age > 180):
        return Receipt(c, FAIL, f"last commit {h} is {age/60:.1f} h old, older than this session — nothing was committed")
    if dirty:
        return Receipt(c, WARN, f"HEAD {h} “{subj[:50]}” ({age:.0f} min ago), but {dirty} tracked file(s) are still uncommitted")
    return Receipt(c, OK, f"HEAD {h} “{subj[:50]}” ({age:.0f} min ago), tree clean")


def fetch(url: str) -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "kvitansiya/0.1", "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.read(400_000).decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, "", url
    except Exception as e:  # DNS, refused, timeout, TLS
        return 0, type(e).__name__ + ": " + str(getattr(e, "reason", e))[:80], url


def check_deploy(c: Claim, cwd: str) -> Receipt:
    code, body, final = fetch(c.target)
    if code == 0:
        # DNS failure, timeout, no network: we could not look, which is not a contradiction
        return Receipt(c, WARN, f"could not reach {c.target} ({body}) — not verified")
    if code >= 400:
        return Receipt(c, FAIL, f"{c.target} returns HTTP {code}")
    missing = [e for e in c.expect if e.lower() not in body.lower()]
    if c.sha:
        if c.sha.lower() in body.lower():
            return Receipt(c, OK if not missing else FAIL, f"HTTP {code}, page contains {c.sha[:7]}" + (f"; missing {missing}" if missing else ""))
        base = re.match(r"https?://[^/]+", final).group(0)
        for p in ("/version", "/api/version", "/health", "/version.json", "/__version"):
            vc, vb, _ = fetch(base + p)
            # a real version endpoint is short and names a build; SPA fallbacks and {"status":"OK"} don't count
            if vc != 200 or not vb.strip() or len(vb) > 2000 or "<html" in vb.lower():
                continue
            if c.sha.lower() in vb.lower():
                return Receipt(c, OK, f"HTTP {code}; {p} reports {c.sha[:7]}")
            found = SHA_RE.findall(vb) or VERSION_RE.findall(vb)  # dedicated endpoint: any hash or semver is a version
            if found:
                return Receipt(c, FAIL, f"HTTP {code}, but {p} reports {found[0][:7]} — not {c.sha[:7]}; prod is running an old build")
        return Receipt(c, WARN, f"HTTP {code}, but nothing on the site confirms version {c.sha[:7]}")
    if missing:
        return Receipt(c, FAIL, f"HTTP {code}, but the page does not contain {', '.join(repr(m) for m in missing)}")
    return Receipt(c, OK, f"HTTP {code}" + (f", contains {', '.join(repr(e) for e in c.expect)}" if c.expect else ""))


def check_file(c: Claim, cwd: str, evs: list[dict] | None = None, transcript: str | None = None) -> Receipt:
    p = os.path.expanduser(c.target)
    if os.path.isabs(p) and not os.path.isdir(os.path.join("/", p.split("/")[1])):
        return Receipt(c, SKIP, f"{c.target}: not a real absolute path (a fragment of a longer one?)")
    written = [e["path"] for e in (evs or []) if e.get("path")]
    if not os.path.isabs(p):
        rel = re.sub(r"^(?:\./)+", "", c.target)
        hit = next((w for w in reversed(written) if w.endswith("/" + rel)), None)
        if not hit:  # cwd first, then every repo the agent cd'd into
            roots = [cwd] + [r for r in session_repos(cwd, evs or []) if r != cwd]
            hit = next((os.path.join(r, rel) for r in roots if os.path.exists(os.path.join(r, rel))), None)
        p = hit or os.path.join(cwd, rel)
        if not hit:
            folder = os.path.dirname(p)
            if not (os.path.dirname(rel) and os.path.isdir(folder)):
                # relative to some other folder we can't see: don't accuse without evidence
                return Receipt(c, SKIP, f"{c.target}: location unknown")
            name = os.path.basename(rel)
            shell = any(name in (e["cmd"] or "") for e in (evs or []) if e["tool"] == "Bash")
            if transcript and os.path.isfile(transcript) and not shell:
                # its folder is right here, the file is not, and nothing in this session wrote it
                return Receipt(c, FAIL, f"{c.target} does not exist in {folder}/, and no Write/Edit or shell command "
                                        f"in this session created it")
            why = "a shell command in this session mentions it — maybe written elsewhere" if shell else "no transcript to locate it elsewhere"
            return Receipt(c, WARN, f"{c.target} is not in {folder}/ ({why})")
    if not os.path.exists(p):
        return Receipt(c, FAIL, f"{c.target} does not exist" + (" (the agent wrote it earlier — it is gone now)" if p in written else ""))
    if os.path.isfile(p) and os.path.getsize(p) == 0:
        return Receipt(c, FAIL, f"{c.target} exists but is empty")
    size = os.path.getsize(p) if os.path.isfile(p) else 0
    return Receipt(c, OK, f"{c.target} exists ({size} bytes)")


TEST_CMD_RE = re.compile(
    r"\b(pytest|py\.test|unittest|npm (?:run )?test|pnpm (?:run )?test|yarn test|bun test|jest|vitest|mocha|go test|cargo test|"
    r"phpunit|rspec|mvn test|gradle test|dotnet test|make test|deno test|playwright test|node --test|tox|nox|"
    r"test_\w+\.py|\w+\.test\.[jt]sx?|\w+\.spec\.[jt]sx?|tests?/|run[_-]tests?)\b")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
FAIL_OUT_RE = re.compile(r"(\b[1-9]\d* (?:failed|failing|errors?)\b|\bFAILED\b|\bFAIL\b|Tests?:\s+\d+ failed|AssertionError|Traceback)")


def session_events(transcript: str) -> list[dict]:
    """Tool calls of the session in order: {tool, cmd, error, out}."""
    calls: dict[str, dict] = {}
    order: list[dict] = []
    try:
        fh = open(transcript, encoding="utf-8")
    except OSError:
        return []
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            content = (d.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    ev = {"tool": b.get("name"), "cmd": inp.get("command", ""), "path": inp.get("file_path") or inp.get("notebook_path"),
                          "error": None, "out": ""}
                    calls[b.get("id")] = ev
                    order.append(ev)
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                    ev = calls[b["tool_use_id"]]
                    out = b.get("content")
                    if isinstance(out, list):
                        out = " ".join(x.get("text", "") for x in out if isinstance(x, dict))
                    ev["out"] = str(out or "")
                    ev["error"] = b.get("is_error") in (True, "True", "true") or ev["out"].startswith("Exit code")
    return order


def check_tests(c: Claim, cwd: str, transcript: str | None, evs: list[dict] | None = None) -> Receipt:
    if not transcript:
        return Receipt(c, SKIP, "no session transcript to verify against")
    frac = re.search(r"\b(\d+)\s*/\s*(\d+)\b", c.sentence)
    if frac and int(frac.group(1)) < int(frac.group(2)):
        return Receipt(c, SKIP, "agent itself reported failures — honest partial result")
    evs = evs if evs is not None else session_events(transcript)
    last_edit = max((i for i, e in enumerate(evs) if e["tool"] in EDIT_TOOLS), default=-1)
    runs = [(i, e) for i, e in enumerate(evs) if e["tool"] == "Bash" and TEST_CMD_RE.search(e["cmd"] or "")
            and not re.match(r"\s*git\b", e["cmd"] or "")]
    if not runs:
        return Receipt(c, FAIL, "no test command was run in this session")
    i, e = runs[-1]
    cmd = (e["cmd"] or "").splitlines()[0][:60]
    if i < last_edit:
        n = sum(1 for e2 in evs[i + 1:] if e2["tool"] in EDIT_TOOLS)
        return Receipt(c, FAIL, f"last test run (`{cmd}`) was before {n} later file edit(s) — current code is untested")
    if e["error"] or FAIL_OUT_RE.search(e["out"][-4000:]):
        m = FAIL_OUT_RE.search(e["out"][-4000:])
        return Receipt(c, FAIL, f"last test run `{cmd}` failed" + (f" ({m.group(0)})" if m else ""))
    return Receipt(c, OK, f"`{cmd}` ran after the last edit and passed")


DEPLOY_CMD_RE = re.compile(r"\b(deploy\S*|vercel|netlify|fly deploy|wrangler|kubectl (?:apply|rollout)|docker (?:compose )?(?:push|up)|"
                           r"rsync|scp|pm2 (?:restart|reload)|systemctl restart|gh workflow run|eas (?:build|update)|firebase deploy)\b")


def expected_version(c: Claim, text: str, cwd: str, evs: list[dict]) -> str:
    """A deploy claim rarely repeats the hash. Take it from the rest of the message,
    else from HEAD of the repo where the agent ran its deploy command."""
    for sha in SHA_RE.findall(text):
        if any(  # every candidate is confirmed by git, so all-digit short hashes are fine here
                git(r, "cat-file", "-e", f"{sha}^{{commit}}")[0] == 0 for r in session_repos(cwd, evs, text)):
            return sha
    for e in reversed(evs):
        if e["tool"] == "Bash" and DEPLOY_CMD_RE.search(e["cmd"] or ""):
            rs = session_repos(cwd, [e])
            if rs:
                rc, head = git(rs[0], "rev-parse", "--short", "HEAD")
                return head if rc == 0 else ""
    return ""


def verify(text: str, cwd: str, transcript: str | None = None) -> list[Receipt]:
    out = []
    claims = extract_claims(text)
    evs = session_events(transcript) if transcript and claims else []
    for c in claims:
        if c.kind == "deploy" and not c.sha and not c.status_only:
            c.sha = expected_version(c, text, cwd, evs)
        if c.kind == "push":
            out.append(check_push(c, cwd, evs, text))
        elif c.kind == "commit":
            out.append(check_commit(c, cwd, evs, text, transcript))
        elif c.kind == "deploy":
            out.append(check_deploy(c, cwd))
        elif c.kind == "file":
            out.append(check_file(c, cwd, evs, transcript))
        elif c.kind == "tests":
            out.append(check_tests(c, cwd, transcript, evs))
    return out

# ---------------------------------------------------------------- output ----


def render(receipts: list[Receipt], verbose: bool = False) -> str:
    shown = [r for r in receipts if r.status != SKIP]
    bad = sum(r.status == FAIL for r in shown)
    skipped = len(receipts) - len(shown)
    head = f"KVITANSIYA — {len(shown)} claim(s) checked, {bad} false" + (f", {skipped} skipped" if skipped else "")
    lines = [head, "─" * len(head)]
    for r in receipts:
        if r.status != SKIP or verbose:
            lines.append(f"{ICON[r.status]} {r.claim.kind:<6} {r.evidence}")
    return "\n".join(lines)


def log(receipts: list[Receipt], cwd: str, session: str = ""):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(os.path.join(LOG_DIR, "log.jsonl"), "a", encoding="utf-8") as f:
            for r in receipts:
                if r.status == SKIP:
                    continue
                f.write(json.dumps({"t": int(time.time()), "cwd": cwd, "session": session, "kind": r.claim.kind,
                                    "status": r.status, "evidence": r.evidence, "claim": r.claim.sentence[:200]},
                                   ensure_ascii=False) + "\n")
    except OSError:
        pass


def last_assistant_text(transcript: str) -> str:
    text = ""
    try:
        with open(transcript, encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                parts = [b.get("text", "") for b in (d.get("message") or {}).get("content") or []
                         if isinstance(b, dict) and b.get("type") == "text"]
                if parts:
                    text = "\n".join(parts)
    except OSError:
        pass
    return text


def run_hook() -> int:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    cwd = data.get("cwd") or os.getcwd()
    transcript = data.get("transcript_path")
    text = data.get("last_assistant_message") or (last_assistant_text(transcript) if transcript else "")
    if not text:
        return 0
    receipts = verify(text, cwd, transcript)
    if not any(r.status != SKIP for r in receipts):
        return 0
    log(receipts, cwd, data.get("session_id", ""))
    report = render(receipts)
    failed = any(r.status == FAIL for r in receipts)
    if failed and not data.get("stop_hook_active"):
        reason = (report + "\n\nSome of your claims are contradicted by the real state above. "
                  "Fix the actual problem (or correct your message) and report again with evidence.")
        print(json.dumps({"decision": "block", "reason": reason, "systemMessage": report}, ensure_ascii=False))
    else:
        print(json.dumps({"systemMessage": report}, ensure_ascii=False))
    return 0


def stats() -> int:
    path = os.path.join(LOG_DIR, "log.jsonl")
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except OSError:
        pass
    week = [r for r in rows if r["t"] > time.time() - 7 * 86400]
    fails = [r for r in week if r["status"] == FAIL]
    print(f"Last 7 days: {len(week)} claims checked, {len(fails)} were false.")
    by = {}
    for r in fails:
        by[r["kind"]] = by.get(r["kind"], 0) + 1
    for k, v in sorted(by.items(), key=lambda x: -x[1]):
        print(f"  {k:<7} {v}")
    for r in fails[-5:]:
        print(f"  ❌ {time.strftime('%m-%d %H:%M', time.localtime(r['t']))} {r['evidence'][:90]}")
    return 0


def install(argv: list[str]) -> int:
    """Add the Stop hook to .claude/settings.json of a project (default: cwd) or ~/.claude (--user).
    Dry run unless --write; the old file is kept as settings.json.bak-kvitansiya."""
    user = "--user" in argv
    base = os.path.expanduser("~/.claude") if user else os.path.join(
        argv[argv.index("--project") + 1] if "--project" in argv else os.getcwd(), ".claude")
    path = os.path.join(base, "settings.json")
    cmd = f"python3 {os.path.abspath(__file__)} hook"
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        cfg = json.loads(raw) if raw.strip() else {}
    except FileNotFoundError:
        raw, cfg = None, {}
    except ValueError as e:
        print(f"{path} is not valid JSON ({e}); not touching it.")
        return 1
    stop = cfg.setdefault("hooks", {}).setdefault("Stop", [])
    if any(cmd == h.get("command") or "kvitansiya.py hook" in h.get("command", "")
           for g in stop for h in g.get("hooks", [])):
        print(f"Already installed in {path}")
        return 0
    stop.append({"hooks": [{"type": "command", "command": cmd, "timeout": 60}]})
    if "--write" not in argv:
        print(f"Would add to {path}:\n" + json.dumps({"hooks": {"Stop": stop[-1:]}}, indent=2) + "\nRun again with --write.")
        return 0
    os.makedirs(base, exist_ok=True)
    if raw is not None:
        with open(path + ".bak-kvitansiya", "w", encoding="utf-8") as f:
            f.write(raw)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Installed in {path}" + (f" (backup: {path}.bak-kvitansiya)" if raw is not None else ""))
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "hook":
        return run_hook()
    if argv[0] == "stats":
        return stats()
    if argv[0] == "install":
        return install(argv[1:])
    if argv[0] == "check" and len(argv) > 1:
        text = sys.stdin.read() if argv[1] == "-" else argv[1]
        cwd = argv[argv.index("--cwd") + 1] if "--cwd" in argv else os.getcwd()
        tr = argv[argv.index("--transcript") + 1] if "--transcript" in argv else None
        receipts = verify(text, cwd, tr)
        if "--json" in argv:
            print(json.dumps([{**asdict(r.claim), "status": r.status, "evidence": r.evidence} for r in receipts],
                             ensure_ascii=False, indent=2))
        else:
            print(render(receipts, verbose=True) if receipts else "No verifiable claims found.")
        return 1 if any(r.status == FAIL for r in receipts) else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
