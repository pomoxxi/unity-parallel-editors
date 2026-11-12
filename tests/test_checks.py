import json
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from upe.checks import failed_tests, parse_unity_yaml, run_tests, static_checks, unity_checks
from upe.unity_cli import UnityCli

HEAD = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
GUID_MAT = "1" * 32
GUID_PKG = "2" * 32
GUID_SCENE = "3" * 32


def scene(*docs: str) -> str:
    return HEAD + "".join(docs)


def go(fid, name, transform):
    return f"--- !u!1 &{fid}\nGameObject:\n  m_Component:\n  - component: {{fileID: {transform}}}\n  m_Name: {name}\n"


def tr(fid, go_id, father=0, children=()):
    kids = "".join(f"  - {{fileID: {c}}}\n" for c in children) if children else ""
    kids_line = "  m_Children:\n" + kids if children else "  m_Children: []\n"
    return f"--- !u!4 &{fid}\nTransform:\n  m_GameObject: {{fileID: {go_id}}}\n{kids_line}  m_Father: {{fileID: {father}}}\n"


def renderer(fid, go_id, guid):
    return f"--- !u!23 &{fid}\nMeshRenderer:\n  m_GameObject: {{fileID: {go_id}}}\n  m_Materials:\n  - {{fileID: 2100000, guid: {guid}, type: 2}}\n"


GOOD_SCENE = scene(
    go(1, "Root", 2), tr(2, 1, children=(4,)),
    go(3, "Child", 4), tr(4, 3, father=2),
    renderer(5, 3, GUID_MAT),
    renderer(6, 3, GUID_PKG),
    "--- !u!23 &7\nMeshRenderer:\n  m_Materials:\n  - {fileID: 10303, guid: 0000000000000000f000000000000000, type: 0}\n",
)


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.put("Assets/Scenes.meta", "guid: " + "9" * 32 + "\nfolderAsset: yes\n")  # a folder's .meta
        self.put("Assets/Scenes/Arena.unity", GOOD_SCENE)
        self.put("Assets/Scenes/Arena.unity.meta", f"guid: {GUID_SCENE}\n")
        self.put("Assets/Mat.mat", HEAD + "--- !u!21 &2100000\nMaterial:\n  m_Name: Mat\n")
        self.put("Assets/Mat.mat.meta", f"guid: {GUID_MAT}\n")
        self.put("Library/PackageCache/com.x/Pkg.mat.meta", f"guid: {GUID_PKG}\n")

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def checks(self, changed=None):
        return {(f.path, f.check) for f in static_checks(self.root, changed).findings}


class StaticChecks(ProjectCase):
    def test_good_project_passes(self):
        report = static_checks(self.root)
        self.assertTrue(report.ok, report.findings)
        self.assertIn("static checks covered 2 YAML files", report.notes)

    def test_duplicate_file_id(self):
        self.put("Assets/Scenes/Arena.unity", GOOD_SCENE + go(1, "Dup", 2))
        self.assertIn(("Assets/Scenes/Arena.unity", "file-id"), self.checks())

    def test_dangling_local_reference(self):
        self.put("Assets/Scenes/Arena.unity", scene(go(1, "Root", 99)))
        self.assertIn(("Assets/Scenes/Arena.unity", "local-ref"), self.checks())

    def test_missing_guid(self):
        self.put("Assets/Scenes/Arena.unity", scene(go(1, "R", 2), tr(2, 1), renderer(3, 1, "f" * 32)))
        self.assertIn(("Assets/Scenes/Arena.unity", "guid-ref"), self.checks())

    def test_guid_check_skipped_without_package_cache(self):
        import shutil

        shutil.rmtree(self.root / "Library")
        self.put("Assets/Scenes/Arena.unity", scene(go(1, "R", 2), tr(2, 1), renderer(3, 1, "f" * 32)))
        report = static_checks(self.root)
        self.assertTrue(report.ok, report.findings)
        self.assertTrue(any("GUID references not checked" in n for n in report.notes))

    def test_hierarchy_mismatch(self):
        self.put("Assets/Scenes/Arena.unity", scene(go(1, "Root", 2), tr(2, 1, children=(4,)), go(3, "Child", 4), tr(4, 3, father=0)))
        self.assertIn(("Assets/Scenes/Arena.unity", "hierarchy"), self.checks())

    def test_stripped_children_are_skipped(self):
        doc = scene(go(1, "Root", 2), tr(2, 1, children=(8,)), "--- !u!4 &8 stripped\nTransform:\n  m_PrefabInstance: {fileID: 9}\n",
                    "--- !u!1001 &9\nPrefabInstance:\n  m_SourcePrefab: {fileID: 100100000, guid: " + GUID_MAT + ", type: 3}\n")
        self.put("Assets/Scenes/Arena.unity", doc)
        self.assertEqual(self.checks(), set())

    def test_not_unity_yaml(self):
        self.put("Assets/Scenes/Arena.unity", "%YAML 1.1\nno tag here\n")
        self.assertIn(("Assets/Scenes/Arena.unity", "yaml"), self.checks())

    def test_conflict_markers(self):
        self.put("Assets/Scenes/Arena.unity", GOOD_SCENE + "<<<<<<< HEAD\n=======\n>>>>>>> upe/feat\n")
        self.assertIn(("Assets/Scenes/Arena.unity", "conflict-marker"), self.checks(["Assets/Scenes/Arena.unity"]))

    def test_missing_and_orphan_meta(self):
        self.put("Assets/New.prefab", HEAD)
        self.put("Assets/Gone.png.meta", "guid: " + "4" * 32 + "\n")
        found = self.checks()
        self.assertIn(("Assets/New.prefab", "meta"), found)
        self.assertIn(("Assets/Gone.png.meta", "meta"), found)

    def test_hidden_folders_ignored(self):
        self.put("Assets/Docs~/readme.md", "x")
        self.put("Assets/.hidden/x.txt", "x")
        self.assertEqual(self.checks(), set())

    def test_duplicate_guid(self):
        self.put("Assets/Copy.mat", HEAD)
        self.put("Assets/Copy.mat.meta", f"guid: {GUID_MAT}\n")
        found = self.checks()
        self.assertIn(("Assets/Copy.mat.meta", "meta"), found)
        self.assertIn(("Assets/Mat.mat.meta", "meta"), found)

    # A folder's .meta and a file's are told apart by `folderAsset: yes`, and a folder .meta with nothing under it is
    # an empty folder, which git tracks only as its .meta.
    def test_a_folder_meta_with_nothing_under_it_passes(self):
        # Red before: "orphan .meta" (the folder is not on disk).
        self.put("Assets/Empty.meta", "fileFormatVersion: 2\nguid: " + "5" * 32 + "\nfolderAsset: yes\n")
        self.assertEqual(self.checks(), set())

    def test_a_file_meta_with_nothing_under_it_is_an_orphan(self):
        self.put("Assets/Empty.meta", "fileFormatVersion: 2\nguid: " + "5" * 32 + "\n")
        self.assertEqual(self.messages(), {("Assets/Empty.meta", "orphan .meta: its asset is missing")})

    def test_a_folder_meta_over_a_file_is_a_finding(self):
        # Red before: no finding (the .meta's type was not read).
        self.put("Assets/Mat.mat.meta", f"guid: {GUID_MAT}\nfolderAsset: yes\n")
        self.assertEqual(self.messages(), {("Assets/Mat.mat.meta", "a folder .meta (folderAsset: yes) over a file")})

    def test_a_file_meta_over_a_folder_is_a_finding(self):
        # Red before: no finding.
        self.put("Assets/Scenes.meta", "guid: " + "9" * 32 + "\n")
        self.assertEqual(self.messages(),
                         {("Assets/Scenes.meta", "a file .meta over a folder: it has no `folderAsset: yes`")})

    def test_a_meta_with_no_guid_is_a_finding(self):
        # Unity gives such an asset a new GUID. Red before: no finding.
        self.put("Assets/Tex.png", "png")
        self.put("Assets/Tex.png.meta", "fileFormatVersion: 2\n")
        self.assertEqual(self.messages(),
                         {("Assets/Tex.png.meta", "no GUID read in the .meta, so Unity will give its asset a new one")})

    def test_a_path_that_moves_from_one_duplicated_guid_to_another_is_reported(self):
        # On the target A and B hold g1, C and D hold g2; the merge gives B g2. Its key's GUID changed, so
        # B is the merge's own finding. Red with the GUID taken out of the key.
        from upe.checks import CheckReport, MetaTree, check_meta_files

        def tree(holders):
            files = frozenset(p for path in holders for p in (path, path + ".meta"))
            guids: dict = {}
            for path, guid in holders.items():
                guids.setdefault(guid, []).append(path + ".meta")
            return MetaTree(files, frozenset(), {g: tuple(m) for g, m in guids.items()})

        g1, g2 = "1" * 32, "2" * 32
        target = tree({"Assets/A.png": g1, "Assets/B.png": g1, "Assets/C.png": g2, "Assets/D.png": g2})
        merge = tree({"Assets/A.png": g1, "Assets/B.png": g2, "Assets/C.png": g2, "Assets/D.png": g2})
        report = CheckReport()
        check_meta_files(merge, report, target)
        self.assertEqual([(f.path, f.message) for f in report.findings],
                         [("Assets/B.png.meta", f"GUID {g2} is also used by Assets/C.png.meta, Assets/D.png.meta")])

    def messages(self):
        return {(f.path, f.message) for f in static_checks(self.root).findings if f.check == "meta"}

    def test_changed_limits_yaml_checks(self):
        self.put("Assets/Scenes/Arena.unity", scene(go(1, "Root", 99)))
        self.assertEqual(self.checks(changed=["Assets/Mat.mat"]), set())


class ParseTests(unittest.TestCase):
    def test_documents(self):
        docs = parse_unity_yaml(GOOD_SCENE)
        self.assertEqual([d.file_id for d in docs], ["1", "2", "3", "4", "5", "6", "7"])

    def test_malformed_header(self):
        with self.assertRaises(ValueError):
            parse_unity_yaml(HEAD + "--- !u!x &1\n")


def nunit(*failed, passed=("Ok.One",)):
    cases = [f'<test-case fullname="{n}" result="Passed" />' for n in passed]
    cases += [f'<test-case fullname="{n}" result="Failed" />' for n in failed]
    return '<test-run result="Failed"><test-suite>' + "".join(cases) + "</test-suite></test-run>"


def fake_cli(test_exit=0, report=None, error_code=None, calls=None):
    """`unity test` that exits `test_exit` and writes `report` (NUnit XML) to --output."""
    def run(argv, **kw):
        if calls is not None:
            calls.append(argv)
        if report is not None:
            Path(argv[argv.index("--output") + 1]).write_text(report, encoding="utf-8")
        errors = [{"code": error_code}] if error_code else []
        return subprocess.CompletedProcess(argv, test_exit, json.dumps({"success": test_exit == 0, "errors": errors}), "")

    return UnityCli("unity.exe", runner=run)


def flaky_cli(report, flaky, failed=(), calls=None):
    """`unity test --retries`: the report holds the first attempt, and a summary sits beside it."""
    def run(argv, **kw):
        if calls is not None:
            calls.append((argv, kw))
        output = Path(argv[argv.index("--output") + 1])
        output.write_text(report, encoding="utf-8")
        output.with_suffix(".retries.json").write_text(json.dumps(
            {"flaky": [{"test": t, "attempts": 2} for t in flaky], "failed": [{"test": t, "attempts": 2} for t in failed]}), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 8 if failed else 0, json.dumps({"success": not failed, "errors": []}), "")

    return UnityCli("unity.exe", runner=run)


class Retries(unittest.TestCase):
    REPORT = ('<test-run><test-case fullname="A.Fine" result="Passed"/><test-case fullname="B.Slow" result="Failed"/>'
              '<test-case fullname="C.Broken" result="Failed"/></test-run>')

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = Path(self._tmp.name) / "P"
        self.p.mkdir()
        self.out = Path(self._tmp.name) / "r" / "report.xml"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_test_that_passes_on_a_retry_does_not_stop_the_land(self):
        report = unity_checks(flaky_cli(self.REPORT, ["B.Slow", "C.Broken"]), self.p, results=self.out, retries=1)
        self.assertTrue(report.ok, report.findings)
        self.assertIn("2 tests failed, then passed on a retry, so they do not stop the land: B.Slow, C.Broken", report.notes)

    def test_a_test_that_fails_again_does(self):
        report = unity_checks(flaky_cli(self.REPORT, ["B.Slow"], ["C.Broken"]), self.p, results=self.out, retries=1)
        self.assertEqual([f.path for f in report.findings], ["C.Broken"])

    def test_the_summary_cannot_excuse_a_test_the_report_does_not_list(self):
        run = run_tests(flaky_cli(self.REPORT, ["Nothing.Here"], ["B.Slow", "C.Broken"]), self.p, self.out, retries=1)
        self.assertEqual((run.failed, run.flaky), ({"B.Slow", "C.Broken"}, set()))

    def test_it_asks_for_the_retries_and_waits_for_them(self):
        calls = []
        run_tests(flaky_cli(self.REPORT, [], calls=calls), self.p, self.out, 600, retries=1)
        argv, kw = calls[0]
        self.assertEqual(argv[argv.index("--retries") + 1], "1")
        self.assertEqual(kw["timeout"], 2 * 600 + 300)

    def test_the_summary_cannot_make_a_test_fail_twice(self):
        # The summary lists B.Slow and C.Broken under `failed`, but the second report holds only
        # C.Broken (the CLI does not re-run parameterised tests, yet lists them there). Only C.Broken failed twice.
        # Red with `twice` taken from the summary's `failed` list.
        def cli(argv, **kw):
            output = Path(argv[argv.index("--output") + 1])
            output.write_text(self.REPORT, encoding="utf-8")
            output.with_suffix(".retries.json").write_text(json.dumps(
                {"flaky": [], "failed": [{"test": t, "attempts": 2} for t in ("B.Slow", "C.Broken")]}), encoding="utf-8")
            output.with_name(f"{output.stem}.attempt-2.xml").write_text(
                '<test-run><test-case fullname="C.Broken" result="Failed"/></test-run>', encoding="utf-8")
            return subprocess.CompletedProcess(argv, 8, json.dumps({"success": False, "errors": []}), "")

        run = run_tests(UnityCli("unity.exe", runner=cli), self.p, self.out, retries=1)
        self.assertEqual((run.failed, run.twice, run.retried), ({"B.Slow", "C.Broken"}, {"C.Broken"}, {"C.Broken"}))
        self.assertEqual(run.unconfirmed, {"B.Slow"})

    def test_with_no_second_report_nothing_failed_twice(self):
        run = run_tests(flaky_cli(self.REPORT, [], ["B.Slow", "C.Broken"]), self.p, self.out, retries=1)
        self.assertEqual((run.failed, run.twice), ({"B.Slow", "C.Broken"}, set()))

    def test_an_old_summary_is_not_read(self):
        self.out.parent.mkdir(parents=True)
        self.out.with_suffix(".retries.json").write_text(json.dumps({"flaky": [{"test": "C.Broken"}]}))

        def no_summary(argv, **kw):
            Path(argv[argv.index("--output") + 1]).write_text(self.REPORT)
            return subprocess.CompletedProcess(argv, 8, "{}", "")

        run = run_tests(UnityCli("unity.exe", runner=no_summary), self.p, self.out)
        self.assertEqual((run.failed, run.flaky), ({"B.Slow", "C.Broken"}, set()))


def param_cli(runs, calls):
    """`unity test` whose n-th call writes `runs[n]`: (first report's [(name, result)], second report's or None, exit).
    Each case's class is its name cut at `(`, then at the last `.`, as Unity's `classname` gives it."""
    def report(cases):
        def one(name, result):
            cls = name.split("(", 1)[0].rsplit(".", 1)[0]
            return f'<test-case fullname="{name.replace(chr(34), "&quot;")}" classname="{cls}" result="{result}"/>'
        return "<test-run>" + "".join(one(n, r) for n, r in cases) + "</test-run>"

    def run(argv, **kw):
        calls.append(argv)
        first, second, code = runs.pop(0)
        output = Path(argv[argv.index("--output") + 1])
        if first is not None:
            output.write_text(report(first), encoding="utf-8")
        if second is not None:
            output.with_name(f"{output.stem}.attempt-2.xml").write_text(report(second), encoding="utf-8")
        return subprocess.CompletedProcess(argv, code, json.dumps({"success": code == 0, "errors": []}), "")

    return UnityCli("unity.exe", runner=run)


class Rerun(unittest.TestCase):
    """The failed tests the CLI did not retry get one run of their classes."""

    PARAM = 'Levels.EveryLevelLoads("Hall")'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = Path(self._tmp.name) / "P"
        self.p.mkdir()
        self.out = Path(self._tmp.name) / "r" / "upe-test-results.xml"
        self.calls = []

    def tearDown(self):
        self._tmp.cleanup()

    def checks(self, *runs):
        from upe.checks import confirm_failures

        cli = param_cli(list(runs), self.calls)
        notes = []

        def rerun(run):
            confirmed, note = confirm_failures(cli, self.p, self.out, run, 600)
            notes.append(note)
            return confirmed

        return unity_checks(cli, self.p, results=self.out, retries=1, rerun=rerun), notes

    def test_the_cli_untried_failures_get_one_run_of_their_classes(self):
        # Without the re-run, the first-attempt failure stops the land with no second sample.
        first = [("Ok.One", "Passed"), (self.PARAM, "Failed"), ("Timed.Waits", "Failed")]
        report, notes = self.checks((first, [("Timed.Waits", "Failed")], 8), ([(self.PARAM, "Failed")], None, 8))
        self.assertEqual(len(self.calls), 2)
        rerun = self.calls[1]
        self.assertEqual(rerun[rerun.index("--filter") + 1], "Levels")
        self.assertNotIn("--retries", rerun)
        self.assertEqual(Path(rerun[rerun.index("--output") + 1]).name, "upe-test-results.rerun.xml")
        self.assertEqual(report.tests.twice, {self.PARAM, "Timed.Waits"})
        self.assertIn("1 failed tests the CLI did not retry (parameterised) were run again, by class (Levels): "
                      "1 failed again, 0 passed (flaky)", notes[0])

    def test_a_rerun_that_passes_is_flaky_and_does_not_stop_the_land(self):
        first = [("Ok.One", "Passed"), (self.PARAM, "Failed")]
        report, _ = self.checks((first, [], 8), ([(self.PARAM, "Passed")], None, 0))
        self.assertTrue(report.ok, report.findings)
        self.assertEqual((report.tests.failed, report.tests.flaky), (set(), {self.PARAM}))

    def test_a_test_the_rerun_does_not_name_or_a_rerun_with_no_report_stays_failed_once(self):
        first = [("Ok.One", "Passed"), (self.PARAM, "Failed")]
        for again in (([("Ok.Two", "Passed")], None, 0), (None, None, 6)):
            with self.subTest(again=again):
                self.calls.clear()
                report, notes = self.checks((first, [], 8), again)
                self.assertEqual([f.path for f in report.findings], [self.PARAM])
                self.assertEqual(report.tests.unconfirmed, {self.PARAM})
                self.assertIn("count as failed once", notes[0])

    def test_a_rerun_that_names_the_test_failed_once_and_passed_once_keeps_it_failed(self):
        # Two cases of one name in the re-run, the failed one first. Red before: the last case won, the
        # test became flaky, and the exit 8 was rewritten to 0.
        first = [("Ok.One", "Passed"), (self.PARAM, "Failed")]
        report, _ = self.checks((first, [], 8), ([(self.PARAM, "Failed"), (self.PARAM, "Passed")], None, 8))
        self.assertEqual([f.path for f in report.findings], [self.PARAM])
        self.assertEqual((report.tests.twice, report.tests.flaky), ({self.PARAM}, set()))

    def test_only_passed_in_the_rerun_is_a_pass(self):
        # A test the re-run names Inconclusive or Skipped (an `Assert.Ignore` when its class runs
        # alone) is not flaky; it stays failed once and stops the land. Red before: any result but Failed passed it.
        first = [("Ok.One", "Passed"), (self.PARAM, "Failed")]
        for result in ("Inconclusive", "Skipped"):
            with self.subTest(result=result):
                self.calls.clear()
                report, notes = self.checks((first, [], 8), ([(self.PARAM, result)], None, 0))
                self.assertEqual([f.path for f in report.findings], [self.PARAM])
                self.assertEqual((report.tests.unconfirmed, report.tests.flaky), ({self.PARAM}, set()))
                self.assertIn("neither failed nor passed there", notes[0])

    def test_a_stale_rerun_report_is_deleted(self):
        # An earlier land's re-run report must not be read as this run's: `run_tests` deletes it before it runs.
        # Red with the `.rerun*` glob removed from `run_tests`' stale files.
        self.out.parent.mkdir(parents=True)
        stale = self.out.with_name("upe-test-results.rerun.xml")
        stale.write_text('<test-run><test-case fullname="Old.Stale" result="Failed"/></test-run>', encoding="utf-8")
        run_tests(param_cli([([("Ok.One", "Passed")], None, 0)], self.calls), self.p, self.out)
        self.assertFalse(stale.exists())


class UnityChecks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = Path(self._tmp.name) / "P"
        self.p.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_pass(self):
        self.assertTrue(unity_checks(fake_cli(report='<test-run><test-case fullname="A.B" result="Passed"/></test-run>'), self.p).ok)

    def test_a_report_with_no_tests_is_not_a_pass(self):
        report = unity_checks(fake_cli(report="<test-run></test-run>"), self.p)
        self.assertIn("lists no tests", report.findings[0].message)

    def test_a_failing_exit_with_no_failures_listed_is_not_a_pass(self):
        xml = '<test-run><test-case fullname="A.B" result="Passed"/></test-run>'
        report = unity_checks(fake_cli(6, xml, error_code="COMMAND_FAILED"), self.p)
        self.assertIn("lists no failures, but the run exited 6", report.findings[0].message)

    def test_each_failed_test_is_a_finding(self):
        report = unity_checks(fake_cli(8, nunit("A.Broken", "B.Broken")), self.p)
        self.assertEqual([(f.path, f.message) for f in report.findings], [("A.Broken", "test failed"), ("B.Broken", "test failed")])

    def test_failures_already_on_the_target_do_not_count(self):
        report = unity_checks(fake_cli(8, nunit("Old.Broken", "New.Broken")), self.p, baseline={"Old.Broken", "Gone.Fixed"})
        self.assertEqual([f.path for f in report.findings], ["New.Broken"])
        self.assertIn("1 tests also fail on the target before the merge: Old.Broken", report.notes)

    def test_only_known_failures_passes(self):
        report = unity_checks(fake_cli(8, nunit("Old.Broken")), self.p, baseline={"Old.Broken"})
        self.assertTrue(report.ok)

    def test_tests_failed_without_a_report(self):
        report = unity_checks(fake_cli(1, error_code="TESTS_FAILED"), self.p)
        self.assertEqual(report.findings[0].message, "one or more tests failed, and no report names them")

    def test_did_not_finish(self):
        self.assertIn("no results report", unity_checks(fake_cli(6), self.p).findings[0].message)

    def test_the_notes_count_the_tests_and_the_branchs_own(self):
        xml = ('<test-run><test-case fullname="Game.MoveTests.A" result="Passed"/>'
               '<test-case fullname="MoveTests.B(1)" result="Passed"/><test-case fullname="Other.C" result="Failed"/>'
               '<test-case fullname="Other.D" result="Skipped"/></test-run>')
        out = Path(self._tmp.name) / "run" / "results.xml"
        report = unity_checks(fake_cli(8, xml), self.p, baseline={"Other.C"}, results=out, classes=["MoveTests"])
        self.assertTrue(out.exists())
        self.assertIn(f"tests: 1 failed, 2 passed, 1 skipped; results in {out}", report.notes)
        self.assertIn("MoveTests, changed by the branch: 2 passed", report.notes)

    def test_a_timeout_says_it_is_not_about_the_code(self):
        report = unity_checks(fake_cli(14, error_code="TEST_TIMED_OUT"), self.p, test_timeout_s=60)
        self.assertIn("did not finish in 60 s", report.findings[0].message)
        self.assertIn("Not a finding about the code", report.findings[0].message)

    def test_exit_0_without_a_report_is_not_a_pass(self):
        # Branch code can end the Editor with exit 0 before any test runs.
        report = unity_checks(fake_cli(0), self.p)
        self.assertFalse(report.ok)
        self.assertIn("no results report", report.findings[0].message)

    def test_protected_namespace_and_case_checks(self):
        from upe.checks import CheckReport, check_case_duplicates, check_protected, check_tool_namespace

        report = CheckReport()
        check_protected(["Assets/A.cs", ".claude/settings.json", "CLAUDE.md"], [".claude/", "CLAUDE.md"], report)
        self.assertEqual([f.path for f in report.findings], [".claude/settings.json", "CLAUDE.md"])
        (self.p / "Assets").mkdir(exist_ok=True)
        (self.p / "Assets" / "Fake.cs").write_text("namespace Upe.Agent.Editor { class SceneCheck {} }")
        check_tool_namespace(self.p, ["Assets/Fake.cs"], report)
        self.assertEqual(report.findings[-1].check, "tool-namespace")
        check_case_duplicates(["Assets/Foo.cs", "Assets/foo.cs", "Assets/Bar.cs"], ["Assets/foo.cs", "Assets/Bar.cs"], report)
        self.assertEqual(report.findings[-1].path, "Assets/foo.cs")

    def test_runs_test_alone_with_results_under_logs(self):
        calls = []
        unity_checks(fake_cli(calls=calls), self.p)
        self.assertEqual([a[1] for a in calls], ["test"])  # no `open`: test starts its own Editor
        self.assertIn(str(self.p / "Logs" / "upe-test-results.xml"), calls[0])

    def test_a_name_with_any_failed_case_is_failed(self):
        # One full name, several cases. Red before (the last case won).
        from upe.checks import read_report

        xml = ('<test-run><test-case fullname="D.C(N)" result="Failed"/><test-case fullname="D.C(N)" result="Passed"/>'
               '<test-case fullname="E.F" result="Passed"/><test-case fullname="E.F" result="Skipped"/>'
               '<test-case fullname="G.H" result="Passed"/><test-case fullname="G.H" result="Passed"/></test-run>')
        self.assertEqual({n: r for n, (r, _) in read_report(xml).items()},
                         {"D.C(N)": "Failed", "E.F": "Skipped", "G.H": "Passed"})

    def test_a_case_with_no_classname_takes_its_class_from_its_name(self):
        # `class_of` without its cut at `(` must not live. A report with no `classname` falls
        # back to the full name, cut at its first `(` and then at its last `.`: the class the class-only run and the
        # re-run filter by. Red with the cut at `(` removed (`Levels.EveryLevelLoads("a` is no class).
        from upe.checks import read_report

        xml = '<test-run><test-case fullname="Levels.EveryLevelLoads(&quot;a.b&quot;)" result="Failed"/></test-run>'
        self.assertEqual(read_report(xml), {'Levels.EveryLevelLoads("a.b")': ("Failed", "Levels")})

    def test_failed_tests_parses_nunit(self):
        self.assertEqual(failed_tests(nunit("X.Y(1)", passed=("Z",))), {"X.Y(1)"})


# The shapes of a scene-check report, and what a land does with each ------------------------------------
#
# A scene report is another report than a Unity test report: `tests/test_report_shapes.py` reads none of these. Each
# row is one shape of one batch run of `SceneCheck.Run` (the report, the Editor's exit code, its log, whether the report
# is older than the run, whether it was a run of some paths). Two verdicts:
# - `at`: what `scene_checks` (the merge's check, with no baseline) gives at the commit these shapes were
#   first written against. A fixed field: worked out once by feeding each row to that commit's own `scene_checks`
#   (a `git archive` of its `src`). The test checks the code today gives the same: the reading of the merge's report did not change.
# - `kept`: whether today a run of that shape is kept as its commit's full result (`FullScenes.of`); at that commit nothing
#   was kept. Only the first row is.


@dataclass(frozen=True)
class SceneShape:
    name: str
    report: dict | None  # the JSON the run writes; None: no report
    exit_code: int = 0
    log: str = ""  # the Editor's log
    stale: bool = False  # the report is older than the run (written before it, not by it)
    paths: tuple = ()  # the run's -upePaths; () is the full run
    at: str = ""  # the verdict at that commit: "passes" or "stops: <checks>"
    kept: bool = False


ROW = {"path": "Assets/Scenes/Arena.unity", "kind": "missing-script", "where": "Arena/Old", "detail": "Missing"}
THREW = {"path": "Assets/Scenes/Arena.unity", "kind": "exception", "where": "", "detail": "ArgumentException"}
LICENCE = "Licensing initialization failed\n"
CS_ERROR = "Assets/X.cs(1,1): error CS0246: The type or namespace name 'Y' could not be found\n"

SCENE_SHAPES = [
    SceneShape("complete, no problem", {"scenes": 96, "prefabs": 825, "problems": []}, at="passes", kept=True),
    SceneShape("complete, with a problem (exit 1)", {"scenes": 96, "prefabs": 825, "problems": [ROW]}, 1,
               at="stops: missing-script", kept=True),
    SceneShape("an exception row (exit 2)", {"scenes": 3, "prefabs": 0, "problems": [ROW, THREW]}, 2,
               at="stops: exception,missing-script"),
    SceneShape("no report", None, 2, at="stops: scene-check"),
    SceneShape("no report, licensing failed twice", None, 1, log=LICENCE, at="stops: scene-check"),
    SceneShape("a report, and licensing failed in its log", {"scenes": 96, "prefabs": 825, "problems": []},
               log=LICENCE, at="passes"),
    SceneShape("a report, and a compile error in its log", {"scenes": 96, "prefabs": 825, "problems": []},
               log=CS_ERROR, at="passes"),
    SceneShape("no scene and no prefab", {"scenes": 0, "prefabs": 0, "problems": []}, at="passes"),
    SceneShape("no counts", {"problems": []}, at="passes"),
    SceneShape("counts that are not numbers", {"scenes": "96", "prefabs": True, "problems": []}, at="passes"),
    SceneShape("a stale report", {"scenes": 96, "prefabs": 825, "problems": []}, stale=True, at="passes"),
    SceneShape("a run of some paths", {"scenes": 1, "prefabs": 0, "problems": []},
               paths=("Assets/Scenes/Arena.unity",), at="passes"),
    SceneShape("a report and exit 3", {"scenes": 96, "prefabs": 825, "problems": []}, 3, at="passes"),
]


def shape_editor(shape: SceneShape):
    """An `Editor` whose batch run ends as the shape says."""
    from upe.editor import Editor

    def run(argv, **kw):
        out = Path(argv[argv.index("-upeOut") + 1])
        log = Path(argv[argv.index("-logFile") + 1])
        log.write_text(shape.log, encoding="utf-8")
        if shape.report is not None:
            out.write_text(json.dumps(shape.report), encoding="utf-8")
            if shape.stale:
                os.utime(out, (time.time() - 3600, time.time() - 3600))
        return subprocess.CompletedProcess(argv, shape.exit_code, "", "")

    return Editor("U.exe", runner=run, job_workers=None)


def shape_verdict(shape: SceneShape, folder: Path, scene_checks) -> str:
    """What `scene_checks` (any commit's, passed in) says of the shape as the merge's check, with no baseline."""
    project = folder / "project"
    project.mkdir(parents=True, exist_ok=True)
    report = scene_checks(shape_editor(shape), project, paths=shape.paths, out_dir=folder / "out")
    return "passes" if report.ok else "stops: " + ",".join(sorted({f.check for f in report.findings}))


class SceneShapes(unittest.TestCase):
    def test_each_shape_reads_as_at_its_base_and_only_a_complete_full_run_is_kept(self):
        from upe.checks import run_scene_check, scene_checks
        from upe.kept import FullScenes

        for shape in SCENE_SHAPES:
            with self.subTest(shape.name), tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(shape_verdict(shape, Path(tmp) / "a", scene_checks), shape.at, "the merge's verdict")
                run = run_scene_check(shape_editor(shape), Path(tmp) / "a" / "project", shape.paths, Path(tmp) / "b")
                self.assertEqual(FullScenes.of(run) is not None, shape.kept, FullScenes.why_not(run))
        print(f"scene report shapes: {len(SCENE_SHAPES)} read; kept: {sum(s.kept for s in SCENE_SHAPES)}")


class SceneStoreTests(unittest.TestCase):
    C = "a" * 40
    M = "1" * 32  # the slot's Library, as made

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def full(self):
        from upe.kept import FullScenes

        return FullScenes(frozenset({tuple(ROW.values())}), 96, 825)

    def test_only_a_full_result_is_put_never_a_run_or_a_test_result(self):
        from upe.checks import SceneRun
        from upe.kept import Kept, SceneStore

        store = SceneStore(self.root / "scene-baselines")
        for other in (SceneRun((), 0, frozenset(), 96, 825), Kept(), {"scope": "full"}):
            with self.subTest(type(other).__name__):
                try:
                    store.put(self.C, "s1", self.M, other)
                except TypeError:
                    pass
                except Exception as exc:  # a put that took it, then could not write it: a failure, not an error
                    self.fail(f"put took a {type(other).__name__}: {exc!r}")
                else:
                    self.fail(f"put took a {type(other).__name__}")
                self.assertFalse(store.path(self.C, "s1").exists())
        self.assertTrue(store.put(self.C, "s1", self.M, self.full()))
        self.assertEqual(store.get(self.C, "s1", self.M), self.full())

    def test_a_file_that_reads_is_never_written_over_one_that_does_not_is(self):
        from upe.kept import FullScenes, SceneStore

        store = SceneStore(self.root)
        store.put(self.C, "s1", self.M, self.full())
        self.assertFalse(store.put(self.C, "s1", self.M, FullScenes(frozenset(), 1, 0)))
        self.assertEqual(store.get(self.C, "s1", self.M), self.full())
        for broken in ("{", '{"format": 1}', json.dumps({**json.loads(self.full().to_json()), "kind": "tests"})):
            with self.subTest(broken):
                store.path(self.C, "s1").write_text(broken, encoding="utf-8")
                self.assertIsNone(store.get(self.C, "s1", self.M))
                self.assertTrue(store.put(self.C, "s1", self.M, self.full()), "an unreadable file would cost a full run each land")

    def test_a_kept_file_of_any_odd_shape_reads_as_none(self):
        from upe.kept import FullScenes

        good = json.loads(self.full().to_json())
        for name, change in (("format 2", {"format": 2}), ("scope paths", {"scope": "paths"}),
                             ("no kind", {"kind": None}), ("counts 0", {"scenes": 0, "prefabs": 0}),
                             ("a count not a number", {"scenes": "96"}), ("a bool count", {"prefabs": True}),
                             ("a problem not four strings", {"problems": [{"path": "a", "kind": 1}]}),
                             ("an exception row", {"problems": [THREW]}), ("problems not a list", {"problems": {}})):
            with self.subTest(name):
                self.assertIsNone(FullScenes.from_json(json.dumps({**good, **change})))
        self.assertEqual(FullScenes.from_json(json.dumps(good)), self.full())

    def test_the_test_store_and_the_scene_store_never_read_each_others_files(self):
        from upe.kept import Kept, KeptStore, SceneStore

        kept = KeptStore(self.root / "test-baselines")
        kept.put(self.C, Kept(failed=frozenset({"A.B"}), cases=3))
        scenes = SceneStore(self.root / "scene-baselines")
        scenes.put(self.C, "s1", self.M, self.full())
        self.assertIsNone(Kept.from_json(scenes.path(self.C, "s1").read_text(encoding="utf-8")))
        self.assertIsNone(SceneStore(self.root / "test-baselines").get(self.C, "s1", self.M))
        self.assertEqual(kept.get(self.C).failed, frozenset({"A.B"}))

    def test_a_name_that_is_not_a_commit_is_neither_read_nor_written(self):
        from upe.kept import SceneStore

        store = SceneStore(self.root)
        self.assertIsNone(store.get("../x", "s1", self.M))
        with self.assertRaises(ValueError):
            store.put("HEAD", "s1", self.M, self.full())

    def test_a_result_is_read_only_in_the_slot_that_made_it(self):
        # One commit's full check can differ between slots (the same commit gave a different problem count in two slots). Red with the slot left out of the file's name.
        from upe.kept import SceneStore

        store = SceneStore(self.root)
        self.assertTrue(store.put(self.C, "s2", self.M, self.full()))
        self.assertEqual(store.get(self.C, "s2", self.M), self.full())
        self.assertIsNone(store.get(self.C, "s3", self.M))
        for slot in ("", "../s1", "s0", "x1", "s1/..", "S1"):
            with self.subTest(slot), self.assertRaises(ValueError):
                store.path(self.C, slot)

    def test_a_result_is_read_only_by_the_library_that_made_it(self):
        # A slot removed and made again under the same name has a new Library (the golden's copy). A result
        # the old Library made is not read by the new one, and the new one's result is written over it. Red before:
        # the key was the slot's name only.
        from upe.kept import SceneStore

        store = SceneStore(self.root)
        self.assertTrue(store.put(self.C, "s2", self.M, self.full()))
        self.assertEqual(store.get(self.C, "s2", self.M), self.full())
        self.assertIsNone(store.get(self.C, "s2", "2" * 32), "another Library under the slot's name")
        self.assertTrue(store.put(self.C, "s2", "2" * 32, self.full()), "the new Library's result is kept")
        self.assertEqual(store.get(self.C, "s2", "2" * 32), self.full())
        self.assertIsNone(store.get(self.C, "s2", self.M), "the old one's is gone")
        for made in ("", "x" * 32, "1" * 31):
            with self.subTest(made), self.assertRaises(ValueError):
                store.put(self.C, "s2", made, self.full())


TEST_ASMDEF = json.dumps({"name": "T", "references": ["UnityEngine.TestRunner"], "defineConstraints": ["UNITY_INCLUDE_TESTS"]})


class AllowList(unittest.TestCase):
    """Every kind of added path, and why it keeps or skips the full scene check."""

    TARGET = {"Assets/Tests/T.asmdef": TEST_ASMDEF, "Assets/Tests/Old.cs": "class Old {}",
              "Assets/Art/Old.png": "png", "Assets/Art/Old.png.meta": "guid: " + "e" * 32 + "\n",
              "Assets/Ref/R.asmref": '{"reference": "T"}', "Assets/Plain/P.asmdef": '{"name": "P"}',
              "Assets/Scenes/A.unity": "x"}

    def judge(self, added=None, changes=None, target=None, taken=None):
        from upe.checks import scene_plan

        added = added or {}
        target = self.TARGET if target is None else target
        changes = changes if changes is not None else [("A", p) for p in added]
        read_new = lambda p: added[p].encode("utf-8") if isinstance(added.get(p), str) else added.get(p)  # noqa: E731
        read_target = lambda p: target[p].encode("utf-8") if p in target else None  # noqa: E731
        return scene_plan(changes, read_new, set(target), read_target, taken)

    def with_meta(self, path, text="x", guid="f" * 32):
        return {path: text, path + ".meta": f"fileFormatVersion: 2\nguid: {guid}\n"}

    def assertFull(self, plan, words):
        self.assertEqual(plan.kind, "full")
        self.assertIn(words, "; ".join(plan.reasons))

    def test_passive_assets_with_their_metas_skip(self):
        for path in ("Assets/Art/New.png", "Assets/Art/New.mat", "Assets/Data/D.asset", "Assets/Audio/A.wav",
                     "Assets/Models/M.fbx", "Assets/Text/T.json"):
            with self.subTest(path):
                self.assertEqual(self.judge(self.with_meta(path)).kind, "skip")
        folder = {**self.with_meta("Assets/Art/New/T.png"), "Assets/Art/New.meta": "guid: " + "d" * 32 + "\n"}
        self.assertEqual(self.judge(folder).kind, "skip", "a new folder's .meta is paired with its folder")

    def test_kinds_that_change_compilation_or_import_keep_the_full_check(self):
        for path, words in (("Assets/Code/X.asmdef", "a .asmdef, not a kind"), ("Assets/Code/X.asmref", "a .asmref"),
                            ("Assets/csc.rsp", "a .rsp"), ("Assets/Plugins/P.dll", "a .dll"),
                            ("Assets/Plugins/P.so", "a .so"), ("Assets/Shaders/S.shader", "a .shader"),
                            ("Assets/Shaders/S.hlsl", "a .hlsl"), ("Assets/Art/NoSuffix", "a file with no extension")):
            with self.subTest(path):
                self.assertFull(self.judge(self.with_meta(path)), f"{path}: {words}")
        for path in ("Packages/com.x/package.json", "ProjectSettings/TagManager.asset", "docs/x.md"):
            with self.subTest(path):
                self.assertFull(self.judge({path: "x"}), f"{path}: outside Assets/")

    def test_a_script_outside_a_test_assembly_keeps_the_full_check(self):
        for path, words in (("Assets/Editor/Tool.cs", "a script outside any test assembly"),
                            ("Assets/Plain/More.cs", "compiles into Assets/Plain/P.asmdef, which has no test-only"),
                            ("Assets/Ref/More.cs", "compiles through Assets/Ref/R.asmref, an .asmref")):
            with self.subTest(path):
                self.assertFull(self.judge(self.with_meta(path, "class A {}")), f"{path}: {words}")
        added = {**self.with_meta("Assets/Tests/Sub/S.asmdef", TEST_ASMDEF), **self.with_meta("Assets/Tests/Sub/S.cs")}
        self.assertFull(self.judge(added), "Assets/Tests/Sub/S.cs: compiles into Assets/Tests/Sub/S.asmdef, which the "
                                           "merge adds")

    def test_a_script_in_a_test_assembly_with_no_hook_skips(self):
        plan = self.judge(self.with_meta("Assets/Tests/New/MoveTests.cs", "class MoveTests { [Test] void T() {} }"))
        self.assertEqual(plan.kind, "skip")

    def test_the_hook_scan_sees_each_way_of_naming_a_hook(self):
        for name, text in (
                ("direct", "class P : UnityEditor.AssetPostprocessor {}"),
                ("alias", "using P = UnityEditor.AssetPostprocessor;\nclass Q : P {}"),
                ("using static", "using static UnityEditor.AssetDatabase;\n[InitializeOnLoadMethod] static void M() {}"),
                ("partial", "public partial class Old { }"),
                ("a hook method in a partial's other half", "class X { static void OnPostprocessAllAssets() {} }"),
                ("behind #if", "#if FALSE\n[InitializeOnLoad]\n#endif\nclass L {}"),
                ("Cf character", "class P : Asset\u200bPostprocessor {}"),
                ("\\u escape", "class P : Asset\\u0050ostprocessor {}"),
                ("Callbacks namespace", "using UnityEditor . Callbacks;\n[DidReloadScripts] static void R() {}"),
                ("in a comment", "// AssetModificationProcessor\nclass C {}"),
                ("scripted importer", "class I : UnityEditor.AssetImporters.ScriptedImporter {}"),
                ("not UTF-8", b"\xff\xfeclass C {}")):
            with self.subTest(name):
                self.assertFull(self.judge({"Assets/Tests/H.cs": text, "Assets/Tests/H.cs.meta": "guid: " + "c" * 32}),
                                "Assets/Tests/H.cs: a script in a test assembly that names")

    # Each name the scan looks for, written here and not read from `checks` a name taken out of the scan
    # turns its subtest red.
    SCAN_NAMES = ("AssetPostprocessor", "ScriptedImporter", "AssetImporter", "AssetModificationProcessor",
                  "InitializeOnLoad", "DidReloadScripts", "ModuleInitializer", "Callbacks", "PostProcessScene",
                  "ExecuteAlways", "ExecuteInEditMode", "OnValidate", "partial", "AssetsModifiedProcessor",
                  "OnAssetsModified", "InitializeOnEnterPlayMode", "ScriptableObject", "OnEnable", "Awake",
                  "ISerializationCallbackReceiver", "OnAfterDeserialize", "OnBeforeSerialize",
                  "OnPreprocessTexture", "OnPostprocessAllAssets", "OnWillSaveAssets", "OnWillCreateAsset",
                  "OnWillDeleteAsset", "OnWillMoveAsset", "IsOpenForEdit", "OnImportAsset")

    def test_each_name_of_the_scan_alone_keeps_the_full_check(self):
        for name in self.SCAN_NAMES:
            with self.subTest(name):
                plan = self.judge({"Assets/Tests/H.cs": f"class H {{ /* {name} */ }}",
                                   "Assets/Tests/H.cs.meta": "guid: " + "c" * 32})
                self.assertFull(plan, "Assets/Tests/H.cs: a script in a test assembly that names")
                self.assertIn(name, "; ".join(plan.reasons))

    def test_import_callbacks_and_new_scriptable_objects_keep_the_full_check(self):
        # Unity's other import callback, in both namespaces, and a new ScriptableObject whose instance the merge
        # adds (its OnEnable runs on the import). Red before: each skipped.
        meta = lambda n: f"fileFormatVersion: 2\nguid: {n:032x}\n"  # noqa: E731
        for name, files, words in (
                ("AssetsModifiedProcessor", {"Assets/Tests/Mod.cs": "class Mod : UnityEditor.AssetsModifiedProcessor "
                                             "{ protected override void Changed() {} }"}, "AssetsModifiedProcessor"),
                ("Experimental.AssetsModifiedProcessor",
                 {"Assets/Tests/Mod.cs": "class Mod : UnityEditor.Experimental.AssetsModifiedProcessor { protected "
                                         "override void OnAssetsModified(string[] c) {} }"}, "OnAssetsModified"),
                ("a ScriptableObject and its instance",
                 {"Assets/Tests/Evil.cs": "public class Evil : UnityEngine.ScriptableObject { void OnEnable() { "
                                          "UnityEditor.AssetDatabase.DeleteAsset(\"Assets/Scripts/Player.cs\"); } }",
                  "Assets/Tests/Evil.asset": "--- !u!114 &11400000\nMonoBehaviour:\n  m_Script: {fileID: 11500000, "
                                             f"guid: {1:032x}, type: 3}}\n"}, "ScriptableObject, OnEnable")):
            with self.subTest(name):
                added = {}
                for i, (path, text) in enumerate(files.items()):
                    added.update({path: text, path + ".meta": meta(i + 1)})
                self.assertFull(self.judge(added), words)

    def test_a_script_that_does_not_read_keeps_the_full_check(self):
        # Red before: read as a script with no hook, so skipped.
        self.assertFull(self.judge({"Assets/Tests/H.cs": None, "Assets/Tests/H.cs.meta": "guid: " + "c" * 32}),
                        "Assets/Tests/H.cs: a script that does not read")

    def test_two_assembly_files_in_one_folder_keep_the_full_check(self):
        # Unity takes one of them, which this rule does not guess.
        target = {**self.TARGET, "Assets/Tests/U.asmdef": TEST_ASMDEF}
        self.assertFull(self.judge(self.with_meta("Assets/Tests/New.cs", "class N {}"), target=target),
                        "Assets/Tests/New.cs: its folder Assets/Tests holds 2 assembly files")

    def test_a_meta_with_no_guid_keeps_the_full_check(self):
        # The judgment after the merge (the GUID rule given) reads each added .meta's GUID.
        taken = lambda guid, meta: ""  # noqa: E731
        added = {"Assets/Art/New.png": "png", "Assets/Art/New.png.meta": "fileFormatVersion: 2\n"}
        self.assertFull(self.judge(added, taken=taken), "Assets/Art/New.png.meta: no GUID")
        self.assertEqual(self.judge(self.with_meta("Assets/Art/New.png"), taken=taken).kind, "skip", "the control")

    def test_a_meta_with_two_guid_lines_keeps_the_full_check(self):
        # The GUID rule read the first `guid:` line only; which one Unity takes is not shown, so
        # the second could be the target's `Old.png`'s. Red before: skipped.
        taken = lambda guid, meta: "Assets/Art/Old.png.meta" if guid == "e" * 32 else ""  # noqa: E731
        for second in ("guid: " + "e" * 32, "  guid: " + "e" * 32, "guid: not-hex"):
            with self.subTest(second):
                added = {"Assets/Art/New.png": "png",
                         "Assets/Art/New.png.meta": f"fileFormatVersion: 2\nguid: {'f' * 32}\n{second}\n"}
                self.assertFull(self.judge(added, taken=taken), "Assets/Art/New.png.meta: 2 guid lines")

    def test_a_meta_alone_or_a_meta_whose_guid_is_taken_keeps_the_full_check(self):
        self.assertFull(self.judge({"Assets/Art/Old.png.meta": "guid: " + "b" * 32},
                                   changes=[("A", "Assets/Art/Old.png.meta")]),
                        "Assets/Art/Old.png.meta: a .meta for an asset or folder the merge does not add")
        taken = lambda guid, meta: "an asset in the package cache's" if guid == "f" * 32 else ""  # noqa: E731
        self.assertFull(self.judge(self.with_meta("Assets/Art/New.png"), taken=taken),
                        f"its GUID {'f' * 32} is also an asset in the package cache's")
        self.assertEqual(self.judge(self.with_meta("Assets/Art/New.png", guid="1" * 32), taken=taken).kind, "skip")

    def test_a_change_a_delete_or_a_rename_keeps_the_full_check(self):
        self.assertFull(self.judge(changes=[("M", "Assets/Art/Old.png")]), "Assets/Art/Old.png: changed (status M)")
        self.assertFull(self.judge(changes=[("D", "Assets/Art/Old.png"), ("A", "Assets/Art/New.png")],
                                   added={"Assets/Art/New.png": "png"}), "Assets/Art/Old.png: deleted (status D)")

    def test_scenes_and_prefabs_keep_their_paths_and_a_deleted_one_the_full_check(self):
        plan = self.judge(changes=[("M", "Assets/Scenes/A.unity"), ("A", "Assets/Editor/Tool.cs")])
        self.assertEqual((plan.kind, plan.paths), ("paths", ("Assets/Scenes/A.unity",)), "a scene plus code")
        self.assertFull(self.judge(changes=[("D", "Assets/Scenes/A.unity")]),
                        "Assets/Scenes/A.unity: a scene or prefab deleted (or renamed)")

    def test_the_test_runner_references_are_the_ones_unitys_own_test_assembly_names(self):
        # A test assembly as Unity's own packages ship it (the HDRP config package's editor tests).
        from upe.checks import TEST_REFERENCES, test_assembly

        shown = ASMDEF_OF_UNITY_PACKAGE.encode("utf-8")
        refs = set(json.loads(ASMDEF_OF_UNITY_PACKAGE)["references"])
        self.assertEqual(TEST_REFERENCES, refs, "the runner by name, as a Unity package's test assembly names it")
        self.assertTrue(test_assembly(shown))
        guids = json.dumps({"references": ["GUID:27619889b8ba8c24980f49ee34dbb44a"]}).encode()
        self.assertFalse(test_assembly(guids), "a reference by a GUID no project file names is not trusted")


ASMDEF_OF_UNITY_PACKAGE = """{
    "name": "Unity.RenderPipelines.HighDefinitionConfig.Editor.Tests",
    "references": [
        "UnityEngine.TestRunner",
        "UnityEditor.TestRunner"
    ],
    "includePlatforms": [
        "Editor"
    ],
    "excludePlatforms": [],
    "allowUnsafeCode": true,
    "overrideReferences": true,
    "precompiledReferences": [
        "nunit.framework.dll"
    ],
    "autoReferenced": false,
    "defineConstraints": [
        "UNITY_INCLUDE_TESTS"
    ],
    "versionDefines": []
}"""


# Command lines that grow with a land, in groups --------------------------------------------------------


def group_editor(project: Path, problems=(), no_report=(), log_text=None, between=None):
    """An `Editor` stand-in that reads its `-upePaths` as `SceneCheck.cs` does (each path in turn; one that is not in the
    project is an `exception` row and ends the loop). `problems`: rows, each reported when its path is checked.
    `no_report`: the runs (1, 2, ...) that write no report. `log_text`: run -> its log. `between(n)`: called after run
    n. `.argvs` holds each run's argv."""
    from upe.editor import Editor

    argvs = []

    def run(argv, **kw):
        argvs.append(list(argv))
        n = len(argvs)
        out, log = Path(argv[argv.index("-upeOut") + 1]), Path(argv[argv.index("-logFile") + 1])
        log.write_text((log_text or {}).get(n, ""), encoding="utf-8")
        # As `SceneCheck.Run` (unity/com.upe.agent/Editor/SceneCheck.cs:42-84): every scene of the list,
        # then every prefab; an exception ends both loops with an `exception` row that names no path, and exit 2; else
        # exit 1 when there is a problem.
        found, counts, exit_code = [], {".unity": 0, ".prefab": 0}, 0
        paths = argv[argv.index("-upePaths") + 1].split(";") if "-upePaths" in argv else []
        ordered = [p for p in paths if p.lower().endswith(".unity")] + [p for p in paths if p.lower().endswith(".prefab")]
        for path in ordered:
            if not (project / path).is_file():
                found.append({"path": "", "kind": "exception", "where": "", "detail": f"ArgumentException: {path}"})
                exit_code = 2
                break
            found += [p for p in problems if p["path"] == path]
            counts[Path(path).suffix.lower()] += 1
        if n not in no_report:
            out.write_text(json.dumps({"scenes": counts[".unity"], "prefabs": counts[".prefab"], "problems": found}),
                           encoding="utf-8")
        if between:
            between(n)
        return subprocess.CompletedProcess(argv, exit_code or (1 if found else 0), "", "")

    editor = Editor("U.exe", runner=run, job_workers=None)
    editor.argvs = argvs
    return editor


class SceneGroups(unittest.TestCase):
    """`run_scene_check` with more paths than one command line takes. In a test project of about 17,000 tracked files,
    about 530 scenes and prefabs in two folders join to about 48,000 characters, and about 1,100 in all of `Assets/` to
    about 87,000: one `-upePaths` past Windows' 32,767. Without the split the first and the last two tests here are
    red by their assertions (one run, whose command line is over the limit); the others need a split to exist."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name) / "project"
        self.out = Path(self._tmp.name) / "out"
        self.paths = [f"Assets/Levels/Zone {i // 50:02}/Prefabs/Prop with a long name {i:04}.prefab" for i in range(690)]
        self.paths += [f"Assets/Levels/Scenes/Level with a long name {i:04}.unity" for i in range(10)]  # loaded first
        for p in self.paths:
            (self.project / p).parent.mkdir(parents=True, exist_ok=True)
            (self.project / p).write_text("x")
        self.row = lambda p: {"path": p, "kind": "missing-script", "where": "Root", "detail": "Missing"}

    def tearDown(self):
        self._tmp.cleanup()

    def assert_fits(self, editor):
        for argv in editor.argvs:
            self.assertLess(len(subprocess.list2cmdline(argv)), 32_767, "one Unity command line")

    def test_many_paths_are_checked_in_groups_each_under_the_limit_and_read_as_one(self):
        from upe.checks import run_scene_check
        from upe.kept import FullScenes

        self.assertGreater(len(";".join(self.paths)), 32_767)
        editor = group_editor(self.project, [self.row(self.paths[3]), self.row(self.paths[650])])
        run = run_scene_check(editor, self.project, self.paths, self.out)
        self.assert_fits(editor)
        self.assertGreater(len(editor.argvs), 1)
        given = [p for a in editor.argvs for p in a[a.index("-upePaths") + 1].split(";")]
        self.assertEqual(given, self.paths, "every path once, in order")
        self.assertEqual(run.paths, tuple(self.paths))
        self.assertEqual({p[0] for p in run.problems}, {self.paths[3], self.paths[650]}, "the union of the groups")
        self.assertEqual((run.scenes, run.prefabs), (10, 690))
        self.assertIsNone(FullScenes.of(run), "a split run is never a commit's full result")
        self.assertTrue((self.out / "upe-scene-check.json").exists() and (self.out / "upe-scene-check-2.json").exists())

    def test_a_missing_path_in_group_1_leaves_group_2_checked(self):
        # As one run, `SceneCheck.cs` ends its loop at the missing path: what is after it is never loaded. In groups,
        # the later groups still run; the exception row stops the land either way.
        from upe.checks import run_scene_check, scene_checks

        (self.project / self.paths[5]).unlink()
        editor = group_editor(self.project, [self.row(self.paths[680])])
        run = run_scene_check(editor, self.project, self.paths, self.out)
        self.assertEqual({(p[0], p[1]) for p in run.problems},
                         {("", "exception"), (self.paths[680], "missing-script")})
        report = scene_checks(group_editor(self.project), self.project, paths=self.paths, out_dir=self.out)
        self.assertFalse(report.ok)

    def test_a_group_that_leaves_no_report_makes_the_whole_unrun(self):
        # As one run with no report: "scene check did not run". So here, and no group after it starts.
        from upe.checks import run_scene_check, scene_checks

        editor = group_editor(self.project, no_report={2})
        run = run_scene_check(editor, self.project, self.paths, self.out)
        self.assert_fits(editor)
        self.assertIsNone(run.problems)
        self.assertEqual(len(editor.argvs), 2)
        first = group_editor(self.project, no_report={1})  # the 700 paths are two groups: the first fails
        self.assertIsNone(run_scene_check(first, self.project, self.paths, self.out).problems)
        self.assertEqual(len(first.argvs), 1, "no group after it is started")
        report = scene_checks(group_editor(self.project, no_report={2}), self.project, paths=self.paths,
                              out_dir=self.out)
        self.assertEqual([f.check for f in report.findings], ["scene-check"])
        self.assertIn("scene check did not run", report.findings[0].message)

    def test_a_group_with_no_report_is_named_and_what_the_groups_before_it_found_is_kept(self):
        # Red with the finding naming the group only by its log's file name, or with group 1's missing script not
        # shown.
        from upe.checks import scene_checks

        editor = group_editor(self.project, [self.row(self.paths[3]), self.row(self.paths[600])], no_report={2})
        report = scene_checks(editor, self.project, paths=self.paths, out_dir=self.out)
        self.assertIn("scene check did not run (group 2 of 2 of a check split", report.findings[0].message)
        self.assertEqual([(f.path, f.check) for f in report.findings[1:]], [(self.paths[3], "missing-script")])
        self.assertIn("the groups before group 2 of 2 found 1 scene problems", " ".join(report.notes))
        known = scene_checks(group_editor(self.project, [self.row(self.paths[3])], no_report={2}), self.project,
                             paths=self.paths, out_dir=self.out, baseline={tuple(self.row(self.paths[3]).values())})
        self.assertEqual([f.check for f in known.findings], ["scene-check"], "the target's own problem is not new")

    def test_a_licensing_failure_in_a_later_groups_log_is_read(self):
        from upe.checks import scene_checks

        editor = group_editor(self.project, no_report={2, 3}, log_text={2: LICENCE, 3: LICENCE})
        report = scene_checks(editor, self.project, paths=self.paths, out_dir=self.out)
        self.assertTrue(report.findings, "a group with no report stops the check")
        self.assertIn("Unity's licensing did not start, twice", report.findings[0].message)
        self.assertIn("upe-scene-check-2.log", report.findings[0].message, "the failing group's log")

    def test_an_editor_that_cannot_start_for_a_group_stops_the_check_naming_it(self):
        # An open risk, untestable without Unity: if the first group's Editor still held the project when the second
        # starts, the check stops with an error naming the group, never a pass.
        # With a stand-in for Unity: the lockfile is held by this test after group 1.
        import sys
        from contextlib import ExitStack

        from upe.checks import run_scene_check
        from upe.editor import EditorError

        if sys.platform != "win32":
            self.skipTest("Windows share modes")
        from test_workspace import held_exclusively

        lock = self.project / "Temp" / "UnityLockfile"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            editor = group_editor(self.project, between=lambda n: stack.enter_context(held_exclusively(lock))
                                  if n == 1 else None)
            with self.assertRaises(EditorError) as raised:
                run_scene_check(editor, self.project, self.paths, self.out)
        self.assertIn("scene check, group 2 of", str(raised.exception))
        self.assertIn("is open in an Editor", str(raised.exception))
        self.assertIn("Unity still held the project", str(raised.exception))  # the cause and the step
        again = run_scene_check(group_editor(self.project), self.project, self.paths, self.out)  # no Editor holds it
        self.assertEqual(again.problems, frozenset())

    def test_an_earlier_splits_group_reports_are_gone_before_a_run(self):
        # A group report of an earlier split is never there to be read.
        from upe.checks import run_scene_check

        self.out.mkdir(parents=True)
        stale = [self.out / "upe-scene-check-3.json", self.out / "upe-scene-check-3.log"]
        for path in stale:
            path.write_text("{}")
        run_scene_check(group_editor(self.project), self.project, self.paths[:3], self.out)
        self.assertEqual([p.exists() for p in stale], [False, False])

    def test_a_list_that_fits_is_the_one_run_of_before(self):
        from upe.checks import run_scene_check

        editor = group_editor(self.project)
        run = run_scene_check(editor, self.project, self.paths[:3], self.out)
        self.assertEqual(len(editor.argvs), 1)
        argv = editor.argvs[0]
        self.assertEqual(argv[argv.index("-upeOut"):], ["-upeOut", str(self.out / "upe-scene-check.json"),
                                                        "-upePaths", ";".join(self.paths[:3])])
        self.assertEqual(run.log, str(self.out / "upe-scene-check.log"))
        self.assertFalse(list(self.out.glob("upe-scene-check-*")))


class ClassGroups(unittest.TestCase):
    """The `--filter` class lists: the re-run of `confirm_failures` and the class-only run on the target."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = Path(self._tmp.name) / "P"
        self.p.mkdir()
        self.out = Path(self._tmp.name) / "r" / "upe-test-results.xml"
        self.out.parent.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def cli(self, calls, answer):
        """`unity test`: `answer(n, classes)` gives the n-th run's [(name, result)], or None for no report."""
        def run(argv, **kw):
            calls.append(argv)
            classes = argv[argv.index("--filter") + 1].split(";")
            cases = answer(len(calls), classes)
            if cases is not None:
                Path(argv[argv.index("--output") + 1]).write_text(
                    "<test-run>" + "".join(f'<test-case fullname="{escape_attr(n)}" classname="{n.split("(")[0].rsplit(".", 1)[0]}" '
                                           f'result="{r}"/>' for n, r in cases) + "</test-run>", encoding="utf-8")
            code = 8 if cases and any(r == "Failed" for _, r in cases) else 0 if cases is not None else 6
            return subprocess.CompletedProcess(argv, code, json.dumps({"success": code == 0, "errors": []}), "")

        return UnityCli("unity.exe", runner=run)

    def test_the_rerun_of_many_classes_is_in_groups_each_under_the_limit(self):
        # Red with one `--filter` of 1,500 classes, about 66,000 characters.
        from upe.checks import TestRun, confirm_failures

        names = [f"Game.Feature{i:04}.SomeLongTestClassName{i:04}.Case(\"x\")" for i in range(1500)]
        run = TestRun(8, "TESTS_FAILED", set(names), classes={n: n.split("(")[0].rsplit(".", 1)[0] for n in names})
        calls = []
        cli = self.cli(calls, lambda n, classes: [(f"{c}.Case(\"x\")", "Failed") for c in classes])
        confirmed, note = confirm_failures(cli, self.p, self.out, run)
        for argv in calls:
            self.assertLess(len(subprocess.list2cmdline(argv)), 32_767)
        self.assertGreater(len(calls), 1)
        self.assertEqual(sorted(c for a in calls for c in a[a.index("--filter") + 1].split(";")),
                         sorted(run.classes.values()), "every class once")
        self.assertEqual(confirmed.twice, set(names), "each failed again, in its own group")
        self.assertIn(f"{len(calls)} runs, one per group of classes", note)

    def test_one_group_is_the_call_of_before(self):
        from upe.checks import run_tests_by_classes

        calls = []
        run = run_tests_by_classes(self.cli(calls, lambda n, c: [("A.B.T", "Passed")]), self.p, self.out, 60,
                                   ["A.B", "C.D"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][calls[0].index("--filter"):calls[0].index("--filter") + 2], ["--filter", "A.B;C.D"])
        self.assertEqual(calls[0][calls[0].index("--output") + 1], str(self.out))
        self.assertEqual(run.passed, {"A.B.T"})

    def test_groups_are_read_as_one_run(self):
        from unittest import mock

        from upe import checks

        def answer(n, classes):
            return {1: [("A.T.x", "Failed"), ("X.Y.Both", "Failed")],
                    2: [("B.T.y", "Passed"), ("X.Y.Both", "Passed")],
                    3: [("C.T.z", "Passed")]}[n]

        calls = []
        with mock.patch.object(checks, "FILTER_CAP", 4):
            run = checks.run_tests_by_classes(self.cli(calls, answer), self.p, self.out, 60, ["A.T", "B.T", "C.T"])
        self.assertEqual([a[a.index("--output") + 1] for a in calls],
                         [str(self.out), str(self.out.with_name("upe-test-results.part-2.xml")),
                          str(self.out.with_name("upe-test-results.part-3.xml"))])
        self.assertEqual(run.failed, {"A.T.x", "X.Y.Both"}, "a name failed in one report is failed")
        self.assertEqual(run.passed, {"B.T.y", "C.T.z"})
        self.assertEqual(run.ran, {"A.T.x", "B.T.y", "C.T.z", "X.Y.Both"})
        self.assertEqual(run.exit_code, 8)

    def test_a_joined_run_keeps_the_exit_of_its_failing_group_its_classes_and_no_stale_part(self):
        # The exit code `unity_checks` reads for "no failure but a failing exit"; the class map; a part file of an earlier split is never left to be read.
        from unittest import mock

        from upe import checks

        stale = self.out.with_name("upe-test-results.part-4.xml")
        stale.write_text("<test-run/>")
        calls = []
        with mock.patch.object(checks, "FILTER_CAP", 4):
            run = checks.run_tests_by_classes(
                self.cli(calls, lambda n, c: [("A.T.x", "Passed")] if n == 1 else [("B.T.y", "Failed")]),
                self.p, self.out, 60, ["A.T", "B.T"])
        self.assertEqual(run.exit_code, 8, "group 2's failing exit, not group 1's 0")
        self.assertEqual(run.classes, {"A.T.x": "A.T", "B.T.y": "B.T"})
        self.assertFalse(stale.exists())

    def test_a_group_with_no_report_leaves_the_run_unfinished(self):
        from unittest import mock

        from upe import checks

        calls = []
        with mock.patch.object(checks, "FILTER_CAP", 4):
            run = checks.run_tests_by_classes(self.cli(calls, lambda n, c: None if n == 2 else [("A.T.x", "Passed")]),
                                              self.p, self.out, 60, ["A.T", "B.T", "C.T"])
        self.assertFalse(run.finished)
        self.assertEqual((run.exit_code, len(calls)), (6, 2), "its exit; no group after it")


def escape_attr(text: str) -> str:
    return text.replace("&", "&amp;").replace('"', "&quot;")


class FindingsOutsideBranchTests(unittest.TestCase):
    """`next` says "tell the user, do not fix" only when this is true. It must be false whenever a finding is in the
    branch's own work: each case below is one that could break it."""

    def outside(self, findings, paths):
        from upe.checks import Finding, findings_outside_branch

        return findings_outside_branch([Finding(p, c, "m") for p, c in findings], paths)

    def test_a_finding_in_the_branchs_work_is_the_branchs(self):
        changed = ["Assets/Features/Dash/Dash.cs", "Assets/Features/Dash/Dash.cs.meta"]
        cases = {
            "the changed path": ([("Assets/Features/Dash/Dash.cs", "meta")], changed),
            "the .meta of a changed asset": ([("Assets/Features/Dash/Dash.cs.meta", "meta")], changed[:1]),
            "the asset of a changed .meta": ([("Assets/Features/Dash/Dash.cs", "meta")], changed[1:]),
            "the .meta left by a deleted file": ([("Assets/X.cs.meta", "meta")], ["Assets/X.cs"]),
            "a folder the branch adds": ([("Assets/New", "meta")], ["Assets/New/A.cs"]),
            "a folder's .meta the branch adds": ([("Assets/New.meta", "meta")], ["Assets/New/A.cs"]),
            "a rename (a delete and an add)": ([("Assets/Old.cs.meta", "meta"), ("Assets/Renamed.cs", "meta")],
                                               ["Assets/Old.cs", "Assets/Renamed.cs"]),
            "a path in another case": ([("assets/features/dash/dash.cs", "meta")], changed[:1]),
            "a duplicate GUID with one holder the branch's": ([("Assets/A.cs.meta", "meta"), ("Assets/B.cs.meta", "meta")],
                                                              ["Assets/B.cs.meta"]),
            "a file inside a folder the branch deletes": ([("Assets/Gone/A.cs.meta", "meta")], ["Assets/Gone"]),
            "one of many": ([("Assets/Ghost/G.cs", "meta")] * 3 + [("Assets/Features/Dash/Dash.cs", "yaml")], changed),
            "a scene problem among them": ([("Assets/Ghost/G.cs", "meta"), ("Assets/Scenes/A.unity", "missing-script")],
                                           changed),
            "a failed test": ([("Game.Tests.MoveTests.Breaks", "unity-tests")], changed),
            "a scene check that did not run": ([("C:/work/slots/s1", "scene-check")], changed),
            "no finding": ([], changed),
        }
        for name, (findings, paths) in cases.items():
            with self.subTest(name):
                self.assertFalse(self.outside(findings, paths))

    def test_only_static_findings_at_other_paths_are_not_the_branchs(self):
        self.assertTrue(self.outside([(f"Assets/Ghost/G{i}.cs", "meta") for i in range(24)],
                                     ["Assets/Features/Dash/Dash.cs"]))
        # A prefix that is not a folder of it: `Assets/Features/DashBoard.cs` is not under `Assets/Features/Dash`.
        self.assertTrue(self.outside([("Assets/Features/DashBoard.cs", "meta")], ["Assets/Features/Dash"]))


if __name__ == "__main__":
    unittest.main()
