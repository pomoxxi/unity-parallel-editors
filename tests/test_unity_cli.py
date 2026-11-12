import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from upe.unity_cli import (
    MIN_VERSION,
    CliVersion,
    ExitCode,
    UnityCli,
    UnityCliError,
    find_cli,
)


class FakeRunner:
    """Stands in for subprocess.run: records calls and returns canned output."""

    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)

    @property
    def argv(self):
        return self.calls[-1][0]

    @property
    def kwargs(self):
        return self.calls[-1][1]


def envelope(success=True, data=None, errors=None):
    return json.dumps({"success": success, "command": "x", "data": data, "errors": errors or [], "warnings": []})


EXE = Path("C:/fake/unity.exe")
PROJECT = Path("C:/work/slots/s1")


class VersionTests(unittest.TestCase):
    def test_parse_beta(self):
        v = CliVersion.parse("1.0.0-beta.14")
        self.assertEqual((v.major, v.minor, v.patch, v.is_release, v.beta), (1, 0, 0, False, 14))
        self.assertEqual(str(v), "1.0.0-beta.14")

    def test_parse_inside_text(self):
        self.assertEqual(str(CliVersion.parse("unity 1.0.0-beta.13\n")), "1.0.0-beta.13")

    def test_ordering(self):
        order = ["1.0.0-beta.9", "1.0.0-beta.13", "1.0.0-beta.14", "1.0.0", "1.0.1-beta.1", "1.1.0"]
        parsed = [CliVersion.parse(t) for t in order]
        self.assertEqual(sorted(parsed), parsed)

    def test_no_version(self):
        with self.assertRaises(ValueError):
            CliVersion.parse("command not found")

    def test_check_version_rejects_beta13(self):
        cli = UnityCli(EXE, runner=FakeRunner("1.0.0-beta.13\n"))
        with self.assertRaises(UnityCliError):
            cli.check_version()

    def test_check_version_accepts_pin_and_later(self):
        for text in ("1.0.0-beta.14", "1.0.0-beta.15", "1.0.0"):
            self.assertGreaterEqual(UnityCli(EXE, runner=FakeRunner(text)).check_version(), MIN_VERSION)

    def test_version_uses_plain_output(self):
        runner = FakeRunner("1.0.0-beta.14")
        UnityCli(EXE, runner=runner).version()
        self.assertEqual(runner.argv, [str(EXE), "--version"])

    def test_version_failure(self):
        cli = UnityCli(EXE, runner=FakeRunner("", returncode=1, stderr="boom"))
        with self.assertRaises(UnityCliError):
            cli.version()


class TargetingTests(unittest.TestCase):
    def test_open_passes_path_positionally_with_flags(self):
        runner = FakeRunner(envelope(data={"alreadyOpen": False, "launchedPid": 42}))
        result = UnityCli(EXE, runner=runner).open(PROJECT)
        self.assertEqual(
            runner.argv,
            [str(EXE), "open", str(PROJECT), "--batchmode", "--until-ready", "--format", "json"],
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["launchedPid"], 42)

    def test_open_gui_with_upgrades(self):
        runner = FakeRunner(envelope())
        UnityCli(EXE, runner=runner).open(PROJECT, batchmode=False, until_ready=False, accept_upgrades=True)
        self.assertEqual(runner.argv, [str(EXE), "open", str(PROJECT), "--accept-upgrades", "--format", "json"])

    def test_status_uses_project_path_flag(self):
        runner = FakeRunner(envelope())
        UnityCli(EXE, runner=runner).status(PROJECT)
        self.assertEqual(runner.argv, [str(EXE), "status", "--project-path", str(PROJECT), "--format", "json"])

    def test_every_project_call_sets_cwd_and_env(self):
        runner = FakeRunner(envelope())
        cli = UnityCli(EXE, runner=runner)
        for call in (cli.open, cli.status, cli.close, cli.test):
            call(PROJECT)
            self.assertEqual(runner.kwargs["cwd"], str(PROJECT))
            self.assertEqual(runner.kwargs["env"]["UNITY_PROJECT_PATH"], str(PROJECT))
            self.assertEqual(runner.kwargs["env"]["UNITY_NON_INTERACTIVE"], "1")

    def test_machine_calls_drop_inherited_project_path(self):
        runner = FakeRunner(envelope())
        os.environ["UNITY_PROJECT_PATH"] = "C:/somewhere/else"
        try:
            UnityCli(EXE, runner=runner).doctor()
        finally:
            del os.environ["UNITY_PROJECT_PATH"]
        self.assertNotIn("UNITY_PROJECT_PATH", runner.kwargs["env"])
        self.assertIsNone(runner.kwargs["cwd"])

    def test_test_timeout_is_whole_seconds(self):
        runner = FakeRunner(envelope())
        UnityCli(EXE, runner=runner).test(PROJECT, cli_timeout_s=90.7)
        self.assertEqual(runner.argv[-4:], ["--timeout", "90", "--format", "json"])

    def test_test_waits_as_long_as_the_cli_may_take(self):
        # The wait used to be the 600 s default; a full EditMode run of a large project can take ~15 minutes.
        runner = FakeRunner(envelope())
        UnityCli(EXE, runner=runner).test(PROJECT, cli_timeout_s=1800)
        self.assertEqual(runner.kwargs["timeout"], 2100)

    def test_command_forwards_name_and_args(self):
        runner = FakeRunner(envelope())
        UnityCli(EXE, runner=runner).command(PROJECT, "editor.status", "--until-ready")
        self.assertEqual(runner.argv, [str(EXE), "command", "--project-path", str(PROJECT), "editor.status", "--until-ready", "--format", "json"])

    def test_close_and_test_take_the_path_positionally(self):
        runner = FakeRunner(envelope())
        cli = UnityCli(EXE, runner=runner)
        cli.close(PROJECT, force=True)
        self.assertEqual(runner.argv, [str(EXE), "close", str(PROJECT), "--force", "--format", "json"])
        cli.test(PROJECT, "--mode", "EditMode")
        self.assertEqual(runner.argv, [str(EXE), "test", str(PROJECT), "--mode", "EditMode", "--format", "json"])

    def test_pipeline_install_uses_project_path_flag(self):
        runner = FakeRunner(envelope())
        UnityCli(EXE, runner=runner).pipeline_install(PROJECT)
        self.assertEqual(runner.argv, [str(EXE), "pipeline", "install", "--project-path", str(PROJECT), "--format", "json"])

    def test_subprocess_timeout_defaults(self):
        runner = FakeRunner(envelope())
        UnityCli(EXE, runner=runner, default_timeout=5).doctor()
        self.assertEqual(runner.kwargs["timeout"], 5)


class ResultTests(unittest.TestCase):
    def test_error_code_from_envelope(self):
        out = envelope(success=False, errors=[{"code": "OPEN_BLOCKED_BY_DIALOG", "message": "..."}])
        result = UnityCli(EXE, runner=FakeRunner(out, returncode=6)).open(PROJECT)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "OPEN_BLOCKED_BY_DIALOG")
        self.assertTrue(result.retryable)

    def test_success_false_with_exit_zero_is_not_ok(self):
        # beta.14: status commands that report nothing return success:false.
        result = UnityCli(EXE, runner=FakeRunner(envelope(success=False))).auth_status()
        self.assertEqual(result.exit_code, ExitCode.OK)
        self.assertFalse(result.ok)

    def test_tests_failed_is_not_retryable(self):
        result = UnityCli(EXE, runner=FakeRunner(envelope(success=False), returncode=8)).test(PROJECT)
        self.assertFalse(result.retryable)

    def test_ndjson_takes_last_frame(self):
        out = '{"type":"progress","pct":10}\n{"type":"progress","pct":90}\n' + envelope(data={"done": True})
        result = UnityCli(EXE, runner=FakeRunner(out)).doctor()
        self.assertEqual(result.data, {"done": True})

    def test_non_json_output(self):
        result = UnityCli(EXE, runner=FakeRunner("not json at all")).doctor()
        self.assertIsNone(result.envelope)
        self.assertTrue(result.ok)
        self.assertIsNone(result.error_code)


class FindCliTests(unittest.TestCase):
    def test_override_wins(self):
        self.assertEqual(find_cli({"UPE_UNITY_CLI": "X:/u.exe"}), Path("X:/u.exe"))

    def test_looks_nowhere_the_user_has_not_named(self):
        # PATH and %LOCALAPPDATA% are not searched, even when a unity.exe is there.
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "Unity" / "bin" / "unity.exe"
            installed.parent.mkdir(parents=True)
            installed.write_text("")
            with mock.patch.object(shutil, "which", return_value=str(installed)):
                with self.assertRaises(UnityCliError) as ctx:
                    find_cli({"LOCALAPPDATA": tmp, "PATH": str(installed.parent)})
        self.assertIn("unity_cli in upe.toml", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
