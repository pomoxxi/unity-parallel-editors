"""The verdict a land gives for each odd shape of a Unity test report.

A Unity report is an outside input, and its odd shapes are found one by one: a name given twice, a result that is
neither `Passed` nor `Failed`, a re-run that does not name the test, a summary that disagrees with an attempt report.
Two were found by hand (A: a failed case followed by a passed one of the same name; I: a re-run that names the test
`Inconclusive` or `Skipped`), and both let through a land that the base stops.

Each row of `SHAPES` is one shape: the test runs a land meets, in the order it makes them, and two verdicts ("stops" or
"lands"); `MADE` holds, for each row, the kind of each run the land makes, in order (`full`, `rerun`, `classes`):
- `before`: the verdict at `at`, the base: the commit the work that added the row started from. A fixed field: the
  repo cannot run that commit's code. It was worked out once, by a script that feeds the row's fixtures to that
  commit's own `run_tests` and `unity_checks(baseline=)`, the way its land used them (run on a `git archive` of the
  base's `src`).
- `now`: the verdict at the commit that holds this file.

Each row that lands now has its reason: in `LOOSER` when it stopped at `at`, else in `ALSO_LANDS`. The test
drives every shape the way a real land does (`Flow.land`: real git, the merge's run with `rerun=` and `known_for=` as
`_unity_checks` gives them, the class-only run, the target's full run, the kept files), and fails when a verdict
differs from the table, when the land makes other runs than `MADE` says, by kind and order (a change of path with the
same verdict, even one that keeps the count), or when a row that lands is not named with its reason. It prints
`report shapes: N read; looser than their base: M`.

The paths a row drives:
- `merge`: the target's own kept result knows `KNOWN` (as known failures explain the CLI's exit 8), so
  only the reading of the merge's report, its attempt report, the summary and the tool's re-run decides (case 1).
- `class-only`: no result of the target's own; the ancestor's full result lists `KNOWN` as failed on every attempt, so
  the class-only run's report decides (`Flow._class_run`, then `kept.decide`; case 2).
- `kept`: the producer of a kept file (`Kept.of`). A first land keeps the merge's run, with a re-run of the shape; the
  row also states the list the kept file puts `PARAM` in. A second land, on a commit after that merge, uses it as the
  ancestor's result: its merge and class-only run fail `PARAM`, and the target's full run passes it.

Not read here: `test_counts` (it counts for the notes only, no verdict); a report that does not parse (`run_tests`
reads it as no report: "tests left no results report", tested in `test_checks`); a shape of the target's own full run
before the merge (case 3, which reads the report the same way as the merge's run).

The unit tests of A and I stay where they are (`test_checks.UnityChecks.test_a_name_with_any_failed_case_is_failed`,
`test_checks.Rerun.test_only_passed_in_the_rerun_is_a_pass`, and at land level
`test_flow.TargetTestsEdgeCaseTests.test_a_failed_case_followed_by_a_passed_case_of_one_name_stops_the_land`); this
table holds one fixture per shape and copies none of theirs.

A new shape found later gets a row here.
"""

import unittest
from dataclasses import dataclass

KNOWN = "Game.LevelTests.Known"  # fails on the target, on every attempt
DUP = "Game.LevelTests.EveryLevel(Game.LevelConfig)"  # two cases render to one full name
PARAM = 'Levels.EveryLevelLoads("Hall")'  # parameterised: the CLI's retry does not re-run it
DOOR = "Game.SwitchTests.Toggles"
OK = "Ok.One"


@dataclass(frozen=True)
class Run:
    """One `unity test` call: the report's cases in order, the `attempt-2.xml` cases (None: none written), the retry
    summary (None: none written), and the exit code (None: 8 when a case failed, else 0)."""
    cases: tuple
    attempt: tuple | None = None
    flaky: tuple | None = None  # the summary's `flaky` list; with `failed`, the summary is written
    failed: tuple | None = None  # the summary's `failed` list
    exit: int | None = None
    report: bool = True  # False: the run writes no report (exit 6), as an Editor that never got to the tests

    @property
    def code(self) -> int:
        return self.exit if self.exit is not None else 8 if any(r == "Failed" for _, r in self.cases) else 0


@dataclass(frozen=True)
class Shape:
    n: str
    name: str
    path: str  # "merge", "class-only" or "kept" (the module's docstring)
    runs: tuple  # the land's test runs, in the order it makes them; for "kept", the first land's then the second's
    before: str  # the verdict at `at`: fixed
    now: str  # the verdict at the commit that holds this file
    kept: str | None = None  # "kept" only: the list of the kept file that holds PARAM after the first land
    at: str = "base"  # the commit the work that added the row started from
    known: tuple = (KNOWN,)  # the failed tests of the target's (or the ancestor's) kept result
    cap: int | None = None  # `checks.FILTER_CAP` for the land, so its class lists split one class a group


# The merge's run, whose failed test is a parameterised one the CLI does not re-run: the tool's re-run follows.
MERGE_PARAM = Run(((OK, "Passed"), (KNOWN, "Failed"), (PARAM, "Failed")), attempt=((KNOWN, "Failed"),),
                  flaky=(), failed=(KNOWN, PARAM))
# The merge's run when only KNOWN fails, retried and failing again.
MERGE_KNOWN = Run(((OK, "Passed"), (KNOWN, "Failed")), attempt=((KNOWN, "Failed"),), flaky=(), failed=(KNOWN,))
# The target's full run in the class-only rows: KNOWN fails there on every attempt.
TARGET_KNOWN = MERGE_KNOWN
# The second land of a "kept" row: its merge fails PARAM, and its re-run fails it again; the class-only run fails both;
# the target's full run passes PARAM (the second branch broke it in the suite's order; the target fixed it since).
SECOND = (MERGE_PARAM, Run(((PARAM, "Failed"),)), Run(((KNOWN, "Failed"), (PARAM, "Failed"))),
          Run(((OK, "Passed"), (KNOWN, "Failed"), (PARAM, "Passed")), attempt=((KNOWN, "Failed"),), flaky=(),
              failed=(KNOWN,)))


def dup(order):
    """Shapes 1 and 2: the merge's report names DUP twice. Its re-run (when the land makes one) fails it again."""
    return (Run(((OK, "Passed"), (KNOWN, "Failed"), *((DUP, r) for r in order)), attempt=((KNOWN, "Failed"),),
                flaky=(), failed=(KNOWN, DUP)),
            Run(((DUP, "Failed"),)))


def rerun(*cases):
    """Shapes 3 to 9: the tool's re-run of PARAM names it so."""
    return (MERGE_PARAM, Run(tuple(cases)))


def alone(*cases):
    """The class-only run on the target names KNOWN so; the target's full run (row e, if the land makes it) fails it."""
    return (MERGE_KNOWN, Run(tuple(cases)), TARGET_KNOWN)


def kept(*cases):
    """The first land's re-run of PARAM names it so; then the second land."""
    return (MERGE_PARAM, Run(tuple(cases)), *SECOND)


# The class lists of the re-run and of the class-only run are split into groups when longer than one command line
# takes (`checks.run_tests_by_classes`); the rows G1 to G4 set the cap so that each class is a group. Their `before` is
# the verdict the base, which ran each list in one run, gives the same cases: worked out once with the rows' runs
# joined into one.
KNOWN2 = "Game.GateTests.Known2"  # a second class that fails on the target, on every attempt
PARAM2 = 'Gates.EveryGateOpens("Iron gate")'  # a second parameterised test, of another class
MERGE_TWO = Run(((OK, "Passed"), (KNOWN, "Failed"), (KNOWN2, "Failed")), attempt=((KNOWN, "Failed"), (KNOWN2, "Failed")),
                flaky=(), failed=(KNOWN, KNOWN2))
MERGE_PARAMS = Run(((OK, "Passed"), (KNOWN, "Failed"), (PARAM, "Failed"), (PARAM2, "Failed")),
                   attempt=((KNOWN, "Failed"),), flaky=(), failed=(KNOWN, PARAM, PARAM2))


SHAPES = (
    Shape("1", "a name twice, Failed then Passed", "merge", dup(["Failed", "Passed"]), "stops", "stops"),
    Shape("2", "a name twice, Passed then Failed", "merge", dup(["Passed", "Failed"]), "stops", "stops"),
    Shape("3", "the re-run names it Passed", "merge", rerun((PARAM, "Passed")), "stops", "lands"),
    Shape("4", "the re-run names it Inconclusive", "merge", rerun((PARAM, "Inconclusive")), "stops", "stops"),
    Shape("5", "the re-run names it Skipped", "merge", rerun((PARAM, "Skipped")), "stops", "stops"),
    Shape("6", "the re-run names it Warning", "merge", rerun((PARAM, "Warning")), "stops", "stops"),
    Shape("7", "the re-run does not name it", "merge", rerun(("Levels.Other", "Passed")), "stops", "stops"),
    Shape("8", "the re-run names it Failed then Passed", "merge", rerun((PARAM, "Failed"), (PARAM, "Passed")),
          "stops", "stops"),
    Shape("9", "the re-run names it Passed then Failed", "merge", rerun((PARAM, "Passed"), (PARAM, "Failed")),
          "stops", "stops"),
    # The tool trusts the summary's `flaky` list, as the base did: not looser. A later reader could break it.
    Shape("S1", "the summary calls a test flaky while the attempt report names it Failed", "merge",
          (Run(((OK, "Passed"), (KNOWN, "Failed"), (DOOR, "Failed")), attempt=((KNOWN, "Failed"), (DOOR, "Failed")),
               flaky=(DOOR,), failed=(KNOWN,)),), "lands", "lands"),
    # The class-only run. The base had none: it ran the target's full suite, which fails KNOWN, so it landed each.
    # Rows c and d decide here, so the target's full run is not made (`MADE`).
    Shape("C1", "the class-only run names it twice, Failed then Passed", "class-only",
          alone((KNOWN, "Failed"), (KNOWN, "Passed")), "lands", "lands"),
    Shape("C2", "the class-only run names it twice, Passed then Failed", "class-only",
          alone((KNOWN, "Passed"), (KNOWN, "Failed")), "lands", "lands"),
    Shape("C3", "the class-only run names it twice, Passed and Passed", "class-only",
          alone((KNOWN, "Passed"), (KNOWN, "Passed")), "lands", "stops"),
    Shape("C4", "the class-only run names it Inconclusive", "class-only", alone((KNOWN, "Inconclusive")),
          "lands", "stops"),
    # Kept.of's split, then the second land's verdict. The base ran the second target's full suite, which passes
    # PARAM. Where row d decides (K1, K5), the target's full run is not made (`MADE`).
    Shape("K1", "the kept run's re-run names it Failed", "kept", kept((PARAM, "Failed")), "stops", "lands",
          kept="failed"),
    Shape("K2", "the kept run's re-run names it Passed", "kept", kept((PARAM, "Passed")), "stops", "stops",
          kept="flaky"),
    Shape("K3", "the kept run's re-run does not name it", "kept", kept(("Levels.Other", "Passed")), "stops", "stops",
          kept="unconfirmed"),
    Shape("K4", "the kept run's re-run names it Inconclusive", "kept", kept((PARAM, "Inconclusive")), "stops", "stops",
          kept="unconfirmed"),
    Shape("K5", "the kept run's re-run names it Failed then Passed", "kept", kept((PARAM, "Failed"), (PARAM, "Passed")),
          "stops", "lands", kept="failed"),
    # Split runs. The class-only run's groups go in sorted class order: Game.GateTests, then
    # Game.LevelTests; the re-run's: Gates, then Levels.
    Shape("G1", "a split class-only run: a name Failed in group 1's report and Passed in group 2's", "class-only",
          (MERGE_TWO, Run(((KNOWN2, "Failed"), (KNOWN, "Failed"))), Run(((KNOWN, "Passed"),)), MERGE_TWO),
          "lands", "lands", known=(KNOWN, KNOWN2), cap=4),
    Shape("G2", "a split class-only run: group 2 left no report", "class-only",
          (MERGE_TWO, Run(((KNOWN2, "Failed"),)), Run((), report=False), MERGE_TWO),
          "stops", "stops", known=(KNOWN, KNOWN2), cap=4),
    Shape("G3", "a split re-run: group 2 left no report", "merge",
          (MERGE_PARAMS, Run(((PARAM2, "Failed"),)), Run((), report=False)), "stops", "stops", cap=4),
    Shape("G4", "a split re-run: each group names its test Passed", "merge",
          (MERGE_PARAMS, Run(((PARAM2, "Passed"),)), Run(((PARAM, "Passed"),))), "lands", "lands", cap=4),
)

R1 = ("accepted: PARAM failed on every attempt in the ancestor's full run; the target fixed it since, by a commit the tool did not check; it fails when its class runs alone, and the branch "
      "breaks it again. Row d takes it as known; `checks.md` names the open case")
LOOSER = {
    "3": "wanted: the merge's own retry for the parameterised tests the CLI does not "
         "re-run",
    "K1": R1,
    "K5": R1 + " (the re-run's failed case is a failure)",
}
# The rows that land now and landed at their `at` too, each with its reason: a row that starts to land must say why
# as `LOOSER` does, even where `at` landed it (a fixture's world can hide what it lets through).
ALSO_LANDS = {
    "S1": "the base also took the summary's `flaky` list over the attempt report (`_flaky(summary) & failed`)",
    "C1": "row d: KNOWN failed on every attempt at the ancestor, and a failed case of the class-only run is a failure "
          "; the base's full run on the target failed it too",
    "C2": "row d, as C1",
    "G1": "as C2 split in two runs: KNOWN failed in one group's report, so the join reads it failed (row d)",
    "G4": "as shape 3 split in two runs: each parameterised test passed in the tool's re-run (flaky)",
}

# The runs each row's land makes, by kind, in order: `full` (the whole suite, no
# filter), `rerun` (the tool's re-run, to `<report>.rerun.xml`) and `classes` (the class-only run on the target). A row's
# `runs` not in it are left unmade: the target's full run, where row c or d decides.
MERGE_AND_RERUN = ("full", "rerun")
MADE = {
    **{n: MERGE_AND_RERUN for n in ("1", "2", "3", "4", "5", "6", "7", "8", "9")},
    "S1": ("full",),  # nothing the CLI did not retry, so no re-run
    **{n: ("full", "classes") for n in ("C1", "C2", "C3", "C4")},  # rows d (C1, C2) and c (C3, C4): no full run
    **{n: MERGE_AND_RERUN * 2 + ("classes",) for n in ("K1", "K5")},  # the second land: row d
    **{n: MERGE_AND_RERUN * 2 + ("classes", "full") for n in ("K2", "K3", "K4")},  # the second land: row e
    "G1": ("full", "classes", "classes"),  # row d decides
    "G2": ("full", "classes", "classes"),  # unfinished: stops, as one class-only run with no report does
    **{n: ("full", "rerun", "rerun") for n in ("G3", "G4")},
}


def kind(argv) -> str:
    """The kind of one `unity test` call, from its argv."""
    if ".rerun" in Path(argv[argv.index("--output") + 1]).name:  # `.rerun.xml`, or a group's `.rerun.part-k.xml`
        return "rerun"
    return "classes" if "--filter" in argv else "full"

# The land -------------------------------------------------------------------------------------------------------------

import json  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

from test_flow import TargetTestsCase, fake_cli, git, nunit_cases  # noqa: E402


def shape_cli(runs, calls, argvs):
    """`unity` stand-in: each `test` call writes the next run of `runs` (a list it pops) as it is, cases in order; any
    other call is `test_flow.fake_cli`'s. A `test` call past the last run fails the test; what is left in `runs` after
    the land is what it did not make."""
    inner = fake_cli(calls, [], [])._runner

    def run(argv, **kw):
        if argv[1] != "test":
            return inner(argv, **kw)
        calls.append(argv[1:3])
        argvs.append([*argv, kw.get("cwd")])
        if not runs:
            raise AssertionError(f"a test run the shape does not hold: {argv}")
        spec = runs.pop(0)
        if not spec.report:
            return subprocess.CompletedProcess(argv, 6, json.dumps({"success": False, "data": None}), "")
        out = Path(argv[argv.index("--output") + 1])
        out.write_text(nunit_cases(spec.cases))
        if spec.attempt is not None:
            out.with_name(f"{out.stem}.attempt-2.xml").write_text(nunit_cases(spec.attempt))
        if spec.failed is not None:
            out.with_suffix(".retries.json").write_text(json.dumps(
                {"flaky": [{"test": n, "attempts": 2} for n in spec.flaky or ()],
                 "failed": [{"test": n, "attempts": 2} for n in spec.failed]}))
        return subprocess.CompletedProcess(argv, spec.code, json.dumps({"success": spec.code == 0, "data": None}), "")

    return run


VERDICT = {"landed": "lands", "checks-failed": "stops"}


class ReportShapes(TargetTestsCase):
    def land(self, shape):
        """The shape's verdict, by the land that meets it; for "kept", also the list the kept file holds PARAM in.
        `self.left` holds the runs the land did not make."""
        flow = self.flow()
        self.start_and_race(flow)
        self.left = list(shape.runs)
        flow.cli._runner = shape_cli(self.left, self.cli_calls, self.argvs)
        if shape.path == "merge":
            self.put_kept(self.target, failed=list(shape.known))
        elif shape.path == "class-only":
            self.put_kept(self.ancestor, failed=list(shape.known))
        else:
            self.put_kept(self.target, failed=[KNOWN, PARAM])
        if shape.cap is not None:
            from unittest import mock

            import upe.checks

            with mock.patch.object(upe.checks, "FILTER_CAP", shape.cap, create=True):  # `create`: the script of `at`
                outcome = flow.land("dash")
        else:
            outcome = flow.land("dash")
        if shape.path != "kept":
            return VERDICT.get(outcome.state, outcome.state), None
        self.assertEqual(outcome.state, "landed", f"shape {shape.n}: the first land knows KNOWN and PARAM")
        merge = flow.task("dash").landed_commit
        held = self.kept_file(merge)
        where = [key for key in ("failed", "flaky", "unconfirmed") if PARAM in held[key]]
        tree = git(self.repo, "rev-parse", f"{merge}^{{tree}}").strip()
        after = git(self.repo, "commit-tree", "-p", merge, "-m", "a fix the tool did not check", tree).strip()
        git(self.repo, "update-ref", "refs/heads/main", after)
        flow.start("kick", "Add a kick.", ["Assets/Features/Kick/"])
        self.work(flow, "kick", "Assets/Features/Kick/Kick.cs", "class Kick {}\n")
        outcome = flow.land("kick")
        return VERDICT.get(outcome.state, outcome.state), ", ".join(where) or "none"

    def test_each_shape_gives_the_verdict_of_the_table(self):
        read = 0
        for shape in SHAPES:
            with self.subTest(shape=shape.n, name=shape.name):
                self.tearDown()
                self.setUp()
                got, where = self.land(shape)
                read += 1
                if shape.kept is not None:
                    self.assertEqual(where, shape.kept, f"shape {shape.n} ({shape.name}): `Kept.of` put PARAM in "
                                                        f"{where}, the table says {shape.kept}")
                if got != shape.now:
                    if got == "lands":
                        fix = (f"if that is wanted, set its `now` to 'lands' and add it, with the reason, to "
                               f"{'`LOOSER`' if shape.before == 'stops' else '`ALSO_LANDS`'}; else mend the reader")
                    else:
                        fix = "set its `now`, and say why the verdict changed"
                    self.fail(f"shape {shape.n} ({shape.name}) now {got}; the table says {shape.now} "
                              f"(at {shape.at}: {shape.before}): {fix}")
                # The same verdict by another path is a change too, whether it starts
                # or drops a run, or swaps one kind of run for another with the same count.
                made = tuple(kind(argv) for argv in self.argvs)
                self.assertEqual(made, MADE[shape.n],
                                 f"shape {shape.n} ({shape.name}): the land made the runs {made}, the table says "
                                 f"{MADE[shape.n]}: the path changed; set `MADE`, and say why")
                self.assertEqual(len(self.left), len(shape.runs) - len(made), "each run made reads the next fixture")
        looser = sorted(s.n for s in SHAPES if s.before == "stops" and s.now == "lands")
        print(f"\nreport shapes: {read} read; looser than their base: {len(looser)} (in LOOSER: {', '.join(looser)})")

    def test_each_shape_that_lands_is_named_with_its_reason(self):
        looser = {s.n for s in SHAPES if s.before == "stops" and s.now == "lands"}
        self.assertEqual(set(LOOSER), looser, "each shape that lands where its base stopped it is in `LOOSER`, with its "
                                              "reason; and `LOOSER` names no other")
        also = {s.n for s in SHAPES if s.before == "lands" and s.now == "lands"}
        self.assertEqual(set(ALSO_LANDS), also, "each shape that lands where its base landed it too is in `ALSO_LANDS`, "
                                                "with its reason; and `ALSO_LANDS` names no other")
        self.assertTrue(all(s.before in VERDICT.values() and s.now in VERDICT.values() for s in SHAPES))
        self.assertEqual(set(MADE), {s.n for s in SHAPES}, "`MADE` holds each row, and no other")
        self.assertTrue(all(0 < len(MADE[s.n]) <= len(s.runs) for s in SHAPES))
        self.assertEqual(len({s.n for s in SHAPES}), len(SHAPES), "one row per shape")


if __name__ == "__main__":
    unittest.main()
