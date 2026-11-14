"""Every way a `land` ends, and the other states `status` gives, printed with the `next` line the code gives.

One fixture per way a `land` ends (numbered as in `tests/next_rows.txt`), and one per other state `status` shows. Each fixture is a task record, the task's lock (held,
free, or one that cannot be tried) and the job records; the clock and the job ids are fixed, so the print is
the same on every run. The print is committed as `tests/next_rows.txt`, and the test below compares the code's
print with it.

What this test is, and what it is not:

- It puts every line of `next` in front of the reader, in one file, so a changed
  line shows in the diff of `tests/next_rows.txt`. It does not judge the words: a wrong line printed here is
  still wrong, and passes once it is written to the file. It does not make a wrong line impossible; the
  reader is the check.
- It is for reading. The mapping test (`test_every_state_has_a_next_step_of_its_own`, in
  `test_flow.py`) stays as it is, and asserts on `next_step`, never on the words of a message.
- A new case is added here as a fixture, then the file is printed again and read.

To print the file again, after reading what changed:

    PYTHONPATH=src python tests/test_next_rows.py --write
"""

import difflib
import json
import sys
import tempfile
import unittest
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from upe import flow as flowmod
from upe.flow import Config, Flow, Task

ROWS_FILE = Path(__file__).resolve().parent / "next_rows.txt"
NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)  # the fixed clock of every fixture
LIVE, DEAD = 4242, 4343  # the pids a fixture gives a job: `process_alive` says yes to LIVE only
TASK = "dash"
STARTED = "working: started in its slot"  # the entry before a land, as any working task has one


def ago(seconds: int) -> datetime:
    return NOW - timedelta(seconds=seconds)


def job_id(seconds_ago: int, n: int = 1) -> str:
    """A land job id as `detach` makes it, `seconds_ago` before the fixed clock (`-<n>`: the n-th in one second)."""
    return f"land-{TASK}-{ago(seconds_ago).strftime('%Y%m%dT%H%M%S')}" + (f"-{n}" if n > 1 else "")


def land_started(job: str = "") -> str:
    return "working: " + flowmod.land_started_note(job)


A = job_id(3600)  # a land job made an hour before the clock
B = job_id(30)  # a land job made 30 s before the clock: still inside JOB_START_GRACE_S
OLD = job_id(11 * 3600)  # eleven hours before: past a land's limit
FUTURE = job_id(-3600)  # an hour after the clock: made before the clock was set back
SAME, SAME_2 = job_id(600), job_id(600, 2)  # two `land --detach` in one second


@dataclass
class Row:
    number: str  # the row of the table, or `S <state>` for another state `status` gives
    ends: str  # how it ends, in the table's words
    state: str = "working"  # the record's state
    history: list = field(default_factory=list)  # (entry, seconds before the clock, or None: no stamp)
    jobs: dict = field(default_factory=dict)  # job id -> its record (`pid`: LIVE or DEAD)
    lock: bool | None = False  # the task's lock: held, free, or None (could not be tried)
    then: tuple | None = None  # (state, entry) the land writes while `status` tries the lock (row 17)
    shown: str = "record"  # "record"; `start` (a claim with no record); or a state with no fact at all
    claim: str = ""  # for `start`: "marked" (made by `start` under the task's lock), "old" (before claims were marked), "" none
    claimed: int = 0  # for `start`: when the claim was made, seconds before the clock
    moved: str = ""  # the target's tip, when it moved since the merge under review (`Flow.moved_since`)
    voted: bool = False  # with `moved`: a vote is already recorded on that merge (`Flow._has_votes`)
    unparked: bool = False  # a slot of its land is still held (`Flow.land_slot_held`)


def _job(state: str, *, exit_code: int | None = None, pid: int = DEAD, error: str = "") -> dict:
    return {"state": state, "pid": pid, **({"exit_code": exit_code} if exit_code is not None else {}),
            **({"error": error} if error else {})}


ROWS = [
    Row("1", "exit 0, `awaiting-council`", "awaiting-council",
        [(STARTED, 7200), (land_started(A), 3600), ("awaiting-council: checks passed; ask two members", 60)],
        {A: _job("done", exit_code=0)}),
    Row("1 a", "exit 0, `awaiting-council`, then another land moved the target before the votes", "awaiting-council",
        [(STARTED, 7200), (land_started(A), 3600), ("awaiting-council: checks passed; ask two members", 60)],
        {A: _job("done", exit_code=0)}, moved="9f3c2e1d4b5a" + "0" * 28),
    Row("1 b", "the same, with a vote already recorded on the old merge", "awaiting-council",
        [(STARTED, 7200), (land_started(A), 3600), ("awaiting-council: checks passed; ask two members", 60)],
        {A: _job("done", exit_code=0)}, moved="9f3c2e1d4b5a" + "0" * 28, voted=True),
    Row("1 c", "exit 0, `awaiting-council`, then the land withdrawn with `upe withdraw`", "working",
        [(STARTED, 7200), (land_started(A), 3600), ("awaiting-council: checks passed; ask two members", 600),
         ("working: " + flowmod.land_withdrawn_note("awaiting-council"), 60)],
        {A: _job("done", exit_code=0)}),
    Row("1 d", "the same, and an Editor still had the merge candidate's slot, so it was not parked", "working",
        [(STARTED, 7200), (land_started(A), 3600), ("awaiting-council: checks passed; ask two members", 600),
         ("working: " + flowmod.land_withdrawn_note("awaiting-council"), 60)],
        {A: _job("done", exit_code=0)}, unparked=True),
    Row("2", "exit 1, `conflict`", "conflict",
        [(STARTED, 7200), (land_started(A), 3600), ("conflict: Assets/Scenes/Arena.unity", 60)],
        {A: _job("done", exit_code=1)}),
    Row("3", "exit 1, `checks-failed`", "checks-failed",
        [(STARTED, 7200), (land_started(A), 3600), ("checks-failed: 2 findings", 60)],
        {A: _job("done", exit_code=1)}),
    Row("4", "exit 2 before \"land started\", on a task whose last entry is not a land's",
        "working", [(STARTED, 7200)], {B: _job("done", exit_code=2)}),
    Row("4 a", "exit 2 before \"land started\", after an older land with a job that died",
        "working", [(STARTED, 7200), (land_started(A), 3600)],
        {A: _job("done", exit_code=3, error="the job's process ended without recording a result"),
         B: _job("done", exit_code=2)}),
    Row("4 b", "exit 2 before \"land started\", after an older land with no job",
        "working", [(STARTED, 7200), (land_started(), 3600)], {B: _job("done", exit_code=2)}),
    Row("5", "exit 2 after \"land started\"", "working",
        [(STARTED, 7200), (land_started(A), 3600)], {A: _job("done", exit_code=2)}),
    Row("6", "another exception, with `--detach`", "working",
        [(STARTED, 7200), (land_started(A), 3600)], {A: _job("done", exit_code=3, error="boom")}),
    Row("7", "another exception, without `--detach`", "working", [(STARTED, 7200), (land_started(), 3600)]),
    Row("8", "Ctrl-C, without `--detach`", "working", [(STARTED, 7200), (land_started(), 3600)]),
    Row("9 a", "Ctrl-C, with `--detach`: the child runs on (as 12)", "working",
        [(STARTED, 7200), (land_started(A), 3600)], {A: _job("running", pid=LIVE)}, lock=True),
    Row("9 b", "Ctrl-C, with `--detach`: the child killed, its record left `running` (as 6)", "working",
        [(STARTED, 7200), (land_started(A), 3600)], {A: _job("running", pid=DEAD)}),
    Row("10", "process killed or machine lost, with a job", "working",
        [(STARTED, 7200), (land_started(A), 3600)], {A: _job("running", pid=DEAD)}),
    Row("10 a", "the same, with the pid used again by another program", "working",
        [(STARTED, 7200), (land_started(A), 3600)], {A: _job("running", pid=LIVE)}),
    Row("11", "process killed or machine lost, without a job", "working",
        [(STARTED, 7200), (land_started(), 3600)]),
    Row("12", "still running, with a job", "working",
        [(STARTED, 7200), (land_started(A), 3600)], {A: _job("running", pid=LIVE)}, lock=True),
    Row("13", "still running, without a job (another terminal)", "working",
        [(STARTED, 7200), (land_started(), 3600)], lock=True),
    Row("13 a", "the same, with an entry from before times were kept", "working",
        [(STARTED, None), (land_started(), None)], lock=True),
    Row("14", "in the gap of a retry", "working",
        [(STARTED, 7200), (land_started(A), 3600), ("working: target moved; landing again (2 of 3)", 60)],
        {A: _job("running", pid=LIVE)}, lock=True),
    Row("15", "a new `land --detach` before its child takes the lock, after an older land ended", "working",
        [(STARTED, 7200), (land_started(A), 600)], {A: _job("done", exit_code=2), B: _job("started", pid=LIVE)}),
    Row("16", "a land in its first seconds, before \"land started\" (or a `verdict`, `decide`, `withdraw`, `finish`)",
        "checks-failed", [(STARTED, 7200), ("checks-failed: 2 findings", 600)], lock=True),
    Row("16 a", "the same, with its new job starting", "checks-failed",
        [(STARTED, 7200), ("checks-failed: 2 findings", 600)], {B: _job("started", pid=LIVE)}, lock=True),
    Row("17", "a land writes its result and frees the lock while `status` reads", "working",
        [(STARTED, 7200), (land_started(A), 3600)], {A: _job("running", pid=LIVE)},
        then=("awaiting-council", "awaiting-council: checks passed; ask two members")),
    Row("18", "the lock is held by another command, and the entry's job is over", "working",
        [(STARTED, 7200), (land_started(A), 3600)], {A: _job("done", exit_code=3)}, lock=True),
    Row("19", "a land hung, with a job", "working",
        [(STARTED, 12 * 3600), (land_started(OLD), 11 * 3600)], {OLD: _job("running", pid=LIVE)}, lock=True),
    Row("19 a", "a land hung, without a job", "working",
        [(STARTED, 12 * 3600), (land_started(), 11 * 3600)], lock=True),
    Row("20", "the clock set back after `detach`, the lock held", "working",
        [(STARTED, 7200), (land_started(FUTURE), 60)], {FUTURE: _job("running", pid=LIVE)}, lock=True),
    Row("20 a", "the entry names a job from before the clock was set back, whose child took the lock and wrote "
        "it; the lock is now free, so that land is over, and its record still says started", "working",
        [(STARTED, 7200), (land_started(FUTURE), 60)], {FUTURE: _job("started", pid=LIVE)}),
    Row("20 b", "the clock set back after `detach`: an older land ended, and a new job from before the clock "
        "went back is starting, its child not yet at the lock (the lock free)", "working",
        [(STARTED, 7200), (land_started(A), 600)], {A: _job("done", exit_code=2), FUTURE: _job("started", pid=LIVE)}),
    Row("20 c", "the same, after an older land with no job", "working",
        [(STARTED, 7200), (land_started(), 600)], {FUTURE: _job("started", pid=LIVE)}),
    Row("21", "two `land --detach` of one task in one second: the first died, the second was refused", "working",
        [(STARTED, 7200), (land_started(SAME), 590)],
        {SAME: _job("done", exit_code=3), SAME_2: _job("done", exit_code=2)}),
    Row("-", "the lock could not be tried, after a land entry", "working",
        [(STARTED, 7200), (land_started(A), 3600)], {A: _job("done", exit_code=0)}, lock=None),
    Row("- a", "the lock could not be tried, on another state", "conflict",
        [(STARTED, 7200), ("conflict: Assets/Scenes/Arena.unity", 600)], lock=None),
    # The other states `status` gives, with no command on the task.
    Row("S working", "a task being worked on", "working", [(STARTED, 600)]),
    Row("S checks-failed", "exit 1, `checks-failed`, and none of the findings is in a path the branch changed "
        "(a false stop)", "checks-failed",
        [(STARTED, 7200), (land_started(A), 3600), (f"checks-failed: 24 findings{flowmod.NOT_IN_BRANCH}", 60)],
        {A: _job("done", exit_code=1)}),
    Row("S refused", "the council refused it", "refused", [(STARTED, 7200), ("refused: 2 of 3 said no", 60)]),
    Row("S awaiting-user", "the user decides", "awaiting-user", [(STARTED, 7200), ("awaiting-user: a split", 60)]),
    Row("S landed", "landed", "landed", [(STARTED, 7200), ("landed: abc1234", 60)]),
    Row("S finished", "finished", "finished", [(STARTED, 7200), ("finished: claim freed", 60)]),
    Row("S starting", "`starting` with no fact at all", shown="starting"),
    Row("S unreadable", "a record that does not read", shown="unreadable"),
    Row("S left-behind", "a slot or a branch `upe/<task>` with no claim and no record (the `start` error)",
        shown="left-behind"),
    # Every way `start` ends that leaves something `next` speaks of.
    Row("St 1", "a `start` making its slot (or a `finish` clearing a dead one)", shown="start", claim="marked",
        claimed=120, lock=True),
    Row("St 2", "a `start` that hangs (the Library copy or the Unity CLI never returns)", shown="start",
        claim="marked", claimed=2 * 3600, lock=True),
    Row("St 3", "a `start` running, the clock set back after it claimed", shown="start", claim="marked",
        claimed=-3600, lock=True),
    Row("St 4", "a `start` killed, or interrupted, or whose clean-up failed, after it claimed", shown="start",
        claim="marked", claimed=600),
    Row("St 5", "the same, long ago: the age does not change the answer", shown="start", claim="marked",
        claimed=10 * 3600),
    Row("St 6", "a marked claim whose lock cannot be tried", shown="start", claim="marked", claimed=600, lock=None),
    Row("St 7", "a claim from before claims were marked (no mark), young", shown="start", claim="old", claimed=600),
    Row("St 8", "the same, over 30 minutes old", shown="start", claim="old", claimed=2 * 3600),
    Row("St 9", "`start --detach` before its child claims", shown="start",
        jobs={f"start-{TASK}-{ago(30).strftime('%Y%m%dT%H%M%S')}": _job("started", pid=LIVE)}),
]


def _record(state: str, history: list) -> Task:
    entries = [entry for entry, _ in history]
    stamped = [ago(s).isoformat(timespec="seconds") for _, s in history if s is not None]
    return Task(task_id=TASK, brief="Add a dash.", claim=["Assets/Features/Dash/"], base="0" * 40, target="main",
                state=state, history=entries, stamps=stamped)


def _write(flow: Flow, task: Task) -> None:
    path = flow.state_dir / "tasks" / f"{TASK}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(task), indent=2) + "\n", encoding="utf-8")


def _shown(row: Row, root: Path) -> tuple[dict, str]:
    """What `status --json` shows for the row's task (the state and the land's fields) and its `next`."""
    if row.shown not in ("record", "start"):
        return {"state": row.shown}, flowmod.next_step(TASK, row.shown)
    flow = Flow(Config(repo=root / "game", root=root / "work", golden=root / "golden"))
    jobs = flow.state_dir / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    for name, record in row.jobs.items():
        (jobs / f"{name}.json").write_text(json.dumps({"job": name, "log": f"<state>/jobs/{name}.log", **record}),
                                           encoding="utf-8")
    if row.shown == "start":
        if row.claim:
            mark = {"locked": True} if row.claim == "marked" else {}
            (flow.state_dir / "claims.json").write_text(json.dumps({TASK: {
                "entries": ["assets/features/dash/"], "since": ago(row.claimed).isoformat(timespec="seconds"), **mark}}))
        with mock.patch.object(flowmod, "_now", lambda: NOW), \
                mock.patch.object(flowmod, "process_alive", lambda pid, created=None: pid == LIVE), \
                mock.patch.object(flow, "task_lock_held", lambda _task_id: row.lock):
            claim = flow.starting().get(TASK)
            facts = flow.start_facts(TASK, claim)
            return {"state": "starting", **flowmod.start_fields(claim, facts)}, flow.starting_step(TASK, facts)
    task = _record(row.state, row.history)
    _write(flow, task)

    def lock(_task_id):
        if row.then:  # the land writes its result, then frees the lock, while `status` tries it
            state, entry = row.then
            _write(flow, _record(state, [*row.history, (entry, 0)]))
        return row.lock

    with mock.patch.object(flowmod, "_now", lambda: NOW), \
            mock.patch.object(flowmod, "process_alive", lambda pid, created=None: pid == LIVE), \
            mock.patch.object(flow, "task_lock_held", lock), \
            mock.patch.object(flow, "_moved_under", lambda _task_id: row.moved), \
            mock.patch.object(flow, "_has_votes", lambda _task_id: row.voted), \
            mock.patch.object(flow, "land_slot_held", lambda _task_id: row.unparked):
        state, land, step = flow.situation(task)
    return {"state": state, **(land.to_json() if land else {})}, step


def _facts(row: Row) -> list[str]:
    if row.shown not in ("record", "start"):
        return []
    def when(s):
        return "no time" if s is None else f"{s} s ago"
    if row.shown == "start":
        lines = ["  record: none; claim: " + ({"marked": "marked (made by `start` under the task's lock)",
                                               "old": "no mark (made before claims were marked)"}.get(row.claim, "none"))
                 + (f", made {row.claimed} s ago" if row.claim else "")]
    else:
        lines = [f"  record: {row.state}; entries: " + "; ".join(f"\"{e}\" ({when(s)})" for e, s in row.history)]
    lines.append("  lock: " + {True: "held", False: "free", None: "cannot be tried"}[row.lock]
                 + (f"; while it is tried, the land writes \"{row.then[1]}\"" if row.then else ""))
    if row.moved:
        lines.append(f"  target: moved since the merge, to {row.moved[:12]}"
                     + ("; a vote is recorded on that merge" if row.voted else ""))
    if row.unparked:
        lines.append("  slot: the merge candidate's slot is still held")
    for name, record in row.jobs.items():
        pid = "live pid" if record.get("pid") == LIVE else "dead pid"
        code = f", exit {record['exit_code']}" if "exit_code" in record else ""
        lines.append(f"  job {name}: record says {record['state']}{code}, {pid}")
    return lines


def render() -> str:
    head = [
        "# `next` for each way a `land` ends (by row number) and each other state `status` gives.",
        "# Printed by tests/test_next_rows.py; the clock is fixed at " + NOW.isoformat(timespec="seconds") + ".",
        "# This file puts every line in front of the reader: it does not make a wrong line impossible.",
        "# Read each changed line before it is committed. Print it again with:",
        "#   PYTHONPATH=src python tests/test_next_rows.py --write",
        "",
    ]
    out = list(head)
    with tempfile.TemporaryDirectory() as tmp:
        for i, row in enumerate(ROWS):
            shown, step = _shown(row, Path(tmp) / str(i))
            out.append(f"## row {row.number}: {row.ends}")
            out += _facts(row)
            out.append("  status: " + json.dumps(shown))
            out.append("  next: " + step)
            out.append("")
    return "\n".join(out)


class NextRowsTests(unittest.TestCase):
    def test_the_printed_rows_are_the_committed_file(self):
        printed = render().splitlines()
        committed = ROWS_FILE.read_text(encoding="utf-8").splitlines() if ROWS_FILE.exists() else []
        if printed == committed:
            return
        changed = [line.split(":")[0][3:] for line in printed if line.startswith("## row ")
                   and _block(printed, line) != _block(committed, line)]
        diff = "\n".join(difflib.unified_diff(committed, printed, "tests/next_rows.txt", "the code", n=0, lineterm=""))
        self.fail(f"`next` changed for {', '.join(changed) or 'the head or the order of the rows'}: run "
                  "`PYTHONPATH=src python tests/test_next_rows.py --write`, read each changed line of "
                  f"tests/next_rows.txt, and commit it\n{diff}")

    def test_every_row_of_the_table_is_printed(self):
        numbers = {row.number.split(" ")[0] for row in ROWS if not row.number.startswith(("S ", "St "))}
        self.assertEqual(numbers, {*(str(n) for n in range(1, 22)), "-"})
        shown = {row.number[2:] for row in ROWS if row.number.startswith("S ")}
        self.assertEqual(shown | {"conflict", "checks-failed", "awaiting-council"},
                         {*flowmod.STATES, "starting", "unreadable", "left-behind"})
        starts = {row.number for row in ROWS if row.number.startswith("St ")}
        self.assertEqual(starts, {f"St {n}" for n in range(1, 10)}, "the rows of the ways `start` ends")

    def test_the_hung_rows_are_past_the_land_bound(self):
        # The bound grew past rows 19 and 19 a's ages, and their hung-land `next` lines left
        # the file unnoticed. Each hung row's "land started" entry is older than the bound, so it prints over_limit.
        with tempfile.TemporaryDirectory() as tmp:
            for i, row in enumerate(r for r in ROWS if r.number in ("19", "19 a")):
                with self.subTest(row.number):
                    flow = Flow(Config(repo=Path(tmp) / "repo", root=Path(tmp) / "work", golden=Path(tmp) / "golden"))
                    self.assertGreater(min(s for _, s in row.history if s is not None), flow.land_limit_s())
                    self.assertTrue(_shown(row, Path(tmp) / str(i))[0]["over_limit"])


def _block(lines: list[str], head: str) -> list[str]:
    if head not in lines:
        return []
    start = lines.index(head)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## row ")), len(lines))
    return lines[start:end]


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        ROWS_FILE.write_text(render(), encoding="utf-8", newline="\n")
        print(f"wrote {ROWS_FILE}")
    else:
        unittest.main()
