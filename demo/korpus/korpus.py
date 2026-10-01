#!/usr/bin/env python3
"""False-positive check on a real corpus: the last assistant message of every Claude Code
session on this machine (~/.claude/projects/*/*.jsonl) is run through Kvitansiya.

Read-only, but not offline: KVITANSIYA_NO_FETCH=1 skips `git fetch` only; `git ls-remote` and HTTP GETs
to URLs named in the messages still run (as the hook does). The log goes to a temp dir.
Sessions of the demo sandbox (kv-demo / kv-h2h) are skipped — they contain planted lies.
Output: counts per (kind, status) + every ❌/⚠ with session id and evidence, for manual review.
Claim sentences are not printed (they are private session text); use --sentences to see them.

    python3 demo/korpus/korpus.py [--sentences] > demo/korpus/natija.txt
"""
import collections, glob, json, os, sys, tempfile, time

os.environ["KVITANSIYA_NO_FETCH"] = "1"
os.environ["KVITANSIYA_HOME"] = tempfile.mkdtemp(prefix="kv-korpus-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import kvitansiya as K  # noqa: E402

show = "--sentences" in sys.argv
files = sorted(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")))
n_final = n_claim = n_checked = 0
kinds, res, rows = collections.Counter(), collections.Counter(), []
for f in files:
    t = K.last_assistant_text(f)
    if not t:
        continue
    n_final += 1
    claims = K.extract_claims(t)
    if not claims:
        continue
    n_claim += 1
    kinds.update(c.kind for c in claims)
    cwd = None
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            cwd = d.get("cwd") or cwd
    if not cwd or not os.path.isdir(cwd) or "kv-demo" in cwd or "kv-h2h" in cwd:
        continue
    n_checked += 1
    for r in K.verify(t, cwd, f):
        res[(r.claim.kind, r.status)] += 1
        if r.status in (K.FAIL, K.WARN):
            rows.append((K.ICON[r.status].strip(), r.claim.kind, os.path.basename(f)[:8], r.evidence[:140],
                         r.claim.sentence[:140] if show else ""))

print(f"# Kvitansiya korpus — {time.strftime('%Y-%m-%d %H:%M')}")
print(f"sessiya fayllari: {len(files)} · yakuniy javob: {n_final} · da'vosi bor: {n_claim} · cwd hozir mavjud (tekshirildi): {n_checked}")
print(f"da'volar turi bo'yicha: {dict(kinds)}")
print("natija (tur, holat): " + ", ".join(f"{k}/{s}={n}" for (k, s), n in sorted(res.items())))
print(f"❌ jami: {sum(n for (k, s), n in res.items() if s == K.FAIL)} · ⚠ jami: {sum(n for (k, s), n in res.items() if s == K.WARN)}")
for r in rows:
    print(" · ".join(x for x in r if x))
