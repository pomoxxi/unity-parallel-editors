"""Every command line that grows with a land goes through `capped` or is a measured `BOUNDED` entry.

The scan (`argv_scan.py`) reads `src/upe/*.py` and lists each growth expression: a starred argument, a `";".join`, an
`argv += x` or `argv.extend(x)`. One whose names come from `capped`/`_capped` in the same function passes. Every other
one must be a row of `ENTRIES`, keyed by file, function and the expression's text (no line numbers), with a class:
- `PASS`: a parameter forwarded to a program; the callers that give it a list that grows are named, and each must bind
  that list from `capped` (checked by name);
- `BOUNDED`: the largest it can be, in characters, and why; with a constant to measure, the test measures it;
- `BY_HAND`: a line with its own batching, named (council's diff, `_PATHS_PER_CALL`); the test checks the constant is
  read in the function;
- `NOT_A_COMMAND`: not a command line at all (a message, a constructor, a set union, a loop over files).

What it misses, by design:
- a command built in pieces the scan does not read: `argv = base + more`, `itertools.chain`, a list a helper makes and
  returns, passed whole (`run(make_argv())`), or a list passed whole as `extra=` to a `PASS` sink (only starred and
  joined values are read where they are formed);
- a `","` or `" "` join into a command (only `";"`, Unity's and the `unity` CLI's separator, is read);
- stdin (`rev-list --stdin` in `Flow._own_commits`), a file a program reads, a shell string (`shell=True`: none in
  `src`);
- a `PASS` caller the table does not name: a new call of a sink with a list that grows is seen only when that list is
  starred or joined in the caller.
The behaviour tests of the five capped lines (`test_checks.SceneGroups`, `test_checks.ClassGroups`, `test_flow`'s
restore test, `test_workspace`'s scrub test) read the real argv for the part the scan cannot see.
"""

import ast
import subprocess
import unittest
from pathlib import Path

from argv_scan import scan, scan_source

SRC = Path(__file__).resolve().parents[1] / "src" / "upe"

PASS, BOUNDED, BY_HAND, NOT_A_COMMAND = "PASS", "BOUNDED", "BY_HAND", "NOT_A_COMMAND"


def _local_only() -> int:
    from upe.agent_package import LOCAL_ONLY

    return sum(len(subprocess.list2cmdline([p])) + 1 for p in LOCAL_ONLY)


# (file, function, expression) -> (class, the reason; for PASS the growing callers (file, function); for BOUNDED the
# largest it can be in characters and a measure, or None)
ENTRIES = {
    # Pass-throughs: the sink forwards what its caller gives.
    ("checks.py", "run_tests", "extra"): (PASS, "the caller's options", [("checks.py", "run_tests_by_classes")]),
    ("checks.py", "_one_scene_run", "paths"): (PASS, "one group of `run_scene_check`", [("checks.py", "run_scene_check")]),
    ("flow.py", "Flow._grep", "output"): (PASS, "a caller's output options, a constant tuple each", []),
    ("flow.py", "Flow._grep", "[x for p in patterns for x in ('-e', p)]"): (
        PASS, "a caller's patterns; the one caller with many is `_guids_named`, in capped groups",
        [("flow.py", "Flow._guids_named")]),
    ("flow.py", "Flow._grep", "pathspecs"): (PASS, "a caller's pathspecs, a constant tuple each", []),
    ("flow.py", "Flow._grep", "tail"): (PASS, "the three above, for the argv and the message", [("flow.py", "Flow._guids_named")]),
    ("flow.py", "Flow._git", "args"): (PASS, "every git call of the flow; the growing ones are capped in their caller",
                                       [("flow.py", "Flow._candidate_at"), ("flow.py", "Flow._after_editor")]),
    ("workspace.py", "Workspaces._git", "args"): (PASS, "every git call of the slots", [("workspace.py", "Workspaces._scrub")]),
    ("merge.py", "Merger._git", "args"): (PASS, "every git call of the merge: options, refs, one path each", []),
    ("agent_package.py", "_git", "args"): (PASS, "agent_package's git calls (rows below)", []),
    ("setup.py", "_git", "args"): (PASS, "setup's git calls: options and refs", []),
    ("council.py", "_resolutions.git", "args"): (PASS, "the council's git calls (rows below)", []),
    ("editor.py", "Editor.batch", "list(args)"): (PASS, "the Unity arguments of a caller; `-upePaths` is capped in "
                                                        "`run_scene_check`", [("checks.py", "run_scene_check")]),
    ("editor.py", "Editor.execute_method", "args"): (PASS, "as `Editor.batch`", [("checks.py", "run_scene_check")]),
    ("unity_cli.py", "UnityCli.run", "spec.args"): (PASS, "a `COMMANDS` entry's fixed words", []),
    ("unity_cli.py", "UnityCli.run", "extra"): (PASS, "the operation's options; `--filter` is capped in "
                                                      "`run_tests_by_classes`", [("checks.py", "run_tests_by_classes")]),
    ("unity_cli.py", "UnityCli.open", "extra"): (PASS, "fixed options", []),
    ("unity_cli.py", "UnityCli.test", "args"): (PASS, "as `UnityCli.run`", [("checks.py", "run_tests_by_classes")]),
    ("unity_cli.py", "UnityCli.command", "args"): (PASS, "as `UnityCli.run`", []),
    ("merge.py", "unity_yaml_merge.merge", "extra"): (BOUNDED, 200, "`yaml_merge_flags`: fixed flags", None),
    ("flow.py", "Flow._gitlinks", "args"): (BOUNDED, 100, "one of two fixed option tuples; the paths beside it come "
                                                          "from `_capped` in the same loop", None),
    # Typed by a person, or a fixed length.
    ("flow.py", "Flow.editor_run", "args"): (BOUNDED, 8_000, "typed by the user at `upe editor run`: the terminal "
                                                              "bounds it", None),
    ("__main__.py", "detach", "rest"): (BOUNDED, 8_000, "the user's own command line, forwarded to the job", None),
    ("workspace.py", "Workspaces.acquire", "to"): (BOUNDED, 300, "`(\"-b\", branch)`, `(\"-B\", branch)` or "
                                                                   "`(\"--detach\",)`", None),
    ("workspace.py", "Workspaces.robocopy", "exclude_dirs"): (BOUNDED, 1_000, "no caller passes any", None),
    ("agent_package.py", "install", "present"): (BOUNDED, 1_000, "members of `LOCAL_ONLY`", _local_only),
    ("agent_package.py", "uninstall", "tracked"): (BOUNDED, 1_000, "members of `LOCAL_ONLY`", _local_only),
    ("council.py", "_resolutions", "parents"): (BOUNDED, 1_000, "the parents of one merge commit (an octopus of 20 "
                                                                  "is 820)", None),
    # Its own batching.
    ("council.py", "_resolutions.diff", "batch"): (BY_HAND, "_PATHS_PER_CALL", "batches of `_PATHS_PER_CALL` "
                                                                                "characters, counted length + 3", None),
    # Not a command line.
    ("__main__.py", "main", "(a for a in argv if a != '--json')"): (NOT_A_COMMAND, "argparse's own list", None),
    ("__main__.py", "_said", "getattr(exc, '__notes__', ())"): (NOT_A_COMMAND, "a message", None),
    ("__main__.py", "install_skill", "f' --config \"{Path(config).resolve()}\"'"): (NOT_A_COMMAND, "a string added "
                                                                                     "to a string", None),
    ("checks.py", "run_tests", "results.parent.glob(f'{results.stem}.attempt-*')"): (NOT_A_COMMAND, "files to delete", None),
    ("checks.py", "run_tests", "results.parent.glob(f'{results.stem}.rerun*')"): (NOT_A_COMMAND, "files to delete", None),
    ("checks.py", "run_tests", "retrying"): (NOT_A_COMMAND, "`(\"--retries\", n)` or `()`", None),
    ("checks.py", "run_tests_by_classes", "(r.failed or set() for r in runs)"): (NOT_A_COMMAND, "a set union", None),
    ("checks.py", "run_tests_by_classes", "(r.ran for r in runs)"): (NOT_A_COMMAND, "a set union", None),
    ("checks.py", "run_tests_by_classes", "(r.passed for r in runs)"): (NOT_A_COMMAND, "a set union", None),
    ("checks.py", "run_scene_check", "out_dir.glob('upe-scene-check-*.json')"): (NOT_A_COMMAND, "files to delete", None),
    ("checks.py", "run_scene_check", "out_dir.glob('upe-scene-check-*.log')"): (NOT_A_COMMAND, "files to delete", None),
    ("checks.py", "_join_scene_runs", "(r.problems for r in runs)"): (NOT_A_COMMAND, "a set union", None),
    ("checks.py", "_join_scene_runs", "(r.problems for r in runs if r.problems is not None)"): (
        NOT_A_COMMAND, "a set union", None),
    ("merge.py", "Merger.merge", "names"): (NOT_A_COMMAND, "a record's constructor: the branch's and the target's "
                                                           "short names", None),
    ("council.py", "_resolutions", "by_parent.values()"): (NOT_A_COMMAND, "a set union", None),
    ("flow.py", "Flow._jobs", "_"): (NOT_A_COMMAND, "an unpacking in a comprehension", None),
    ("flow.py", "Flow._land", "changed"): (NOT_A_COMMAND, "a list given to `findings_outside_branch`", None),
    ("flow.py", "Flow._land", "merged"): (NOT_A_COMMAND, "a list given to `findings_outside_branch`", None),
    ("flow.py", "Flow._land", "(self.run_dir(task_id) / folder).glob('upe-scene-check-*.json')"): (
        NOT_A_COMMAND, "files to delete", None),
    ("flow.py", "Flow._land", "(self.run_dir(task_id) / folder).glob('upe-scene-check-*.log')"): (
        NOT_A_COMMAND, "files to delete", None),
    ("flow.py", "Flow._own_commits", "others"): (NOT_A_COMMAND, "stdin of `rev-list --stdin`, no argv limit", None),
    ("flow.py", "Flow._own_commits", "also"): (NOT_A_COMMAND, "stdin of `rev-list --stdin`", None),
    ("kept.py", "Kept.from_json", "(frozenset(v) for v in lists)"): (NOT_A_COMMAND, "a constructor", None),
    ("kept.py", "FullScenes.from_json", "counts"): (NOT_A_COMMAND, "a constructor", None),
    ("merge.py", "_layout", "e"): (NOT_A_COMMAND, "a tuple of an edit", None),
    ("merge.py", "_fits", "middle"): (NOT_A_COMMAND, "an unpacking", None),
    ("setup.py", "_free_gb", "path.parents"): (NOT_A_COMMAND, "folders to look at", None),
    ("setup.py", "check", "problem"): (NOT_A_COMMAND, "a constructor", None),
    ("workspace.py", "Workspaces.robocopy", "Path(dest).parents"): (NOT_A_COMMAND, "folders to look at", None),
}

MESSAGE = ("{file}:{line} ({function}) puts a list that grows with a land on a command line: `{expr}`. Pass it through "
           "`capped` (upe.cmdline) in groups, or add it to ENTRIES in tests/test_argv_capped.py as BOUNDED with the "
           "largest it can be, measured, and why")


def _functions(file: str) -> dict[str, ast.AST]:
    tree = ast.parse((SRC / file).read_text(encoding="utf-8"))
    out = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[prefix + child.name] = child
                walk(child, prefix + child.name + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")
    walk(tree, "")
    return out


class ArgvCapped(unittest.TestCase):
    def test_every_growing_command_line_is_capped_or_bounded(self):
        # Red with `-upePaths`, the two `--filter` lines, the checkout restore or the scrub's `update-index` put
        # back in one command line.
        found = [g for g in scan(SRC) if not g.capped and (g.file, g.function, g.expr) not in ENTRIES]
        self.assertEqual([], [MESSAGE.format(**vars(g)) for g in found])

    def test_the_table_has_no_stale_row(self):
        seen = {(g.file, g.function, g.expr) for g in scan(SRC) if not g.capped}
        self.assertEqual([], sorted(key for key in ENTRIES if key not in seen),
                         "a row no growth expression matches: delete it")

    def test_a_bounded_entry_is_measured_and_under_the_cap(self):
        from upe.cmdline import ARGV_CAP

        for key, (kind, *rest) in ENTRIES.items():
            with self.subTest(key=key):
                self.assertIn(kind, (PASS, BOUNDED, BY_HAND, NOT_A_COMMAND))
                if kind == BOUNDED:
                    largest, _, measure = rest
                    self.assertLess(largest, ARGV_CAP)
                    if measure is not None:
                        self.assertLessEqual(measure(), largest)

    def test_a_pass_through_has_its_growing_callers_capped(self):
        # A `PASS` row names the callers that give it a list that grows; each must bind it from `capped`.
        for key, (kind, *rest) in ENTRIES.items():
            if kind != PASS:
                continue
            for file, function in rest[1]:
                with self.subTest(sink=key, caller=(file, function)):
                    body = _functions(file).get(function)
                    self.assertIsNotNone(body, f"{file} has no {function}")
                    called = {n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
                              for n in ast.walk(body) if isinstance(n, ast.Call)}
                    self.assertTrue(called & {"capped", "_capped"}, f"{function} groups nothing by `capped`")

    def test_a_line_with_its_own_batching_reads_its_constant(self):
        for (file, function, _), (kind, *rest) in ENTRIES.items():
            if kind == BY_HAND:
                body = _functions(file)[function]
                self.assertIn(rest[0], {n.id for n in ast.walk(body) if isinstance(n, ast.Name)})

    def test_the_scan_reads_the_shapes_it_claims_to(self):
        def flagged(code):
            return [g.expr for g in scan_source(code) if not g.capped]

        self.assertEqual(flagged("def f(paths):\n    run(['git', 'add', '--', *paths])\n"), ["paths"])
        self.assertEqual(flagged("def f(paths):\n    run(['-upePaths', ';'.join(paths)])\n"), ["paths"])
        self.assertEqual(flagged("def f(argv, args):\n    argv += list(args)\n"), ["list(args)"])
        self.assertEqual(flagged("def f(argv, more):\n    argv.extend(more)\n"), ["more"])
        self.assertEqual(flagged("def f(paths):\n    for group in capped(paths):\n        run(['git', *group])\n"), [])
        self.assertEqual(flagged("def f(gs):\n    for group in _capped(gs):\n"
                                 "        run([a for g in group for a in ('-e', g)])\n"), [], "a comprehension over a group")
        self.assertEqual(flagged("def f(cs):\n    groups = list(capped(cs))\n    run(';'.join(groups[0]))\n"
                                 "    for k, group in enumerate(groups):\n        run(';'.join(group))\n"), [])
        self.assertEqual(flagged("def f(paths):\n    for group in chunks(paths):\n        run(['git', *group])\n"),
                         ["group"], "a loop over another capper is not capped")
        self.assertEqual(flagged("def f(x):\n    run(['git', *SAFE_GIT, *(['-q'] if x else [])])\n"), [],
                         "constants and lists written out")
        # Its limits, read here so they are not forgotten: a command built in pieces, a `,` join.
        self.assertEqual(flagged("def f(paths):\n    argv = ['git', 'add'] + paths\n    run(argv)\n"), [])
        self.assertEqual(flagged("def f(paths):\n    run(['x', ','.join(paths)])\n"), [])

    def test_a_capped_group_fits_a_windows_command_line_even_with_spaces(self):
        # Windows quotes an item with a space (+2). `capped` counts each item as `list2cmdline` writes it, so a worst
        # group (short names with spaces and quotes) stays under 32,767 with the rest of a git call. Red with a count
        # of `len(item) + 1`: 3-character items with a space are 6 each.
        from upe.cmdline import capped
        from upe.safegit import SAFE_GIT

        for items in (["a b"] * 20_000, ['a"b c'] * 10_000, [f"Assets/{'x y' * 30}/{i}.png" for i in range(5_000)]):
            groups = list(capped(items))
            self.assertEqual([i for g in groups for i in g], items)
            for group in groups:
                argv = ["git", *SAFE_GIT, "-C", "C:\\" + "f" * 250, "checkout", "--", *group]
                self.assertLess(len(subprocess.list2cmdline(argv)), 32_767)


if __name__ == "__main__":
    unittest.main()
