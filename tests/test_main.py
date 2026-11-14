import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from upe import __main__ as cli
from upe.flow import Outcome


class MainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Resolved, as Config.load resolves the paths it reads: %TEMP% may be an 8.3 short path.
        self.tmp = tmp = Path(self._tmp.name).resolve()
        self.config = tmp / "tool" / "upe.toml"
        self.config.parent.mkdir()
        self.config.write_text(f'repo = "{(tmp / "game").as_posix()}"\nroot = "{(tmp / "work").as_posix()}"\n'
                               f'golden = "{(tmp / "golden").as_posix()}"\n', encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--config", str(self.config), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_status_as_json(self):
        code, out, _ = self.main("--json", "status")
        self.assertEqual((code, json.loads(out)), (0, {"tasks": []}))

    def test_status_of_one_task(self):
        tasks = self.tmp / "work" / ".upe" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "dash.json").write_text(json.dumps({"task_id": "dash", "brief": "b", "claim": ["Assets/A/"], "base": "x", "target": "t",
                                                     "state": "conflict", "history": ["first", "second"]}))
        code, out, _ = self.main("--json", "status", "dash")
        answer = json.loads(out)
        self.assertEqual((code, answer["state"], answer["history"]), (0, "conflict", ["first", "second"]))
        self.assertTrue(answer["run_folder"].endswith("dash"))

    def test_a_task_being_started_shows_as_starting(self):
        # A second agent saw `start` say "already holds a claim" and `status` say "no task",
        # because `start` claims first and writes the record only once the slot is made.
        from upe.claims import ClaimRegistry

        state = self.tmp / "work" / ".upe"
        state.mkdir(parents=True)
        ClaimRegistry(state / "claims.json").claim("quiet", ["Assets/A.unity"])
        code, out, _ = self.main("--json", "status", "quiet")
        self.assertEqual((code, json.loads(out)["state"]), (0, "starting"))
        code, out, _ = self.main("--json", "status")
        self.assertEqual([(t["task"], t["state"]) for t in json.loads(out)["tasks"]], [("quiet", "starting")])

    def test_status_lists_a_broken_record_and_shows_times(self):
        tasks = self.tmp / "work" / ".upe" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "dash.json").write_text(json.dumps({"task_id": "dash", "brief": "b", "claim": [], "base": "x", "target": "t",
                                                     "state": "working", "history": ["old", "new"], "stamps": ["2026-11-07T10:00:00+00:00"]}))
        (tasks / "bad.json").write_text("{not json")
        code, out, _ = self.main("--json", "status")
        self.assertEqual([(t["task"], t["state"]) for t in json.loads(out)["tasks"]], [("dash", "working"), ("bad", "unreadable")])
        code, out, _ = self.main("status", "dash")
        self.assertIn("\nold\n2026-11-07T10:00:00+00:00 new", out)  # the entry from before times were kept has none

    def record(self, task_id="dash", **fields):
        tasks = self.tmp / "work" / ".upe" / "tasks"
        tasks.mkdir(parents=True, exist_ok=True)
        data = {"task_id": task_id, "brief": "b", "claim": [], "base": "x", "target": "t", "state": "working",
                "history": ["old", "new"], **fields}
        (tasks / f"{task_id}.json").write_text(json.dumps(data))

    def test_since_is_the_last_time_when_older_entries_have_none(self):
        # A record from before times were kept showed no `since` at all.
        self.record(stamps=["2026-11-07T10:00:00+00:00"])
        code, out, _ = self.main("--json", "status")
        self.assertEqual(json.loads(out)["tasks"][0]["since"], "2026-11-07T10:00:00+00:00")

    def test_a_record_with_odd_fields_is_unreadable_not_a_crash(self):
        # Each of these crashed `status` or `status <task>`.
        for odd in ({"stamps": 5}, {"stamps": None}, {"stamps": {"a": 1}}, {"stamps": [None, None]}, {"stamps": [{}, {}]},
                    {"stamps": [1, 2]}, {"stamps": ["t1", "t2", "t3"]}, {"history": 5}, {"history": None}, {"state": None}):
            with self.subTest(odd=odd):
                self.record(**odd)
                code, out, _ = self.main("--json", "status")
                self.assertEqual((code, [(t["task"], t["state"]) for t in json.loads(out)["tasks"]]), (0, [("dash", "unreadable")]))
                code, out, _ = self.main("status")
                self.assertEqual(code, 0)
                code, out, err = self.main("--json", "status", "dash")
                self.assertEqual((code, out), (2, ""))
                self.assertIn("its record does not read", json.loads(err)["error"])

    def test_status_of_a_broken_record_is_an_error_not_a_traceback(self):
        tasks = self.tmp / "work" / ".upe" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "bad.json").write_text("{not json")
        code, out, err = self.main("status", "bad")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("upe: task bad: its record does not read", err)

    def test_status_of_a_task_with_a_record_does_not_read_the_claims(self):
        # A broken or locked claims.json stopped `status <task>`.
        from upe.claims import ClaimError, file_lock

        self.record()
        claims = self.tmp / "work" / ".upe" / "claims.json"
        claims.write_text("{not json")
        code, out, _ = self.main("--json", "status", "dash")
        self.assertEqual((code, json.loads(out)["state"]), (0, "working"))
        with file_lock(claims.with_name("claims.json.lock"), ClaimError):  # another process changes the claims
            code, out, _ = self.main("--json", "status", "dash")
        self.assertEqual((code, json.loads(out)["state"]), (0, "working"))

    def test_the_list_says_when_the_claims_do_not_read(self):
        from upe.claims import ClaimError, file_lock

        self.record()
        claims = self.tmp / "work" / ".upe" / "claims.json"
        claims.write_text("{not json")
        code, out, _ = self.main("--json", "status")
        answer = json.loads(out)
        self.assertEqual((code, [(t["task"], t["state"]) for t in answer["tasks"]]), (0, [("dash", "working")]))
        self.assertIn("the claims could not be read", answer["claims_error"])
        code, out, _ = self.main("status")
        self.assertIn("the claims could not be read", out)
        claims.unlink()
        with mock.patch.object(cli, "CLAIMS_WAIT", 0.2), file_lock(claims.with_name("claims.json.lock"), ClaimError):
            code, out, _ = self.main("--json", "status")
        answer = json.loads(out)
        self.assertEqual((code, [(t["task"], t["state"]) for t in answer["tasks"]]), (0, [("dash", "working")]))
        self.assertIn("the claims could not be read", answer["claims_error"])

    def test_a_record_written_between_the_two_reads_is_listed_once(self):
        # `start` writing its record between the reads of the claims and of
        # the records hid the task. Whichever is read first, the record appears right after it.
        from upe.claims import ClaimRegistry
        from upe.flow import Flow

        state = self.tmp / "work" / ".upe"
        state.mkdir(parents=True)
        ClaimRegistry(state / "claims.json").claim("quiet", ["Assets/A.unity"])
        reads = []

        def then_the_record(original):
            def read(flow, *args, **kwargs):
                out = original(flow, *args, **kwargs)
                reads.append(original)
                if len(reads) == 1:
                    self.record("quiet")
                return out
            return read

        with mock.patch.object(Flow, "_task_records", then_the_record(Flow._task_records)), \
                mock.patch.object(Flow, "starting", then_the_record(Flow.starting)):
            code, out, _ = self.main("--json", "status")
        self.assertEqual((code, [(t["task"], t["state"]) for t in json.loads(out)["tasks"]]), (0, [("quiet", "working")]))

    def test_json_may_come_after_the_subcommand(self):
        code, out, _ = self.main("status", "--json")
        self.assertEqual((code, json.loads(out)), (0, {"tasks": []}))

    def test_errors_as_json(self):
        code, _, err = self.main("--json", "verdict", "../claims", "--member", "m", "--file", str(self.config))
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(err))

    def test_asking_about_a_job_does_not_change_it(self):
        jobs = self.tmp / "work" / ".upe" / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "land-dash-20261028T120000.json").write_text(json.dumps({"job": "x", "state": "running", "log": "nope"}))
        code, out, _ = self.main("--json", "job", "land-dash-20261028T120000")
        self.assertEqual((code, json.loads(out)["state"]), (0, "running"))
        record = json.loads((jobs / "land-dash-20261028T120000.json").read_text())
        self.assertEqual(record["state"], "running")
        self.assertNotIn("exit_code", record)

    def test_a_verdict_file_with_a_bom_is_read(self):
        verdict = self.tmp / "v.json"
        verdict.write_bytes(b"\xef\xbb\xbf" + b'{"commit": "x", "verdict": "GO"}')
        answer = Outcome("dash", "awaiting-council", "recorded")
        with mock.patch.object(cli.Flow, "verdict", return_value=answer) as recorded:
            code, _, _ = self.main("--json", "verdict", "dash", "--member", "m", "--file", str(verdict))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(recorded.call_args.args[2])["verdict"], "GO")  # json.loads refuses a BOM

    def test_job_wait_returns_when_the_job_is_done(self):
        import threading

        jobs = self.tmp / "work" / ".upe" / "jobs"
        jobs.mkdir(parents=True)
        path = jobs / "land-dash-20261028T120000.json"
        path.write_text(json.dumps({"job": "x", "state": "running", "log": "nope"}))
        threading.Timer(0.5, lambda: path.write_text(json.dumps({"job": "x", "state": "done", "exit_code": 0, "log": "nope"}))).start()
        code, out, _ = self.main("--json", "job", "land-dash-20261028T120000", "--wait", "20")
        self.assertEqual(json.loads(out)["state"], "done")
        code, out, _ = self.main("--json", "job", "land-dash-20261028T120000", "--wait", "0")
        self.assertEqual(code, 0)

    def test_a_job_whose_process_is_gone_is_done(self):
        jobs = self.tmp / "work" / ".upe" / "jobs"
        jobs.mkdir(parents=True)
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        path = jobs / "land-dash-20261028T120000.json"
        path.write_text(json.dumps({"job": "x", "state": "running", "pid": gone.pid, "log": "nope"}))
        code, out, _ = self.main("--json", "job", "land-dash-20261028T120000", "--wait", "30")
        answer = json.loads(out)
        self.assertEqual((answer["state"], answer["exit_code"]), ("done", 3))
        self.assertEqual(json.loads(path.read_text())["state"], "running")  # asking changes nothing

    def test_a_job_that_ended_first_keeps_its_result(self):
        flow = cli.Flow(cli.Config.load(self.config))
        cli._job_update(flow, "land-dash-20261028T120000", state="done", exit_code=0)
        cli._job_update(flow, "land-dash-20261028T120000", state="started", pid=1)  # the parent, late
        self.assertEqual(cli._job_read(flow, "land-dash-20261028T120000")["state"], "done")

    def test_a_running_job_stays_running(self):
        jobs = self.tmp / "work" / ".upe" / "jobs"
        jobs.mkdir(parents=True)
        path = jobs / "land-dash-20261028T120000.json"
        path.write_text(json.dumps({"job": "x", "state": "running", "pid": os.getpid(), "log": "nope"}))
        _, out, _ = self.main("--json", "job", "land-dash-20261028T120000")
        self.assertEqual(json.loads(out)["state"], "running")

    @unittest.skipUnless(sys.platform == "win32", "Windows gives a dead process's id to new processes")
    def test_a_job_whose_pid_a_new_process_has_is_done(self):
        # A killed land's pid, given to another program, read as the land still running: `job --wait` waited its
        # whole bound, and `status` told the agent to wait on it.
        from upe import flow as flowmod

        jobs = self.tmp / "work" / ".upe" / "jobs"
        jobs.mkdir(parents=True)
        mine = flowmod.process_created(os.getpid())
        self.assertIsInstance(mine, int)
        path = jobs / "land-dash-20261028T120000.json"
        text = json.dumps({"job": "x", "state": "running", "pid": os.getpid(), "created": mine - 1, "log": "nope"})
        path.write_text(text)
        _, out, _ = self.main("--json", "job", "land-dash-20261028T120000", "--wait", "30")
        answer = json.loads(out)
        self.assertEqual((answer["state"], answer["exit_code"]), ("done", 3))
        self.assertEqual(path.read_text(), text)  # asking changes nothing
        path.write_text(json.dumps({"job": "x", "state": "running", "pid": os.getpid(), "created": mine, "log": "nope"}))
        _, out, _ = self.main("--json", "job", "land-dash-20261028T120000")
        self.assertEqual(json.loads(out)["state"], "running")

    @unittest.skipUnless(sys.platform == "win32", "a process's start time is read on Windows only")
    def test_every_pid_a_job_record_gets_has_its_start_time(self):
        # A pid written without its own start time would make the job read as gone, or keep the reused-id fault.
        from upe import flow as flowmod

        self.state().mkdir(parents=True)
        mine = flowmod.process_created(os.getpid())
        with mock.patch.object(cli.subprocess, "Popen", lambda argv, **kwargs: mock.Mock(pid=os.getpid())), \
                mock.patch.object(cli, "agent_process", lambda: {}):
            job = json.loads(self.main("--json", "--detach", "land", "dash")[1])["job"]
        record = json.loads((self.state() / "jobs" / f"{job}.json").read_text())
        self.assertEqual((record["state"], record["pid"], record["created"]), ("started", os.getpid(), mine))
        written = []
        real = cli._job_update

        def update(flow, job_id, **fields):
            written.append(fields)
            real(flow, job_id, **fields)

        with mock.patch.object(cli.Flow, "land", lambda flow, task_id: Outcome(task_id, "awaiting-council", "x")), \
                mock.patch.object(cli, "lower_own_priority"), mock.patch.object(cli, "_job_update", update):
            self.main("--job-id", job, "--json", "land", "dash")
        self.assertEqual([(f["pid"], f.get("created")) for f in written if "pid" in f], [(os.getpid(), mine)])

    # `status` gives `next` from the one `next_step`, and what it knows of a land ---------

    JOB = "land-dash-20261101T120000"  # made long ago (see flow.JOB_START_GRACE_S)

    def state(self):
        return self.tmp / "work" / ".upe"

    def dead_pid(self):
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        return gone.pid

    def test_status_json_gives_the_same_next_step(self):
        from upe import flow as flowmod
        from upe.claims import ClaimRegistry

        self.record(state="conflict")
        (self.state() / "tasks" / "bad.json").write_text("{not json")
        ClaimRegistry(self.state() / "claims.json").claim("quiet", ["Assets/A.unity"])
        # A claim made by `ClaimRegistry.claim` alone carries no mark, as one made before claims were marked.
        want = {"dash": flowmod.next_step("dash", "conflict", target="t"), "bad": flowmod.next_step("bad", "unreadable"),
                "quiet": flowmod.next_step("quiet", "starting", flowmod.Land(running=None, unmarked=True))}
        code, out, _ = self.main("--json", "status")
        self.assertEqual({t["task"]: t["next"] for t in json.loads(out)["tasks"]}, want)
        for task in ("dash", "quiet"):
            code, out, _ = self.main("--json", "status", task)
            self.assertEqual(json.loads(out)["next"], want[task])
        code, out, _ = self.main("status", "dash")
        self.assertIn(f"\nnext: {want['dash']}\n", out)

    def test_a_dead_job_shows_as_done_exit_3(self):
        from upe import flow as flowmod

        self.record(history=["working: worktree x", f"working: land started (job {self.JOB}); any earlier merge and its votes no longer count"])
        jobs = self.state() / "jobs"
        jobs.mkdir(parents=True)
        (jobs / f"{self.JOB}.json").write_text(json.dumps({"job": self.JOB, "state": "running", "pid": self.dead_pid(),
                                                            "log": "nope"}))
        code, out, _ = self.main("--json", "status", "dash")
        answer = json.loads(out)
        self.assertEqual((code, answer["state"], answer["job"], answer["job_state"], answer["exit_code"], answer["running"]),
                         (0, "landing", self.JOB, "done", 3, False))
        self.assertEqual(answer["next"], flowmod.next_step("dash", "landing", flowmod.Land(False, self.JOB, "done", 3)))
        code, out, _ = self.main("--json", "status")
        row = json.loads(out)["tasks"][0]
        self.assertEqual((row["state"], row["job"], row["job_state"], row["running"], row["next"]),
                         ("landing", self.JOB, "done", False, answer["next"]))
        code, out, _ = self.main("status")
        self.assertIn(f"next: {answer['next']}", out)  # the list's text names the next step of a land that ended
        code, out, _ = self.main("status", "dash")
        self.assertIn(f"job: {self.JOB} (done, exit 3); running: no", out)

    def test_status_writes_no_file(self):
        # A read stays a read: no lock file, no job record, no task record is made or changed. A guard: `status` on
        # main never tried the lock, so only the `running` field is red there.
        self.record(history=["working: worktree x", f"working: land started (job {self.JOB}); any earlier merge and its votes no longer count"])
        jobs = self.state() / "jobs"
        jobs.mkdir(parents=True)
        (jobs / f"{self.JOB}.json").write_text(json.dumps({"job": self.JOB, "state": "running", "pid": self.dead_pid()}))
        # The list reads the claims under their lock, whose file `start` made when it claimed.
        (self.state() / "claims.json.lock").write_text("")

        def files():
            return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(self.state().rglob("*")) if p.is_file()} | {
                p: None for p in self.state().rglob("*") if p.is_dir()}

        before = files()
        for argv in (("--json", "status", "dash"), ("--json", "status"), ("status", "dash"), ("status",)):
            self.main(*argv)
        self.assertEqual(files(), before)
        self.assertFalse((self.state() / "locks").exists(), "no lock file is made to try the lock")
        (self.state() / "locks").mkdir()
        (self.state() / "locks" / "dash.lock").write_text("")  # a lock file left by an earlier land
        before = files()
        code, out, _ = self.main("--json", "status", "dash")
        self.assertEqual((json.loads(out)["running"], files()), (False, before))

    def test_a_job_record_that_is_missing_is_called_missing(self):
        # It said "does not read" of a file that is not there.
        self.record(history=["working: worktree x", f"working: land started (job {self.JOB}); any earlier merge and its votes no longer count"])
        code, out, _ = self.main("status", "dash")
        self.assertIn(f"job: {self.JOB} (its record is missing); running: no", out)
        code, out, _ = self.main("--json", "status", "dash")
        self.assertEqual(json.loads(out)["job_record"], "missing")

    def test_starting_takes_its_step_from_the_flow(self):
        # `status <task>` and the list ask one Flow method.
        from upe.claims import ClaimRegistry

        self.state().mkdir(parents=True)
        ClaimRegistry(self.state() / "claims.json").claim("quiet", ["Assets/A.unity"])
        with mock.patch.object(cli.Flow, "starting_step", lambda flow, task_id, *facts: f"the one step of {task_id}"):
            _, one, _ = self.main("--json", "status", "quiet")
            _, listed, _ = self.main("--json", "status")
        self.assertEqual(json.loads(one)["next"], "the one step of quiet")
        self.assertEqual(json.loads(listed)["tasks"][0]["next"], "the one step of quiet")

    def test_status_shows_the_history_of_the_record_it_judged(self):
        # The land writes its result between `status`'s first read and its lock probe. The history, the stamps
        # and the list's `last` and `since` are those of the record whose `state` and `next` are given.
        self.record(history=["working: worktree x", "working: land started; any earlier merge and its votes no longer count"],
                    stamps=["2026-11-08T10:00:00+00:00", "2026-11-08T10:01:00+00:00"])
        path = self.state() / "tasks" / "dash.json"
        probe = cli.Flow.task_lock_held

        def the_land_ends_first(flow, task_id):
            data = json.loads(path.read_text())
            if data["state"] == "working":
                data.update(state="awaiting-council", history=[*data["history"], "awaiting-council: checks passed"],
                            stamps=[*data["stamps"], "2026-11-08T11:00:00+00:00"])
                path.write_text(json.dumps(data))
            return probe(flow, task_id)

        def reset():
            self.record(history=["working: worktree x", "working: land started; any earlier merge and its votes no longer count"],
                        stamps=["2026-11-08T10:00:00+00:00", "2026-11-08T10:01:00+00:00"])

        with mock.patch.object(cli.Flow, "task_lock_held", the_land_ends_first):
            _, out, _ = self.main("--json", "status", "dash")
            answer = json.loads(out)
            self.assertEqual((answer["state"], answer["history"][-1], answer["stamps"][-1]),
                             ("awaiting-council", "awaiting-council: checks passed", "2026-11-08T11:00:00+00:00"))
            reset()
            _, out, _ = self.main("status", "dash")
            self.assertTrue(out.startswith("dash: awaiting-council\n"))
            self.assertTrue(out.rstrip().endswith("2026-11-08T11:00:00+00:00 awaiting-council: checks passed"))
            reset()
            _, out, _ = self.main("--json", "status")
            row = json.loads(out)["tasks"][0]
            self.assertEqual((row["state"], row["last"], row["since"]),
                             ("awaiting-council", "awaiting-council: checks passed", "2026-11-08T11:00:00+00:00"))

    def test_the_list_gives_next_where_the_lock_could_not_be_tried(self):
        # On a state other than `landing`, a lock that cannot be tried is "could not tell", in the list's text too.
        from upe import flow as flowmod

        self.record(state="conflict", history=["conflict: x"])
        with mock.patch.object(flowmod, "lock_held", side_effect=PermissionError("denied")):
            _, out, _ = self.main("--json", "status", "dash")
            answer = json.loads(out)
            _, text, _ = self.main("status")
        self.assertEqual((answer["state"], answer["running"]), ("conflict", None))
        self.assertEqual(answer["next"], flowmod.next_step("dash", "conflict", flowmod.Land(running=None)))
        self.assertIn(f"next: {answer['next']}", text)

    def test_two_detached_jobs_in_one_second_get_two_ids(self):
        # Two `land --detach` of one task in one second shared one record and one log.
        from upe import flow as flowmod

        class Frozen(cli.datetime):
            @classmethod
            def now(cls, tz=None):
                return cli.datetime(2026, 11, 1, 12, 0, 0, tzinfo=tz)

        self.state().mkdir(parents=True)
        launched = []

        def popen(argv, **kwargs):
            launched.append(argv[argv.index("--job-id") + 1])
            return mock.Mock(pid=os.getpid())

        with mock.patch.object(cli, "datetime", Frozen), mock.patch.object(cli.subprocess, "Popen", popen), \
                mock.patch.object(cli, "agent_process", lambda: {}):
            ids = [json.loads(self.main("--json", "--detach", "land", "dash")[1])["job"] for _ in range(2)]
        self.assertEqual(ids, ["land-dash-20261101T120000", "land-dash-20261101T120000-2"])
        self.assertEqual(launched, ids)
        jobs = self.state() / "jobs"
        self.assertEqual(sorted(p.name for p in jobs.glob("*.log")), sorted(f"{i}.log" for i in ids))
        self.assertEqual([json.loads((jobs / f"{i}.json").read_text())["log"] for i in ids], [str(jobs / f"{i}.log") for i in ids])
        flow = flowmod.Flow(flowmod.Config.load(self.config))
        self.assertEqual(flow._land_jobs("dash"), list(reversed(ids)))

    # A `start` that died ------------------------------------------------------------------------------

    def marked_claim(self, task_id="dash"):
        """A claim as `start` writes it since claims were marked (marked: made under the task's lock), with no record."""
        from upe.claims import ClaimRegistry

        self.state().mkdir(parents=True, exist_ok=True)
        ClaimRegistry(self.state() / "claims.json").claim(task_id, ["Assets/A/"], locked=True)

    def files(self):
        # A lock file another process holds cannot be read on Windows: its size and time are what is compared.
        return {p: (b"" if p.suffix == ".lock" else p.read_bytes(), p.stat().st_size, p.stat().st_mtime_ns)
                for p in sorted(self.state().rglob("*")) if p.is_file()} | {
            p: None for p in self.state().rglob("*") if p.is_dir()}

    def test_status_of_a_dead_start(self):
        from upe import flow as flowmod
        from test_flow import held_by_another_process

        self.marked_claim()
        code, out, _ = self.main("--json", "status", "dash")
        answer = json.loads(out)
        self.assertEqual((code, answer["state"], answer["running"]), (0, "starting", False))
        self.assertTrue(answer["claimed_since"])
        self.assertEqual(answer["next"], flowmod.next_step("dash", "starting", flowmod.Land(running=False)))
        self.assertIn("upe finish dash", answer["next"])
        code, out, _ = self.main("--json", "status")
        row = json.loads(out)["tasks"][0]
        self.assertEqual((row["task"], row["state"], row["running"], row["next"]), ("dash", "starting", False, answer["next"]))
        _, text, _ = self.main("status")
        self.assertIn(f"next: {answer['next']}", text, "the list's text does not hide a dead start")
        _, text, _ = self.main("status", "dash")
        self.assertIn("running: no", text)
        with held_by_another_process(self.state() / "locks" / "dash.lock"):
            answer = json.loads(self.main("--json", "status", "dash")[1])
        self.assertEqual((answer["state"], answer["running"]), ("starting", True))

    def test_status_of_a_start_is_a_read(self):
        # No file made or changed, and no wait on a start that holds its lock.
        import time
        from test_flow import held_by_another_process

        self.marked_claim()  # it made claims.json.lock, which the list reads the claims under
        before = self.files()
        for argv in (("--json", "status", "dash"), ("--json", "status"), ("status", "dash"), ("status",)):
            self.main(*argv)
        self.assertEqual(self.files(), before)
        self.assertFalse((self.state() / "locks").exists(), "no lock file is made to try the lock")
        with held_by_another_process(self.state() / "locks" / "dash.lock"):
            before = self.files()
            began = time.monotonic()
            code, out, _ = self.main("--json", "status", "dash")
            self.assertLess(time.monotonic() - began, 1.0)
            self.assertEqual((code, json.loads(out)["running"]), (0, True))
            self.assertEqual(self.files(), before)

    def test_finish_clears_a_dead_start_from_the_command_line(self):
        from upe.claims import ClaimRegistry

        game = self.tmp / "game"
        game.mkdir()
        for argv in (("init", "-q", "-b", "main"), ("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q",
                                                     "--allow-empty", "-m", "init")):
            subprocess.run(["git", "-C", str(game), *argv], check=True, capture_output=True, stdin=subprocess.DEVNULL)
        self.marked_claim()
        with mock.patch.object(cli, "lower_own_priority"):
            code, out, _ = self.main("--json", "finish", "dash")
            self.assertEqual((code, json.loads(out)["state"]), (0, "finished"))
            code, _, err = self.main("--json", "status", "dash")
            self.assertEqual(code, 2)
            self.assertIn("no task dash", json.loads(err)["error"])
            ClaimRegistry(self.state() / "claims.json").claim("dash", ["Assets/A/"])  # no mark: before claims were marked
            code, _, err = self.main("--json", "finish", "dash")
        self.assertEqual(code, 2)
        self.assertIn("older version of the tool", json.loads(err)["error"])

    def test_withdraw_exits_0_on_working_and_2_on_a_refusal(self):
        # `working` is what `withdraw` does, unlike a land that ends there.
        from upe import flow as flowmod

        withdrawn = flowmod.Outcome("dash", "working", "land withdrawn (awaiting-council)")
        with mock.patch.object(cli, "lower_own_priority"):
            with mock.patch.object(flowmod.Flow, "withdraw", return_value=withdrawn):
                code, out, _ = self.main("--json", "withdraw", "dash")
            self.assertEqual((code, json.loads(out)["state"]), (0, "working"))
            with mock.patch.object(flowmod.Flow, "withdraw", side_effect=flowmod.FlowError("nothing to withdraw")):
                code, _, err = self.main("--json", "withdraw", "dash")
        self.assertEqual((code, json.loads(err)["error"]), (2, "nothing to withdraw"))

    def test_a_start_job_beginning_shows_as_starting(self):
        # `start --detach` before its child claims: never "no task" alone.
        from datetime import datetime, timezone

        jobs = self.state() / "jobs"
        jobs.mkdir(parents=True)
        job = f"start-dash-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
        (jobs / f"{job}.json").write_text(json.dumps({"job": job, "state": "started", "pid": os.getpid(), "log": "x"}))
        code, out, _ = self.main("--json", "status", "dash")
        answer = json.loads(out)
        self.assertEqual((code, answer["state"], answer["running"], answer["job"]), (0, "starting", True, job))
        self.assertIn(f"upe job {job} --wait", answer["next"])
        code, out, _ = self.main("--json", "status")
        self.assertEqual([(t["task"], t["state"], t["running"]) for t in json.loads(out)["tasks"]], [("dash", "starting", True)])

    def test_a_start_that_ends_while_status_asks_shows_its_record(self):
        # The start writes its record and frees the lock between the claims' read and the lock probe.
        self.marked_claim()
        probe = cli.Flow.task_lock_held

        def the_start_ends_first(flow, task_id):
            if not (self.state() / "tasks" / "dash.json").exists():
                self.record(history=["working: worktree x from abc"])
            return probe(flow, task_id)

        with mock.patch.object(cli.Flow, "task_lock_held", the_start_ends_first):
            code, out, _ = self.main("--json", "status", "dash")
            self.assertEqual((code, json.loads(out)["state"]), (0, "working"))
            (self.state() / "tasks" / "dash.json").unlink()
            code, out, _ = self.main("--json", "status")
        self.assertEqual([(t["task"], t["state"]) for t in json.loads(out)["tasks"]], [("dash", "working")])

    def test_a_start_that_ends_with_a_record_that_does_not_read_is_listed(self):
        # The list dropped the row (`except FlowError: continue`), so the task was not shown.
        self.marked_claim()
        probe = cli.Flow.task_lock_held

        def the_start_ends_first(flow, task_id):
            path = self.state() / "tasks" / "dash.json"
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{not json", encoding="utf-8")
            return probe(flow, task_id)

        with mock.patch.object(cli.Flow, "task_lock_held", the_start_ends_first):
            code, out, _ = self.main("--json", "status")
        rows = json.loads(out)["tasks"]
        self.assertEqual((code, [(t["task"], t["state"]) for t in rows]), (0, [("dash", "unreadable")]))
        self.assertEqual(rows[0]["next"], cli.next_step("dash", "unreadable"))

    def test_ctrl_c_in_a_wait_or_a_read_says_nothing_was_left(self):
        # `job --wait` and `judge --watch` change no task; "see what it left" did not fit them.
        jobs = self.state() / "jobs"
        jobs.mkdir(parents=True)
        (jobs / "land-dash-20261101T120000.json").write_text(json.dumps({"job": "x", "state": "running", "log": "nope"}))
        for argv, says in ((("--json", "job", "land-dash-20261101T120000", "--wait"), "runs on"),
                           (("--json", "judge", "--watch"), "the judge stopped"),
                           (("--json", "status"), "nothing was left")):
            with self.subTest(argv=argv), mock.patch.object(cli, "run", side_effect=KeyboardInterrupt), \
                    mock.patch.object(cli, "lower_own_priority"):
                try:
                    code, _, err = self.main(*argv)
                except KeyboardInterrupt:
                    self.fail("KeyboardInterrupt left main")
                error = json.loads(err)["error"]
                self.assertEqual(code, 2)
                self.assertIn(says, error)
                self.assertNotIn("see what it left", error)

    def test_ctrl_c_exits_2_and_says_to_ask_status(self):
        # A KeyboardInterrupt left `main` as a traceback with Python's own code.
        with mock.patch.object(cli, "run", side_effect=KeyboardInterrupt), mock.patch.object(cli, "lower_own_priority"):
            try:
                code, out, err = self.main("--json", "finish", "dash")
            except KeyboardInterrupt:
                self.fail("KeyboardInterrupt left main")
        self.assertEqual((code, out), (2, ""))
        error = json.loads(err)["error"]
        self.assertIn("interrupted", error)
        self.assertIn("upe status dash", error)

    def test_main_passes_the_job_id_to_the_flow(self):
        seen = []

        def land(flow, task_id):
            seen.append(flow.job_id)
            return Outcome(task_id, "awaiting-council", "x")

        with mock.patch.object(cli.Flow, "land", land), mock.patch.object(cli, "lower_own_priority"):
            self.main("--job-id", self.JOB, "--json", "land", "dash")
            self.main("--json", "land", "dash")
        self.assertEqual(seen, [self.JOB, ""])

    def test_decide_needs_a_person_at_a_terminal(self):
        with mock.patch("sys.stdin", io.StringIO("dash\n")):
            code, _, err = self.main("decide", "dash", "land")
        self.assertEqual(code, 2)
        self.assertIn("decide is the user's call", err)

    def test_the_config_is_never_taken_from_the_current_folder(self):
        with mock.patch.dict(os.environ, {"UPE_CONFIG": ""}):
            self.assertEqual(cli.default_config(), cli.TOOL_HOME / "upe.toml")
        with mock.patch.dict(os.environ, {"UPE_CONFIG": str(self.config)}):
            self.assertEqual(cli.default_config(), self.config)

    # The clocks ---------------------------------------------------------------------------------------------------

    def test_a_log_line_says_its_date_and_zone(self):
        # Before: `HH:MM:SS`, no date and no zone.
        class Frozen(cli.datetime):
            @classmethod
            def now(cls, tz=None):
                return cli.datetime(2026, 11, 12, 14, 3, 7, tzinfo=tz)

        args = cli.parser().parse_args(["land", "dash"])
        err = io.StringIO()
        with mock.patch.object(cli, "datetime", Frozen), redirect_stderr(err):
            cli.progress(None, args, "merge: done")
        self.assertEqual(err.getvalue(), "2026-11-12T14:03:07Z merge: done\n")

    def job(self, state="running", pid=None):
        jobs = self.state() / "jobs"
        jobs.mkdir(parents=True, exist_ok=True)
        (jobs / f"{self.JOB}.json").write_text(json.dumps({"job": self.JOB, "state": state, "log": "nope",
                                                           **({"pid": pid} if pid else {}),
                                                           **({"exit_code": 0} if state == "done" else {})}))

    def test_job_wait_says_how_the_wait_ended_and_the_time_now(self):
        # Without `wait` and `now`, a timeout reads as any other `running`.
        self.job("running", os.getpid())  # a live process: the job stays running
        with mock.patch.object(cli, "WAIT_POLL_S", 0.1, create=True):
            code, out, _ = self.main("--json", "job", self.JOB, "--wait", "0.3")
        answer = json.loads(out)
        self.assertEqual((answer["state"], answer["wait"]["ended"], answer["wait"]["asked_s"]), ("running", "timeout", 0.3))
        self.assertGreaterEqual(answer["wait"]["waited_s"], 0.3)
        self.assertTrue(answer["now"].endswith("+00:00"), answer["now"])
        _, text, _ = self.main("job", self.JOB, "--wait", "0.2")
        self.assertIn("of 0.2: not done, wait again", text)
        self.job("done")
        answer = json.loads(self.main("--json", "job", self.JOB, "--wait", "5")[1])
        self.assertEqual(answer["wait"]["ended"], "done")
        self.assertLess(answer["wait"]["waited_s"], 5)
        answer = json.loads(self.main("--json", "job", self.JOB)[1])
        self.assertNotIn("wait", answer)
        self.assertTrue(answer["now"].endswith("+00:00"))

    # What `start` and the skill print, run as printed ---------------------------------------------------------------

    def test_start_names_the_tests_in_text_and_json(self):
        from upe.flow import Task
        from upe.workspace import Workspace

        slot = self.tmp / "slot"
        task = Task("dash", "b", ["Assets/A/"], "x", "main")
        with mock.patch.object(cli.Flow, "start", return_value=task), \
                mock.patch("upe.workspace.Workspaces.workspace", return_value=Workspace("dash", slot, "upe/dash")):
            answer = json.loads(self.main("--json", "start", "dash", "--brief", "b", "--claim", "Assets/A/")[1])
            text = self.main("start", "dash", "--brief", "b", "--claim", "Assets/A/")[1]
        self.assertEqual((answer["tests"]["folders"], answer["tests"]["from"]), ([], "none"))
        self.assertIn("ls-files", answer["tests"]["find"])
        self.assertIn(answer["tests"]["find"], text)
        self.config.write_text(self.config.read_text(encoding="utf-8") + 'test_folders = ["Assets/Tests/"]\n',
                               encoding="utf-8")
        with mock.patch.object(cli.Flow, "start", return_value=task), \
                mock.patch("upe.workspace.Workspaces.workspace", return_value=Workspace("dash", slot, "upe/dash")):
            answer = json.loads(self.main("--json", "start", "dash", "--brief", "b", "--claim", "Assets/A/")[1])
            text = self.main("start", "dash", "--brief", "b", "--claim", "Assets/A/")[1]
        self.assertEqual(answer["tests"], {"folders": ["Assets/Tests/"], "from": "upe.toml"})
        self.assertIn("tests: Assets/Tests/ (test_folders in upe.toml)", text)

    def test_the_installed_skill_gives_a_look_before_claiming_command_that_runs(self):
        # The skill's read-only command, as `upe skill` writes it with this machine's project and target, run as
        # printed (with the folder filled in) on real git. Red with the command taken out of the skill.
        from test_trial_gaps import run_printed  # a real POSIX shell (Git Bash's sh), so `$` and a backtick count

        game = self.tmp / "game"
        (game / "Assets" / "Features" / "Dash").mkdir(parents=True)
        (game / "Assets" / "Features" / "Dash" / "Dash.cs").write_text("class Dash {}\n")
        (game / "Assets" / "Features" / "Dash" / "Ünï.cs").write_text("class U {}\n")  # a non-ASCII name, printed as it is
        (game / "Assets" / "Other.cs").write_text("class Other {}\n")
        for args in (["init", "-q", "-b", "dev"], ["add", "-A"],
                     ["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "first"],
                     ["branch", "upe/t$x`y"]):
            subprocess.run(["git", "-C", str(game), *args], check=True, capture_output=True)
        self.config.write_text(self.config.read_text(encoding="utf-8") + 'target = "upe/t$x`y"\n',
                               encoding="utf-8")
        skills = self.tmp / "skills"
        code, _, err = self.main("skill", "--to", str(skills))
        self.assertEqual(code, 0, err)
        text = (skills / "upe" / "SKILL.md").read_text(encoding="utf-8")
        [line] = [l.strip() for l in text.splitlines() if l.strip().startswith("git -c core.quotepath=off -C ") and "ls-tree" in l]
        self.assertNotIn("{{", line)
        done = run_printed(line.replace("'<folder>/'", "'Assets/Features/'"))
        self.assertEqual((done.returncode, done.stdout.splitlines()),
                         (0, ["Assets/Features/Dash/Dash.cs", "Assets/Features/Dash/Ünï.cs"]), done.stderr)
        # Step 1 sends the agent to the tests' line of `setup --check`, and says to claim the test it
        # adds, before the `start` command.
        step1 = text[text.index("1. **Start.**"):text.index("2. **Work and commit**")]
        self.assertLess(step1.index("setup --check"), step1.index("start <task>"))
        self.assertLess(step1.index("claim that test file or folder too"), step1.index("start <task>"))

    def test_text_answers_print_any_name_whatever_the_code_page(self):
        # In a pipe on Windows, Python writes in the locale's code page, where a CJK name raised
        # UnicodeEncodeError after the state was saved. Run as the tool runs, in a process of its own.
        import upe

        tasks = self.state() / "tasks"
        tasks.mkdir(parents=True)
        name = "Assets/地图/Map.unity"
        (tasks / "dash.json").write_text(json.dumps({"task_id": "dash", "brief": "b", "claim": ["Assets/A/"], "base": "x",
                                                     "target": "t", "state": "working", "history": [f"working: {name}"]}),
                                         encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
        env["PYTHONPATH"] = str(Path(upe.__file__).resolve().parents[1])
        done = subprocess.run([sys.executable, "-m", "upe", "--config", str(self.config), "status", "dash"],
                              capture_output=True, env=env, stdin=subprocess.DEVNULL, cwd=self.tmp)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        self.assertIn(name, done.stdout.decode("utf-8"))

    def test_a_jobs_stage_stays_one_short_line(self):
        # The log keeps the whole message; the job record's `stage` is cut.
        flow = cli.Flow(cli.Config.load(self.config))
        args = cli.parser().parse_args(["--job-id", self.JOB, "land", "dash"])
        long = "land dash: " + "x" * 1000
        err = io.StringIO()
        with redirect_stderr(err):
            cli.progress(flow, args, long)
        self.assertIn(long, err.getvalue())
        stage = json.loads(flow.job_path(self.JOB).read_text(encoding="utf-8"))["stage"]
        self.assertLessEqual(len(stage), 160)
        self.assertTrue(stage.startswith("land dash: x"))

    def test_a_config_inside_the_game_is_refused(self):
        inside = self.tmp / "game" / "upe.toml"
        inside.parent.mkdir()
        inside.write_text(self.config.read_text(encoding="utf-8"), encoding="utf-8")
        self.config = inside
        code, _, err = self.main("status")
        self.assertEqual(code, 2)
        self.assertIn("inside repo", err)


if __name__ == "__main__":
    unittest.main()
