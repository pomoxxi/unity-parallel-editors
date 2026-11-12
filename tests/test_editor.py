import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from upe.checks import scene_checks, scene_problems
from upe.editor import Editor, EditorError, compile_errors, find_editor, project_version, yaml_merge_for
from upe.workspace import LOCKFILE


def project(root: Path) -> Path:
    (root / "ProjectSettings").mkdir(parents=True)
    (root / "ProjectSettings" / "ProjectVersion.txt").write_text(
        "m_EditorVersion: 6000.3.0f1\nm_EditorVersionWithRevision: 6000.3.0f1 (d1870ce95baf)\n"
    )
    return root


class Recorder:
    def __init__(self, code=0, side_effect=None):
        self.code, self.side_effect, self.argv = code, side_effect, None

    def __call__(self, argv, **kw):
        self.argv = argv
        if self.side_effect:
            self.side_effect(argv)
        return subprocess.CompletedProcess(argv, self.code, "", "")


class EditorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = project(Path(self._tmp.name) / "wt")

    def tearDown(self):
        self._tmp.cleanup()

    def test_version_and_override(self):
        self.assertEqual(project_version(self.p), "6000.3.0f1")
        self.assertEqual(find_editor(self.p, {"UPE_UNITY_EDITOR": "X:/U.exe"}), Path("X:/U.exe"))

    def test_no_editor_named_is_an_error_not_a_search(self):
        with self.assertRaises(EditorError) as ctx:
            find_editor(self.p, {})
        self.assertIn("needs Unity 6000.3.0f1", str(ctx.exception))
        self.assertIn("unity_editor in upe.toml", str(ctx.exception))

    def test_yaml_merge_ships_beside_the_editor(self):
        self.assertEqual(yaml_merge_for(Path("X:/Unity/6000.4.1f1/Editor/Unity.exe")),
                         Path("X:/Unity/6000.4.1f1/Editor/Data/Tools/UnityYAMLMerge.exe"))

    def test_batch_argv(self):
        rec = Recorder()
        Editor("U.exe", runner=rec).refresh(self.p, self.p / "Logs" / "r.log")
        self.assertEqual(rec.argv, [
            "U.exe", "-batchmode", "-nographics", "-accept-apiupdate", "-projectPath", str(self.p),
            "-logFile", str(self.p / "Logs" / "r.log"), "-job-worker-count", "4", "-quit",
        ])

    def test_execute_method_does_not_quit(self):
        rec = Recorder()
        Editor("U.exe", runner=rec, job_workers=None).execute_method(self.p, "A.B.C", self.p / "l.log", ["-x", "1"])
        self.assertEqual(rec.argv[-4:], ["-executeMethod", "A.B.C", "-x", "1"])
        self.assertNotIn("-quit", rec.argv)

    def test_graphics_keeps_the_gpu_device(self):
        rec = Recorder()
        Editor("U.exe", runner=rec).execute_method(self.p, "A.B.C", self.p / "l.log", graphics=True)
        self.assertIn("-batchmode", rec.argv)
        self.assertNotIn("-nographics", rec.argv)

    def test_stale_lock_does_not_block(self):
        (self.p / "Temp").mkdir()
        (self.p / LOCKFILE).write_text("")
        Editor("U.exe", runner=Recorder()).refresh(self.p, self.p / "l.log")

    def test_compile_errors(self):
        log = "x\nAssets/A.cs(3,5): error CS0103: The name 'y' does not exist\nwarning CS0168\nAssets/A.cs(3,5): error CS0103: The name 'y' does not exist\n"
        self.assertEqual(compile_errors(log), ["Assets/A.cs(3,5): error CS0103: The name 'y' does not exist"])


class SceneCheckTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p = project(Path(self._tmp.name) / "wt")

    def tearDown(self):
        self._tmp.cleanup()

    def editor_writing(self, problems, scenes=2, prefabs=3, code=1):
        def write(argv):
            out = Path(argv[argv.index("-upeOut") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({"scenes": scenes, "prefabs": prefabs, "problems": problems}))

        return Editor("U.exe", runner=Recorder(code, write))

    def test_new_problems_are_findings_old_ones_are_notes(self):
        old = {"path": "Assets/A.unity", "kind": "missing-script", "where": "Root/Old", "detail": ""}
        new = {"path": "Assets/A.unity", "kind": "missing-reference", "where": "Root/Dash", "detail": "Dash.target"}
        baseline = scene_problems(json.dumps({"problems": [old]}))
        report = scene_checks(self.editor_writing([old, new]), self.p, baseline=baseline)
        self.assertEqual([(f.path, f.check, f.message) for f in report.findings],
                         [("Assets/A.unity", "missing-reference", "Root/Dash Dash.target")])
        self.assertIn("1 scene problems were already on the target before the merge", report.notes)
        self.assertIn("scene check loaded 2 scenes and 3 prefabs", report.notes)

    def licensing_fails(self, times):
        runs = []

        def write(argv):
            runs.append(argv)
            log = Path(argv[argv.index("-logFile") + 1])
            log.parent.mkdir(parents=True, exist_ok=True)
            if len(runs) <= times:
                log.write_text("[Licensing::Module] Error: Licensing initialization failed after 75.80s\nA.cs(1,1): error CS0234: no\n")
            else:
                log.write_text("fine\n")
                Path(argv[argv.index("-upeOut") + 1]).write_text(json.dumps({"scenes": 1, "prefabs": 0, "problems": []}))

        return Editor("U.exe", runner=Recorder(1, write)), runs

    def test_a_licensing_failure_is_tried_again(self):
        editor, runs = self.licensing_fails(1)
        report = scene_checks(editor, self.p)
        self.assertEqual(len(runs), 2)
        self.assertTrue(report.ok, report.findings)

    def test_a_licensing_failure_that_stays_is_not_blamed_on_the_code(self):
        editor, runs = self.licensing_fails(5)
        report = scene_checks(editor, self.p)
        self.assertEqual(len(runs), 2)
        self.assertIn("Not a finding about the code", report.findings[0].message)
        self.assertNotIn("CS0234", report.findings[0].message)

    def test_a_compile_error_is_not_tried_again(self):
        runs = []

        def write(argv):
            runs.append(argv)
            log = Path(argv[argv.index("-logFile") + 1])
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("A.cs(1,1): error CS0234: no\n")

        report = scene_checks(Editor("U.exe", runner=Recorder(1, write)), self.p)
        self.assertEqual(len(runs), 1)
        self.assertIn("CS0234", report.findings[0].message)

    def test_paths_are_passed_and_filtered(self):
        editor = self.editor_writing([], code=0)
        scene_checks(editor, self.p, paths=["Assets/A.unity", "Assets/B.cs", "Assets/C.prefab"])
        argv = editor._runner.argv
        self.assertEqual(argv[argv.index("-upePaths") + 1], "Assets/A.unity;Assets/C.prefab")

    def test_no_report_names_compile_errors(self):
        def log_only(argv):
            Path(argv[argv.index("-logFile") + 1]).write_text("Assets/X.cs(1,1): error CS1002: ; expected\n")

        report = scene_checks(Editor("U.exe", runner=Recorder(1, log_only)), self.p)
        self.assertEqual(report.findings[0].check, "scene-check")
        self.assertIn("error CS1002", report.findings[0].message)

    def test_stale_report_is_not_reused(self):
        (self.p / "Logs").mkdir()
        (self.p / "Logs" / "upe-scene-check.json").write_text('{"problems": []}')
        report = scene_checks(Editor("U.exe", runner=Recorder(2)), self.p)
        self.assertFalse(report.ok)


if __name__ == "__main__":
    unittest.main()
