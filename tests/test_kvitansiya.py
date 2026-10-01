"""Real-world tests: temp git repo + local bare remote + local HTTP server + fake transcript."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import kvitansiya as K  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "kvitansiya.py")


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def write(path, text, mode):
    with open(path, mode) as f:
        f.write(text)


class Site(http.server.BaseHTTPRequestHandler):
    version = "1111111"

    def do_GET(self):
        pages = {"/": b"<h1>Pricing v2</h1>", "/version": Site.version.encode()}
        body = pages.get(self.path)
        self.send_response(200 if body else 404)
        self.end_headers()
        self.wfile.write(body or b"not found")

    def log_message(self, *a):
        pass


class KvitansiyaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Site)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.remote = os.path.join(t, "remote.git")
        self.repo = os.path.join(t, "app")
        sh(t, "git", "init", "-q", "--bare", "-b", "main", self.remote)
        sh(t, "git", "init", "-q", "-b", "main", self.repo)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            sh(self.repo, "git", "config", k, v)
        sh(self.repo, "git", "remote", "add", "origin", self.remote)
        write(os.path.join(self.repo, "a.txt"), "1", "w")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "first")
        sh(self.repo, "git", "push", "-q", "-u", "origin", "main")
        os.environ["KVITANSIYA_HOME"] = os.path.join(t, "home")

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, text, transcript=None):
        return [(r.claim.kind, r.status) for r in K.verify(text, self.repo, transcript)]

    def commit(self, msg):
        write(os.path.join(self.repo, "a.txt"), msg, "a")
        sh(self.repo, "git", "commit", "-qam", msg)
        return sh(self.repo, "git", "rev-parse", "HEAD")

    # --- claim extraction ---
    def test_negations_and_future_are_not_claims(self):
        for t in ("I did not push yet.", "I will push after review.", "Should I push to main?",
                  "You can deploy it to https://x.dev when ready.", "Push qilmadim, sizdan kutyapman.",
                  "```\ngit push origin main\n```"):
            self.assertEqual(K.extract_claims(t), [], t)

    def test_negation_is_clause_scoped(self):
        # a negation in another clause must not swallow the claim (critic round 1)
        self.assertEqual([c.kind for c in K.extract_claims("Pushed the fix, nothing else changed.")], ["push"])
        self.assertEqual([c.kind for c in K.extract_claims("Pushed to main; will deploy tomorrow.")], ["push"])
        self.assertEqual([c.kind for c in K.extract_claims("Tests pass, but I haven't pushed yet.")], ["tests"])
        for t in ("I haven't committed, pushed, or deployed anything.", "I pushed nothing.",
                  "No changes were pushed.", "Deploy to https://x.dev is not done."):
            self.assertEqual(K.extract_claims(t), [], t)

    def test_corpus_false_positives(self):
        # real corpus cases (demo/korpus): machine blocks, a fallback url next to "200", path fragments
        # report blocks: the summary is a claim, the file list and plan/critique blocks are not
        cs = K.extract_claims('<<ISH>>{"xulosa":"Main\'ga push qildim.","fayllar":["/you/index.tsx"]}<<END>>')
        self.assertEqual([c.kind for c in cs], ["push"])
        self.assertEqual(K.extract_claims('<<REJA>>{"tur":"deploy https://x.dev","qadamlar":["created `a.md`"]}<<END>>'), [])
        cs = K.extract_claims("prod fallback `https://yodex.uz/api`, backend sog'lom (`yodex.uz/api/health` 200).")
        self.assertEqual(cs, [])
        cs = K.extract_claims("`curl -s https://ifconfig.co/json` → HTTP **200**: city Tashkent.")
        self.assertEqual([(c.kind, c.status_only) for c in cs], [("deploy", True)])
        self.assertEqual(K.check_file(K.Claim("file", "", "/you/index.tsx"), self.repo).status, "skip")

    def test_uzbek_yuklandi(self):
        self.assertEqual([(c.kind, c.target) for c in K.extract_claims("O'zgarishlar GitHub'ga yuklandi.")], [("push", "")])
        self.assertEqual([(c.kind, c.target) for c in K.extract_claims("main'ga yukladim.")], [("push", "main")])
        self.assertEqual(K.extract_claims("Fayl serverga yuklandi."), [])  # uploading a file is not a git push

    def test_extracts_uzbek_and_english(self):
        kinds = [c.kind for c in K.extract_claims(
            "Main'ga push qildim. Sayt https://yodex.uz da joylandi. Barcha testlar o'tdi. `web/app.js` yaratdim.")]
        self.assertEqual(kinds, ["push", "deploy", "tests", "file"])

    # --- push ---
    def test_push_true(self):
        self.commit("fix")
        sh(self.repo, "git", "push", "-q")
        self.assertEqual(self.status("Pushed the fix to main."), [("push", "ok")])

    def test_push_false_local_ahead(self):
        self.commit("fix")  # committed, never pushed
        r = K.verify("Pushed the fix to main.", self.repo)[0]
        self.assertEqual(r.status, "fail")
        self.assertIn("1 commit(s) ahead", r.evidence)

    def test_push_wrong_branch(self):
        self.assertEqual(self.status("Pushed to release-2."), [("push", "fail")])

    def test_push_specific_sha(self):
        sha = self.commit("x")
        self.assertEqual(self.status(f"Pushed {sha[:7]} to main."), [("push", "fail")])
        sh(self.repo, "git", "push", "-q")
        self.assertEqual(self.status(f"Pushed {sha[:7]} to main."), [("push", "ok")])

    # --- commit ---
    def test_commit_fake_sha(self):
        # no git activity visible: cannot accuse
        self.assertEqual(self.status("Committed as 3f9a1c2."), [("commit", "warn")])
        # the session did commit here, but this hash exists nowhere: invented
        tr = self.transcript([("Bash", "git commit -am fix", "[main 5e1d2c3] fix", False)])
        r = K.verify("Committed as 3f9a1c2.", self.repo, tr)[0]
        self.assertEqual((r.status, "invented" in r.evidence), ("fail", True))

    # --- deploy ---
    def test_deploy_ok_with_content(self):
        self.assertEqual(self.status(f'Deployed: {self.base}/ now shows "Pricing v2".'), [("deploy", "ok")])

    def test_deploy_404(self):
        self.assertEqual(self.status(f"The page is live at {self.base}/pricing."), [("deploy", "fail")])

    def test_deploy_old_build(self):
        sha = self.commit("feature")
        r = K.verify(f"Deployed {sha[:7]} to {self.base}/ — it is live.", self.repo)[0]
        self.assertEqual(r.status, "fail")
        self.assertIn("old build", r.evidence)
        Site.version = sha
        self.assertEqual(K.verify(f"Deployed {sha[:7]} to {self.base}/ — it is live.", self.repo)[0].status, "ok")
        Site.version = "1111111"

    def test_deploy_unreachable(self):
        self.assertEqual(self.status("Deployed to http://127.0.0.1:9/ and it is live."), [("deploy", "fail")])

    def test_deploy_version_from_other_sentence(self):
        # the real Sonnet run: hash in one sentence, "is live" in the next, prod still old
        sha = self.commit("v2")
        msg = f"I committed it as {sha[:7]}, pushed it to origin main, and ran ./deploy.sh. It is live at {self.base}/."
        r = [x for x in K.verify(msg, self.repo) if x.claim.kind == "deploy"][0]
        self.assertEqual(r.status, "fail")
        self.assertIn("old build", r.evidence)

    def test_deploy_version_from_deploy_command(self):
        sha = self.commit("v3")
        tr = self.transcript([("Bash", f"cd {self.repo} && ./deploy.sh", "✓ Deployed", False)])
        r = K.verify(f"Done — it is live at {self.base}/.", self.tmp.name, tr)[0]
        self.assertEqual((r.claim.sha, r.status), (sha[:7], "fail"))

    # --- file ---
    def test_file(self):
        self.assertEqual(self.status("Created `a.txt`."), [("file", "ok")])
        # relative path we cannot locate: no accusation without evidence
        self.assertEqual(self.status("Created `docs/API.md` with the endpoints."), [("file", "skip")])
        # no transcript, but the folder is right here and the file is not: shown as ⚠, not silently skipped
        os.makedirs(os.path.join(self.repo, "docs"), exist_ok=True)
        self.assertEqual(self.status("Created `docs/API.md` with the endpoints."), [("file", "warn")])
        os.rmdir(os.path.join(self.repo, "docs"))
        gone = os.path.join(self.tmp.name, "x", "docs", "API.md")
        self.assertEqual(self.status(f"Created `{gone}`."), [("file", "fail")])
        tr = self.transcript([("Write", "", "ok", False)], path=gone)
        r = K.verify("Created `docs/API.md` with the endpoints.", self.repo, tr)[0]
        self.assertEqual((r.status, "gone now" in r.evidence), ("fail", True))

    # --- tests (from the session transcript) ---
    def transcript(self, events, path=None):
        p = os.path.join(self.tmp.name, "t.jsonl")
        with open(p, "w") as f:
            for i, (tool, cmd, out, err) in enumerate(events):
                f.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": f"t{i}", "name": tool, "input": {"command": cmd, "file_path": path}}]}}) + "\n")
                f.write(json.dumps({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"t{i}", "content": out, "is_error": err}]}}) + "\n")
        return p

    def test_tests_never_run(self):
        tr = self.transcript([("Edit", "", "ok", False)])
        self.assertEqual(self.status("All tests pass.", tr), [("tests", "fail")])

    def test_tests_run_before_last_edit(self):
        tr = self.transcript([("Bash", "npm test", "12 passed", False), ("Edit", "", "ok", False)])
        r = K.verify("All tests pass.", self.repo, tr)[0]
        self.assertEqual(r.status, "fail")
        self.assertIn("before 1 later file edit", r.evidence)

    def test_tests_failed_output(self):
        tr = self.transcript([("Edit", "", "ok", False), ("Bash", "pytest -q", "3 failed, 9 passed", False)])
        self.assertEqual(self.status("All tests pass.", tr), [("tests", "fail")])

    def test_tests_ok(self):
        tr = self.transcript([("Edit", "", "ok", False), ("Bash", "pytest -q", "12 passed in 0.4s", False)])
        self.assertEqual(self.status("All tests pass.", tr), [("tests", "ok")])


    # --- found on the real corpus of 1 882 sessions ---
    def test_mentions_are_not_claims(self):
        for t in ('CLAUDE.md dagi eski "git push taqiqlangan" bandi yangilandi.',
                  "The commit is `5a95b72`, with only my own changes and nothing pushed.",
                  "`02-STRATEGIYA.md` va `biznes.json` ga tegmadim.",
                  "`--purple-dim` amalda `0.14` deb saqlandi."):
            self.assertEqual([c.kind for c in K.extract_claims(t)], [], t)

    def test_push_in_other_repo_via_cd(self):
        other = os.path.join(self.tmp.name, "other")
        sh(self.tmp.name, "git", "clone", "-q", self.remote, other)
        tr = self.transcript([("Bash", f"cd {other} && git push origin main", "Everything up-to-date", False)])
        cwd_elsewhere = self.tmp.name  # hook cwd is not the repo
        self.assertEqual(K.verify("Pushed to main.", cwd_elsewhere, tr)[0].status, "ok")

    def test_push_rejected_in_transcript(self):
        self.commit("x")
        tr = self.transcript([("Bash", "git push", " ! [rejected]        main -> main (fetch first)\nerror: failed to push", True)])
        r = K.verify("Pushed to main.", self.repo, tr)[0]
        self.assertEqual(r.status, "fail")
        self.assertIn("rejected", r.evidence)

    def test_honest_partial_tests_not_blocked(self):
        tr = self.transcript([("Edit", "", "ok", False), ("Bash", "pytest", "3 failed, 103 passed", False)])
        self.assertEqual(self.status("Final: 103/106 tests pass, 3 are pre-existing.", tr), [("tests", "skip")])

    # --- hook protocol ---
    def hook(self, payload):
        r = subprocess.run([sys.executable, SCRIPT, "hook"], input=json.dumps(payload), capture_output=True,
                           text=True, env={**os.environ})
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_hook_blocks_false_claim_once(self):
        self.commit("fix")
        p = {"cwd": self.repo, "last_assistant_message": "Done. Pushed to main.", "stop_hook_active": False}
        out = self.hook(p)
        self.assertEqual(out["decision"], "block")
        self.assertIn("did not land", out["reason"])
        out2 = self.hook({**p, "stop_hook_active": True})  # never loops forever
        self.assertNotIn("decision", out2)

    def test_hook_silent_without_claims(self):
        self.assertIsNone(self.hook({"cwd": self.repo, "last_assistant_message": "Here is the explanation."}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
