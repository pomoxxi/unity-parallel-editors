import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from upe import agent_package
from upe.agent_package import PACKAGE_NAME, PIPELINE, install, local_edits_hidden, scrub_lock, uninstall
from upe.unity_cli import UnityCli

MANIFEST = {"dependencies": {"com.unity.render-pipelines.core": "17.3.0"}}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


class InstallTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.repo = tmp / "game"
        (self.repo / "Packages").mkdir(parents=True)
        (self.repo / "Packages" / "manifest.json").write_text(json.dumps(MANIFEST, indent=2) + "\n")
        (self.repo / "Packages" / "packages-lock.json").write_text('{"dependencies": {}}\n')
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@e.com")
        git(self.repo, "config", "user.name", "t")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.wt = tmp / "wt"
        git(self.repo, "worktree", "add", "-q", "-b", "upe/t", str(self.wt))

    def tearDown(self):
        self._tmp.cleanup()

    def fake_pipeline_cli(self):
        def run(argv, **kw):
            manifest = Path(kw["cwd"]) / "Packages" / "manifest.json"
            data = json.loads(manifest.read_text())
            data["dependencies"][PIPELINE] = "0.9.0"
            manifest.write_text(json.dumps(data, indent=2))
            return subprocess.CompletedProcess(argv, 0, '{"success": true}', "")

        return UnityCli("unity.exe", runner=run)

    def test_install_leaves_git_clean(self):
        install(self.wt, cli=self.fake_pipeline_cli())
        self.assertTrue((self.wt / "Packages" / PACKAGE_NAME / "Editor" / "SceneCheck.cs").exists())
        self.assertIn(PIPELINE, (self.wt / "Packages" / "manifest.json").read_text())
        (self.wt / "Packages" / "packages-lock.json").write_text('{"dependencies": {"x": {}}}\n')  # Unity rewrites it
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")
        self.assertEqual(sorted(local_edits_hidden(self.wt)), ["Packages/manifest.json", "Packages/packages-lock.json"])

    def test_commit_after_install_does_not_carry_tool_files(self):
        install(self.wt, cli=self.fake_pipeline_cli())
        (self.wt / "Assets").mkdir()
        (self.wt / "Assets" / "Dash.cs").write_text("class Dash {}\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", "work")
        files = git(self.wt, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(files, ["Assets/Dash.cs"])
        self.assertNotIn(PIPELINE, git(self.wt, "show", "HEAD:Packages/manifest.json"))

    def test_ide_files_unity_writes_stay_out_of_commits(self):
        (self.repo / ".vscode").mkdir()
        (self.repo / ".vscode" / "settings.json").write_text('{"dotnet.defaultSolution": "game.slnx"}\n')
        (self.repo / "game.slnx").write_text("<Solution />\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "ide")
        git(self.wt, "merge", "-q", "main")
        install(self.wt)
        # What Unity's IDE integration writes when it opens the worktree.
        (self.wt / ".vscode" / "settings.json").write_text('{"dotnet.defaultSolution": "wt.slnx"}\n')
        (self.wt / "wt.slnx").write_text("<Solution />\n")
        self.assertEqual(git(self.wt, "status", "--porcelain"), "")
        (self.wt / "game.slnx").write_text("<Solution changed />\n")  # the game's own solution is still tracked
        self.assertEqual(git(self.wt, "status", "--porcelain").strip(), "M game.slnx")

    def test_install_twice_and_exclude_once(self):
        install(self.wt)
        install(self.wt)
        exclude = agent_package.exclude_file(self.wt).read_text()
        self.assertEqual(exclude.count(agent_package.EXCLUDE_LINE), 1)
        self.assertEqual(exclude.count("/wt.slnx"), 1)

    def test_uninstall_restores_committed_files(self):
        install(self.wt, cli=self.fake_pipeline_cli())
        uninstall(self.wt)
        self.assertFalse((self.wt / "Packages" / PACKAGE_NAME).exists())
        self.assertEqual(json.loads((self.wt / "Packages" / "manifest.json").read_text()), MANIFEST)
        self.assertEqual(local_edits_hidden(self.wt), [])

    def test_missing_source(self):
        with self.assertRaises(agent_package.AgentPackageError):
            install(self.wt, source=Path(self._tmp.name) / "nope")


class StripTests(unittest.TestCase):
    def test_strips_tool_packages_unless_the_target_has_them(self):
        merged = json.dumps({"dependencies": {"com.a": "1", PIPELINE: "0.9.0"}})
        self.assertEqual(json.loads(agent_package.strip_tool_packages(merged, json.dumps(MANIFEST))),
                         {"dependencies": {"com.a": "1"}})
        self.assertIsNone(agent_package.strip_tool_packages(merged, json.dumps({"dependencies": {PIPELINE: "0.9.0"}})))
        self.assertIsNone(agent_package.strip_tool_packages(json.dumps(MANIFEST), json.dumps(MANIFEST)))


class ScrubTests(unittest.TestCase):
    def test_removes_tool_packages_and_what_only_they_need(self):
        lock = {"dependencies": {
            "com.unity.render-pipelines.core": {"version": "17.3.0", "depth": 0, "source": "builtin",
                                                "dependencies": {"com.unity.mathematics": "1.3.2"}},
            "com.unity.mathematics": {"version": "1.3.2", "depth": 1, "source": "registry", "dependencies": {}},
            "com.jbooth.microsplat.core": {"version": "file:x", "depth": 0, "source": "embedded", "dependencies": {"com.unity.shared": "1"}},
            "com.unity.shared": {"version": "1", "depth": 1, "source": "registry", "dependencies": {}},
            PIPELINE: {"version": "0.9.0", "depth": 0, "source": "registry", "dependencies": {"com.unity.nuget.newtonsoft-json": "3.2.1", "com.unity.mathematics": "1.3.2"}},
            "com.unity.nuget.newtonsoft-json": {"version": "3.2.1", "depth": 1, "source": "registry", "dependencies": {}},
            PACKAGE_NAME: {"version": "file:com.upe.agent", "depth": 0, "source": "embedded", "dependencies": {}},
        }}
        out = json.loads(scrub_lock(json.dumps(lock), json.dumps(MANIFEST)))
        self.assertEqual(
            sorted(out["dependencies"]),
            ["com.jbooth.microsplat.core", "com.unity.mathematics", "com.unity.render-pipelines.core", "com.unity.shared"],
        )

    def test_keeps_pipeline_if_the_game_uses_it(self):
        manifest = {"dependencies": {PIPELINE: "0.9.0"}}
        lock = {"dependencies": {PIPELINE: {"version": "0.9.0", "source": "registry", "dependencies": {}}}}
        self.assertIn(PIPELINE, json.loads(scrub_lock(json.dumps(lock), json.dumps(manifest)))["dependencies"])


if __name__ == "__main__":
    unittest.main()
