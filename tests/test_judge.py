import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from upe import judge
from upe.judge import (
    PRIORITY_CLASS, Judge, JudgeError, Machine, Proc, activities, apply, declare, editor_trees, list_processes,
    project_of, snapshot,
)

WT = Path("C:/work/slots")


def unity(pid, project, parent=1, rss=4 * 2**30, cpu=0.0, extra=""):
    return Proc(pid, parent, "Unity.exe", f'"C:/U/Unity.exe" -projectPath "{project}" {extra}', rss, cpu)


class ProcessTests(unittest.TestCase):
    def test_project_of(self):
        self.assertEqual(project_of('Unity.exe -batchmode -projectPath "D:/a b/c" -quit'), Path("D:/a b/c"))
        self.assertEqual(project_of("Unity.exe -projectpath D:/x -logFile l"), Path("D:/x"))
        self.assertIsNone(project_of("Unity.exe -name AssetImportWorker0"))

    def test_children_count_with_their_editor(self):
        procs = [
            unity(10, WT / "dash"),
            Proc(11, 10, "Unity.exe", "Unity.exe -name AssetImportWorker0", 2**30, 0),
            Proc(12, 11, "UnityShaderCompiler.exe", "UnityShaderCompiler.exe", 2**28, 0),
            Proc(13, 1, "notepad.exe", "notepad", 1, 0),
        ]
        trees = editor_trees(procs)
        self.assertEqual(len(trees), 1)
        self.assertEqual(sorted(p.pid for p in next(iter(trees.values()))), [10, 11, 12])

    def test_list_processes_parses_cim_json(self):
        row = {"ProcessId": 5, "ParentProcessId": 1, "Name": "Unity.exe", "CommandLine": "x",
               "WorkingSetSize": 2048, "KernelModeTime": 10_000_000, "UserModeTime": 20_000_000}

        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, json.dumps(row), "")

        self.assertEqual(list_processes(run), [Proc(5, 1, "Unity.exe", "x", 2048, 3.0)])


class SnapshotTests(unittest.TestCase):
    def test_sessions_cpu_memory_and_other_editors(self):
        samples = iter([
            [unity(10, WT / "dash", cpu=100.0), unity(20, WT / "jump", cpu=50.0), unity(30, "C:/Mine/Game", cpu=0)],
            [unity(10, WT / "dash", cpu=104.0), unity(20, WT / "jump", cpu=50.5), unity(30, "C:/Mine/Game", cpu=9)],
        ])
        known = {"dash": WT / "dash", "jump": WT / "jump", "golden": WT / "golden"}
        declared = {"dash": {"activity": "rendering", "note": "bake", "since": "t"}}
        snap, pids = snapshot(known, declared, lambda: next(samples), lambda: Machine(16, 64 * 2**30, 20 * 2**30),
                              interval_s=2.0, sleep=lambda s: None)
        by = {s.session: s for s in snap.sessions}
        self.assertEqual(sorted(by), ["dash", "jump"])
        self.assertEqual(by["dash"].cpu_percent, 200.0)
        self.assertEqual(by["dash"].activity, "rendering")
        self.assertEqual(by["jump"].activity, "editing")  # nothing declared
        self.assertEqual(by["dash"].rss_mb, 4096)
        self.assertEqual(len(snap.other_editors), 1)
        self.assertNotIn(30, [pid for p in pids.values() for pid in p])
        self.assertEqual(snap.free_ram_mb, 20 * 1024)


class DecisionTests(unittest.TestCase):
    def snap(self, *sessions, free_mb=20000):
        rows = [judge.Session(n, str(WT / n), act, "", since, [i], 1, 0.0) for i, (n, act, since) in enumerate(sessions)]
        return judge.Snapshot("t", 16, 65536, free_mb, rows)

    def test_heavy_is_high_and_the_rest_give_way(self):
        decision = Judge().decide(self.snap(("dash", "playmode", "t1"), ("jump", "editing", ""), ("bake", "testing", "")))
        self.assertEqual(decision.priorities, {"dash": "high", "jump": "normal", "bake": "low"})

    def test_at_most_max_high_and_the_first_to_declare_wins(self):
        decision = Judge(max_high=1).decide(self.snap(("late", "rendering", "t2"), ("early", "building", "t1")))
        self.assertEqual(decision.priorities, {"late": "normal", "early": "high"})
        two = Judge(max_high=2).decide(self.snap(("late", "rendering", "t2"), ("early", "building", "t1")))
        self.assertEqual(set(two.priorities.values()), {"high"})

    def test_nothing_heavy_is_all_normal_unless_memory_is_short(self):
        rows = (("dash", "idle", ""), ("jump", "editing", ""))
        self.assertEqual(Judge().decide(self.snap(*rows)).priorities, {"dash": "normal", "jump": "normal"})
        short = Judge(memory_short_mb=4096).decide(self.snap(*rows, free_mb=3000))
        self.assertEqual(short.priorities, {"dash": "low", "jump": "normal"})

    def test_no_sessions(self):
        self.assertEqual(Judge().decide(self.snap()).priorities, {})


class ApplyTests(unittest.TestCase):
    def test_sets_every_process_of_a_session(self):
        calls = []

        def setter(pid, cls):
            if pid == 3:
                raise JudgeError("process 3 exited")
            calls.append((pid, cls))

        decision = judge.Decision({"dash": "high", "jump": "low"}, {})
        failures = apply(decision, {"dash": [1, 2], "jump": [3, 4]}, setter)
        self.assertEqual(calls, [(1, PRIORITY_CLASS["high"]), (2, PRIORITY_CLASS["high"]), (4, PRIORITY_CLASS["low"])])
        self.assertEqual(failures, ["process 3 exited"])
        self.assertNotIn(0x80, PRIORITY_CLASS.values())  # HIGH_PRIORITY_CLASS is never used

    def test_classes_step_from_the_base(self):
        self.assertEqual(judge.priority_classes("normal"),
                         {"high": judge.ABOVE_NORMAL, "normal": judge.NORMAL, "low": judge.BELOW_NORMAL})
        self.assertEqual(judge.priority_classes("below_normal"),
                         {"high": judge.NORMAL, "normal": judge.BELOW_NORMAL, "low": judge.IDLE})


class ActivityTests(unittest.TestCase):
    def test_declare_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            declare(Path(tmp), "dash", "rendering", "lighting bake")
            self.assertEqual(activities(Path(tmp))["dash"]["activity"], "rendering")
            with self.assertRaises(JudgeError):
                declare(Path(tmp), "dash", "dancing")


if __name__ == "__main__":
    unittest.main()
