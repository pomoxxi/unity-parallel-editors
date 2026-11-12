"""One owner of `git grep`: `Flow._grep`.

`test_no_other_git_grep_in_src` reads `src/upe/*.py` and flags a `"grep"` word anywhere outside `Flow._grep`, a string
that starts a shell `git grep`/`grep ` command, and a call of `_grep` that does not state `binary=` and `mode=` as
constants. It misses (named so they are read): a verb built by concatenation or read from a variable
(`"gr" + "ep"`), `git log --grep=` (another command), `git` run through a shell string made at run time, and a `grep`
in the argv of another program.

`test_each_caller_reads_what_it_read_before` is a differential test on real git: the four argv lists the tool ran
before `Flow._grep`, copied here as data, and `Flow._grep` for the same read, side by side, in each state of
git's environment that could change a read; `(exit, stdout, stderr)` must be equal for G1, G3 and G4, and the lines
for G2 (read line by line).
"""

import ast
import hashlib
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from upe.checks import FOLDER_ASSET, HOOK_PATTERN
from upe.flow import GUID_REFERENCE, Flow, FlowError, _pathspec_env
from upe.safegit import SAFE_GIT

from test_flow import FlowCase, asset, commit, git, write

SRC = Path(__file__).resolve().parents[1] / "src" / "upe"


def other_greps(text: str, file: str = "<snippet>") -> list[str]:
    """Each place in `text` that runs `git grep` outside `Flow._grep`, or calls `_grep` without constant options."""
    tree = ast.parse(text)
    found = []

    def visit(node, qual):
        for child in ast.iter_child_nodes(node):
            name = qual
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{qual}.{child.name}" if qual else child.name
            if name != "Flow._grep":
                if isinstance(child, ast.Constant) and isinstance(child.value, str) and (
                        child.value == "grep" or child.value.startswith(("git grep ", "grep "))) and not isinstance(
                        node, ast.JoinedStr) and not _docstring(node, child):
                    found.append(f"{file}:{child.lineno}")
                if isinstance(child, ast.Call) and getattr(child.func, "attr", getattr(child.func, "id", "")) == "_grep":
                    given = {k.arg: k.value for k in child.keywords}
                    if not all(isinstance(given.get(k), ast.Constant) for k in ("binary", "mode")):
                        found.append(f"{file}:{child.lineno} (binary= and mode= as constants)")
            visit(child, name)

    visit(tree, "")
    return found


def _docstring(parent, child) -> bool:
    return isinstance(parent, ast.Expr)


class GrepOwnerScan(unittest.TestCase):
    def test_no_other_git_grep_in_src(self):
        # Red with any of the four old `git grep` lines of flow.py put back (`_meta_tree`, `_guids_named` twice,
        # `_hook_scripts`).
        found = [f for path in sorted(SRC.glob("*.py")) for f in other_greps(path.read_text(encoding="utf-8"), path.name)]
        self.assertEqual([], found, "`git grep` goes through `Flow._grep`, which fixes git's output and attributes")
        self.assertIn("_grep", vars(Flow), "Flow._grep is the owner")

    def test_the_scan_flags_the_old_shapes(self):
        self.assertTrue(other_greps("def f(self):\n    self._runner(['git', '-C', d, 'grep', '-l', p, c])\n"))
        self.assertTrue(other_greps("def f(self):\n    self._git(cwd, 'grep', '-e', p)\n"))
        self.assertTrue(other_greps("def f():\n    subprocess.Popen(['git', 'grep', '-o', p])\n"))
        self.assertTrue(other_greps("def f():\n    subprocess.run('git grep -l x HEAD', shell=True)\n"))
        self.assertTrue(other_greps("def f(self, m):\n    self._grep(c, [p], 'HEAD', binary='-a', mode=m)\n"))
        self.assertEqual(other_greps("class Flow:\n    def _grep(self):\n        run(['git', 'grep'])\n"
                                     "    def g(self):\n        self._grep(c, [p], 'H', binary='-a', mode='-E')\n"
                                     "        raise FlowError(f'git grep {x} failed')\n"), [])
        # Its limits: a verb built at run time.
        self.assertEqual(other_greps("def f():\n    run(['git', 'gr' + 'ep'])\n"), [])


GUID_OLD = hashlib.md5(b"Assets/Art/Old.png.meta").hexdigest()


class GrepCallersRead(FlowCase):
    """Real git. A target with folder and file `.meta` files, a scene that names a GUID, a hook script, and a path with
    a space and a non-ASCII letter."""

    def setUp(self):
        super().setUp()
        asset(self.repo, "Assets/Art/Old.png", "png")
        asset(self.repo, "Assets/Ä b/x y.png", "png")
        asset(self.repo, "Assets/Editor/Hook.cs", "class H : UnityEditor.AssetPostprocessor {}\n")
        asset(self.repo, "Assets/Scenes/Named.unity", f"--- !u!1 &1\n  m_Tex: {{fileID: 2800000, guid: {GUID_OLD}, type: 3}}\n")
        write(self.repo, "Packages/com.x/P.cs.meta", "fileFormatVersion: 2\nguid: " + "e" * 32 + "\n")
        write(self.repo, "Packages/com.x/P.cs", "class P {}\n")
        commit(self.repo, "the reads' files")
        self.commit = git(self.repo, "rev-parse", "HEAD").strip()

    def reads(self, flow, cwd, commit_):
        """(name, old argv, its env, new call) of each read: G1 twice, G2, G3, G4."""
        c, d = commit_, str(cwd)
        g1 = ["git", *SAFE_GIT, "-C", d, "grep", "-a", "-G", "--no-color", "--no-line-number", "--no-column", "--null"]
        return [
            ("G1 folders", g1 + ["-l", "-e", FOLDER_ASSET, c, "--", "Assets/*.meta"], "pathspec",
             lambda: flow._grep(cwd, [FOLDER_ASSET], c, binary="-a", mode="-G", output=("-l",),
                                pathspecs=("Assets/*.meta",), null=True)),
            ("G1 guids", g1 + ["-e", "^guid:", c, "--", "Assets/*.meta", "Packages/*.meta"], "pathspec",
             lambda: flow._grep(cwd, ["^guid:"], c, binary="-a", mode="-G", pathspecs=("Assets/*.meta", "Packages/*.meta"),
                                null=True)),
            ("G2", ["git", *SAFE_GIT, "-C", d, "grep", "-o", "-h", "-a", "--no-color", "--no-line-number", "--no-column",
                    "-E", GUID_REFERENCE, c], "process", None),
            ("G3", ["git", *SAFE_GIT, "-C", d, "grep", "-o", "-a", "--no-color", "--no-line-number", "--no-column", "-F",
                    "-e", GUID_OLD, "-e", "e" * 32, c], "process",
             lambda: flow._grep(cwd, [GUID_OLD, "e" * 32], c, binary="-a", mode="-F", output=("-o",), null=True)),
            ("G4", ["git", *SAFE_GIT, "-C", d, "grep", "-l", "-a", "-E", "--no-color", "--no-line-number", "--no-column",
                    HOOK_PATTERN, c, "--", "*.cs"], "pathspec",
             lambda: flow._grep(cwd, [HOOK_PATTERN], c, binary="-a", mode="-E", output=("-l",), pathspecs=("*.cs",))),
        ]

    def compare(self, flow, cwd, commit_):
        """{read: (old, new)} for each read, as it is now in this process's environment."""
        out = {}
        for name, argv, env, new in self.reads(flow, cwd, commit_):
            kw = dict(capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL)
            if name == "G2":
                old = subprocess.run(argv, **kw)
                lines = []
                got = flow._grep(cwd, [GUID_REFERENCE], commit_, binary="-a", mode="-E", output=("-o", "-h"),
                                 on_line=lines.append)
                out[name] = ((old.returncode, old.stdout.splitlines(keepends=True), old.stderr),
                             (got.returncode, lines, got.stderr))
                continue
            old = subprocess.run(argv, env=_pathspec_env() if env == "pathspec" else None, **kw)
            got = new()
            # G3 reads with `--null`: the path ends with a NUL where it ended with ":" (and is never
            # quoted); on these ASCII paths the read is the same once the NUL is read as the colon it replaces.
            new_out = got.stdout.replace("\0", ":") if name == "G3" else got.stdout
            out[name] = ((old.returncode, old.stdout, old.stderr), (got.returncode, new_out, got.stderr))
        return out

    def states(self):
        attrs = self.repo / ".gitattributes"
        info = self.repo / ".git" / "info" / "attributes"
        binary = Path(self._tmp.name) / "attrs"
        binary.write_text("* binary\n")

        def config(**keys):
            env = {"GIT_CONFIG_COUNT": str(len(keys))}
            for i, (k, v) in enumerate(keys.items()):
                env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k.replace("_", "."), v
            return env

        return [
            ("plain", {}, None),
            ("an untracked .gitattributes `* binary`", {}, (attrs, "* binary\n")),
            ("info/attributes `* -diff`", {}, (info, "* -diff\n")),
            *[(f"{v}=1", {v: "1"}, None) for v in ("GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_LITERAL_PATHSPECS",
                                                    "GIT_ICASE_PATHSPECS")],
            ("color.ui=always", config(color_ui="always"), None),
            ("grep.lineNumber=true", config(grep_lineNumber="true"), None),
            ("grep.column=true", config(grep_column="true"), None),
            ("grep.patternType=fixed", config(grep_patternType="fixed"), None),
            ("core.attributesFile `* binary`", config(core_attributesFile=str(binary)), None),
        ]

    def test_each_caller_reads_what_it_read_before(self):
        # None of the four changes its read: the old argv and `_grep` give the same exit, stdout
        # and stderr in each state. The one difference by design: G2 and G3 now run without git's pathspec variables
        # (`_pathspec_env`); they give no pathspec, so the read is the same (the four variable states show it).
        flow = self.flow()
        reference = None
        for name, env, planted in self.states():
            with self.subTest(state=name):
                if planted:
                    planted[0].parent.mkdir(parents=True, exist_ok=True)
                    planted[0].write_text(planted[1])
                try:
                    with mock.patch.dict(os.environ, env):
                        pairs = self.compare(flow, self.repo, self.commit)
                finally:
                    if planted:
                        planted[0].unlink()
                for read, (old, new) in pairs.items():
                    self.assertEqual(old, new, f"{read} in {name}")
                if reference is None:
                    reference = pairs
                    self.assertIn("Assets/Ä b.meta", reference["G1 folders"][1][1], "the reference reads something")
                    self.assertTrue(reference["G2"][1][1] and reference["G3"][1][1] and reference["G4"][1][1])

    def test_a_failed_read_fails_the_same_way(self):
        # A blob taken out of the object store (exit 0, `error:` on stderr), and a name that is no revision (exit 128).
        flow = self.flow()
        pairs = self.compare(flow, self.repo, "no-such-revision")
        for read, (old, new) in pairs.items():
            self.assertEqual(old, new, read)
            self.assertEqual(new[0], 128, read)
        blob = git(self.repo, "rev-parse", f"{self.commit}:Assets/Art/Old.png.meta").strip()
        obj = self.repo / ".git" / "objects" / blob[:2] / blob[2:]
        data = obj.read_bytes()
        os.chmod(obj, 0o644)
        obj.unlink()
        try:
            pairs = self.compare(flow, self.repo, self.commit)
            with self.assertRaises(FlowError) as raised:
                flow._grep(self.repo, ["^guid:"], self.commit, binary="-a", mode="-G", pathspecs=("Assets/*.meta",),
                           null=True, strict=True)
        finally:
            obj.write_bytes(data)
        for read, (old, new) in pairs.items():
            self.assertEqual(old, new, read)
        self.assertEqual(pairs["G1 guids"][1][0], 0, "real git exits 0 with a blob it could not read")
        self.assertIn("unable to read", pairs["G1 guids"][1][2])
        self.assertIn(f"git grep -e ^guid: {self.commit} -- Assets/*.meta failed (0)", str(raised.exception))

    def test_the_file_that_names_a_guid_is_named_as_git_holds_it(self):
        # Real git quotes a non-ASCII path in `git grep`'s plain output, or not, by `core.quotePath`; read with
        # `--null`, the name is the path. Read without it: `"Assets/\303\204 b/..."`.
        guid = hashlib.md5("Assets/Ä b/x y.png.meta".encode()).hexdigest()
        write(self.repo, "Assets/Art/Back.png.meta", f"fileFormatVersion: 2\nguid: {guid}\n")
        commit(self.repo, "a .meta with the GUID of a file under a non-ASCII folder")
        flow = self.flow()
        for env in ({}, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.quotePath", "GIT_CONFIG_VALUE_0": "false"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                self.assertEqual(flow._guids_named(self.repo, self.commit),
                                 {guid: "named by Assets/Ä b/x y.png.meta of the target"})

    def test_a_strict_read_fails_on_an_exit_over_1_with_nothing_on_stderr(self):
        # Real git's exit 128 always says something on stderr; a git ended by a signal (a negative code) may say
        # nothing. With a stand-in for that end.
        flow = self.flow()
        for code in (-9, 2, 128):
            with self.subTest(code):
                flow._runner = lambda argv, **kw: subprocess.CompletedProcess(argv, code, "", "")
                with self.assertRaisesRegex(FlowError, rf"failed \({code}\)"):
                    flow._grep(self.repo, ["^guid:"], self.commit, binary="-a", mode="-G", null=True, strict=True)

    def test_a_pattern_that_starts_with_a_dash_is_a_pattern(self):
        # Real git: given by `-e`, a pattern like `-x` is read as a pattern; given in place, as the old G2 and G4 gave
        # theirs, git reads it as an option and fails.
        write(self.repo, "Assets/Editor/Dash.cs", "// -upeMarker in a comment\n")
        commit(self.repo, "a script that holds a word with a dash")
        head = git(self.repo, "rev-parse", "HEAD").strip()
        flow = self.flow()
        got = flow._grep(self.repo, ["-upeMarker"], head, binary="-a", mode="-E", output=("-l",), pathspecs=("*.cs",))
        self.assertEqual((got.returncode, got.stdout.strip()), (0, f"{head}:Assets/Editor/Dash.cs"))
        old = subprocess.run(["git", *SAFE_GIT, "-C", str(self.repo), "grep", "-l", "-a", "-E", "--no-color",
                              "--no-line-number", "--no-column", "-upeMarker", head, "--", "*.cs"],
                             capture_output=True, text=True)
        self.assertNotEqual(old.returncode, 0, "in place, git reads it as an option")

    def test_grep_fixes_its_output_whatever_git_is_told(self):
        # Each caller's recorded argv holds the four options that fix git's output, its own `-a` and its mode.
        # Every child goes through `subprocess.Popen` (`subprocess.run` too), so it records each call once.
        flow = self.flow()
        seen, real_popen = [], subprocess.Popen

        def popen(argv, *a, **kw):
            seen.append(list(argv))
            return real_popen(argv, *a, **kw)

        with mock.patch("subprocess.Popen", popen):
            flow._meta_tree(self.repo, self.commit)
            flow._hook_scripts(self.repo, self.commit)
            # A .meta HEAD adds with a GUID the target's scene names: the read of every reference, then of who names it.
            write(self.repo, "Assets/Art/Back.png.meta", f"fileFormatVersion: 2\nguid: {GUID_OLD}\n")
            commit(self.repo, "a .meta with a GUID the scene names")
            named = flow._guids_named(self.repo, self.commit)
        self.assertEqual(list(named), [GUID_OLD])
        self.assertRegex(named[GUID_OLD], r"^named by Assets/\S+ of the target$")
        greps = [a for a in seen if "grep" in a]
        self.assertEqual(len(greps), 5, "two of `_meta_tree`, one of `_hook_scripts`, two of `_guids_named`")
        for argv in greps:
            at = argv.index("grep")
            self.assertEqual(argv[at + 1], "-a")
            self.assertIn(argv[at + 2], ("-G", "-E", "-F"))
            self.assertEqual(argv[at + 3:at + 6], ["--no-color", "--no-line-number", "--no-column"])


if __name__ == "__main__":
    unittest.main()
