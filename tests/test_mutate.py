"""`tests/mutate.py`, on a small repo of its own, with real git."""

import contextlib
import hashlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mutate  # noqa: E402

BEFORE = "def kept(x):\n    return x\n"
AFTER = '''def kept(x):
    return x


def guard(x):
    if x < 0:
        raise ValueError("negative")
    return x


def loose(x):
    if x > 100:
        return 100
    return x


def untouched(x):
    return x + 1
'''
TESTS = '''import unittest

from upe import m


class M(unittest.TestCase):
    def test_guard_refuses_a_negative(self):
        with self.assertRaises(ValueError):
            m.guard(-1)

    def test_loose_passes_a_small_one(self):
        self.assertEqual(m.loose(3), 3)
'''
MUTATIONS = '''M = [
    ("guard: no refusal", "src/upe/m.py", "if x < 0:", "if False:", ["test_m"]),
    ("loose: no cap", "src/upe/m.py", "if x > 100:", "if False:", ["test_m"]),
]
'''


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "core.autocrlf=false", *args], check=True, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout


def write(root: Path, rel: str, text: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


class MutateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.repo, self.copies = tmp / "repo", tmp / "copies"
        write(self.repo, "src/upe/__init__.py", "")
        write(self.repo, "src/upe/m.py", BEFORE)
        write(self.repo, "tests/test_m.py", TESTS)
        write(self.repo, "mutations.py", MUTATIONS)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").strip()
        write(self.repo, "src/upe/m.py", AFTER)
        git(self.repo, "commit", "-q", "-am", "the work")

    def tearDown(self):
        self._tmp.cleanup()

    def run_it(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mutate.main(["--base", self.base, "--repo", str(self.repo), "--tmp", str(self.copies), *extra])
        return code, out.getvalue()

    def files(self):
        return {str(p.relative_to(self.repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.repo.rglob("*")) if p.is_file() and ".git" not in p.parts}

    def test_it_mutates_a_copy_and_lists_the_changed_functions_no_mutation_touches(self):
        before = self.files()
        code, out = self.run_it("--list", str(self.repo / "mutations.py"))
        self.assertEqual(code, 1, out)  # one lives
        self.assertRegex(out, r"control: upe from .*copies")
        self.assertIn("red    guard: no refusal  (exit 1", out)
        self.assertIn("LIVES  loose: no cap  (exit 0", out)
        self.assertIn("mutations: 2; red: 1; LIVES: 1; void: 0", out)
        self.assertIn("3 functions changed in src; with a mutation: 2; with none: 1", out)
        self.assertIn("  none  src/upe/m.py  untouched  (2 changed lines)", out)
        self.assertNotIn("  none  src/upe/m.py  kept", out, "unchanged since the base")
        self.assertEqual(self.files(), before, "the checkout is never written")
        self.assertEqual(git(self.repo, "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(list(self.copies.iterdir()), [], "its copies are deleted")

    def test_with_no_list_every_changed_function_has_none(self):
        code, out = self.run_it()
        self.assertEqual(code, 0, out)
        self.assertIn("3 functions changed in src; with a mutation: 0; with none: 3", out)

    def test_tmp_is_required_and_never_inside_the_repo(self):
        # No default under the system temp folder, and no
        # copies inside the checkout (they would sit there as untracked folders, and the next run would refuse).
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            mutate.main(["--base", self.base, "--repo", str(self.repo)])
        self.assertEqual(ctx.exception.code, 2)
        for inside in (self.repo, self.repo / "tests" / "tmp", self.repo / "elsewhere"):
            with self.subTest(tmp=str(inside)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = mutate.main(["--base", self.base, "--repo", str(self.repo), "--tmp", str(inside)])
                self.assertEqual(code, 2, out.getvalue())
                self.assertIn("refused: --tmp", out.getvalue())
                self.assertEqual(git(self.repo, "status", "--porcelain", "--untracked-files=all"), "")

    def test_a_timeout_or_an_error_is_void_not_red(self):
        # Only a test that fails is red. A mutation that breaks the import, or that hangs, proves no
        # check: it is void, and the run exits 1.
        write(self.repo, "broken.py", '''M = [
    ("an import error", "src/upe/m.py", "def guard(x):", "import no_such_module_at_all\\n\\n\\ndef guard(x):",
     ["test_m"]),
    ("a hang", "src/upe/m.py", "if x > 100:", "while True:\\n        pass\\n    if x > 100:", ["test_m"]),
]
''')
        git(self.repo, "add", "broken.py")
        git(self.repo, "commit", "-q", "-m", "a broken list")
        code, out = self.run_it("--list", str(self.repo / "broken.py"), "--timeout", "20")
        self.assertEqual(code, 1, out)
        self.assertIn("void   an import error  (exit 1", out)
        self.assertIn("void   a hang  (exit -1", out)
        self.assertIn("mutations: 2; red: 0; LIVES: 0; void: 2", out)

    def test_a_failure_beside_a_skip_or_an_expected_failure_is_red_and_an_error_is_void(self):
        # unittest adds `skipped=N` and `expected failures=N` after the failures when the named tests
        # hold a skipped test (a Windows-only one on POSIX, the UnityYAMLMerge one). A test failed: red. An error: void.
        for tail, want in (("FAILED (failures=2)", "red"), ("FAILED (failures=1, skipped=2)", "red"),
                           ("FAILED (failures=1, expected failures=1)", "red"),
                           ("FAILED (failures=1, skipped=1, expected failures=1)", "red"),
                           ("FAILED (failures=1, errors=1)", "void"), ("FAILED (errors=1)", "void"),
                           ("FAILED (errors=1, skipped=1)", "void"), ("timed out after 20 s", "void")):
            with self.subTest(tail=tail):
                self.assertEqual(mutate.outcome(1 if "FAILED" in tail else -1, tail), want)
        self.assertEqual(mutate.outcome(0, "OK (skipped=1)"), "LIVES")

    def test_a_failure_beside_an_unexpected_success_is_red(self):
        # unittest names `unexpected successes=N` last. A test failed beside
        # one: red, not void. With an error beside it: void. Red before (the first two read `void`).
        for tail, want in (("FAILED (failures=1, unexpected successes=1)", "red"),
                           ("FAILED (failures=1, skipped=1, expected failures=1, unexpected successes=2)", "red"),
                           ("FAILED (failures=1, errors=1, unexpected successes=1)", "void"),
                           ("FAILED (unexpected successes=1)", "void")):
            with self.subTest(tail=tail):
                self.assertEqual(mutate.outcome(1, tail), want)

    def test_a_base_or_commit_git_does_not_know_is_refused(self):
        # A `refused:` line and exit 2, not git's traceback; nothing copied.
        for flag in ("--base", "--commit"):
            with self.subTest(flag=flag):
                args = {"--base": self.base, "--commit": "HEAD", flag: "no-such-commit"}
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = mutate.main(["--repo", str(self.repo), "--tmp", str(self.copies),
                                        "--base", args["--base"], "--commit", args["--commit"]])
                self.assertEqual(code, 2, out.getvalue())
                self.assertIn(f"refused: {flag} no-such-commit is not a commit", out.getvalue())
                self.assertFalse(self.copies.exists(), "nothing was copied")

    def test_it_refuses_to_start_with_a_change_under_src_or_tests(self):
        for rel, text in (("src/upe/m.py", AFTER + "# a change\n"), ("tests/test_new.py", "# untracked\n")):
            with self.subTest(rel=rel):
                git(self.repo, "reset", "-q", "--hard")
                git(self.repo, "clean", "-qfd")
                write(self.repo, rel, text)
                code, out = self.run_it("--list", str(self.repo / "mutations.py"))
                self.assertEqual(code, 2, out)
                self.assertIn("refused:", out)
                self.assertIn(rel, out)
                self.assertFalse(self.copies.exists(), "nothing was copied")
        git(self.repo, "reset", "-q", "--hard")
        git(self.repo, "clean", "-qfd")
        write(self.repo, "notes.txt", "a change outside src and tests\n")
        self.assertEqual(self.run_it()[0], 0)


if __name__ == "__main__":
    unittest.main()
