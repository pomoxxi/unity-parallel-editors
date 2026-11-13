"""The whole flow in DESIGN.md, one task at a time: claim, isolate, merge, check, council, land.

    start   claim the paths, make the worktree, copy the Library in, put the tool's package in
    land    merge onto a candidate, regenerate and replay, check, write the council brief
    verdict a council member's GO or NO-GO; the second one lands, refuses or asks the user
    decide  the user's call on a split council
    finish  remove a task's worktree (and its branch, once landed)

State lives next to the worktrees, in `<root>/.upe/`: the claims registry,
one JSON record per task, and per-task run records (merge report, check
results, council brief and verdicts, Editor logs) under `runs/<task-id>/`. Candidate
worktrees are `<root>/_merge/<task-id>`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import subprocess
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import agent_package
from .checks import (
    PROTECTED, CheckReport, TestRun, check_case_duplicates, check_protected, check_tool_namespace, class_of, confirm_failures,
    HOOK_PATTERN, LOAD_PATTERN, LOAD_SUFFIXES, META_GUID, OPEN_EXISTING_HOOK, OPEN_EXISTING_LOAD, OPEN_HOOK_BASE,
    FOLDER_ASSET, MetaTree, ScenePlan, findings_outside_branch, guid_index, run_counts,
    run_scene_check, run_tests, run_tests_by_classes, scene_checks, scene_plan, static_checks, unity_checks,
)
from .kept import BY_CLASSES, BY_FULL_RUN, BY_OWN, ClassRun, FullScenes, Kept, KeptStore, SceneStore, decide as known_failures
from .claims import Claim, ClaimError, ClaimRegistry, Entry, file_lock, lock_held, normalize, unclaimed, write_atomic
from .council import Ballot, CouncilError, Evidence, Verdict, brief, decide as count_votes, gather_diffs, render_decision
from .editor import Editor, find_editor, yaml_merge_for
from .merge import CANDIDATE_PREFIX, LOCK_FILE, MANIFEST, MergeError, Merger, MergeReport, Step, unity_yaml_merge, yaml_merge_flags
from . import identity as identities
from . import judge as judging
from .unity_cli import UnityCli, find_cli
from .workspace import BRANCH_PREFIX, Workspaces, WorkspaceError, lock_state
from .safegit import PATHSPEC_VARIABLES, SAFE_GIT, pathspec_free_env  # noqa: F401 (PATHSPEC_VARIABLES: still named here)
from .cmdline import ARGV_CAP, capped as _capped  # noqa: F401 (moved; `upe.flow` still names both)

Runner = Any

# Files every task may write: the ProjectSettings lists merge as a union, and the lock is regenerated.
DEFAULT_SHARED = (
    "ProjectSettings/TagManager.asset",
    "ProjectSettings/EditorBuildSettings.asset",
    "Packages/packages-lock.json",
)

STATES = ("working", "conflict", "checks-failed", "awaiting-council", "refused", "awaiting-user", "landed", "finished")
CANDIDATE_OWNER = "merge:"  # slot owner name of a task's merge candidate; also its judge session name
# Slot owner name of a land's run of tests on the target, detached at the target, apart from the candidate
# so it sees nothing the merge's run left. Like `merge:`, it cannot be a task id. Released when the run ends, and by
# `_discard_candidate`, which every way a land or a task ends calls.
BASE_OWNER = "base:"


# What to do next, for each state a task can show ---------------------------------------------------
#
# The one table of next steps: `status` gives it as `next`, the `start` error ends with it, and step 0 of
# the skill says "do what `next` says". Besides STATES, `status` shows three states no record holds:
# `starting` (a claim with no record yet), `unreadable` (a record that does not read) and `landing` (a
# `working` task whose last entry is a land's, so a land may still run; see Flow.land_in_flight).


@dataclass
class Land:
    """What is known of a land (or another command) that may still run on a task, for `next_step` and `status`."""
    running: bool | None  # the task's lock is held, or a new land job is starting; None: could not tell
    job: str = ""  # the job to read: the newest land job of the task that belongs to this land, or ""
    job_state: str = ""  # "started", "running", "done"; "" with no job, or no record of it
    exit_code: int | None = None  # when the job is done
    job_record: str = ""  # with a job but no state: "missing" (no file) or "unreadable"
    since: str = ""  # when the lock's holder started (UTC), when it is known; "" when it is not
    # Whether `since` is surely the holder's start: True for a land whose job runs (the newer of its job's start
    # and its newest "land started"); False for the newest "land started" of a land with no job, which a newer
    # command that holds the lock and has not yet written its own entry would not have written.
    since_sure: bool = True
    limit_s: int | None = None  # past this, a land that still runs is told to the user
    over: bool = False  # it has run longer than `limit_s`, counted from `since`
    log: str = ""  # the job's log, when there is one
    later_job: str = ""  # a land job of the task made after this land started, which ended before writing its entry
    later_job_state: str = ""
    later_exit_code: int | None = None
    # `starting` only: the claim was made by an older version, with no mark, so nothing tells whether its
    # `start` still runs (`running` is None) and the tool never frees it.
    unmarked: bool = False

    def to_json(self) -> dict:
        later = ({"later_job": {"job": self.later_job, "job_state": self.later_job_state or None,
                                **({"exit_code": self.later_exit_code} if self.later_exit_code is not None else {})}}
                 if self.later_job else {})
        return {"running": self.running, "job": self.job or None, "job_state": self.job_state or None,
                **({"exit_code": self.exit_code} if self.exit_code is not None else {}),
                **({"job_record": self.job_record} if self.job_record else {}),
                **({"since": self.since} if self.since else {}),
                **({"over_limit": self.over} if self.running and self.limit_s and self.since else {}),
                **({"log": self.log} if self.log else {}), **later}


@dataclass
class Situation:
    """What `status` shows for a task: the record it judged, the state, what is known of a land, the next step."""
    task: "Task"
    state: str
    land: Land | None
    step: str


UNKNOWN_STEP = "The tool has no next step for this state: tell the user"

# A `--detach` job counts as a land in flight without the task's lock only this long after `detach` made it:
# its child takes the lock within seconds of its start (Python's start, the config, the job record, at most
# the 2 s wait of `_task_lock`), even on a machine busy with Editors at below-normal priority. After that, a
# land holds the lock for its whole run, so a free lock is the answer, and a `running` record whose pid lives
# is a pid used again by another process (a reboot, a lost machine), not a land: it is not waited on.
JOB_START_GRACE_S = 120
# A job id is made from the clock: one that reads more than this in the future was made before the clock was set
# back, so its age, and whether it is new, is not known. It is not counted; the lock decides.
JOB_CLOCK_SKEW_S = 5
# Past `Flow.land_limit_s`, a land that still holds the lock is told to the user (with its log). It is the caps of
# the test steps of one try: a wait on another land's run of the same baseline (`_test_baseline`, `test_timeout_s`
# + 900 s), then two full EditMode runs (on the merge, and on the target), each capped by `run_tests` at
# (`test_retries` + 1) x `test_timeout_s` + 300 s, then three runs with no retry, each capped at `test_timeout_s` +
# 300 s (the re-run of the merge's failed tests the CLI did not retry, the class-only run on the target, and
# the re-run after the target's full run; the longest path is the class-only run that sends the land to the target's
# full run); plus this, a judgment and not a cap, for the rest (the scene lock is not in it: a slot has one
# owner, so no land waits on it). The rest
# has caps of its own that are not added: each Editor batch run (the scene checks on the target and on the merge, each
# tried up to twice, `refresh`, each replay) may take up to 3600 s, so a try whose other runs all hit their caps
# runs past the limit; then the line says "tell the user, and keep waiting", which is still the right act. Runs split
# by length are not counted either, for the same reason and one more: a scene check or a class list longer than one
# command line runs in groups, one Editor or one `unity test` a group, and how many groups a land has is known only once
# it reads its merge, after the limit is fixed. On a test project of about 17,000 tracked files a scene check splits
# only when a land changes more than about 300 scenes and prefabs, and its whole test suite (about 150 test files) is
# one class list. It is counted from the newest "land started", which each try writes again (with a job, the newer of
# that and the job's start).
LAND_SLACK_S = 1800
TEST_RUN_SLACK_S = 300  # what `run_tests` gives a run past its tries' `test_timeout_s` (checks.py)
BASELINE_WAIT_SLACK_S = 900  # what `_test_baseline` waits past `test_timeout_s` for another land's baseline run
# How long a land waits on the lock of a full scene check of its target commit in its slot, counted from its
# own call to the lock: the Editor run's cap (`Editor.batch`, 3600 s) and the slack the tests' wait has. Past it, exit 2.
# A guard only: the lock is per slot, a slot has one owner, so no land waits on it, and the wait is
# not in the land's bound.
SCENE_WAIT_S = 3600 + BASELINE_WAIT_SLACK_S
BUSY_TELL_AFTER = "an hour"  # a `verdict`, `decide` or `finish` holds the lock for minutes
# A `start` holds the task's lock from before its claim to its end, so the lock says whether it runs.
# Past this, counted from its claim's `since`, a start that still runs is told to the user. A judgment: the Library
# copy has no cap of its own, and the Unity CLI call of `install` has 600 s; a start takes minutes.
START_LIMIT_S = 3600
LATER_JOB_WAIT = "10 minutes"  # a later land job whose record still says started: then its log is read
OLD_CLAIM_TELL_S = 1800  # a claim made by an older version (no mark) is told to the user past this
# `finish` does not take away the slot of a dead start while anything in it changed this recently: a killed start's
# robocopy runs on (Windows does not end the children of a killed process). The time can only refuse, never free.
SLOT_QUIET_S = 120
LOCK_TRIES = 3  # a lock read as held is tried again this many times in all, LOCK_TRY_GAP_S apart (see task_lock_held)
LOCK_TRY_GAP_S = 0.05

# The "land started" entry, pinned: `_land` writes it with `land_started_note`, `land_job` reads it back.
LAND_STARTED = "working: land started"
LAND_AGAIN = "working: target moved; landing again"  # the retry's entry, between two "land started"
_LAND_JOB = re.compile(r"^working: land started \(job ([A-Za-z0-9-]+)\)")


# The end of the "checks-failed" entry `_land` writes when every finding is a static one and none is in a path the
# branch changed (`checks.findings_outside_branch`). Pinned: `situation_of` reads it back, so the state and this verdict
# come from one write and cannot disagree; an older record has no such end and reads as before.
NOT_IN_BRANCH = "; none of them is in a path the branch changed"
# The most paths one printed git step names; `finish` says to run it again for the rest.
STEP_PATHS = 25


def land_started_note(job_id: str = "") -> str:
    """The note of the "land started" entry; with the land's job id when it runs as a job."""
    return f"land started{f' (job {job_id})' if job_id else ''}; any earlier merge and its votes no longer count"


def is_land_entry(entry: str) -> bool:
    """An entry a land writes before its result: a land that ends with a result always writes one after it."""
    return entry.startswith(LAND_STARTED) or entry.startswith(LAND_AGAIN)


def land_job(history: Sequence[str]) -> str:
    """The job id in the newest "land started" entry, or "" (a land with no job, or an entry from before ids)."""
    for entry in reversed(history):
        if entry.startswith(LAND_STARTED):
            found = _LAND_JOB.match(entry)
            return found.group(1) if found else ""
    return ""


def next_step(task_id: str, state: str, land: Land | None = None, *, target: str = "", outside_branch: bool = False,
              moved_to: str = "") -> str:
    """What an agent picking up `task_id` does next, in `state` (one of STATES, or `starting`, `unreadable`,
    `landing`). `land` says whether a land runs, for `landing`, and for any other state whether a command
    works on the task now (`running` true: a `land` in its first seconds, a `verdict`, `decide` or `finish`);
    `target` names the target, for `conflict`. For `starting`, `land` is what is known of the `start` (see
    `Flow.start_facts`); `left-behind` is a slot or branch of the task with no claim and no record.
    `outside_branch`, for `checks-failed`: the land's entry says no finding is in a path the branch
    changed (NOT_IN_BRANCH), so the step says to tell the user, not to fix. `moved_to`, for `awaiting-council`: the
    target's tip, when the target moved since the merge under review so that merge can no longer land (`moved_since`)."""
    if state == "landing":
        return _landing_step(task_id, land or Land(running=None))
    if state == "starting":
        return _starting_step(task_id, land or Land(running=None))
    if land is not None and land.running:
        return _busy_step(task_id, land)
    if land is not None and land.running is None and state not in ("finished", "starting", "unreadable"):
        return (f"The tool could not tell whether a upe command works on {task_id} now (its lock could not be "
                f"tried): commit nothing, and ask `upe status {task_id}` again; if it says the same, tell the user")
    steps = {
        "left-behind": f"No `start` of {task_id} runs, and it has no claim and no record, but an earlier start of it "
                       f"left its slot or its branch `upe/{task_id}` behind: run `upe finish {task_id}`, which takes "
                       f"them away if they hold no work (if it refuses, tell the user what it says), then `upe start "
                       f"{task_id}` again with your brief and claim",
        "unreadable": "Its record does not read, so the tool cannot say where it is: tell the user",
        "working": "To go on with it, work in its slot (its history names the worktree), commit, and land it "
                   "when it is done",
        "conflict": f"The merge stopped on a conflict. To resolve it, run `git merge {target or '<target>'}` in its "
                    "slot, keep both sides' intent, commit, and land again. If you cannot tell what the other "
                    "side meant, tell the user",
        "checks-failed": f"The checks failed: read `checks.md` in its run folder (`upe status {task_id}` names it), "
                         "fix in its slot, commit and land it again",
        "awaiting-council": f"The council is judging it: commit nothing more, and ask `upe status {task_id}` "
                            f"again later; if you are not sure the user knows the council for {task_id} is "
                            "waiting, tell them",
        "awaiting-user": "The user decides on it: tell the user, and wait",
        "refused": f"The council refused it: read `council.md` in its run folder, then fix it, commit and land it "
                   f"again, or give it up with `upe finish {task_id}`, which frees its claim",
        "landed": f"It is done, and its slot is kept until it is finished: run `upe finish {task_id}`, "
                  "then pick a new task id for new work",
        "finished": "It is done: for new work, pick a new task id",
    }
    if state == "awaiting-council" and moved_to:
        # The council's votes are on a merge that can no longer land, so every minute spent waiting on them is lost.
        return (f"{target or 'The target'} moved since this merge (it is at {moved_to[:12]} now), so no vote can "
                f"land it: land it again now (`upe land {task_id}`, with --detach), which makes a new merge with its "
                f"own checks and council brief. Tell the user that the council on the old merge can stop")
    if state == "checks-failed" and outside_branch:
        return (f"The checks failed, but none of the findings is in a path this branch changed (they read like the "
                f"tool's or the slot's doing, not your work): do not fix them. Read `checks.md` in its run folder "
                f"(`upe status {task_id}` names it) and tell the user. If a finding does name a path you changed, "
                "it is yours: fix it in its slot, commit and land it again")
    return steps.get(state, UNKNOWN_STEP)


def _hours(seconds: int) -> str:
    minutes = round(seconds / 60)
    hours, rest = divmod(minutes, 60)
    if not rest:
        return f"{hours} hour{'' if hours == 1 else 's'}"
    return f"{hours} h {rest} min" if hours else f"{rest} minutes"


def _wait_on(task_id: str, land: Land, what: str) -> str:
    """The wait line: on the job when one runs, else on the lock's holder; with the time bound, counted from the
    holder's start where it is known, and from the agent's own first ask where it is not."""
    if land.job and land.job_state in ("started", "running"):
        line = (f"{what} is running (job {land.job}): commit nothing; wait on it with `upe job {land.job} --wait`, "
                f"then ask `upe status {task_id}` again")
    else:
        line = (f"{what}, or another upe command on {task_id}, is running, with no job to wait on: commit nothing, "
                f"and ask `upe status {task_id}` again in a few minutes")
    if not land.limit_s:
        return line
    limit = _hours(land.limit_s)
    log = (f"; its log is {land.log}" if land.log
           else "; with no job, its output is in the terminal that runs it" if not land.job else "")
    if not land.since:
        return (f"{line}. The tool does not know when it started: if `upe status {task_id}` still says this {limit} "
                f"after you first asked, tell the user{log}")
    if land.over and not land.since_sure:
        return (f"{line}. Its newest \"land started\" was over {limit} ago (at {land.since}), but a newer command "
                f"may hold the lock: ask `upe status {task_id}` again in 10 minutes, and if it still says it is "
                f"over, tell the user, and keep waiting{log}")
    if land.over:
        return f"{line}. It has run over {limit}, since {land.since}: tell the user, and keep waiting{log}"
    return f"{line}. If it runs over {limit}, since {land.since}, tell the user{log}"


def _busy_step(task_id: str, land: Land) -> str:
    if land.job:
        return _wait_on(task_id, land, "A land of it, which has not yet written its first entry,")
    return (f"A upe command works on {task_id} now (a land in its first seconds, or a `verdict`, `decide` or "
            f"`finish`): commit nothing, and ask `upe status {task_id}` again in a few minutes. The tool does not "
            f"know when it started: if it says the same for over {BUSY_TELL_AFTER} after you first asked, tell the user")


def _starting_step(task_id: str, land: Land) -> str:
    """A task being started: a claim with no record, or a `start --detach` job not yet at its claim."""
    if land.unmarked:
        when = (f"it was claimed over {_hours(OLD_CLAIM_TELL_S)} ago (at {land.since}): tell the user" if land.over
                else f"if `claimed_since` is more than {_hours(OLD_CLAIM_TELL_S)} ago and it still says this, tell the user")
        return (f"The claim of {task_id} was made by an older version of the tool, which leaves no mark, so the tool "
                f"cannot tell whether its `start` still runs, and `upe finish {task_id}` does not free it: do not start "
                f"{task_id} again, and ask `upe status {task_id}` again in a few minutes; {when}, who can free it by "
                f"hand once no `upe start` of it runs")
    if land.running is None:
        return (f"The tool could not tell whether a `start` of {task_id} still runs (its lock could not be tried): do "
                f"not start {task_id} again, and ask `upe status {task_id}` again; if it says the same, tell the user")
    if land.running is False:
        return (f"The `start` of {task_id} died before it wrote its record, and nothing runs it now: run `upe finish "
                f"{task_id}`, which frees its claim, its slot and its branch if they hold no work, then `upe start "
                f"{task_id}` again with your brief and claim. If `finish` refuses, do what its message says (to run it "
                "again in a few minutes, when something still writes in the slot); otherwise tell the user what it says")
    if land.job:  # `start --detach` whose child has not claimed yet: counted only for JOB_START_GRACE_S
        return (f"A `start` of {task_id} is beginning (job {land.job}) and has not claimed its paths yet: do not start "
                f"{task_id} again; wait on it with `upe job {land.job} --wait`, then ask `upe status {task_id}` again")
    line = (f"A `start` of {task_id} (or a `finish` clearing a start of it that died) is working on it now, which can "
            f"take minutes, as when it copies the Library: do not start {task_id} again, and ask `upe status {task_id}` "
            "again in a few minutes")
    limit = _hours(land.limit_s or START_LIMIT_S)
    if not land.since:
        return (f"{line}. The tool does not know when it started: if `upe status {task_id}` still says this {limit} "
                "after you first asked, tell the user")
    if land.over:
        return (f"{line}. It has run over {limit}, since {land.since}: tell the user, and keep waiting. To stop it, the "
                f"user ends that `upe start` process (with --detach, the pid in its job record, which `upe job <id>` "
                f"shows); `upe status {task_id}` then says what to run")
    return f"{line}. If it runs over {limit}, since {land.since}, tell the user"


def start_fields(claim: Claim | None, land: Land) -> dict:
    """What `status` gives of a task being started, beside its state and `next` (`Land.to_json` is a land's)."""
    return {**({"claimed_since": claim.since} if claim is not None else {}), "running": land.running,
            **({"over_limit": land.over} if land.running and land.since else {}),
            **({"job": land.job, "job_state": land.job_state or None} if land.job else {})}


def _landing_step(task_id: str, land: Land) -> str:
    if land.running:
        return _wait_on(task_id, land, "A land")
    if land.running is None:
        return (f"The tool could not tell whether a land of {task_id} still runs (its lock could not be tried): "
                f"commit nothing, and ask `upe status {task_id}` again; if it says the same, tell the user")
    then = "Then go on in its slot and land it again, or tell the user if you cannot tell why it ended"
    later = ""
    detach = "--detach so its log keeps the reason; if it ends without a result again, tell the user"
    again = f"Go on in its slot and land it again, with {detach}"
    if land.later_job and land.later_job_state in ("started", "running"):
        # Not done: it may still be starting (made before the clock was set back, so not counted as starting),
        # or its pid is now another program's. Either way it has not stopped yet, as far as its record says.
        later = (f" A later land, job {land.later_job}, has not written its entry, and its record still says "
                 f"{land.later_job_state}: it may still be starting (as when the clock was set back after it was "
                 f"made), or its process id is now another program's. Ask `upe status {task_id}` again in a minute; "
                 f"if it says the same, read its log with `upe job {land.later_job}`, without --wait.")
        # Ask again first; land only once that land is known to have ended, or, since its
        # record may say `started` for ever (its pid now another program's), once it has said so for LATER_JOB_WAIT,
        # counted from the agent's own first ask (the job's time may be from before the clock was set back).
        wait = (f"Do not land it again yet: only once `upe status {task_id}` says that land has ended too, and names "
                f"no later land still starting, go on in its slot and land it again. If it still names job "
                f"{land.later_job} as {land.later_job_state} {LATER_JOB_WAIT} after you first asked, read that job's "
                f"log (`upe job {land.later_job}`, without --wait): if it says why that land stopped, or shows it never "
                "took the lock, go on in its slot and land it again")
        then = f"{wait}, or tell the user if you cannot tell why it ended"
        # The no-job form says "or tell the user" too, as `then` does; `detach` is of the next land.
        again = f"{wait}, with --detach so its log keeps the reason, or tell the user if you cannot tell why it ended. " \
                "If that land ends without a result again, tell the user"
    elif land.later_job:
        how = (f"exit {land.later_exit_code}" if land.later_exit_code is not None
               else land.later_job_state or "its record does not read")
        later = (f" A later land, job {land.later_job} ({how}), stopped before it wrote its entry: `upe job "
                 f"{land.later_job}` says why (a refusal because the task was locked may have been this land holding the lock, or another upe "
                 f"command, a `finish` say, that held it after this land ended).")
    if land.job and land.job_state == "done":
        exit_code = "" if land.exit_code is None else f", exit {land.exit_code}"
        return (f"The land ended without a result (job {land.job}{exit_code}): read why in its `log_tail` "
                f"(`upe job {land.job}`); exit 2 is a refusal, and its reason is there.{later} {then}")
    if land.job and land.job_state:
        return (f"The land ended without a result: no process holds its lock, though the record of its job "
                f"{land.job} still says {land.job_state} (its process id is now another program's, or the job is "
                f"writing its end at this moment). Read its log with `upe job {land.job}`, without --wait: that "
                f"record may never say done.{later} {then}")
    if land.job:
        what = "is missing" if land.job_record == "missing" else "does not read"
        return (f"The land ended without a result, and the record of its job {land.job} {what}, so the "
                f"reason is not known.{later} {then}")
    # The land has no log of its own, but a later land job named beside it has one.
    first = (f" Read that job's log first (`upe job {land.later_job}`)."
             if land.later_job and land.later_job_state not in ("started", "running") else "")
    return ("The land ended without a result, and it had no job, so no log of its own: its output was in the terminal "
            f"that ran it.{later}{first} {again}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _job_time(job_id: str) -> datetime | None:
    """When `detach` made the job: its id ends with the UTC time, `<command>-<task>-YYYYMMDDTHHMMSS`, and `-<n>`
    for the n-th job of one command and task made in the same second."""
    found = re.search(r"(\d{8}T\d{6})(?:-\d+)?$", job_id or "")
    if not found:
        return None
    try:
        return datetime.strptime(found.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _stamp_time(stamp: object) -> datetime | None:
    try:
        when = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
    except ValueError:
        return None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def _entry_times(task: "Task") -> list[datetime | None]:
    """The time of each history entry, matched from the end as `Task.dated_history` matches them; None if none."""
    stamps = task.stamps[-len(task.history):] if task.history else []
    missing = len(task.history) - len(stamps)
    return [None] * missing + [_stamp_time(s) for s in stamps]


def process_alive(pid: int, created: int | None = None) -> bool:
    """Whether a process with this id runs. Windows gives a dead process's id to new ones, so with `created` (from
    `process_created`, written with the pid) a live process that started at another time is not it. Without
    `created` (a record written before it was kept, or off Windows) a reused id reads as alive."""
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    try:
        if not (kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)) and code.value == 259):  # STILL_ACTIVE
            return False
        return not isinstance(created, int) or _created_of(kernel32, handle) in (None, created)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def process_created(pid: int) -> int | None:
    """When the process `pid` started (a Windows FILETIME, 100 ns since 1601), or None off Windows or
    when it cannot be read. A job record keeps it beside the pid, so `process_alive` can tell a reused id."""
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        return _created_of(kernel32, handle)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _created_of(kernel32, handle) -> int | None:
    import ctypes

    created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
    if not kernel32.GetProcessTimes(ctypes.c_void_p(handle), ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
        return None  # unreadable: the pid alone decides, as before
    return created.value


class FlowError(Exception):
    pass


class TargetMoved(FlowError):
    """The target moved during a Unity run of a land. Nothing landed, and the land can simply start again."""


LAND_TRIES = 3  # a land whose target moves under it starts again on the new target, this many times in all


@dataclass
class Config:
    repo: Path  # the project's repository (its main checkout)
    root: Path  # task worktrees, candidates and the tool's state go here
    golden: Path  # a checkout whose Library is fully imported
    target: str = "main"  # the branch tasks land on
    builders: dict[str, str] = field(default_factory=dict)  # project path -> builder method
    shared: tuple[str, ...] = DEFAULT_SHARED
    local_copies: tuple[str, ...] = ()  # git-ignored paths copied from repo into every worktree
    # The project's test folders, relative to `repo`; `start` names them to the agent. None named:
    # `start` prints the command that lists the files of the folders named Test or Tests in the slot.
    test_folders: tuple[str, ...] = ()
    unity_cli: Path | None = None
    unity_editor: Path | None = None
    yaml_merge: Path | None = None  # else the one beside the Editor
    job_workers: int | None = 4
    council_effort: str = "medium"  # the effort council members are started at; `land` says so (decision 12)
    # The CPU priority of everything the tool runs: "below_normal" gives way to the user's own programs.
    editor_priority: str = "below_normal"
    refresh_golden_after_land: bool = True  # ~20 s plus the changed files' import per land (research/05)
    max_high: int = 1  # sessions that may be high at once (research/05)
    memory_short_mb: int = 4096  # free memory below this is short: one Editor's import peak (research/05)
    judge_every_s: float = 60.0
    # Changes the council may not pass: agent instructions, hooks, tooling (checks.PROTECTED plus these).
    protected_paths: tuple[str, ...] = PROTECTED
    # Refuse to land on a branch someone has checked out in a worktree the tool does not own.
    protect_checked_out_target: bool = True
    # How long one full EditMode run may take. It took ~15 minutes alone on a slow machine, and
    # over 30 with two lands at once at BELOW_NORMAL.
    test_timeout_s: int = 3600
    # Extra attempts for tests that fail. A project may have tests with time limits, which fail when the
    # machine is busy (research/05). A test that passes on a retry does not stop a land; the council sees it.
    test_retries: int = 1

    @classmethod
    def load(cls, path: Path | str) -> "Config":
        """Read `upe.toml`. Relative paths are relative to the file."""
        path = Path(path)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        here = path.parent

        def p(key: str) -> Path | None:
            return (here / data[key]).resolve() if data.get(key) else None

        missing = [k for k in ("repo", "root", "golden") if not data.get(k)]
        if missing:
            raise FlowError(f"{path}: missing {', '.join(missing)}")
        for place in ("repo", "root", "golden"):
            if p(place) and _under(path.resolve(), p(place)):
                raise FlowError(f"{path} is inside {place}; the config must live where agents' branches cannot write it")
        editor_priority = data.get("editor_priority", "below_normal")
        if editor_priority not in judging.BASES:
            raise FlowError(f"{path}: editor_priority must be one of {', '.join(judging.BASES)}")
        return cls(
            repo=p("repo"), root=p("root"), golden=p("golden"),
            target=data.get("target", "main"),
            builders=dict(data.get("builders", {})),
            shared=tuple(data.get("shared", DEFAULT_SHARED)),
            local_copies=_strings(path, data, "local_copies"),
            test_folders=_project_paths(path, data, "test_folders"),
            unity_cli=p("unity_cli"), unity_editor=p("unity_editor"),
            yaml_merge=p("yaml_merge"),
            job_workers=data.get("job_workers", 4),
            council_effort=data.get("council_effort", "medium"),
            editor_priority=editor_priority,
            refresh_golden_after_land=bool(data.get("refresh_golden_after_land", True)),
            max_high=int(data.get("max_high", 1)),
            memory_short_mb=int(data.get("memory_short_mb", 4096)),
            judge_every_s=float(data.get("judge_every_s", 60.0)),
            protected_paths=PROTECTED + tuple(data.get("protected_paths", ())),
            protect_checked_out_target=_flag(path, data, "protect_checked_out_target", True),
            test_timeout_s=int(data.get("test_timeout_s", 3600)),
            test_retries=int(data.get("test_retries", 1)),
        )


@dataclass
class Task:
    task_id: str
    brief: str
    claim: list[str]
    base: str  # the commit the task started from
    target: str
    state: str = "working"
    landed_commit: str = ""
    history: list[str] = field(default_factory=list)
    stamps: list[str] = field(default_factory=list)  # when each history entry was written (UTC); older records have none

    def note(self, text: str) -> None:
        self.history.append(text)
        self.stamps.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def dated_history(self) -> list[str]:
        """History entries with their times, matched from the end; entries written before times were kept have none."""
        stamps = self.stamps[-len(self.history):] if self.history and isinstance(self.stamps, list) else []
        missing = len(self.history) - len(stamps)
        return [(f"{stamps[i - missing]} " if i >= missing else "") + f"{entry}" for i, entry in enumerate(self.history)]

    def well_formed(self) -> bool:
        """Each field of the kind the tool writes: a record that is not is unreadable, not a crash later."""
        def strings(value) -> bool:
            return isinstance(value, list) and all(isinstance(v, str) for v in value)

        return (all(isinstance(v, str) for v in (self.task_id, self.brief, self.base, self.target, self.state, self.landed_commit))
                and strings(self.claim) and strings(self.history) and strings(self.stamps)
                and len(self.stamps) <= len(self.history))


@dataclass
class Outcome:
    task_id: str
    state: str
    message: str


@dataclass
class Dirt:
    """A slot's uncommitted changes: `Flow._slot_dirt`."""
    tracked: list[str]  # `git diff --name-only HEAD` and `--cached`: changed or deleted, staged or not, a staged new file
    untracked: list[str]  # `ls-files --others --exclude-standard`
    inside: list[str]  # of both, those the task's claim covers (or that the claim cannot be judged on)
    outside: list[str]  # the rest: outside the claim, whoever made them (the tool cannot tell the task's from Unity's)
    # A nested repository (untracked, listed as `dir/`) or a submodule (a gitlink in the index or HEAD):
    # `git clean -f` and `git restore` leave it as it is, so the tool prints no step for it.
    stuck: list[str] = field(default_factory=list)
    shared: list[str] = field(default_factory=list)  # of `inside`, those only `shared` covers


@dataclass
class TargetTests:
    """What a land knows of the target's tests before the merge: its commit, its own kept full
    result (case 1, or case 3's full run before the merge), else the nearest ancestor's (case 2), else neither."""
    commit: str
    own: Kept | None = None
    ancestor: tuple[str, Kept, int] | None = None  # (commit, result, commits before the target)
    notes: list[str] = field(default_factory=list)  # for the council, from the full run before the merge
    ran: bool = False  # `own` comes from this land's full run on the target (case 3), not from a kept file


class Flow:
    def __init__(
        self,
        config: Config,
        *,
        cli: UnityCli | None = None,
        editor: Editor | None = None,
        workspaces: Workspaces | None = None,
        merger: Merger | None = None,
        package_source: Path = agent_package.PACKAGE_SOURCE,
        runner: Runner = subprocess.run,
        identity: Callable[[], dict | None] | None = None,
        progress: Callable[[str], None] | None = None,
        job_id: str = "",
    ) -> None:
        self.config = config
        self.job_id = job_id  # the `--detach` job this process runs as, if any; `land` writes it in "land started"
        self._say = progress or (lambda message: None)  # a line per step of a long command, for the job log
        self._identity = identity or identities.agent_process
        self.state_dir = config.root / ".upe"
        self.registry = ClaimRegistry(self.state_dir / "claims.json")
        self.workspaces = workspaces or Workspaces(config.repo, config.root, config.golden, runner=runner, local_copies=config.local_copies)
        self.merger = merger or Merger(
            config.repo,
            yaml_merger=unity_yaml_merge(self._yaml_merge_exe, yaml_merge_flags(config.repo)),
            builders=config.builders,
            runner=runner,
        )
        self._cli = cli
        self._editor = editor
        self.package_source = package_source
        self._runner = runner

    # Lazily made, so `status`, `job` and the like work without Unity; `start` needs the CLI to put the
    # tool's package into the slot.

    @property
    def cli(self) -> UnityCli:
        if self._cli is None:
            self._cli = UnityCli(self.config.unity_cli or find_cli())
            self._cli.check_version()
        return self._cli

    def _yaml_merge_exe(self) -> Path:
        """The UnityYAMLMerge named in upe.toml, else the one that ships with the Editor in use."""
        return self.config.yaml_merge or yaml_merge_for(self.config.unity_editor or find_editor(self.config.repo))

    @property
    def editor(self) -> Editor:
        if self._editor is None:
            exe = self.config.unity_editor or find_editor(self.config.repo)
            self._editor = Editor(exe, job_workers=self.config.job_workers)
        return self._editor

    # Records -------------------------------------------------------------

    def _task_path(self, task_id: str) -> Path:
        self.workspaces.check_id(task_id)
        return self.state_dir / "tasks" / f"{task_id}.json"

    def run_dir(self, task_id: str) -> Path:
        self.workspaces.check_id(task_id)
        return self.state_dir / "runs" / task_id

    def _task_lock(self, task_id: str):
        """One `land`, `verdict`, `decide` or `finish` per task at a time; the OS drops it if the holder dies."""
        self.workspaces.check_id(task_id)
        return file_lock(self.state_dir / "locks" / f"{task_id}.lock", FlowError, timeout=2.0,
                         what=f"task {task_id} (another upe command is working on it)")

    def task_lock_held(self, task_id: str) -> bool | None:
        """Whether a `land`, `verdict`, `decide` or `finish` holds the task's lock now; None if it cannot be tried.

        Tried without waiting on the holder, and with no file made: a missing lock file is one no process holds.
        A probe holds a free lock for an instant, so two `status` probes at once may each read the other's as
        held: a lock read as held is tried again, LOCK_TRIES times in all, LOCK_TRY_GAP_S apart, and counts as held
        only when every try says so. A real holder holds it far longer than those 0.1 s.
        """
        self.workspaces.check_id(task_id)
        path = self.state_dir / "locks" / f"{task_id}.lock"
        try:
            for attempt in range(LOCK_TRIES):
                if not lock_held(path):
                    return False
                if attempt + 1 < LOCK_TRIES:
                    time.sleep(LOCK_TRY_GAP_S)
            return True
        except OSError:
            return None

    # Jobs (`--detach`): __main__ writes their records; these read them -------

    def job_path(self, job_id: str) -> Path:
        if not job_id or not all(c.isalnum() or c in "-T" for c in job_id):
            raise FlowError(f"{job_id!r} is not a job id")
        return self.state_dir / "jobs" / f"{job_id}.json"

    def job_read(self, job_id: str) -> dict:
        path = self.job_path(job_id)
        if not path.exists():
            raise FlowError(f"no job {job_id}")
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise FlowError(f"job {job_id}: its record does not read")
        return record

    def job_state(self, job_id: str) -> dict:
        """The job's record; one whose process is gone without saying how it ended counts as done, with exit 3."""
        record = self.job_read(job_id)
        pid = record.get("pid")
        if record.get("state") == "done" or not isinstance(pid, int) or not pid or process_alive(pid, record.get("created")):
            return record
        record = self.job_read(job_id)  # it may have finished between the two looks
        if record.get("state") == "done":
            return record
        return {**record, "state": "done", "exit_code": 3, "error": "the job's process ended without recording a result"}

    def _job_or_none(self, job_id: str) -> dict | None:
        """The job's state, or None when there is no such job or its record does not read."""
        try:
            record = self.job_state(job_id)
        except (FlowError, OSError, ValueError, TypeError):
            return None
        return record if isinstance(record, dict) else None

    def _land_jobs(self, task_id: str) -> list[str]:
        """This task's land jobs (`land-<task>-<time>`, the ids `detach` makes), newest first: the time is UTC."""
        return self._jobs("land", task_id)

    def _jobs(self, command: str, task_id: str) -> list[str]:
        """This task's jobs of one command (`<command>-<task>-<time>`), newest first."""
        folder = self.state_dir / "jobs"
        # Not `land-dash-2-<time>`, dash-2's; `-<n>` is a second job made in the same second (see `detach`).
        name = re.compile(rf"^{command}-{re.escape(task_id)}-(\d{{8}}T\d{{6}})(?:-(\d+))?$")
        paths = folder.glob(f"{command}-{task_id}-*.json") if folder.is_dir() else []
        found = [(m.group(1), int(m.group(2) or 1), p.stem) for p in paths if (m := name.match(p.stem))]
        return [stem for *_, stem in sorted(found, reverse=True)]

    def _starting_jobs(self, task_id: str, command: str = "land") -> list[dict]:
        """The task's jobs of `command` that may be one not yet holding the lock: made at most JOB_START_GRACE_S ago
        (and not in the future, past JOB_CLOCK_SKEW_S), `started` or `running`, with a live process. Newest first.

        Whether such a job was made before or after the newest land entry does not matter: inside the grace a
        live job not yet `done` may still take the lock, and a job refused by the lock is `done` within seconds.
        A `start` job is read the same way: its child takes the lock, then claims, within seconds."""
        now, found = _now(), []
        for job_id in self._jobs(command, task_id):
            made = _job_time(job_id)
            if made is None or not -JOB_CLOCK_SKEW_S <= (now - made).total_seconds() <= JOB_START_GRACE_S:
                continue
            record = self._job_or_none(job_id)
            pid = record.get("pid") if record else None
            if (record and record.get("state") in ("started", "running") and isinstance(pid, int) and pid
                    and process_alive(pid, record.get("created"))):
                found.append({**record, "job": job_id})
        return found

    def _job_facts(self, job_id: str, record: dict | None) -> dict:
        if not job_id:
            return {}
        if record is None:
            try:
                missing = not self.job_path(job_id).exists()
            except FlowError:
                missing = True
            return {"job": job_id, "job_record": "missing" if missing else "unreadable"}
        exit_code = record.get("exit_code")
        return {"job": job_id, "job_state": str(record.get("state", "")),
                "exit_code": exit_code if isinstance(exit_code, int) else None,
                "log": record["log"] if isinstance(record.get("log"), str) else ""}

    def land_limit_s(self) -> int:
        """How long one try of a land may run before `next` says to tell the user (see LAND_SLACK_S)."""
        timeout = self.config.test_timeout_s
        test_run = (max(0, self.config.test_retries) + 1) * timeout + TEST_RUN_SLACK_S
        one_try = timeout + TEST_RUN_SLACK_S  # a run with no retry
        return (timeout + BASELINE_WAIT_SLACK_S) + 2 * test_run + 3 * one_try + LAND_SLACK_S

    def _bounded(self, land: Land, since: datetime | None, *, sure: bool = True, limit_s: int | None = None) -> Land:
        """`land` with its limit (a land's, unless `limit_s`), and its start when known. A start in the future (the
        clock set back since) is not one the agent or the tool can count from: it is left out, as one not known."""
        land.limit_s = limit_s or self.land_limit_s()
        if since is not None and (_now() - since).total_seconds() >= -JOB_CLOCK_SKEW_S:
            land.since = since.isoformat(timespec="seconds")
            land.since_sure = sure
            land.over = (_now() - since).total_seconds() > land.limit_s
        return land

    # What `status` shows, and what to do next ----------------------------

    def land_in_flight(self, task: Task) -> Land | None:
        """For a `working` task whose last entry is a land's ("land started", or the retry's), what is known of
        that land; None for any other task. A land that ends with a result writes an entry after these.

        The lock decides: a land holds it for its whole run, and the OS drops it when its holder dies. Without
        it, only a land job still starting counts (see `_starting_jobs`). Reads only: no lock file and no job
        record is written.
        """
        if task.state != "working" or not task.history or not is_land_entry(task.history[-1]):
            return None
        times = _entry_times(task)
        started = next((i for i in range(len(task.history) - 1, -1, -1) if task.history[i].startswith(LAND_STARTED)), None)
        started_at = times[started] if started is not None else None
        entry_job = land_job(task.history)
        held = self.task_lock_held(task.task_id)
        starting = self._starting_jobs(task.task_id)
        if starting:
            record = next((r for r in starting if r["job"] == entry_job), starting[0])
            land = Land(running=True, **self._job_facts(record["job"], record))
            return self._bounded(land, _job_time(record["job"]))
        if held:
            land = Land(running=True, **self._job_facts(entry_job, self._job_or_none(entry_job) if entry_job else None))
            if entry_job and land.job_state not in ("started", "running"):
                # The lock's holder is not the land of the entry, whose job is over: a land not yet at its entry,
                # or a `verdict`, `decide` or `finish`. Its start and its log are not known.
                land.log = ""
                return self._bounded(land, None)
            if entry_job:  # the job's land holds the lock: its newest try started at the newer of these
                now = _now()
                return self._bounded(land, max((t for t in (_job_time(entry_job), started_at)
                                                if t and (now - t).total_seconds() >= -JOB_CLOCK_SKEW_S), default=None))
            # No job: the holder is the land of the entry, or a newer command not yet at its own entry.
            return self._bounded(land, started_at, sure=False)
        # The land of the entry comes first, with its job or with none; a later land job that ended before its
        # entry (refused, by the lock or otherwise) is named beside it, never in its place.
        later = self._later_land_job(task.task_id, entry_job, started_at)
        land = Land(running=held, **self._job_facts(entry_job, self._job_or_none(entry_job) if entry_job else None))
        if later:
            facts = self._job_facts(later, self._job_or_none(later))
            land.later_job, land.later_job_state, land.later_exit_code = later, facts.get("job_state", ""), facts.get("exit_code")
        return land

    def _later_land_job(self, task_id: str, entry_job: str, started_at: datetime | None) -> str:
        """For a land that ended: the newest land job of the task made after the entry's land started (a land that
        ended before it wrote its entry: refused, or by the lock while the entry's land ran), or ""."""
        jobs = self._land_jobs(task_id)  # newest first, `<id>-2` before `<id>`
        if entry_job in jobs:
            newer = jobs[:jobs.index(entry_job)]
        else:
            since = _job_time(entry_job) if entry_job else started_at
            newer = [j for j in jobs if since is not None and (_job_time(j) or since) > since]
        return newer[0] if newer else ""

    def _busy(self, task: Task) -> Land | None:
        """For a task in any state but `finished` whose last entry is not a land's: a command that works on it
        now (the lock is held, or a land job is starting), a Land with `running` None when the lock could not be
        tried, else None."""
        if task.state == "finished":
            return None
        held = self.task_lock_held(task.task_id)
        starting = self._starting_jobs(task.task_id)
        if starting:
            return self._bounded(Land(running=True, **self._job_facts(starting[0]["job"], starting[0])),
                                 _job_time(starting[0]["job"]))
        if held is None:
            return Land(running=None)
        return Land(running=True) if held else None

    def starting_step(self, task_id: str, facts: Land | None = None) -> str:
        """The next step of a task being started (see `start_facts`): the one place `status` asks it."""
        return next_step(task_id, "starting", facts)

    def start_facts(self, task_id: str, claim: Claim | None) -> Land | None:
        """What is known of a `start` of a task that has no record, or None when nothing says one began.

        With a claim made by `start` (marked): `running` is whether the task's lock is held, since `start` holds it
        from before its claim to its end, and the OS drops it when the process ends by any means. Held: it runs
        (or a `finish` clears it); free: it died; None: the lock could not be tried. The claim's `since`, written
        inside the lock, gives the time bound (START_LIMIT_S), which only changes the words, never frees.
        With a claim made by an older version (no mark): `running` None and `unmarked`; nothing proves it ended.
        With no claim: a `start --detach` job still beginning (see `_starting_jobs`), or None.
        Reads only: no lock file and no job record is written.
        """
        if claim is None:
            jobs = self._starting_jobs(task_id, "start")
            if not jobs:
                return None
            return Land(running=True, **{k: v for k, v in self._job_facts(jobs[0]["job"], jobs[0]).items() if k != "log"})
        since = _stamp_time(claim.since)
        if not claim.locked:
            return self._bounded(Land(running=None, unmarked=True), since, limit_s=OLD_CLAIM_TELL_S)
        return self._bounded(Land(running=self.task_lock_held(task_id)), since, limit_s=START_LIMIT_S)

    def starts_beginning(self) -> dict[str, Land]:
        """Every task with a `start --detach` job still beginning (not yet at its claim), with its facts."""
        folder = self.state_dir / "jobs"
        name = re.compile(r"^start-([a-z0-9][a-z0-9-]{0,47})-\d{8}T\d{6}(?:-\d+)?$")
        tasks = sorted({m.group(1) for p in (folder.glob("start-*.json") if folder.is_dir() else [])
                        if (m := name.match(p.stem))})
        return {task_id: facts for task_id in tasks if (facts := self.start_facts(task_id, None)) is not None}

    def situation(self, task: Task) -> tuple[str, Land | None, str]:
        """The state `status` shows for the task, what is known of a land or command on it, and its next step
        (see `situation_of`, which also gives the record they were judged from)."""
        seen = self.situation_of(task)
        return seen.state, seen.land, seen.step

    def situation_of(self, task: Task, *, again: bool = True) -> "Situation":
        """The record judged, the state `status` shows for it, what is known of a land or command on it, and its
        next step.

        A land that ends writes its result, then frees the lock: when the lock reads free and no job runs, the
        record is read again after the probe, so a result written in between is seen, not taken for a land
        that ended without one. The record returned is the one judged: `status` shows its history, so the
        history, the state and `next` all come from one read.
        """
        land = self.land_in_flight(task)
        if land is not None and land.running is False and again:
            try:
                fresh = self.task(task.task_id)
            except FlowError:
                fresh = task
            if (fresh.state, fresh.history) != (task.state, task.history):
                return self.situation_of(fresh, again=False)
        if land is None:
            land = self._busy(task)
            state = task.state
        else:
            state = "landing"
        outside = state == "checks-failed" and bool(task.history) and task.history[-1].endswith(NOT_IN_BRANCH)
        moved = (self._moved_under(task.task_id)
                 if state == "awaiting-council" and (land is None or land.running is False) else "")
        return Situation(task, state, land, next_step(task.task_id, state, land, target=task.target,
                                                      outside_branch=outside, moved_to=moved))

    def _moved_under(self, task_id: str) -> str:
        """`moved_since` for the task's merge under review; "" when its record does not read."""
        try:
            report = report_from_json((self.run_dir(task_id) / "merge.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, KeyError):
            return ""
        return self.moved_since(report)

    def moved_since(self, report: MergeReport) -> str:
        """The target's tip when the target moved since `report`'s merge so that the merge can no longer land
        (`Merger.land` only fast-forwards, and this is the test it makes first); "" when it can still land, or when
        git cannot tell (no target, no commit). Unlike `Merger.land`, which refuses on any failure, only git's
        answer "not an ancestor" counts as moved here: a refusal on a guess would stop a merge that can land."""
        tip = self._tip()
        if not tip or not report.committed or report.target != self.config.target:
            return ""
        code = self._runner(["git", *SAFE_GIT, "-C", str(self.config.repo), "merge-base", "--is-ancestor", tip,
                             report.committed], capture_output=True).returncode
        return tip if code == 1 else ""  # 1: not an ancestor; any other failure says nothing

    def candidate_path(self, task_id: str) -> Path | None:
        """The slot holding the task's merge candidate, if it has one."""
        return self.workspaces.path_of(CANDIDATE_OWNER + task_id)

    def _discard_candidate(self, task_id: str, retry: str = "run this command again") -> None:
        """Throw the candidate away and park its slot, Library kept for the next one; and the slot of the land's run on
        the target, when a land killed in that run left it. Each slot is parked on its own, so one that
        refuses does not keep the other; a refusal names the slot, why, and what to do: `retry`, the
        command that tries again (after a verdict, `upe finish`, not "this command")."""
        refusals = [refusal for refusal in (
            self._park(BASE_OWNER + task_id, f"the slot of {task_id}'s run of tests on the target", "its run of tests on the target",
                       retry),
            self._park(CANDIDATE_OWNER + task_id, f"the slot of {task_id}'s merge candidate", "its checks on the merge", retry),
        ) if refusal]
        self._git(self.config.repo, "branch", "-D", CANDIDATE_PREFIX + task_id, check=False)
        if refusals:
            raise WorkspaceError("; ".join(refusals))

    def _park(self, owner: str, what: str, during: str, retry: str = "run this command again") -> str:
        """Park one slot a land of the task holds; "" when parked (or none is held), else the refusal for it.

        A slot whose folder is gone loses only its entry. A removal git stopped half way (no `.git`, the tool's mark,
        `acquire`'s clean-up of a slot it made) is finished, as `finish` finishes a dead start's."""
        path = self.workspaces.path_of(owner)
        if path is None:
            return ""
        try:
            if not path.exists() or (not (path / ".git").exists() and self.workspaces.removal_begun(path.name, owner)):
                self.workspaces.remove_slot(owner)
                return ""
            if not (path / ".git").exists():
                return (f"{what}, {path}, is not a git worktree, and the tool did not begin removing it, so it does not "
                        f"know what the folder holds: tell the user; once they have looked in it and deleted it, {retry}")
            if lock_state(path) == "held":
                return (f"{what}, {path}, is open in an Editor, most likely the Unity of a land of the task killed during "
                        f"{during}, which runs on after it: once that Unity has ended, {retry}; if it is still open in "
                        f"{_hours(self._longest_run_s())}, tell the user")
            self.workspaces.release(owner, discard_changes=True)
            return ""
        except WorkspaceError as exc:
            return f"{what}, {path}, was not parked ({exc}): {retry}; if it says the same, tell the user"

    def _longest_run_s(self) -> int:
        """The cap of the longest run a land's Unity holds a slot for: a full EditMode run with its retries (the refusal's "in an hour" was shorter than the target's full run)."""
        return (max(0, self.config.test_retries) + 1) * self.config.test_timeout_s + TEST_RUN_SLACK_S

    def task(self, task_id: str) -> Task:
        path = self._task_path(task_id)
        if not path.exists():
            raise FlowError(f"no task {task_id}")
        try:
            task = Task(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            raise FlowError(f"task {task_id}: its record does not read ({exc}). {next_step(task_id, 'unreadable')}") from exc
        if not task.well_formed():
            raise FlowError(f"task {task_id}: its record does not read (a field of the wrong kind). "
                            f"{next_step(task_id, 'unreadable')}")
        return task

    def has_record(self, task_id: str) -> bool:
        return self._task_path(task_id).exists()

    def tests_answer(self, slot: Path) -> dict:
        """Where the project's tests live, for `start`'s answer: the folders `upe.toml` names
        (`test_folders`), or, with none named, the printed command that lists the files directly in every folder named
        Test or Tests in the slot, any case (a Unity folder shows there by its subfolders' `.meta` files). Names are
        printed as they are, not octal-escaped (`core.quotepath` off)."""
        if self.config.test_folders:
            return {"folders": list(self.config.test_folders), "from": "upe.toml"}
        find = (f"git -c core.quotepath=off -C {_slot_word(slot)} ls-files -- {_sh(':(glob,icase)**/tests/*')} "
                f"{_sh(':(glob,icase)**/test/*')}")
        return {"folders": [], "from": "none", "find": find}

    def free_id(self, task_id: str) -> str | None:
        """An id for new work in place of a spent one: the id, then `-2`, `-3`, ... `-99`, the first
        that no task takes, cut to a task id's 48 characters. The base is the id itself, or, when the id ends in `-<n>`
        and the id without it has a record (`dash-2` after `dash`), that id (`fix-issue-42` gives
        `fix-issue-42-2`, not `fix-issue-2`). Taken: a record file of that name (one that does not read too), a claim,
        a slot's owner, a branch `upe/<id>`, or a `start --detach` of it still beginning. None when the claims, the
        slots or the branches do not read, or every one is taken. A hint, not a reservation: another `start` may take
        it first, and `start` then refuses it with its own answer. Each name is a task id: the base is one, cut, with
        no trailing dash, and the suffix is digits."""
        cut = re.sub(r"-\d+$", "", task_id)
        base = cut if cut and cut != task_id and (self.state_dir / "tasks" / f"{cut}.json").exists() else task_id
        try:
            claimed = set(self.registry.all())
            owners = {owner for owner in self.workspaces.slots().values() if owner}
            heads = "refs/heads/" + BRANCH_PREFIX
            branches = {ref[len("refs/heads/"):] for ref in self._git(self.config.repo, "for-each-ref",
                                                                      "--format=%(refname)", heads).split()
                        if ref.startswith(heads)}
        except (ClaimError, WorkspaceError, FlowError, OSError):
            return None
        beginning = set(self.starts_beginning())
        for n in range(2, 100):
            suffix = f"-{n}"
            name = base[:48 - len(suffix)].rstrip("-") + suffix
            if name in claimed or name in owners or name in beginning \
                    or BRANCH_PREFIX + name in branches or (self.state_dir / "tasks" / f"{name}.json").exists():
                continue
            return name
        return None

    def starting(self, timeout: float | None = None) -> dict[str, Claim]:
        """Tasks being started: `start` holds their claim (task id -> the claim) but has not written their record yet.

        `start` claims first and writes the record only once the slot is made, which can take minutes.
        `timeout` caps the wait for the claims' lock; ClaimError after it, or when the claims do not read.
        """
        records = self.state_dir / "tasks"
        return {task_id: claim for task_id, claim in self.registry.all(timeout).items()
                if not (records / f"{task_id}.json").exists()}

    def tasks(self) -> list[Task]:
        folder = self.state_dir / "tasks"
        return [self.task(p.stem) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []

    def _save(self, task: Task, state: str | None = None, note: str = "") -> None:
        if state:
            task.state = state
        if note:
            task.note(f"{task.state}: {note}")
        write_atomic(self._task_path(task.task_id), json.dumps(asdict(task), indent=2) + "\n")

    def _write_run(self, task_id: str, name: str, text: str) -> Path:
        path = self.run_dir(task_id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _git(self, cwd: Path, *args: str, check: bool = True, input: str | None = None,
             env: Mapping[str, str] | None = None) -> str:
        completed = self._runner(
            ["git", *SAFE_GIT, "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            **({"input": input} if input is not None else {}), **({"env": env} if env is not None else {}))
        if check and completed.returncode != 0:
            raise FlowError(f"git {' '.join(args)} failed: {(completed.stderr or '').strip()}")
        return completed.stdout or ""

    # Start ---------------------------------------------------------------

    def start(self, task_id: str, brief: str, claim: Sequence[str], base: str | None = None) -> Task:
        """Claim, isolate, and put the tool's package in. Raises ClaimRefused on overlap."""
        if self._task_path(task_id).exists():
            try:
                task = self.task(task_id)
            except (OSError, ValueError, TypeError, FlowError):
                # `status` fails on this record too, so the error does not send the agent there.
                raise FlowError(f"task {task_id} already exists, but its record does not read. "
                                f"{next_step(task_id, 'unreadable')}") from None
            shown, _, what = self.situation(task)  # a land may still run: work in the slot now would change what it merges
            spent = ""
            if task.state == "finished":  # the record says finished, so the id is spent, not unreadable
                free = self.free_id(task_id)
                spent = (f"Task {task_id} is spent: it was finished, and a finished id cannot start again. Start the "
                         + (f"work under a free id: `upe start {free} --brief \"<what and why>\" --claim <the paths you "
                            f"will write>`. " if free else
                            "work under an id no task uses (`upe status` lists the ids in use). "))
            raise FlowError(f"{spent}task {task_id} already exists ({shown}); `upe status {task_id}` shows its history. {what}")
        self.workspaces.check_id(task_id)
        ref = base or self.config.target
        if ref.startswith("-"):
            raise FlowError(f"--base {ref!r} is not a ref")
        base_commit = self._git(self.config.repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()
        # The task's lock, from before the claim to the end, clean-up included. The OS drops it when this
        # process ends by any means, so a marked claim with no record and the lock free is a start that died.
        inside = False
        try:
            with self._task_lock(task_id):
                inside = True
                return self._start(task_id, brief, claim, base_commit)
        except FlowError:
            if inside:
                raise
            # Another command holds the lock: a `start` of this id that runs, or a `finish` clearing one. Not
            # `situation()`: there is no record. Nothing was claimed.
            facts = self.start_facts(task_id, self.registry.all().get(task_id)) or Land(running=True)
            raise FlowError(f"task {task_id}: another upe command is working on it, so nothing was claimed. "
                            f"{next_step(task_id, 'starting', facts)}") from None

    def _start(self, task_id: str, brief: str, claim: Sequence[str], base_commit: str) -> Task:
        """`start`, under the task's lock. It must not ask `situation()` or `start_facts()`: the lock reads as held
        from another handle of this process."""
        if self._task_path(task_id).exists():
            raise FlowError(f"task {task_id} already exists: another command made it while this one waited; "
                            f"`upe status {task_id}` says where it is")
        held = self.registry.all().get(task_id)
        if held is not None:  # this process holds the lock, so no `start` of it runs: a marked claim is a dead start
            since = _stamp_time(held.since)
            facts = (self._bounded(Land(running=False), since, limit_s=START_LIMIT_S) if held.locked
                     else self._bounded(Land(running=None, unmarked=True), since, limit_s=OLD_CLAIM_TELL_S))
            raise FlowError(f"task {task_id} already holds a claim (since {held.since}) but has no record. "
                            f"{next_step(task_id, 'starting', facts)}")
        branch = BRANCH_PREFIX + task_id
        if self.workspaces.path_of(task_id) is not None or self._git(self.config.repo, "branch", "--list", branch).strip():
            # what an earlier start left with no claim (an older version's failed clean-up, or by hand).
            raise FlowError(f"task {task_id} has no claim and no record, but its slot or its branch {branch} is "
                            f"there. {next_step(task_id, 'left-behind')}")
        parked_before = {slot for slot, owner in self.workspaces.slots().items() if owner is None}
        # Read before the claim: a broken one costs nothing here. This start reads it only for a note, and a
        # start that reuses a parked slot would not need it at all; but the golden refresh after every land reads it
        # and stops on it, so it is told now, before anything is claimed.
        try:
            golden_at = self._golden_record().get("commit")
        except FlowError as exc:
            raise FlowError(f"{exc}. This start reads it only for a note (a start that reuses a parked slot would not "
                            "need it), but the golden refresh after every land stops on it: tell the user now; "
                            "nothing was claimed") from exc
        ws = None
        try:
            # Inside the `try`: a Ctrl-C the instant the claim is written is undone too. No claim of this
            # id was there (checked above, under the lock), so the undo frees only this one.
            self.registry.claim(task_id, claim, locked=True)
            ws = self.workspaces.create(task_id, base_commit)
            agent_package.install(ws.path, self.cli, self.package_source)
            if Claim.of(task_id, claim).covers(MANIFEST):
                agent_package.allow_manifest_edits(ws.path)
            task = Task(task_id, brief, list(claim), base_commit, self.config.target)
            if ws.path.name in parked_before:
                was_at = self._git(ws.path, "rev-parse", "--verify", "-q", "HEAD@{1}", check=False).strip()
                if was_at and was_at != base_commit:
                    task.note(f"working: reused slot {ws.path.name}, whose Library was last at {was_at[:12]}; "
                              "Unity imports the difference")
            else:
                if golden_at and golden_at != base_commit:
                    task.note(f"working: Library copied from golden at {golden_at[:12]}; Unity imports the difference")
            self._save(task, "working", f"worktree {ws.path} from {base_commit[:12]}")
        except BaseException as exc:  # Ctrl-C too; a killed process runs nothing, and leaves a dead start
            if not self._task_path(task_id).exists():  # once the record is written, the start is done
                self._undo_start(task_id, ws, exc)
            raise
        return task

    def _undo_start(self, task_id: str, ws, error: BaseException) -> None:
        """Take back what this `start` made: its slot, its branch (made by it: none was there before its claim),
        then its claim, last. A step that fails stops the rest and keeps the claim, so what is left is a dead start
        that `finish` clears; its error is added to `error` as a note, and `error` is the one raised."""
        try:
            if ws is not None:
                agent_package.uninstall(ws.path)
                self.workspaces.release(task_id, discard_changes=True)  # a finished copy: parked, Library kept
            elif self.workspaces.path_of(task_id) is not None:
                raise FlowError("its slot was not given back")
            branch = BRANCH_PREFIX + task_id
            if self._git(self.config.repo, "branch", "--list", branch).strip():
                self._git(self.config.repo, "branch", "-D", branch)
            self.registry.release(task_id)
        except Exception as failed:
            error.add_note(f"The clean-up after it failed too ({failed}), so the claim of {task_id} is kept: "
                           f"`upe status {task_id}` says what to run")

    # Land ----------------------------------------------------------------

    def land(self, task_id: str) -> Outcome:
        """Run steps 3 and 4, then write the council brief, for a task whose work is committed on its branch."""
        with self._task_lock(task_id):
            for attempt in range(1, LAND_TRIES + 1):
                try:
                    return self._land(task_id)
                except TargetMoved as exc:
                    # Usually another task landed. Its merged test run is kept as the new target's
                    # baseline, so the next try skips those tests. Only a move forward to a commit one of
                    # this tool's lands made is followed: branch code run by Unity could move the target too.
                    target, during = self.config.target, getattr(exc, "during", "a land")
                    tip = self._tip()
                    by_a_land = self._moved_by_a_land(getattr(exc, "before", ""), tip)
                    if attempt < LAND_TRIES and by_a_land:
                        self._say(f"{target} moved during {during}: another task landed; landing again on the new "
                                  f"target ({attempt + 1} of {LAND_TRIES})")
                        self._save(self.task(task_id), "working", f"target moved; landing again ({attempt + 1} of {LAND_TRIES})")
                        continue
                    self._drop_candidate(task_id)
                    if by_a_land:
                        raise TargetMoved(f"{target} moved during {during}, because other tasks landed. Nothing landed. "
                                          f"Tried {LAND_TRIES} times and it keeps moving: land again a little later") from exc
                    raise TargetMoved(f"{target} moved during {during} to {tip[:12] or '(nothing)'}, which is not a land "
                                      f"of this tool on top of the target it had. Nothing landed. Look at that commit "
                                      f"(code run by Unity may have moved it), then land again") from exc
            raise AssertionError("unreachable")

    def _tip(self) -> str:
        # By its full name, never a tag of the same name.
        return self._git(self.config.repo, "rev-parse", "--verify", "-q", f"refs/heads/{self.config.target}",
                         check=False).strip()

    def _refuse_tags(self, target: str, branch: str) -> None:
        """Refuse a land when a tag has the name of the target or of the task branch. Git reads a
        short name as the tag first (`refs/tags/X` before `refs/heads/X`, with only a warning), so a tag `main` ahead of
        the target once landed commits no check and no council saw. The land reads both by their full names too; this
        stops it before it reads anything, and names the tag.

        The tag is the user's, so the message names it and its commit and gives no step that deletes it (a
        tag has no reflog: its commit may have no other name)."""
        tags = [name for name in (target, branch)
                if self._runner(["git", *SAFE_GIT, "-C", str(self.config.repo), "show-ref", "--verify", "-q",
                                 f"refs/tags/{name}"], capture_output=True).returncode == 0]
        if tags:
            at = {t: self._git(self.config.repo, "rev-parse", "--verify", "-q", f"refs/tags/{t}^{{}}", check=False).strip()
                  or "(no commit)" for t in tags}
            raise FlowError(f"a tag has the name of {' and of '.join(tags)} ("
                            + ", ".join(f"refs/tags/{t} at {at[t]}" for t in tags) + "): git reads such a name as the "
                            "tag, so the land stops before it reads either; nothing was merged. The tag is the user's, "
                            "not the agent's: tell the user, naming the tag and its commit, and land again once they "
                            "have renamed or removed it. Do not delete or move it yourself: a tag has no reflog, and its "
                            "commit may have no other name")

    def _moved_by_a_land(self, before: str, tip: str, wait_s: float = 10.0) -> bool:
        """Whether the target went forward from `before` to `tip`, and a land of this tool made `tip`.

        A land moves the target before it records its commit, so a few seconds are allowed for that.
        """
        if not before or not tip or before == tip:
            return False
        forward = self._runner(["git", *SAFE_GIT, "-C", str(self.config.repo), "merge-base", "--is-ancestor", before, tip],
                               capture_output=True).returncode == 0
        if not forward:
            return False
        deadline = time.monotonic() + wait_s
        while True:
            if any(t.landed_commit == tip for t in self._readable_tasks()):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(1.0)

    def _task_records(self) -> tuple[list[Task], list[str]]:
        """Every task record that reads, and the file names of those that do not, from one read of the folder.

        A record does not read when its name is not a task id, its JSON is broken, or a field has the wrong
        kind; such a record is skipped and named, not a reason to crash a land at its end.
        """
        folder = self.state_dir / "tasks"
        found, broken = [], []
        for p in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                found.append(self.task(p.stem))  # which checks each field's kind
            except (OSError, ValueError, TypeError, FlowError, WorkspaceError):
                broken.append(p.stem)
        return found, broken

    def _readable_tasks(self) -> list[Task]:
        return self._task_records()[0]

    def _drop_candidate(self, task_id: str) -> None:
        """Throw away a stopped land's candidate; one an Editor still holds is cleared by the next land or `finish`."""
        try:
            self._discard_candidate(task_id)
        except (WorkspaceError, FlowError):
            pass

    def _land(self, task_id: str) -> Outcome:
        task = self.task(task_id)
        if task.state not in ("working", "conflict", "checks-failed", "awaiting-council", "refused"):
            raise FlowError(f"task {task_id} is {task.state}")
        ws = self.workspaces.workspace(task_id)
        # An uncommitted change inside the claim stops the land, as before; one outside it is named and the land goes
        # on. A land merges the branch's commits only (by ref, in the candidate's own slot), so nothing
        # uncommitted in this slot reaches the merge either way. Read only, before "land started": a refusal writes
        # nothing.
        dirt = self._slot_dirt(task, ws.path)
        if dirt.inside:
            n = len(dirt.inside)
            beside = (f" Outside the claim, {len(dirt.outside)} more {'is' if len(dirt.outside) == 1 else 'are'} "
                      f"uncommitted ({_quoted(dirt.outside[:10])}{', ...' if len(dirt.outside) > 10 else ''}): they do "
                      "not stop a land, and no land has them; if one is the task's work, tell the user (the claim "
                      "cannot be changed)." if dirt.outside else "")
            shared = (f" Of these, {_quoted(dirt.shared)} {'is' if len(dirt.shared) == 1 else 'are'} not in the claim but "
                      "shared (a file every task may write), and counts as the task's." if dirt.shared else "")
            raise FlowError(f"{ws.path} has uncommitted work inside the claim of {task_id} ({_quoted(dirt.inside[:10])}"
                            f"{f' and {n - 10} more' if n > 10 else ''}); a land merges only commits: commit what belongs "
                            f"to the task on {ws.branch} and restore the rest (git restore <path>), then land again."
                            f"{shared}{beside}")
        left = self._dirt_notes(task, ws.path, dirt)
        target = self.config.target
        self._refuse_tags(target, ws.branch)
        # The target and the task branch by their full names, here and in the merge, so a tag of either name is never
        # read (the refusal above is the first guard, these the second).
        target_ref, branch_ref = f"refs/heads/{target}", f"refs/heads/{ws.branch}"
        merge_base = self._git(self.config.repo, "merge-base", target_ref, branch_ref).strip()
        changed = self._changed(self.config.repo, merge_base, branch_ref)
        if not changed:
            raise FlowError(f"{ws.branch} has nothing to merge")

        # From here the earlier merge and its votes are void, however this land ends: a land cut
        # short must not leave a new, unchecked merge open to votes.
        self._save(task, "working", land_started_note(self.job_id))
        self.ballot(task_id).clear()
        for name in ("council-brief.md", "council.md", "lander.json"):
            (self.run_dir(task_id) / name).unlink(missing_ok=True)
        # An earlier land's scene reports are not this land's, in the folders the council and a human read.
        for name in ("baseline/upe-scene-check.json", "baseline/upe-scene-check.log", "merged/upe-scene-check.json",
                     "merged/upe-scene-check.log"):
            (self.run_dir(task_id) / name).unlink(missing_ok=True)
        for folder in ("baseline", "merged"):  # and the groups of a split check, `upe-scene-check-<k>.*`
            for name in (*(self.run_dir(task_id) / folder).glob("upe-scene-check-*.json"),
                         *(self.run_dir(task_id) / folder).glob("upe-scene-check-*.log")):
                name.unlink(missing_ok=True)
        self._say(f"land {task_id}: {len(changed)} changed paths; making the merge candidate")
        for note in left:
            self._say(f"land {task_id}: {note}")
        self._discard_candidate(task_id)  # a candidate left from an earlier try
        candidate = self.workspaces.acquire(CANDIDATE_OWNER + task_id, CANDIDATE_PREFIX + task_id, target_ref,
                                            reset_branch=True)

        checks = CheckReport()
        checks.notes += left
        also = "".join(f". Also: {note}" for note in left)  # in the land's own answer, whatever its end
        outside = unclaimed(changed, self.registry.all().get(task_id) or Claim.of(task_id, task.claim), self.config.shared)
        for path in outside:
            checks.add(path, "claim", "changed outside the task's claim; claim it, or leave it to the task that owns it")
        check_protected(changed, self.config.protected_paths, checks)

        baseline: dict[str, Any] = {}
        scene_notes: list[str] = []

        def record_baseline(wt: Path) -> None:
            with self._guard(task_id, "the baseline run on the target"):
                _record_baseline(wt)

        def _record_baseline(wt: Path) -> None:
            # The scene baseline the merge will need, judged on the branch's changes (its files read from
            # the branch: they are not in a worktree yet). The judgment after the merge is the one that counts.
            commit = self._git(wt, "rev-parse", "HEAD").strip()
            baseline["commit"] = commit
            branch = branch_ref
            baseline["plan"] = self._scene_plan(self._changes(self.config.repo, merge_base, branch), commit,
                                                lambda path: self._blob(self.config.repo, branch, path))
            self._scene_baseline(task_id, wt, commit, baseline["plan"], baseline, scene_notes)
            baseline["tests"] = self._look_up(wt, self.run_dir(task_id) / "baseline" / "upe-test-results.xml")

        report = self.merger.merge(task_id, ws.branch, target, candidate, before_merge=record_baseline)
        self._say(f"merge: {'done' if report.ok else 'stopped'}")
        look: TargetTests | None = baseline.get("tests")
        checks.notes += look.notes if look is not None else []
        if look is not None and look.own is not None:
            checks.notes.append(f"{len(look.own.seen)} tests failed at least once on the target before the merge")
        checks.notes += scene_notes
        if look is not None and look.own is None and look.ancestor is not None:
            a, _, far = look.ancestor
            checks.notes.append(f"the target {look.commit[:12]} has no kept test result of its own; the nearest ancestor "
                                f"with one is {a[:12]}, {far} commit{'' if far == 1 else 's'} before: a test that fails on "
                                "the merge is compared by a run of its class on the target")
        elif look is None or look.own is None:
            checks.notes.append("no test baseline: the tests did not finish on the target, so every failed test counts as new")
        self._write_run(task_id, "merge.json", report.to_json())
        if not report.ok:
            reasons = "; ".join(f"{s.path}: {s.detail}" for s in report.unresolved)
            self._save(task, "conflict", reasons)
            return Outcome(task_id, "conflict", f"merge stopped, candidate left at {candidate}: {reasons}. "
                                                f"{next_step(task_id, 'conflict', target=target)}{also}")

        with self._guard(task_id, "the regenerate and builder runs", commits_to_candidate=True):
            self._regenerate_and_replay(task_id, candidate, report, checks)
        merged = self._changed(candidate, target_ref, "HEAD")
        # A regenerate or builder run loads the branch's code, which could commit to the candidate.
        added = [p for p in merged if p not in changed]
        check_protected(added, self.config.protected_paths, checks)
        if not report.replays:  # a builder may write outside the claim; the council sees those in the diff
            for path in unclaimed(added, self.registry.all().get(task_id) or Claim.of(task_id, task.claim), self.config.shared):
                checks.add(path, "claim", "appeared in the merge, not on the branch, and outside the task's claim")

        # The .meta check and the GUIDs a reference may name are read from the merge's commit through git,
        # not from the slot's disk, and the .meta findings are the merge's own: those the target's commit lacks.
        merge_commit = self._git(candidate, "rev-parse", "HEAD").strip()
        static = static_checks(candidate, changed=merged, package_cache=candidate / "Library" / "PackageCache",
                               tree=self._meta_tree(candidate, merge_commit),
                               target=self._meta_tree(candidate, baseline["commit"]))
        checks.findings += static.findings
        checks.notes += static.notes
        check_tool_namespace(candidate, merged, checks)
        check_case_duplicates(self._git(candidate, "ls-files", "-z").split("\0"), merged, checks)
        self._say(f"static checks: {len(checks.findings)} findings so far")
        plan = baseline.get("plan")
        if checks.ok:
            commit = baseline["commit"]
            plan = self._scene_plan(self._changes(candidate, commit, "HEAD"), commit, lambda path: _read(candidate / path),
                                    self._guid_taken(candidate, commit))
            need = self._scene_need(plan, commit)
            if need[0] != "skip" and baseline.get("scene") != need:
                # The two judgments differ (the GUID rule, which only the merge can apply, or a builder's change):
                # the baseline the merge needs runs now, on the target, in the candidate's own slot checked out at the
                # target (a scene check's result depends on the slot's Library, so the baseline and the
                # merge's check run in one slot, as the baseline before the merge does).
                notes: list[str] = [f"scene baseline: the judgment before the merge ({baseline['plan'].kind}) and after "
                                    f"it ({plan.kind}) differ, so it ran after the merge, on the target in the "
                                    f"candidate's slot {candidate.name}"]
                baseline.pop("problems", None)
                with self._guard(task_id, "the scene check on the target"), \
                        self._candidate_at(task_id, candidate, commit) as slot:
                    self._scene_baseline(task_id, slot, commit, plan, baseline, notes)
                checks.notes += notes
            with self._guard(task_id, "the checks on the merged project"):
                self._unity_checks(task_id, candidate, merged, baseline.get("problems"), checks, look, plan)
        if plan is not None and plan.kind != "skip" and "problems" not in baseline:
            checks.notes.append("no baseline: the scene check did not run on the target, so every scene problem counts as new")
        # Unity rewrites files of its own when it opens the project; the council reads a clean worktree.
        self._git(candidate, "checkout", "--", ".", check=False)
        self._write_run(task_id, "merge.json", report.to_json())
        self._write_run(task_id, "checks.md", render_checks(checks))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self._write_run(task_id, f"checks-history/{stamp}.md", render_checks(checks))  # each land's own, for comparing them
        self._say(f"checks: {len(checks.findings)} findings")
        if not checks.ok:
            # Every finding static and none in a path the branch changed (`merged` holds what a builder added): a false
            # stop. Marked in the same entry as the state, so `next` reads both from one write.
            outside_branch = findings_outside_branch(checks.findings, [*changed, *merged])
            self._save(task, "checks-failed", f"{len(checks.findings)} findings{NOT_IN_BRANCH if outside_branch else ''}")
            return Outcome(task_id, "checks-failed",
                           f"{len(checks.findings)} check findings; see {self.run_dir(task_id) / 'checks.md'}"
                           + (f". {next_step(task_id, 'checks-failed', outside_branch=True)}" if outside_branch else "")
                           + also)

        self._keep(task_id, report.committed, checks.tests)
        self._keep_scenes(report.committed, candidate, checks.scene)
        records = self._task_records()  # one read for both, so the briefs and the claims name the same tasks
        evidence = Evidence(
            briefs=self._briefs(task, records),
            claims=self._claims(task, records),
            diffs=gather_diffs(self.config.repo, report.base, target, ws.branch, report.committed),
            merge_report=report.to_json(),
            checks=render_checks(checks),
        )
        self._write_run(task_id, "lander.json", json.dumps(self._identity()) + "\n")
        path = self._write_run(task_id, "council-brief.md", brief(task_id, candidate, evidence, report.committed))
        self._save(task, "awaiting-council", f"checks passed; council brief at {path}")
        return Outcome(task_id, "awaiting-council",
                       f"checks passed. Ask the user for two council members: fresh agents that did not do this work, "
                       f"each its own agent (not a subagent of another), started at {self.config.council_effort} effort. "
                       f"You do not start them or vote. Each reads {path} and records a verdict: "
                       f"upe verdict {task_id} --member <name> --file <verdict.json>{also}")

    def ballot(self, task_id: str) -> Ballot:
        return Ballot(self.run_dir(task_id) / "verdicts")

    def verdict(self, task_id: str, member: str, verdict_json: str) -> Outcome:
        """Record one council member's verdict. The second one decides: land, refuse, or the user."""
        with self._task_lock(task_id):
            return self._verdict(task_id, member, verdict_json)

    def _verdict(self, task_id: str, member: str, verdict_json: str) -> Outcome:
        task = self.task(task_id)
        if task.state != "awaiting-council":
            raise FlowError(f"task {task_id} is {task.state}, not waiting for the council")
        report = report_from_json((self.run_dir(task_id) / "merge.json").read_text(encoding="utf-8"))
        try:
            verdict = Verdict.parse(member, verdict_json)
        except CouncilError as exc:
            raise FlowError(str(exc)) from exc
        if verdict.commit != report.committed:
            raise FlowError(f"the verdict names commit {verdict.commit or '(none)'}, but the merge under review is "
                            f"{report.committed}; read the current council brief")
        # Only a GO needs the fast-forward. A NO-GO judges the work, which the target's move does not change, and two
        # of them refuse the task with their findings kept, so it is recorded as before.
        moved = self.moved_since(report) if verdict.verdict == "GO" else ""
        if moved:
            raise FlowError(f"{report.target} moved since this merge (it is at {moved[:12]} now), so a GO cannot "
                            f"land it; this one is not recorded, and your part is done. The task's agent lands "
                            f"{task_id} again, and a new council judges the new merge")
        verdict.agent = self._identity()
        lander_file = self.run_dir(task_id) / "lander.json"
        lander = json.loads(lander_file.read_text(encoding="utf-8")) if lander_file.exists() else None
        if identities.same(verdict.agent, lander):
            raise FlowError("this verdict comes from the agent process that ran `land` for this task; "
                            "a council member must be a separate agent, not the one that did the work or a subagent of it")
        for other in self.ballot(task_id).verdicts():
            if identities.same(verdict.agent, other.agent):
                raise FlowError(f"this verdict comes from the same agent process as {other.member}'s; "
                                "the two members must be separate agents")
        try:
            verdicts = self.ballot(task_id).record(verdict)
        except (CouncilError, FileExistsError) as exc:
            raise FlowError(str(exc) or "another verdict was recorded at the same moment; try again") from exc
        if len(verdicts) < 2:
            return Outcome(task_id, "awaiting-council", f"{member}: {verdicts[-1].verdict} recorded; waiting for the other member")
        decision = count_votes(verdicts)
        self._write_run(task_id, "council.md", render_decision(decision))
        return self._act(task, report, decision.outcome, decision.reason)

    def _regenerate_and_replay(self, task_id: str, candidate: Path, report: MergeReport, checks: CheckReport) -> None:
        """Keep the tool out of the manifest, let Unity rewrite the lock, re-run builders; commit each."""
        logs = self.run_dir(task_id)
        # The target by its full name; `refs/<target>` or a tag made after the refusal is read first by a
        # short one, and the tool's packages a branch committed would then stay in the manifest.
        target_manifest = self._git(candidate, "show", f"refs/heads/{report.target}:{MANIFEST}", check=False)
        merged_manifest = (candidate / MANIFEST).read_text(encoding="utf-8") if (candidate / MANIFEST).exists() else ""
        if target_manifest and merged_manifest:
            stripped = agent_package.strip_tool_packages(merged_manifest, target_manifest)
            if stripped is not None:
                (candidate / MANIFEST).write_text(stripped, encoding="utf-8", newline="\n")
                self._git(candidate, "add", "--", MANIFEST)
                self._commit(candidate, "Keep the tool's packages out of the manifest")
                report.steps.append(Step(MANIFEST, "manifest", "merged", "removed the tool's packages the branch committed"))
                merged_manifest = stripped
            if json.loads(merged_manifest) != json.loads(target_manifest) and LOCK_FILE not in report.regenerate:
                (candidate / LOCK_FILE).unlink(missing_ok=True)  # the branch's lock never saw its own manifest change
                report.regenerate.append(LOCK_FILE)
        if report.regenerate:
            self.editor.refresh(candidate, logs / "regenerate.log")
            lock = candidate / LOCK_FILE
            if lock.exists():
                manifest = self._git(candidate, "show", f"HEAD:{MANIFEST}")
                lock.write_text(agent_package.scrub_lock(lock.read_text(encoding="utf-8"), manifest), encoding="utf-8", newline="\n")
                self._git(candidate, "add", "--", LOCK_FILE)
                self._commit(candidate, "Regenerate packages-lock.json after the merge")
            else:
                checks.add(LOCK_FILE, "packages-lock", "Unity did not write packages-lock.json from the merged manifest")
        for method in report.replays:
            run = self.editor.batch(candidate, logs / f"replay-{method}.log", "-executeMethod", method, quit=True)
            report.steps.append(Step(method, "replay", "merged" if run.exit_code == 0 else "conflict", f"builder exited {run.exit_code}"))
            if run.exit_code != 0:
                checks.add(method, "replay", f"builder exited {run.exit_code}; see {run.log}")
                continue
            self._git(candidate, "add", "-A", "--", "Assets", "ProjectSettings")
            self._commit(candidate, f"Re-run {method} on the merged project")
        report.committed = self._git(candidate, "rev-parse", "HEAD").strip()

    def _changed(self, cwd: Path, a: str, b: str) -> list[str]:
        """Changed paths between two commits, NUL-separated so no file name is quoted or split."""
        out = self._git(cwd, "diff", "--name-only", "--no-renames", "-z", a, b)
        return [p for p in out.split("\0") if p]

    def _changes(self, cwd: Path, a: str, b: str) -> list[tuple[str, str]]:
        """(status, path) of each changed path between two commits, with no rename detection: a rename is
        a `D` and an `A`."""
        items = self._git(cwd, "diff", "--name-status", "--no-renames", "-z", a, b).split("\0")
        return [(items[i][:1], items[i + 1]) for i in range(0, len(items) - 1, 2) if items[i]]

    def _tree(self, commit: str) -> set[str]:
        """Every file of a commit's tree."""
        return {p for p in self._git(self.config.repo, "ls-tree", "-r", "--name-only", "-z", commit).split("\0") if p}

    def _meta_tree(self, cwd: Path, commit: str) -> MetaTree:
        """What the `.meta` check reads of `commit`, through git only, nothing from the folder's disk: every
        tracked file (`ls-tree`), the `.meta` files under `Assets/` with a `folderAsset: yes` line, and the `guid:` line
        of each `.meta` under `Assets/` and `Packages/` (two `git grep`s of the commit, with fixed pathspecs). A grep
        exits 1 when nothing matches; any other code but 0, or a word on stderr, raises `FlowError`, so a failed read
        never reads as no findings.

        The greps read every `.meta` as text (`-a`), whatever git's attributes say (a `.gitattributes` in
        the slot, tracked, or in `info/attributes` marking `.meta` binary or `-diff`; a NUL byte in a `.meta`), and fix
        their own output and pattern kind (`-G --no-color --no-line-number --no-column`), whatever the user's config
        says (`grep.lineNumber`, `grep.column`, `grep.patternType`, `color.ui`, `color.grep`). A line that does not
        start with `<commit>:`, or no GUID read at all while `.meta` files are tracked, raises `FlowError`; a tracked
        `.meta` whose GUID is not read is a finding (`meta_findings`, "no-guid")."""
        files = frozenset(p for p in self._git(cwd, "ls-tree", "-r", "--name-only", "-z", commit).split("\0") if p)

        def grep(output: Sequence[str], pattern: str, pathspecs: Sequence[str]) -> str:
            # Real git: a blob it cannot read is an `error:` line on stderr, and the exit is still 0 when other files
            # match. So any word on stderr fails the read too (`strict`).
            return self._grep(cwd, [pattern], commit, binary="-a", mode="-G", output=output, pathspecs=pathspecs,
                              null=True, strict=True).stdout

        prefix = commit + ":"

        def unread(what: str) -> FlowError:
            return FlowError(f"git grep of {commit[:12]} gave a line this reader does not know ({what!r}): the .meta "
                             "check cannot read the commit")

        listed = [p for p in grep(("-l",), FOLDER_ASSET, ("Assets/*.meta",)).split("\0") if p]
        for p in listed:
            if not p.startswith(prefix):
                raise unread(p[:80])
        folders = frozenset(p[len(prefix):] for p in listed)
        guids: dict[str, list[str]] = {}
        seen: set[str] = set()  # a path's first `guid:` line counts, as `_META_GUID.search` reads a file on disk
        out, at = grep((), "^guid:", ("Assets/*.meta", "Packages/*.meta")), 0
        # `<commit>:<path>\0<line>\n`, read in order: a path may hold a newline and no NUL; a line (read with `-a`) may
        # hold a NUL but no newline.
        while at < len(out):
            nul = out.find("\0", at)
            if nul < 0 or not out.startswith(prefix, at):
                raise unread(out[at:at + 80])
            end = out.find("\n", nul)
            end = len(out) if end < 0 else end
            path, line = out[at + len(prefix):nul], out[nul + 1:end]
            at = end + 1
            match = META_GUID.match(line)
            if match and path not in seen:
                seen.add(path)
                guids.setdefault(match.group(1).lower(), []).append(path)
        if not guids and any(p.endswith(".meta") and p.startswith(("Assets/", "Packages/")) for p in files):
            raise FlowError(f"git grep of {commit[:12]} read no GUID, while the commit tracks .meta files: the .meta "
                            "check cannot read the commit")
        return MetaTree(files, folders, {g: tuple(m) for g, m in guids.items()})

    def _blob(self, cwd: Path, rev: str, path: str) -> bytes | None:
        """A file's bytes at a commit or branch; None when git does not give them."""
        done = self._runner(["git", *SAFE_GIT, "-C", str(cwd), "show", f"{rev}:{path}"], capture_output=True)
        return done.stdout if done.returncode == 0 and isinstance(done.stdout, bytes) else None

    def _fingerprint(self, task_id: str, target: bool = True, candidate: bool = True) -> str:
        """What a Unity run on a candidate has no business changing: the refs it lands through, git config and hooks."""
        import hashlib

        refs = [f"refs/heads/{BRANCH_PREFIX}{task_id}"]
        refs += [f"refs/heads/{self.config.target}"] if target else []
        refs += [f"refs/heads/{CANDIDATE_PREFIX}{task_id}"] if candidate else []
        parts = [self._git(self.config.repo, "rev-parse", "--verify", "-q", r, check=False) for r in refs]
        common = Path(self._git(self.config.repo, "rev-parse", "--git-common-dir").strip())
        common = common if common.is_absolute() else self.config.repo / common
        settings = self._git(self.config.repo, "config", "--file", str(common / "config"), "--list", check=False).splitlines()
        # Branch tracking changes whenever the user checks out or pushes a branch; check_target reads the target's.
        # A remote's URL is where the next push goes, so remote.* is kept.
        parts += sorted(s for s in settings if not s.lower().startswith("branch."))
        for f in sorted((common / "hooks").glob("*")):
            parts.append(f"{f.name}:{f.read_bytes().hex() if f.is_file() else ''}")
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()

    def _guard(self, task_id: str, what: str, commits_to_candidate: bool = False):
        """Fail the land if branch code run by Unity changed refs, git config or hooks (DESIGN.md, "Security")."""
        flow = self
        candidate = not commits_to_candidate  # the tool's own commits move the candidate during these runs

        class Guard:
            def __enter__(self):
                self.tip = flow._tip()  # first: a move after it shows as a change of the fingerprint below
                self.others = flow._fingerprint(task_id, target=False, candidate=candidate)
                self.before = flow._fingerprint(task_id, candidate=candidate)

            def __exit__(self, *exc):
                if exc[0] is not None:
                    return False
                if flow._fingerprint(task_id, target=False, candidate=candidate) != self.others:
                    raise FlowError(f"git refs, config or hooks changed during {what}; code in the branch may have "
                                    "done it. Nothing landed; look at the branch before landing again")
                if flow._fingerprint(task_id, candidate=candidate) != self.before:
                    # `land` decides whether a land of this tool moved it, and words what the user sees.
                    moved = TargetMoved(f"{flow.config.target} moved during {what}. Nothing landed, and this merge "
                                        "could not have")
                    moved.during, moved.before = what, self.tip
                    raise moved
                return False

        return Guard()

    def _commit(self, wt: Path, message: str) -> None:
        if self._runner(["git", *SAFE_GIT, "-C", str(wt), "diff", "--cached", "--quiet"], capture_output=True).returncode == 0:
            return  # nothing staged
        self._git(wt, "commit", "--no-verify", "-q", "-m", message)

    def _unity_checks(
        self, task_id: str, candidate: Path, merged: list[str], baseline: set | None, checks: CheckReport,
        target_tests: TargetTests | None = None, plan: ScenePlan | None = None,
    ) -> None:
        if plan is not None and plan.kind == "skip":
            self._say("merged: scene check skipped: the merge only adds files that cannot break an existing scene")
            # The merge's scripts are the target's here: a skip adds no script that names a hook.
            checks.notes.append(self._skip_note(candidate, plan, "HEAD"))
        else:
            if plan is not None:
                checks.notes.append(self._plan_note(plan))
            agent_package.install(candidate, self.cli, self.package_source)
            self._say("merged: scene check")
            try:
                scene_checks(self.editor, candidate, checks, paths=plan.paths if plan is not None else merged,
                             baseline=baseline, out_dir=self.run_dir(task_id) / "merged")
            finally:
                agent_package.uninstall(candidate)
        if checks.ok:
            self._say(f"merged: EditMode tests (up to {self.config.test_timeout_s} s)")
            classes = sorted({Path(p).stem for p in merged if p.endswith(".cs") and "/Tests/" in f"/{p}"})
            results = self.run_dir(task_id) / "merged" / "upe-test-results.xml"
            look = target_tests or TargetTests("")
            unity_checks(self.cli, candidate, checks, test_timeout_s=self.config.test_timeout_s, results=results,
                         classes=classes, retries=self.config.test_retries,
                         rerun=lambda run: self._confirm(candidate, results, run, checks.notes, "merged"),
                         known_for=lambda run: self._known(task_id, look, run))

    # The scenes ----------------------------------------------------
    #
    # `scene-baselines/<commit>.<slot>.json` is read only through `self.scene_store` (`SceneStore.get`), by a land
    # whose run on the target is in that slot, and written only by
    # `SceneStore.put`, which takes a `FullScenes`: a complete check of every scene and prefab, made by a run of this
    # process (`run_scene_check` returns it; nothing reads a report file left in `runs/`). Two writers: the target's full
    # run, under the commit's lock (`_scene_baseline_full`), and the merge's full run once its checks passed
    # (`_keep_scenes`). One reader: a land that needs the target's whole baseline (no per-path reader).

    @property
    def scene_store(self) -> SceneStore:
        return SceneStore(self.state_dir / "scene-baselines")

    def _scene_plan(self, changes: list[tuple[str, str]], commit: str, read_new: Callable[[str], bytes | None],
                    guid_taken: Callable[[str, str], str] | None = None) -> ScenePlan:
        return scene_plan(changes, read_new, self._tree(commit), lambda path: self._blob(self.config.repo, commit, path),
                          guid_taken)

    def _scene_need(self, plan: ScenePlan, commit: str) -> tuple[str, tuple[str, ...]]:
        """The baseline a plan needs: none, every scene and prefab, or the changed paths the target has (a path
        the target does not have is not given to the run, which would throw on it)."""
        if plan.kind != "paths":
            return plan.kind, ()
        tree = self._tree(commit)
        return "paths", tuple(p for p in plan.paths if p in tree)

    def _guid_taken(self, candidate: Path, commit: str) -> Callable[[str, str], str]:
        """The allow list's GUID rule on the merge: what else holds a GUID, among the merge's metas and the package
        cache's, or names it, among the files of the target `commit` (an asset deleted while a scene still
        refers to it, brought back with its old .meta, gives that scene its target again). Read once, on the first
        question: the holders by `guid_index`, the names by one `git grep` of the GUIDs of every .meta the merge adds.
        The cache is the candidate slot's `Library/PackageCache` as that slot holds it (a new slot's is the golden's
        copy; a parked one's, the last land's there), which can be older than the target's lock; with none, no cache
        asset counts as taken, and `static_checks` says "no package cache"."""
        index: list = []

        def taken(guid: str, meta: str) -> str:
            if not index:
                index.extend(guid_index(candidate, [candidate / "Library" / "PackageCache"]))
                index.append(self._guids_named(candidate, commit))
            own, external, named = index
            others = [m for m in own.get(guid, []) if m != meta]
            if others:
                return f"{others[0]}'s"
            if guid in external:
                return "an asset in the package cache's"
            return named.get(guid, "")

        return taken

    def _guids_named(self, candidate: Path, commit: str) -> dict[str, str]:
        """GUID -> what of the target `commit` names it ("named by <file> of the target"), for the GUIDs of the .meta
        files the candidate's HEAD adds against `commit`; none when none is added.

        One `git grep -o -h -a -E` of the target for every GUID reference (`GUID_REFERENCE`: `guid: <hex>` as Unity
        writes it in YAML, also `m_GUID:`, the JSON `"guid": "<hex>"` and UI Toolkit's `guid=<hex>` forms; a GUID
        written bare is missed, see `GUID_REFERENCE`), the `.meta` files too (a model's import settings name materials),
        whatever the number of added files (no GUID on the command line; 8 to 10 s on a large project), read line by
        line; then a file is named for each GUID found, by a `git grep -F` of those GUIDs only,
        in groups capped by length. Every file is read as text (`-a`). A grep that fails (exit other than 0 and 1) names
        every added GUID as perhaps named: the full check runs. So does any word on its stderr (git
        exits 0 when it cannot read a blob and other files match)."""
        added = self._git(candidate, "diff", "--name-only", "--no-renames", "--diff-filter=A", "-z", commit, "HEAD")
        guids = set()
        for path in added.split("\0"):
            if path.lower().endswith(".meta"):  # one that does not read gives no GUID here, and "no GUID" there
                found = META_GUID.search((_read(candidate / path) or b"").decode("utf-8", errors="replace"))
                if found:
                    guids.add(found.group(1).lower())
        if not guids:
            return {}
        # Read line by line (on a large project the grep prints millions of
        # lines; only the GUIDs the merge adds are kept.
        found_named: set[str] = set()
        # Real git exits 0 when it cannot read a blob and other files match, writing `error: ... unable to
        # read` on stderr; so stderr is kept (in a file: a pipe left unread could fill and stall the grep), and any word
        # on it is a failed read, which keeps the full check, as an exit over 1 does.
        # Every file is read as text (`-a`), whatever git's attributes say (a `.gitattributes`
        # in the slot, tracked, or in `info/attributes` marking scenes binary), and the output's shape is fixed whatever the user's config says (`--no-color`, no line number or
        # column); a line whose last 32 characters are not a GUID is a read this code does not know: perhaps named.
        odd = ""

        def read(match: str) -> None:
            nonlocal odd
            guid = match.rstrip()[-32:].lower()
            if guid in guids:
                found_named.add(guid)
            elif not odd and not _HEX32.fullmatch(guid):
                odd = match.rstrip()[:80]

        every = self._grep(candidate, [GUID_REFERENCE], commit, binary="-a", mode="-E", output=("-o", "-h"),
                           on_line=read)
        said = every.stderr.strip()
        if every.returncode not in (0, 1):  # a negative code (a git ended by a signal) is no answer either
            return dict.fromkeys(guids, f"perhaps named by a file of the target: `git grep` exited {every.returncode}")
        if said:
            return dict.fromkeys(guids, f"perhaps named by a file of the target: `git grep` said {said[:200]!r}")
        if odd:
            return dict.fromkeys(guids, f"perhaps named by a file of the target: `git grep` gave a line this reader "
                                        f"does not know ({odd!r})")
        named = dict.fromkeys(found_named, "named by a file of the target")
        for group in _capped(sorted(found_named)):
            # Each GUID here is named already, so a failed read of which file names it loses no check; its lines are
            # kept only when the grep read every file (exit 0, nothing on stderr), else the plain words stay.
            # `--null` ends the path with a NUL, as git writes it; without
            # it git quotes a non-ASCII path (`"Assets/\303\204 b/..."`, or not, by the user's `core.quotePath`).
            done = self._grep(candidate, group, commit, binary="-a", mode="-F", output=("-o",), null=True)
            out = done.stdout if done.returncode == 0 and not done.stderr.strip() else ""
            for line in out.splitlines():
                where, _, guid = line[len(commit) + 1:].rpartition("\0")
                if guid in named and named[guid] == "named by a file of the target":
                    named[guid] = f"named by {where} of the target"
        return named

    def _scene_baseline(self, task_id: str, wt: Path, commit: str, plan: ScenePlan, baseline: dict,
                        notes: list[str]) -> None:
        """The scene baseline `plan` needs, on the target checked out at `wt`: `baseline["problems"]` when a run (or
        a kept result) gives one, and `baseline["scene"]`, what was made."""
        need = self._scene_need(plan, commit)
        baseline["scene"] = need
        out_dir = self.run_dir(task_id) / "baseline"
        if need[0] == "skip":
            return
        if need[0] == "full":
            problems = self._scene_baseline_full(wt, commit, out_dir, notes, wt.name)  # wt: the candidate
        elif not need[1]:
            notes.append("scene baseline: none needed: every changed scene and prefab is new, not on the target")
            problems = frozenset()
        else:
            agent_package.install(wt, None, self.package_source)
            self._say("baseline: scene check on the target")
            try:
                run = run_scene_check(self.editor, wt, need[1], out_dir)
            finally:
                agent_package.uninstall(wt)
            problems = run.problems
            if problems is not None:
                notes.append(f"scene baseline: this land's check of the changed paths on the target {commit[:12]} "
                             f"({len(need[1])} of them)")
        if problems is not None:
            baseline["problems"] = set(problems)

    def _scene_baseline_full(self, wt: Path, commit: str, out_dir: Path, notes: list[str],
                             slot: str) -> frozenset | None:
        """The problems of every scene and prefab at `commit` in `slot` (the folder `wt`): the result kept for that
        commit in that slot, or a full run here, kept when complete. None when the run left no report. A result made in
        another slot, or by another Library under that slot's name, is never read. The run is under the
        lock of the commit and the slot, a guard: a slot has one owner, so no land waits on it (its wait,
        at most `SCENE_WAIT_S`, then exit 2, is not in the land's bound)."""
        library = self.workspaces.slot_id(wt)
        kept = self.scene_store.get(commit, slot, library)
        if kept is None:
            self._say(f"baseline: scene check on the target {commit[:12]} in slot {slot}, every scene and prefab")
            with file_lock(self.scene_store.path(commit, slot).with_suffix(".lock"), FlowError, timeout=SCENE_WAIT_S,
                           what=f"the full scene check for {commit[:12]} in slot {slot}"):
                kept = self.scene_store.get(commit, slot, library)
                if kept is None:
                    agent_package.install(wt, None, self.package_source)
                    try:
                        run = run_scene_check(self.editor, wt, (), out_dir)
                    finally:
                        agent_package.uninstall(wt)
                    full = FullScenes.of(run)
                    if full is not None:
                        self.scene_store.put(commit, slot, library, full)
                        notes.append(f"scene baseline: this land's full run on the target {commit[:12]} ({run.scenes} "
                                     f"scenes, {run.prefabs} prefabs; kept for that commit in slot {slot})")
                    elif run.problems is not None:
                        notes.append(f"scene baseline: this land's full run on the target {commit[:12]}, not kept: "
                                     f"{FullScenes.why_not(run)}")
                    else:
                        notes.append(f"scene baseline: none: the full run on the target {commit[:12]} left no report "
                                     f"({FullScenes.why_not(run)}), so every scene problem counts as new")
                    return run.problems
        notes.append(f"scene baseline: kept full result of the target {commit[:12]} in slot {slot} ({kept.scenes} "
                     f"scenes, {kept.prefabs} prefabs; a full run on that commit in this slot, or the full check of the "
                     "merge that made it here)")
        return kept.problems

    def _keep_scenes(self, commit: str, candidate: Path, run) -> None:
        """The merge's full scene check is its commit's in the candidate's slot and Library, so once it lands a later
        land on it there reads it. Only a complete full run (`FullScenes.of`) of this land's merge, after its checks
        passed."""
        full = FullScenes.of(run) if run is not None else None
        if commit and full is not None:
            self.scene_store.put(commit, candidate.name, self.workspaces.slot_id(candidate), full)

    @contextmanager
    def _candidate_at(self, task_id: str, candidate: Path, commit: str):
        """The candidate's slot checked out at the target `commit` for a run on the target, then back
        on the candidate's branch at the commit it held. Its commits stay on that branch; Unity's rewrites in the tree
        are dropped, as after the checks. Back on the branch by any way out; one that does not get back is a
        `FlowError` (the next land makes the candidate again from the target)."""
        branch = CANDIDATE_PREFIX + task_id
        head = self._git(candidate, "rev-parse", "HEAD").strip()
        self._git(candidate, "reset", "-q", "--hard", "HEAD")
        self._git(candidate, "checkout", "-q", "--detach", commit)
        try:
            yield candidate
        finally:
            # Unity makes files of its own, and git refuses to check out over those the branch tracks.
            self._git(candidate, "reset", "-q", "--hard", "HEAD")
            coming = self._git(candidate, "diff", "--name-only", "--no-renames", "--diff-filter=A", "-z", "HEAD", head)
            # Only those paths are asked, not the slot's whole Library (in groups capped by length).
            for group in _capped([path for path in coming.split("\0") if path]):
                # Git refuses `--literal-pathspecs` beside `GIT_GLOB_PATHSPECS` or `GIT_ICASE_PATHSPECS`.
                untracked = self._git(candidate, "--literal-pathspecs", "ls-files", "-z", "--others", "--", *group,
                                      env=_pathspec_env())
                for path in set(untracked.split("\0")) - {""}:
                    (candidate / path).unlink()
            self._git(candidate, "checkout", "-q", branch)
            if self._git(candidate, "rev-parse", "HEAD").strip() != head:
                raise FlowError(f"the candidate {candidate} is not back at {head[:12]} after the run on the target; "
                                f"run `upe land {task_id}` again")

    def _hook_scripts(self, cwd: Path, commit: str, pattern: str = HOOK_PATTERN) -> int:
        """How many of the target's scripts name a hook, by the allow list's scan (`git grep`): the reach of the open
        case of an existing hook; with `pattern` "ScriptableObject", the reach of an existing one's load code."""
        # Every `.cs` read as text and the output's shape fixed, whatever git's attributes or the
        # user's config say (as `_meta_tree` and `_guids_named`); `-E` fixes the pattern's kind (the scan's patterns are
        # extended); the pathspec read as a glob, whatever git's pathspec variables say.
        done = self._grep(cwd, [pattern], commit, binary="-a", mode="-E", output=("-l",), pathspecs=("*.cs",))
        return len([line for line in done.stdout.splitlines() if line.strip()])

    def _grep(self, cwd: Path, patterns: Sequence[str], commit: str, *, binary: str, mode: str,
              output: Sequence[str] = (), pathspecs: Sequence[str] = (), null: bool = False, strict: bool = False,
              on_line: Callable[[str], None] | None = None) -> "GrepResult":
        """Every `git grep` of the tool, of one commit. It fixes what git's
        attributes, the user's config and git's environment would change in the output: `--no-color`, no line number,
        no column; the pathspec variables removed (`_pathspec_env`); the tool's safe options (`SAFE_GIT`). Each caller
        says how a file is read (`binary`: `-a` every file as text, or `-I` no binary file) and the pattern's kind
        (`mode`: `-G`, `-E` or `-F`), its output options (`-l`, `-o`, `-h`; `null` for `--null`), its patterns (each
        by `-e`, so one that starts with `-` is never an option) and its pathspecs.

        `on_line` given: stdout is passed to it line by line as git writes it (a large output is never held), and
        `stdout` is "". `strict`: an exit other than 0 and 1, or any word on stderr (real git exits 0 when it cannot
        read a blob and other files match), raises `FlowError`; else the caller reads `returncode` and `stderr`."""
        if binary not in ("-a", "-I") or mode not in ("-G", "-E", "-F"):
            raise ValueError(f"git grep: binary {binary!r}, mode {mode!r}")
        tail = [*output, *[x for p in patterns for x in ("-e", p)], commit, *(["--", *pathspecs] if pathspecs else [])]
        argv = ["git", *SAFE_GIT, "-C", str(cwd), "grep", binary, mode, "--no-color", "--no-line-number", "--no-column",
                *(["--null"] if null else []), *tail]
        if on_line is None:
            done = self._runner(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                env=_pathspec_env(), stdin=subprocess.DEVNULL)
            result = GrepResult(done.returncode, done.stdout or "", done.stderr or "")
        else:
            # stderr goes to a file: a pipe left unread could fill and stall the grep.
            with tempfile.TemporaryFile() as errors, subprocess.Popen(
                    argv, stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                    errors="replace", env=_pathspec_env()) as every:
                for line in every.stdout:
                    on_line(line)
                every.wait()
                errors.seek(0)
                result = GrepResult(every.returncode, "", errors.read().decode("utf-8", errors="replace"))
        if strict and (result.returncode not in (0, 1) or result.stderr.strip()):
            raise FlowError(f"git grep {' '.join(tail)} failed ({result.returncode}): {result.stderr.strip()}")
        return result

    def _skip_note(self, candidate: Path, plan: ScenePlan, commit: str) -> str:
        files = [p for p in plan.added if not p.endswith(".meta")]
        kinds = sorted({("." + p.rsplit(".", 1)[1].lower()) if "." in p.rsplit("/", 1)[-1] else "(none)" for p in files})
        hooks = self._hook_scripts(candidate, commit)
        note = (f"scene check on the merge: skipped; every path it adds is a passive asset or a script inside a test "
                f"assembly ({len(plan.added)} files, {', '.join(kinds) or 'only .meta'}), and none changes or deletes a "
                f"tracked file. Not seen by the allow list: {OPEN_EXISTING_HOOK} (the target has {hooks} scripts that "
                "name a hook, by its scan)")
        if any(p.lower().endswith(LOAD_SUFFIXES) for p in files):  
            loads = self._hook_scripts(candidate, commit, LOAD_PATTERN)
            note += (f"; and {OPEN_EXISTING_LOAD} (the target has at least {loads} scripts that name ScriptableObject, "
                     "StateMachineBehaviour, PlayableAsset or TrackAsset; a class derived through a base class of the "
                     "project is not counted)")
        if any(p.lower().endswith(".cs") for p in files):
            note += f"; and {OPEN_HOOK_BASE}"
        return note

    def _plan_note(self, plan: ScenePlan) -> str:
        if plan.kind == "paths":
            return (f"scene check on the merge: the changed scenes and prefabs only ({len(plan.paths)}); other scenes "
                    "and prefabs are not loaded, also when the merge changes code (an open case, "
                    "DESIGN.md)")
        more = len(plan.reasons) - 5
        return (f"scene check on the merge: every scene and prefab, because {'; '.join(plan.reasons[:5])}"
                + (f"; and {more} more" if more > 0 else ""))

    # The target's tests -----------------------------------------
    #
    # `test-baselines/<commit>.json` is read only through `self.kept` (`KeptStore.get`, `nearest`) and written only
    # by `KeptStore.put`, which takes a `Kept`, a full result: by `_full_target_run` (the target's full run, before the
    # merge or in the slot of the target's run) and by `_keep` (the merge's run). A class-only run is a `ClassRun`,
    # written only under `runs/<task>/target-classes/`.

    @property
    def kept(self) -> KeptStore:
        return KeptStore(self.state_dir / "test-baselines")

    def _confirm(self, project: Path, results: Path, run: TestRun, notes: list[str], what: str) -> TestRun:
        """The re-run of the failed tests the CLI did not retry, after a full run whose result is kept. Off with
        `test_retries` 0: the user asked for no retries, and every failure stays unconfirmed."""
        if self.config.test_retries <= 0 or not run.finished:
            return run
        untried = (run.failed or set()) - run.twice - run.retried
        if untried:
            self._say(f"{what}: re-run of {len(untried)} tests the CLI did not retry (classes "
                      f"{', '.join(sorted({run.classes.get(t) or class_of(t) for t in untried}))}; up to "
                      f"{self.config.test_timeout_s} s)")
        confirmed, note = confirm_failures(self.cli, project, results, run, self.config.test_timeout_s)
        if note:
            notes.append(note)
        return confirmed

    def _distance(self, ancestor: str, commit: str) -> int | None:
        """How many commits `commit` has that `ancestor` has not, or None when `ancestor` is not an ancestor of it."""
        if self._runner(["git", *SAFE_GIT, "-C", str(self.config.repo), "merge-base", "--is-ancestor", ancestor, commit],
                        capture_output=True).returncode != 0:
            return None
        count = self._git(self.config.repo, "rev-list", "--count", f"{ancestor}..{commit}", check=False).strip()
        return int(count) if count.isdigit() else None

    def _look_up(self, worktree: Path, results: Path) -> TargetTests:
        """Before the merge, in the candidate at the target. A kept result of the target's own: nothing runs on
        it (case 1). Else the nearest ancestor's full result in the new format: nothing runs before the merge (case 2).
        Else the full suite runs here, and its result is kept (case 3)."""
        commit = self._git(worktree, "rev-parse", "HEAD").strip()
        own = self.kept.get(commit)
        if own is not None:
            self._say(f"baseline: tests on {commit[:12]} already known")
            return TargetTests(commit, own=own)
        ancestor = self.kept.nearest(commit, lambda other: self._distance(other, commit))
        if ancestor is not None:
            self._say(f"baseline: no result of its own for {commit[:12]}; the nearest ancestor with one is "
                      f"{ancestor[0][:12]}, {ancestor[2]} commit{'' if ancestor[2] == 1 else 's'} before")
            return TargetTests(commit, ancestor=ancestor)
        notes: list[str] = []
        before = self.kept.path(commit).exists()
        own = self._full_target_run(worktree, results, notes)
        return TargetTests(commit, own=own, notes=notes, ran=own is not None and not before)

    def test_baseline(self, worktree: Path, results: Path) -> set[str] | None:
        """The tests that failed even once on the target, from its full run (kept per commit); None when the tests did
        not finish there. It stays as the full run's thin form."""
        kept = self._full_target_run(worktree, results, [])
        return set(kept.seen) if kept is not None else None

    def _full_target_run(self, worktree: Path, results: Path, notes: list[str], what: str = "baseline") -> Kept | None:
        """The target's full EditMode run at the worktree's commit, with its re-run, kept for the commit; or the
        result already kept for it. None when the tests did not finish there (compile error, crash, timeout).
        Two lands on one target commit run it once: the second waits for the first's result."""
        commit = self._git(worktree, "rev-parse", "HEAD").strip()
        cache = self.kept.path(commit)
        wait = self.config.test_timeout_s + BASELINE_WAIT_SLACK_S  # counted in `land_limit_s`
        with file_lock(cache.with_suffix(".lock"), FlowError, timeout=wait, what=f"the test baseline for {commit[:12]}"):
            known = self.kept.get(commit)
            if known is not None:
                self._say(f"{what}: tests on {commit[:12]} already known")
                return known
            self._say(f"{what}: EditMode tests on the target {commit[:12]} (up to {self.config.test_timeout_s} s)")
            run = run_tests(self.cli, worktree, results, self.config.test_timeout_s, retries=self.config.test_retries)
            cases = run_counts(run, results) if run.finished else 0
            if not cases:
                return None
            # Kept before its re-run: a Ctrl-C or a kill in the re-run keeps the finished full run, its
            # untried failures unconfirmed (row e for them, never row d); the re-run then only refines it.
            self.kept.put(commit, Kept.of(run, cases))
            run = self._confirm(worktree, results, run, notes, what)
            kept = Kept.of(run, cases)
            self.kept.put(commit, kept, refine=True)
            return kept

    def _keep(self, task_id: str, commit: str, run: TestRun | None) -> None:
        """The merge's test run is the run at its commit, so if it lands, the next land need not repeat it, and a land
        on a commit after it can use it as an ancestor's result.

        Keyed by commit. A report that hides failures only makes later lands stricter, and one that adds them fails
        this land. Kept only when it finished and lists tests, and never over a result already kept."""
        results = self.run_dir(task_id) / "merged" / "upe-test-results.xml"
        if not commit or run is None or not run.finished or not results.exists():
            return
        cases = run_counts(run, results)
        if cases:
            self.kept.put(commit, Kept.of(run, cases))

    @contextmanager
    def _target_slot(self, task_id: str, commit: str, notes: list[str]):
        """A slot of its own for the land's run on the target, detached at `commit`, owned by `base:<task>`:
        nothing the merge's run left in the candidate is in it. Parked again at the end, by any way out; one an Editor
        still holds is named in the notes and parked by the next land or `finish` (`_discard_candidate`)."""
        owner = BASE_OWNER + self._checked(task_id)
        left = self._park(owner, f"the slot of {task_id}'s run of tests on the target", "its run of tests on the target")
        if left:  # one a killed land left, which will not park
            raise WorkspaceError(left)
        path = self.workspaces.acquire(owner, None, commit)
        try:
            yield path
        except BaseException as exc:
            try:
                self.workspaces.release(owner, discard_changes=True)
            except Exception as failed:
                exc.add_note(f"The slot of the run on the target, {path}, was not parked ({failed}): the next land of "
                             f"{task_id}, or `upe finish {task_id}`, parks it")
            raise
        try:
            self.workspaces.release(owner, discard_changes=True)
        except Exception as exc:  # any: the run is done, so a slot that will not park is a note
            notes.append(f"the slot of the run on the target, {path}, was not parked ({exc}): the next land of {task_id}, "
                         f"or `upe finish {task_id}`, parks it")

    def _checked(self, task_id: str) -> str:
        self.workspaces.check_id(task_id)
        return task_id

    def _class_run(self, task_id: str, commit: str, run: TestRun, notes: list[str]) -> ClassRun:
        """On the target, only the classes of the merge's failed tests, once, with no retry. Its report and its
        class list go under `runs/<task>/target-classes/`, never to `test-baselines/`."""
        classes = sorted({run.classes.get(name) or class_of(name) for name in run.failed or ()})
        folder = self.run_dir(task_id) / "target-classes"
        folder.mkdir(parents=True, exist_ok=True)
        results = folder / "upe-test-results.xml"
        write_atomic(folder / "classes.json", json.dumps({"commit": commit, "classes": classes}, indent=2) + "\n")
        with self._guard(task_id, "the tests on the target"), self._target_slot(task_id, commit, notes) as slot:
            self._say(f"target: tests of {len(classes)} classes at {commit[:12]} (slot {slot.name}; up to "
                      f"{self.config.test_timeout_s} s)")
            # In groups when the class list is longer than one command line takes (`run_tests_by_classes`).
            alone = run_tests_by_classes(self.cli, slot, results, self.config.test_timeout_s, classes)
        return ClassRun(commit, tuple(classes), frozenset(alone.failed) if alone.finished else None, frozenset(alone.ran))

    def _known(self, task_id: str, look: TargetTests, run: TestRun) -> tuple[set[str], list[str]]:
        """Which of the merge's failed tests are known, by the guard of `kept.decide`, and the notes that say how each
        was decided. May run the failed classes on the target (case 2), then the target's full run (row e)."""
        notes: list[str] = []
        failed = run.failed or set()
        alone = None
        if look.own is None and look.ancestor is not None:
            alone = self._class_run(task_id, look.commit, run, notes)
        decision = known_failures(failed, look.own, look.ancestor, alone)
        if look.ran:  # case 3: the target's own result is this land's full run on it
            decision.known = {name: BY_FULL_RUN for name in decision.known}
        if alone is not None and not alone.finished:
            notes.append(f"not compared: the run of the failed tests' classes on the target {look.commit[:12]} left no "
                         f"report, so every failed test counts as new; results in "
                         f"{self.run_dir(task_id) / 'target-classes'}")
        elif alone is not None:
            # A test the run did not name was not compared (a class the target does not
            # have, or a filter that did not reach it); one it named did not fail alone on the target.
            unnamed = sorted(name for name in decision.new if name not in alone.ran)
            alone_ok = sorted(name for name in decision.new if name in alone.ran)
            if unnamed:
                notes.append(f"not compared: the run of the failed tests' classes on the target {look.commit[:12]} did "
                             "not name these (a class the target does not have, or a filter that did not reach them), "
                             f"so each counts as new: {', '.join(unnamed)}")
            if alone_ok:
                notes.append(f"did not fail when its class ran alone on the target {look.commit[:12]}, so it is new: "
                             f"{', '.join(alone_ok)}")
        if decision.new and decision.need_full:
            notes.append(f"not compared, as other failed tests are new (the target's full run would have decided "
                         f"them): {', '.join(sorted(decision.need_full))}")
        if not decision.new and decision.need_full:
            self._say(f"target: {len(decision.need_full)} failed tests fail alone on the target, and the ancestor's "
                      "result does not list them as failed on every attempt: the full run on the target")
            with self._guard(task_id, "the tests on the target"), \
                    self._target_slot(task_id, look.commit, notes) as slot:
                self._say(f"target: full EditMode run at {look.commit[:12]} (slot {slot.name})")
                own = self._full_target_run(slot, self.run_dir(task_id) / "target-full" / "upe-test-results.xml", notes,
                                            what="target")
            if own is None:
                notes.append(f"the full run on the target {look.commit[:12]} did not finish, so every failed test "
                             "counts as new")
                return set(), notes
            decision.known = {name: BY_FULL_RUN for name in failed if name in own.seen}
        by = {how: sorted(name for name, h in decision.known.items() if h == how)
              for how in (BY_OWN, BY_CLASSES, BY_FULL_RUN)}
        if by[BY_OWN]:
            notes.append(f"known by the kept result of the target {look.commit[:12]}: {', '.join(by[BY_OWN])}")
        if by[BY_CLASSES]:
            a, _, far = look.ancestor
            notes.append(f"known by a class-only run on the target {look.commit[:12]} and the full result of ancestor "
                         f"{a[:12]} ({far} commit{'' if far == 1 else 's'} before): {', '.join(by[BY_CLASSES])}. Open "
                         f"case: a test that failed in {a[:12]}'s full run on every attempt, was fixed on the target "
                         "since by commits the tool did not check, fails when its class runs alone, and is broken "
                         "again by this branch, is taken as known here; and, kept as failed in this merge's result, "
                         "it is known to the next land too")
        if by[BY_FULL_RUN]:
            notes.append(f"known by a full run on the target {look.commit[:12]} (kept): {', '.join(by[BY_FULL_RUN])}")
        return set(decision.known), notes

    def _landed_since(self, task: Task) -> list[Task]:
        """Tasks that landed on the target after this one started: the other side of the merge."""
        return self._landed_among(task, self._readable_tasks())

    def _landed_among(self, task: Task, records: list[Task]) -> list[Task]:
        out = []
        for other in records:
            if other.task_id == task.task_id or not other.landed_commit:
                continue
            ancestor = self._runner(
                ["git", *SAFE_GIT, "-C", str(self.config.repo), "merge-base", "--is-ancestor", other.landed_commit, task.base],
                capture_output=True,
            ).returncode == 0
            if not ancestor:
                out.append(other)
        return out

    def _briefs(self, task: Task, records: tuple[list[Task], list[str]] | None = None) -> dict[str, str]:
        briefs = {f"{task.task_id} (the branch being merged)": task.brief}
        records, broken = records or self._task_records()  # one read, so a record is either shown or named
        for other in self._landed_among(task, records):
            briefs[f"{other.task_id} (landed on {task.target} after this task started)"] = other.brief
        for name in broken:
            briefs[f"{name} (its task record does not read)"] = (
                "Not shown: if this task landed after the one being merged started, its brief and claim are missing here.")
        return briefs

    def _claims(self, task: Task, records: tuple[list[Task], list[str]] | None = None) -> dict[str, list[str]]:
        claims = {task.task_id: task.claim}
        readable, broken = records or self._task_records()
        for other in self._landed_among(task, readable):
            claims[other.task_id] = other.claim
        for name in broken:
            claims[f"{name} (its task record does not read; its claim is not known)"] = []
        claims["any task may write these, unclaimed"] = list(self.config.shared)
        return claims

    # Decisions -----------------------------------------------------------

    def _act(self, task: Task, report: MergeReport, outcome: str, reason: str) -> Outcome:
        if outcome == "land":
            try:
                self._check_landing(task, report)
                self.merger.land(report)
            except (MergeError, FlowError) as exc:
                self._discard_candidate(task.task_id, f"run `upe land {task.task_id}` again")
                self._save(task, "working", str(exc))
                return Outcome(task.task_id, "working", f"not landed: {exc}. The verdicts were on that merge only: "
                                                        f"run `upe land {task.task_id}` again, and a council votes on the new one")
            task.landed_commit = report.committed
            self._save(task, "landed", f"{report.committed[:12]} on {report.target} ({reason})")
            self.registry.release(task.task_id)
            message = f"landed {report.committed[:12]} on {report.target}: {reason}"
            try:
                self._discard_candidate(task.task_id, f"run `upe finish {task.task_id}`")
            except (WorkspaceError, FlowError) as exc:
                message += f"; the land's slots not all parked yet, `upe finish {task.task_id}` tries again: {exc}"
            if self.config.refresh_golden_after_land:
                try:
                    message += "; " + self.refresh_golden()
                except FlowError as exc:
                    message += f"; golden not refreshed: {exc}"
            return Outcome(task.task_id, "landed", message)
        if outcome == "refuse":
            self._discard_candidate(task.task_id, f"run `upe finish {task.task_id}`")
            self._save(task, "refused", reason)
            return Outcome(task.task_id, "refused", f"refused: {reason}; findings in {self.run_dir(task.task_id) / 'council.md'}")
        self._save(task, "awaiting-user", reason)
        return Outcome(task.task_id, "awaiting-user",
                       f"the council split ({reason}); read {self.run_dir(task.task_id) / 'council.md'}, then decide land or refuse")

    def _check_landing(self, task: Task, report: MergeReport) -> None:
        """Land only the merge that was checked and reviewed, and only on the configured target.

        The run records are files any agent can write, so nothing here trusts
        them: the target comes from the config, and the commit must still be
        the candidate branch's tip with the task branch's tip inside it.
        """
        target = self.config.target
        if report.target != target:
            raise FlowError(f"the merge record names target {report.target}, but the configured target is {target}")
        tip = self._git(self.config.repo, "rev-parse", "--verify", "-q", f"refs/heads/{report.candidate}", check=False).strip()
        if tip != report.committed:
            raise FlowError(f"{report.candidate} is not at the commit the checks and the council saw; land again")
        branch_tip = self._git(self.config.repo, "rev-parse", "--verify", "-q", f"refs/heads/{BRANCH_PREFIX}{task.task_id}", check=False).strip()
        if not branch_tip or self._runner(
            ["git", *SAFE_GIT, "-C", str(self.config.repo), "merge-base", "--is-ancestor", branch_tip, report.committed],
            capture_output=True,
        ).returncode != 0:
            raise FlowError(f"{BRANCH_PREFIX}{task.task_id} moved after the merge; land again")
        self.check_target()

    def check_target(self) -> None:
        """Refuse a target that tracks a remote, or that someone has checked out outside the tool's slots."""
        target = self.config.target
        if self._git(self.config.repo, "config", "--get", f"branch.{target}.remote", check=False).strip():
            raise FlowError(f"{target} tracks a remote: the tool lands only on a local branch of its own "
                            f"(git branch --unset-upstream {target})")
        if not self.config.protect_checked_out_target:
            return
        listing = self._git(self.config.repo, "worktree", "list", "--porcelain")
        path = None
        for line in listing.splitlines():
            if line.startswith("worktree "):
                path = Path(line[len("worktree "):])
            elif line == f"branch refs/heads/{target}" and path is not None and not _under(path, self.workspaces.slots_dir):
                raise FlowError(f"{target} is checked out in {path}; the tool does not move a branch someone works on")

    def decide(self, task_id: str, choice: str) -> Outcome:
        """The user's call after a split council. The command line asks for a person at a terminal."""
        with self._task_lock(task_id):
            return self._decide(task_id, choice)

    def _decide(self, task_id: str, choice: str) -> Outcome:
        if choice not in ("land", "refuse"):
            raise FlowError("choose land or refuse")
        task = self.task(task_id)
        if task.state != "awaiting-user":
            raise FlowError(f"task {task_id} is {task.state}, not waiting for a decision")
        report = report_from_json((self.run_dir(task_id) / "merge.json").read_text(encoding="utf-8"))
        return self._act(task, report, choice, f"user chose {choice}")

    # The Editor, for the agent working on a task ------------------------
    #
    # An agent reaches the Editor only through these, and only in its own slot,
    # so the judge sees every Editor and two agents never share one.

    def _slot(self, task_id: str) -> Path:
        task = self.task(task_id)
        if task.state in ("landed", "finished"):
            raise FlowError(f"task {task_id} is {task.state}; it has no slot")
        return self.workspaces.workspace(task_id).path

    def editor_open(self, task_id: str) -> dict:
        """Open a batch-mode Editor in the task's slot and wait until it answers. Close it when done."""
        result = self.cli.open(self._slot(task_id), batchmode=True, until_ready=True, timeout=900)
        if not result.ok:
            raise FlowError(f"the Editor did not open: {result.error_code or result.exit_code}")
        return {"task": task_id, "editor": "open", "data": result.data}

    def editor_close(self, task_id: str, force: bool = False) -> dict:
        result = self.cli.close(self._slot(task_id), force=force)
        if not result.ok:
            raise FlowError(f"the Editor did not close: {result.error_code or result.exit_code}; try --force")
        return {"task": task_id, "editor": "closed", "data": result.data}

    def editor_status(self, task_id: str) -> dict:
        path = self._slot(task_id)
        return {"task": task_id, "editor": "open" if lock_state(path) == "held" else "closed", "slot": str(path)}

    def editor_test(self, task_id: str, test_filter: str | None = None) -> dict:
        """EditMode tests in the task's slot. Its Editor must be closed: `unity test` starts its own."""
        path = self._slot(task_id)
        if lock_state(path) == "held":
            raise FlowError(f"an Editor has {path} open; close it first (upe editor close {task_id})")
        results = path / "Logs" / "upe-agent-test-results.xml"
        before = self._dirt(path)
        judging.declare(self.state_dir, task_id, "testing")
        try:
            run = run_tests(self.cli, path, results, self.config.test_timeout_s,
                            extra=("--filter", test_filter) if test_filter else (), retries=self.config.test_retries)
        finally:
            judging.declare(self.state_dir, task_id, "editing")
        return {"task": task_id, "finished": run.finished, "exit_code": run.exit_code, "error_code": run.error_code,
                "failed": sorted(run.failed or ()), "flaky": sorted(run.flaky), "tests": run_counts(run, results) if run.finished else 0, "results": str(results),
                **self._after_editor(task_id, path, before)}

    def editor_run(self, task_id: str, method: str, args: Sequence[str] = (), graphics: bool = False) -> dict:
        """Run one static Editor method in a batch Editor in the task's slot, then quit. `graphics` keeps a GPU device."""
        path = self._slot(task_id)
        log = path / "Logs" / f"upe-run-{method.rsplit('.', 1)[-1]}.log"
        before = self._dirt(path)
        run = self.editor.batch(path, log, "-executeMethod", method, *args, quit=True, graphics=graphics)
        return {"task": task_id, "method": method, "exit_code": run.exit_code, "log": str(run.log),
                **self._after_editor(task_id, path, before)}

    def _dirt(self, path: Path) -> tuple[set[str], set[str]]:
        """Tracked files with changed content, and untracked files, in a slot."""
        modified = set(p for p in self._git(path, "diff", "--name-only", "--no-renames", "-z", "HEAD").split("\0") if p)
        new = set(p for p in self._git(path, "ls-files", "-z", "--others", "--exclude-standard").split("\0") if p)
        return modified, new

    def _after_editor(self, task_id: str, path: Path, before: tuple[set[str], set[str]]) -> dict:
        """Unity rewrites files of its own when it opens a project (research/05). Put back the tracked
        files the run changed outside the claim, which would stop the land, and name the new files."""
        modified, new = self._dirt(path)
        claim = self.registry.all().get(task_id) or Claim.of(task_id, self.task(task_id).claim)
        rewritten = sorted(modified - before[0])
        restore = unclaimed(rewritten, claim, self.config.shared)
        # In groups capped by length (a run can rewrite thousands of files). Not all or nothing: a group that
        # fails raises, after the groups before it restored theirs. What is left stays changed in the slot: the next
        # Editor command takes it as there before it (`before`), so no later run puts it back, and `land` refuses the
        # slot's uncommitted work until a person restores it.
        groups = list(_capped(restore))
        for k, group in enumerate(groups, 1):
            try:
                # Each path as written, not a glob. `Das[h]/y.cs` read as a glob also names the
                # agent's claimed `Dash/y.cs`, whose uncommitted work the checkout would lose. Git refuses the flag
                # beside `GIT_GLOB_PATHSPECS` or `GIT_ICASE_PATHSPECS`, so the call runs without them.
                self._git(path, "--literal-pathspecs", "checkout", "--", *group, env=_pathspec_env())
            except FlowError as exc:
                left = [p for g in groups[k - 1:] for p in g]
                said = str(exc).rsplit(" failed: ", 1)[-1][:300]  # git's words, not the whole command line
                # The files left are written down, and the step restores those only: `git status` in the
                # slot also lists the agent's own uncommitted work in its claim, which a restore of each would lose.
                listing = self.run_dir(task_id) / "restore-left.txt"
                listing.parent.mkdir(parents=True, exist_ok=True)
                listing.write_text("".join(p + "\0" for p in left), encoding="utf-8", newline="")
                raise FlowError(f"the restore of {len(restore)} files Unity rewrote outside the claim stopped at group "
                                f"{k} of {len(groups)}: git said {said!r}. {len(left)} files are left changed in {path} "
                                f"({', '.join(left[:5])}{', ...' if len(left) > 5 else ''}); no later run puts them "
                                f"back, and `land` refuses the slot until they are restored. They are listed in "
                                f"{listing}; restore those only (`git status` also lists your own uncommitted work): "
                                f"`git -C \"{path}\" --literal-pathspecs restore --pathspec-file-nul "
                                f"--pathspec-from-file=\"{listing}\"`") from None
        created = sorted(new - before[1])
        return {"restored": restore, "new_files": created,
                "new_files_outside_claim": unclaimed(created, claim, self.config.shared)}

    # A slot's uncommitted changes, for `land` and `finish` --------

    def _slot_dirt(self, task: Task, path: Path) -> "Dirt":
        """The slot's uncommitted changes, split by the task's claim: the reads of `_dirt` (`git diff --name-only HEAD`:
        the work tree against HEAD; `ls-files --others --exclude-standard`: untracked), and the index against HEAD
        (`git diff --cached`: a staged change put back on disk, `MM`, and a staged new file deleted from
        disk, `AD`, are not in the first), then `unclaimed`'s rules, as the land's claim check reads a commit (no case,
        a `.meta` of a claimed asset and a folder `.meta` on the way to one are claimed, a shared file is claimed). The
        claim is the record's: it never changes after `start`. Doubt is inside: a record claim that does not read, or a
        path `normalize` refuses (git gives none such)."""
        modified, new = self._dirt(path)
        modified |= {p for p in self._git(path, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD").split("\0")
                     if p}
        if MANIFEST in modified and self._only_the_tools_manifest(path):
            modified.discard(MANIFEST)  # a task that claims it: `start` added the tool's package, not the task
        try:
            claim: Claim | None = Claim.of(task.task_id, task.claim)
        except ClaimError:
            claim = None
        allowed = Claim("shared", tuple(normalize(s) for s in self.config.shared)) if self.config.shared else None
        inside, outside, shared = [], [], []
        for p in sorted(modified | new):
            try:
                out = claim is not None and bool(unclaimed([p], claim, self.config.shared))
                if not out and claim is not None and allowed is not None and not claim.covers(p) and allowed.covers(p):
                    shared.append(p)
            except ClaimError:
                out = False
            (outside if out else inside).append(p)
        stuck = [p for p in sorted(new) if p.endswith("/")] + self._gitlinks(path, sorted(modified))
        return Dirt(sorted(modified), sorted(new), inside, outside, stuck, shared)

    def _gitlinks(self, path: Path, paths: list[str]) -> list[str]:
        """Of `paths`, the submodules: a gitlink (mode 160000) in the slot's index or in HEAD."""
        found: set[str] = set()
        for group in _capped(paths):
            for args in (("ls-files", "-s", "-z", "--"), ("ls-tree", "-z", "HEAD", "--")):
                # git's pathspec variables cleared: `GIT_ICASE_PATHSPECS` refuses `--literal-pathspecs`
                done = self._runner(["git", *SAFE_GIT, "-C", str(path), "--literal-pathspecs", *args, *group],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    env=_pathspec_env())
                if done.returncode != 0:
                    raise FlowError(f"git {' '.join(args)} failed: {(done.stderr or '').strip()}")
                for line in (done.stdout or "").split("\0"):
                    meta, _, name = line.partition("\t")
                    if name and meta.split()[:1] == ["160000"]:
                        found.add(name)
        return sorted(found)

    def _only_the_tools_manifest(self, path: Path) -> bool:
        """Whether the slot's manifest differs from HEAD's only by the tool's own packages, which `start` adds and
        `finish` takes out (`agent_package`). Read as JSON; anything that does not read is a change (doubt is work)."""
        head = self._blob(path, "HEAD", MANIFEST)
        text = _read(path / MANIFEST)
        if head is None or text is None:
            return False
        try:
            stripped = agent_package.strip_tool_packages(text.decode("utf-8"), head.decode("utf-8"))
            return stripped is not None and json.loads(stripped) == json.loads(head.decode("utf-8"))
        except (ValueError, AttributeError, TypeError):
            return False

    def _ignored_in_claim(self, task: Task, path: Path) -> list[str]:
        """What git ignores in the slot in or around the claim (`ls-files --others --ignored --exclude-standard
        --directory`, a wholly ignored folder as one entry): no commit holds it, so no land merges it. A file is listed
        when the claim covers it, a folder when the claim covers it or a claimed entry lies under it. The local copies
        (`local_copies`) and the tool's own package and `.slnx` are not listed: the tool puts them there."""
        try:
            claim = Claim.of(task.task_id, task.claim)
        except ClaimError:
            return []
        own = [line.lstrip("/") for line in agent_package.exclude_lines(path)] + list(self.config.local_copies)
        own = [o.replace("\\", "/").lower().rstrip("/") for o in own if o.strip()]
        found = []
        for p in self._git(path, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory").split("\0"):
            low = p.lower().rstrip("/")
            if not p or any(low == o or low.startswith(o + "/") for o in own):
                continue
            try:
                if claim.covers(p) or (p.endswith("/") and any(
                        Entry(e).literal_prefix.startswith(low + "/") for e in claim.entries)):
                    found.append(p)
            except ClaimError:
                continue
        return found

    def _restore_steps(self, task_id: str, path: Path, dirt: "Dirt", paths: list[str]) -> str:
        """The printed git steps that discard the uncommitted changes of `paths` (a tracked path put back to HEAD, an
        untracked file deleted), at most STEP_PATHS paths; with more, the step says to run `finish` again. Each step
        says, in its own words, what it discards and that it is lost if it is the task's work: the tool
        cannot tell whose a change is. A nested repository or a submodule gets no step: `clean -f` and
        `restore` leave it, so `finish` would refuse again with the same step. Every word single-quoted (`_sh`), with
        `--literal-pathspecs` so a `[`, `*` or `?` in a name matches only that name, and `core.quotepath` off."""
        stuck = [p for p in paths if p in set(dirt.stuck)]
        tracked_set = set(dirt.tracked)
        named = [p for p in paths if p not in set(stuck)][:STEP_PATHS]
        tracked = [p for p in named if p in tracked_set]
        untracked = [p for p in named if p not in tracked_set]
        git = f"git --literal-pathspecs -C {_slot_word(path)}"
        steps = []
        if tracked:
            steps.append(f"`{git} restore --source=HEAD --staged --worktree -- {' '.join(_sh(p) for p in tracked)}` "
                         f"discards the uncommitted change{'' if len(tracked) == 1 else 's'} to "
                         f"{_quoted(tracked)} (changed, deleted, staged or not), which {'is' if len(tracked) == 1 else 'are'} "
                         "lost if the task's work")
        if untracked:
            steps.append(f"`{git} clean -f -- {' '.join(_sh(p) for p in untracked)}` deletes the untracked "
                         f"file{'' if len(untracked) == 1 else 's'} {_quoted(untracked)}, lost if the task's work")
        rest = len(paths) - len(stuck) - len(named)
        more = f"; then run `upe finish {task_id}` again for the other {rest}" if rest else ""
        posix = (" (written for Git Bash: a name holds `'`, which PowerShell reads otherwise)"
                 if any("'" in p for p in named) else "")
        return " and ".join(steps) + more + posix

    def _discard_text(self, task_id: str, path: Path, dirt: "Dirt", paths: list[str], when: str) -> str:
        """What to do with the changes of `paths` that no land has: `when` (the condition), the printed
        steps, then `finish` again; and for a nested repository or a submodule, for which the tool gives no step, one
        sentence of its own: what it is, that there is no step, and what the user decides. Never both orders in one."""
        stuck = [p for p in paths if p in set(dirt.stuck)]
        steps = self._restore_steps(task_id, path, dirt, paths)
        text = f"{when}, {steps}, and then run `upe finish {task_id}` again." if steps else ""
        if stuck:
            one = len(stuck) == 1
            it, them = ("it", "it") if one else ("them", "they")
            text += (f"{' ' if text else ''}{_quoted(stuck)} {'is a nested repository or a submodule' if one else 'are nested repositories or submodules'}, "
                     f"which `git clean -f` and `git restore` leave as {'it is' if one else 'they are'}, so the tool gives "
                     f"no step for {it}: tell the user, who decides whether to keep {it} (moved out of the slot, or "
                     f"committed in {'its' if one else 'their'} own repository) or to delete {it}; once {them} "
                     f"{'is' if one else 'are'} gone from the slot, run `upe finish {task_id}` again.")
        return text

    def _dirt_notes(self, task: Task, path: Path, dirt: "Dirt") -> list[str]:
        """What a land that goes on says of the slot (in its output, `checks.md` and the job log): the uncommitted paths
        outside the claim, with the steps `finish` will need, and what git ignores in the claim. The tool cannot tell
        whose a change outside the claim is (Unity's, or the task's own made outside its claim): it says only that the
        land does not have it."""
        notes = []
        if dirt.outside:
            n = len(dirt.outside)
            notes.append(f"{n} uncommitted path{'' if n == 1 else 's'} outside the claim stay{'s' if n == 1 else ''} in "
                         f"the slot {path} and {'is' if n == 1 else 'are'} not in the merge (a land merges only commits): "
                         f"{_quoted(dirt.outside[:10])}{f' and {n - 10} more' if n > 10 else ''}. If one is the task's "
                         "work, this land does not have it, and the claim cannot be changed: tell the user. "
                         f"`upe finish {task.task_id}` refuses while they are there. "
                         + self._discard_text(task.task_id, path, dirt, dirt.outside,
                                              "Once you know none is the task's work").rstrip("."))
        try:
            ignored = self._ignored_in_claim(task, path)
        except FlowError as exc:
            return notes + [f"the files git ignores in the claim were not listed ({exc})"]
        if ignored:
            n = len(ignored)
            notes.append(f"{n} path{'' if n == 1 else 's'} in or around the claim {'is' if n == 1 else 'are'} ignored by "
                         f"git, so no commit holds {'it' if n == 1 else 'them'} and the merge does not have "
                         f"{'it' if n == 1 else 'them'}: {_quoted(ignored[:10])}{f' and {n - 10} more' if n > 10 else ''}. "
                         "If one is the task's work, tell the user: git must stop ignoring it before it can land")
        return notes

    # Priority judge ------------------------------------------------------

    def sessions(self) -> dict[str, Path]:
        """Every worktree the tool owns that an Editor might have open, by session name."""
        known = {}
        for slot, owner in self.workspaces.slots().items():
            if owner:
                known[owner] = self.workspaces.slots_dir / slot
        known["golden"] = self.config.golden
        return known

    def judge_once(self, judge: "judging.Judge | None" = None, lister=None, setter=None, sleep=None) -> judging.Decision:
        """Look at the machine, let the judge decide, apply it, and record both."""
        judge = judge or judging.Judge(max_high=self.config.max_high, memory_short_mb=self.config.memory_short_mb)
        kwargs = {"sleep": sleep} if sleep else {}
        snap, pids = judging.snapshot(self.sessions(), judging.activities(self.state_dir),
                                      lister or (lambda: judging.list_processes(self._runner)), **kwargs)
        decision = judge.decide(snap)
        failures = judging.apply(decision, pids, setter or judging.set_priority_class,
                                 judging.priority_classes(self.config.editor_priority))
        record = {"snapshot": json.loads(snap.to_json()), "priorities": decision.priorities, "reasons": decision.reasons,
                  "summary": decision.summary, "failures": failures}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "judge.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(record) + "\n")
        return decision

    # Golden --------------------------------------------------------------

    def _golden_record(self) -> dict[str, str]:
        path = self.state_dir / "golden.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except ValueError as exc:  # an error the tool names (exit 2), not a traceback after a claim
            raise FlowError(f"{path} does not read ({exc}); tell the user") from exc
        if not isinstance(record, dict):
            raise FlowError(f"{path} does not read (not an object); tell the user")
        return record

    def create_golden(self) -> str:
        """Make the golden checkout: a worktree of its own at the target, owned by the tool."""
        golden = self.config.golden
        if golden.exists():
            raise FlowError(f"{golden} already exists")
        # The target by its full name (a short name reads `refs/<target>` or a tag first).
        self._git(self.config.repo, "worktree", "add", "--detach", str(golden), f"refs/heads/{self.config.target}")
        self.workspaces.copy_local(golden)
        self._write_golden_record({"path": str(golden), "commit": ""})
        return self.refresh_golden()

    def _write_golden_record(self, record: dict[str, str]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "golden.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    def refresh_golden(self) -> str:
        """Move the golden checkout to the target and let Unity import the difference.

        Only a golden checkout the tool made (golden --create) is moved, and
        it is reset by force: nobody works in it. Any other checkout may hold
        someone's branch or work, so it is left alone.
        """
        golden = self.config.golden
        if not golden.exists():
            raise FlowError(f"no golden checkout at {golden}; run golden --create")
        if self._golden_record().get("path") != str(golden):
            raise FlowError(f"{golden} was not made by the tool, so it is not moved; refresh it yourself, "
                            "or point golden at a new path and run golden --create")
        if lock_state(golden) == "held":
            raise FlowError(f"golden {golden} is open in an Editor")
        commit = self._git(self.config.repo, "rev-parse", f"refs/heads/{self.config.target}").strip()  # by full name
        self._git(golden, "checkout", "-q", "--force", "--detach", commit)
        self._git(golden, "clean", "-q", "-fd", "--", "Assets", "Packages", "ProjectSettings")
        run = self.editor.refresh(golden, self.state_dir / "golden.log")
        if run.exit_code != 0:
            raise FlowError(f"golden import failed (Editor exited {run.exit_code}); see {run.log}")
        self._write_golden_record({"path": str(golden), "commit": commit})
        return f"golden Library imported at {commit[:12]}"

    # Finish --------------------------------------------------------------

    def finish(self, task_id: str) -> Outcome:
        """Remove the task's worktree. Its branch goes too once landed; otherwise it is kept.

        A task with no record is a `start` that died: see `_clear_start`."""
        inside = False
        try:
            with self._task_lock(task_id):
                inside = True
                return self._finish(task_id) if self.has_record(task_id) else self._clear_start(task_id)
        except FlowError:
            if inside or self.has_record(task_id):
                raise
            facts = self.start_facts(task_id, self.registry.all().get(task_id)) or Land(running=True)
            raise FlowError(f"task {task_id}: another upe command is working on it, so nothing was freed. "
                            f"{next_step(task_id, 'starting', facts)}") from None

    def _clear_start(self, task_id: str) -> Outcome:
        """Free what a `start` that died left: its claim, its slot and its branch `upe/<task>`, under the task's lock.

        The lock is the proof that no `start` of the task runs: `start` holds it from before its claim to its end,
        so `finish` could not have taken it while one runs. Only a marked claim (made by such a `start`) is freed; a
        claim made by an older version is refused. Every check comes first, and nothing is freed when one refuses: the
        slot (only the one the slot map gives this task now) while anything in it changed in the last SLOT_QUIET_S, or
        while an Editor has it, or when it holds work (`Workspaces.losses`); the branch when it has commits no other
        branch or tag has. Then the slot is taken away (not parked: its Library may be half copied), the branch
        deleted, and the claim freed last, so a step that fails leaves a state this clears again.
        """
        claim = self.registry.all().get(task_id)
        path = self.workspaces.path_of(task_id)
        branch = BRANCH_PREFIX + task_id
        has_branch = bool(self._git(self.config.repo, "branch", "--list", branch).strip())
        if claim is None and path is None and not has_branch:
            raise FlowError(f"no task {task_id}")
        refused = f"task {task_id}: nothing was freed"
        if claim is not None and not claim.locked:
            facts = self._bounded(Land(running=None, unmarked=True), _stamp_time(claim.since), limit_s=OLD_CLAIM_TELL_S)
            raise FlowError(f"{refused}. {next_step(task_id, 'starting', facts)}")
        if path is not None and path.exists():
            if lock_state(path) == "held":
                raise FlowError(f"{refused}: an Editor has its slot {path} open. Tell the user")
            changed = self.workspaces.recent_change(path, SLOT_QUIET_S)
            if changed is not None:
                raise FlowError(f"{refused}: {changed} in its slot {path} changed in the last {_hours(SLOT_QUIET_S)}, so "
                                "something may still write there (the Library copy of a start that died runs on after "
                                f"it). Run `upe finish {task_id}` again in a few minutes; if it says the same for over "
                                "an hour, tell the user")
        tips = {f"its branch {branch}": f"refs/heads/{branch}"} if has_branch else {}
        if path is not None and path.exists() and not (path / ".git").exists():
            # A removal git stopped half way deletes `.git` first. The tool checked that slot before it
            # began, and marked it; a folder with no `.git` and no mark is not known to the tool.
            if not self.workspaces.removal_begun(path.name, task_id):
                raise FlowError(f"{refused}: its slot {path} is not a git worktree, and the tool did not begin removing "
                                f"it, so it does not know what the folder holds. Tell the user: once they have looked in "
                                f"it and deleted it, `upe finish {task_id}` frees the rest")
        elif path is not None and path.exists():
            try:
                self.workspaces._check_slot(path)
                lost = self.workspaces.losses(path)
                head = self._git(path, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
            except (WorkspaceError, FlowError) as exc:
                raise FlowError(f"{refused}: its slot {path} does not read as one of the tool's worktrees ({exc}). "
                                "Tell the user") from exc
            if lost:
                # Every file, each quoted (a name may hold ", "), as the delete line names them; past 5,
                # the command that lists them too.
                listed = (f" (`git -C \"{path}\" status --porcelain --untracked-files=all` lists them)" if len(lost) > 5 else "")
                raise FlowError(f"{refused}: its slot {path} holds work: {_quoted(lost)}{listed}. The work, the "
                                f"claim, the slot and the branch are kept. Tell the user"
                                f"{self._files_step(task_id, path, lost, head, branch, has_branch)}")
            if head:
                tips[f"its slot's HEAD ({head[:12]})"] = head  # a commit made on a detached HEAD
        if tips:
            for name, tip in tips.items():
                own = self._own_commits(tip, branch)
                if own:
                    newest = self._git(self.config.repo, "log", "-1", "--format=%h %s", tip).strip()
                    step = ""
                    if tip != f"refs/heads/{branch}":
                        # A slot last parked at a merge candidate a land threw away (its branch deleted)
                        # holds that commit on its HEAD, and `reset --hard` keeps HEAD: only moving HEAD clears it.
                        onto = branch if has_branch else self.config.target
                        lock, wait = self._stale_lock(path, task_id)
                        step = wait or (f": {self._keep_step(path, task_id, tip)}; if they say that commit is not "
                                        f"wanted (a slot last parked at a merge "
                                        f"candidate a land threw away holds one), {lock}"
                                        f"`git -C \"{path}\" checkout -q --detach {onto}` moves "
                                        f"the slot off it, and then `upe finish {task_id}` clears the rest")
                    raise FlowError(f"{refused}: {name} has {len(own)} commit{'' if len(own) == 1 else 's'} no other "
                                    f"branch, tag or remote has (the newest: {newest}). The branch, its slot and the "
                                    f"claim are kept. Tell the user{step}")
        freed = []
        try:
            slot = self.workspaces.remove_slot(task_id)
        except WorkspaceError as exc:
            raise FlowError(f"task {task_id}: its slot could not be taken away ({exc}); its claim and branch are "
                            f"kept. Run `upe finish {task_id}` again in a few minutes; if it says the same, tell the user"
                            ) from exc
        if slot:
            freed.append(f"slot {slot}")
        if has_branch:
            self._git(self.config.repo, "branch", "-D", branch)
            freed.append(f"branch {branch}")
        if claim is not None:
            self.registry.release(task_id)
            freed.insert(0, "its claim")
        return Outcome(task_id, "finished", f"a start that died, cleared: freed {', '.join(freed)}")

    def _own_commits(self, tip: str, branch: str, also: Sequence[str] = ()) -> list[str]:
        """The commits of `tip` that no branch but `branch`, no tag, no remote ref and none of `also` has.

        Every other branch, tag and remote ref is read by name and given as a negative; no worktree's HEAD is. Not
        `git rev-list upe/<id> --not --exclude=refs/heads/upe/<id> --all`: `--all` takes in each
        worktree's HEAD, and when `finish` checks, the dead start's slot has that branch's tip checked out, so a branch
        with a commit of its own read as having none. Spelled with `--branches`, it
        gives none too: there `--exclude` is matched without the `refs/heads/` prefix, so it excludes nothing.
        """
        others = [ref for ref in self._git(self.config.repo, "for-each-ref", "--format=%(refname)", "refs/heads",
                                           "refs/tags", "refs/remotes").split() if ref != f"refs/heads/{branch}"]
        negatives = "".join(f"^{ref}\n" for ref in [*others, *also])
        return self._git(self.config.repo, "rev-list", "--stdin", tip, input=negatives).split()

    def _files_step(self, task_id: str, path: Path, lost: list[str], head: str, branch: str, has_branch: bool) -> str:
        """The user's step for a dead start's slot that holds work, or "" when the tool has none to give (a step the tool tells the user is
        printed by the tool, and a test runs what it prints).

        Row 14 a: a start killed after `allow_manifest_edits` leaves the manifest the tool changed; when it is the one
        file, putting it back clears it. Row 14 b: a start killed in the `checkout -b` of a reused slot leaves some files
        of the new commit, with the old index (and no `upe/<id>` yet: git makes the branch after it writes the files);
        `reset --hard` alone keeps a file only the new commit has, so the step names where to: `upe/<id>`, else the
        target. Both steps overwrite the files, so they are given only for the user to run once they say the files
        are not wanted. No git step when HEAD is on a branch other than `upe/<id>` (`reset` would move that branch):
        the user restores or deletes the files themselves (rb2, RB2-D).

        A file git does not track and `<onto>` does not have is left by the reset (an agent's new file, or one the
        target deleted since): the step names it, to delete once the user says it is not wanted. When the slot is
        already at `<onto>` and only such files are left, no reset is printed, only that (council rb1, RB-1). A git
        that died in the slot leaves its `index.lock`, which stops both git steps: they name it first (RB-2)."""
        yourself = (f": if they say those files are not wanted, they restore or delete them in \"{path}\" themselves, "
                    f"and then run `upe finish {task_id}` again")
        lock, wait = self._stale_lock(path, task_id)
        if lost == [MANIFEST]:
            return wait or (f": if they say that change is not wanted (one cause: a start killed after the tool added "
                            f"its packages to the manifest), {lock}`git -C \"{path}\" checkout -- {MANIFEST}` puts the "
                            f"file back, and then run `upe finish {task_id}` again")
        attached = self._git(path, "symbolic-ref", "-q", "HEAD", check=False).strip()
        if not head or attached not in ("", f"refs/heads/{branch}"):
            return yourself  # rb2, RB2-D: no git step, as `reset` would move that branch
        onto = branch if has_branch else self.config.target
        untracked = set(p for p in self._git(path, "ls-files", "-z", "--others", "--exclude-standard").split("\0") if p)
        kept_by_onto = set(p for p in self._git(path, "ls-tree", "-r", "-z", "--name-only", onto).split("\0") if p)
        left = [p for p in lost if p in untracked and p not in kept_by_onto]
        # Every one named, each relative to the slot, which is named with them (rb2, RB2-B, RB2-C).
        # Each name quoted, so a name that holds ", " reads back as one. git does not allow
        # `"` in a path on Windows, where the tool runs.
        names = f"from \"{path}\": {_quoted(left)}"
        at_onto = head == self._git(path, "rev-parse", "--verify", "-q", f"{onto}^{{commit}}", check=False).strip()
        if at_onto and len(left) == len(lost):
            return (f": its slot is at {onto} already, and {onto} does not have those files, so no git step clears "
                    f"them. If they say they are not wanted, they delete them ({names}), and then run "
                    f"`upe finish {task_id}` again")
        if wait:
            return wait
        dropped = self._own_commits(head, branch, also=(f"refs/heads/{branch}",) if has_branch else ())
        commit = ""
        keep = ""
        if dropped:
            keep = f"{self._keep_step(path, task_id, head, len(dropped), clears=False)}; then, "
            newest = self._git(self.config.repo, "log", "-1", "--format=%h %s", head).strip()
            commit = (f", nor the {len(dropped)} commit{'' if len(dropped) == 1 else 's'} on its HEAD that no other "
                      f"branch, tag or remote has (the newest: {newest}; a slot last parked at a merge candidate a land "
                      f"threw away holds one)")
        rest = (f"; it leaves the files {onto} does not have: if they say those are not wanted either, they delete "
                f"them ({names})" if left else "")
        return (f": {keep}if they say those files are not wanted{commit} (one cause: a start killed while git wrote a reused "
                f"slot's files), {lock}`git -C \"{path}\" reset -q --hard {onto}` puts the slot at "
                f"{onto}{rest}, and then run `upe finish {task_id}` again")

    def _keep_step(self, path: Path, task_id: str, head: str, count: int = 1, clears: bool = True) -> str:
        """The step that keeps a commit on a slot's detached HEAD, given before any step that moves the slot
        off it, with a branch name the tool chose, so it runs as printed. Not under `upe/`: the tool deletes those.
        `clears`: nothing else in the slot holds work, so `finish` then clears it; with files that hold work the step
        only keeps the commit, and the rest of the refusal says what clears the files."""
        keep = (f"to keep {'that commit' if count == 1 else 'those commits'}, `git -C {_slot_word(path)} branch "
                f"upe-kept/{task_id}-{head[:8]} {head}` puts {'it' if count == 1 else 'them'} on a branch of its own")
        return keep + (f", and then `upe finish {task_id}` clears the rest" if clears else "")

    def _stale_lock(self, path: Path, task_id: str) -> tuple[str, str]:
        """The slot's `index.lock`, as (the words that name it before a git step, the words that take the step's place).

        Git takes `<gitdir>/index.lock` before it writes the index, and a git killed mid-checkout leaves it, which stops
        every git step that writes the index ("File exists") (council rb1, RB-2). The lock is not in the slot folder,
        so `recent_change` did not read it: a lock written in the last SLOT_QUIET_S, or in the future (read with
        `os.stat`, as `recent_change` reads), may be a git that still runs, and no step is given; the user runs
        `finish` again later (rb2, RB2-A). An older one is named, to delete if no git runs in that slot."""
        gitdir = self._git(path, "rev-parse", "--absolute-git-dir", check=False).strip()
        if not gitdir:
            return "", ""
        lock = Path(gitdir) / "index.lock"
        try:
            written = os.stat(lock).st_mtime
        except FileNotFoundError:
            return "", ""
        if written > time.time() - SLOT_QUIET_S:
            return "", (f": a git may still run in that slot (`{lock}` was written in the last "
                        f"{_hours(SLOT_QUIET_S)}). Run `upe finish {task_id}` again in a few minutes; it then gives "
                        f"the step")
        return (f"first delete `{lock}` (a lock a git that was stopped left), if no git runs in that slot, then ",
                "")

    def _finish(self, task_id: str) -> Outcome:
        task = self.task(task_id)
        if task.state == "awaiting-user":
            raise FlowError(f"task {task_id} is waiting for a decision")
        path = self.workspaces.path_of(task_id)
        ignored: list[str] = []
        if path is not None and (path / ".git").exists():
            # Read first, before anything changes (`uninstall` puts back the manifest), and refused on
            # what `release` refuses (any uncommitted change), now with each path named and, outside the claim, the
            # printed step that puts it back. A slot that is not a worktree goes on to `remove`, which refuses it.
            dirt = self._slot_dirt(task, path)
            if dirt.inside or dirt.outside:
                raise FlowError(self._finish_refusal(task_id, path, dirt))
            # A commit on the slot's HEAD that no branch, tag or remote has (made on a detached HEAD) stays
            # only on the parked slot's HEAD, and the next task to take the slot moves off it: kept first, by a step.
            head = self._git(path, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
            # `finish` deletes a landed task's own branch below, so for a landed task that branch keeps
            # nothing: a commit made on it after the land would be held only by the parked slot.
            own = self._own_commits(head, BRANCH_PREFIX + task_id if task.state == "landed" else "") if head else []
            if own:
                newest = self._git(self.config.repo, "log", "-1", "--format=%h %s", head).strip()
                raise FlowError(f"task {task_id}: its slot's HEAD ({head[:12]}) has {len(own)} commit"
                                f"{'' if len(own) == 1 else 's'} no branch, tag or remote has (the newest: {newest}), "
                                "which the next task to take the slot would leave behind, so it was not parked and "
                                f"nothing was freed: {self._keep_step(path, task_id, head, len(own))}. If the commit is "
                                "the task's work that should land, tell the user")
            try:
                ignored = self._ignored_in_claim(task, path)
            except FlowError:
                ignored = []
        if task.state == "landed":
            # `finish` deletes a landed task's branch below. A commit made on it after the land,
            # with the slot's HEAD moved off it (`checkout --detach HEAD~1`, `checkout -b elsewhere HEAD~1`), is held by
            # that branch alone: kept first, by the printed step, whatever the slot's HEAD holds.
            branch = BRANCH_PREFIX + task_id
            tip = self._git(self.config.repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).strip()
            own = self._own_commits(tip, branch) if tip else []
            if own:
                newest = self._git(self.config.repo, "log", "-1", "--format=%h %s", tip).strip()
                raise FlowError(f"task {task_id}: its branch {branch} has {len(own)} commit"
                                f"{'' if len(own) == 1 else 's'} no other branch, tag or remote has (the newest: "
                                f"{newest}), made after the land; `finish` deletes a landed task's branch, so it was not "
                                f"parked and nothing was freed: {self._keep_step(path or self.config.repo, task_id, tip, len(own))}"
                                ". If the commit is the task's work that should land, tell the user")
        self._discard_candidate(task_id)  # one a conflict left behind
        if path is not None:
            agent_package.uninstall(path)
            self.workspaces.remove(task_id)
        self.registry.release(task_id)
        kept = ""
        branch = BRANCH_PREFIX + task_id
        if task.state == "landed":
            self._git(self.config.repo, "branch", "-D", branch, check=False)
        else:
            kept = f"; branch {branch} kept"
        self._save(task, "finished", f"worktree parked for reuse{kept}")
        left = ""
        if ignored:  # the next task that takes the slot clears what git ignores (`_scrub`), the Library aside
            n = len(ignored)
            left = (f". {n} path{'' if n == 1 else 's'} in or around the claim {'is' if n == 1 else 'are'} ignored by git "
                    f"and stay{'s' if n == 1 else ''} in the parked slot {path} until the next task takes it, which "
                    f"deletes {'it' if n == 1 else 'them'}: {_quoted(ignored[:10])}{f' and {n - 10} more' if n > 10 else ''}"
                    ". If one is the task's work, tell the user now")
        return Outcome(task_id, "finished", f"worktree parked for reuse{kept}{left}")

    def _finish_refusal(self, task_id: str, path: Path, dirt: Dirt) -> str:
        """`finish`'s refusal of a slot with uncommitted changes: each path named, by side of the claim;
        for those outside it, the printed git steps that discard them, each saying it may be the task's work; for those
        inside, no step while the task can still land them, and once it has landed the steps, to run only
        once the user says the changes are not wanted. If `finish` refuses again with the same paths, the skill says to
        tell the user."""
        text = f"task {task_id}: its slot {path} has uncommitted changes, so it was not parked and nothing was freed."
        landed = self.task(task_id).state == "landed"
        if dirt.inside:
            n = len(dirt.inside)
            listed = f"{_quoted(dirt.inside[:10])}{f' and {n - 10} more' if n > 10 else ''}"
            if landed:
                text += (f" Inside the claim ({listed}): work of the task that no commit holds, and the task has "
                         "landed, so no land can take it now: tell the user. "
                         + self._discard_text(task_id, path, dirt, dirt.inside, "Once they say it is not wanted"))
            else:
                text += (f" Inside the claim ({listed}): work of the task that no commit holds; commit it on "
                         f"{BRANCH_PREFIX}{task_id} (and land it again), or tell the user. The tool gives no step that "
                         "discards it while the task can still land it.")
        if dirt.outside:
            n = len(dirt.outside)
            text += (f" Outside the claim ({_quoted(dirt.outside[:10])}{f' and {n - 10} more' if n > 10 else ''}): no "
                     "land has these, and the tool cannot tell whether they are the task's work or Unity's; if one is "
                     "the task's work, tell the user. "
                     + self._discard_text(task_id, path, dirt, dirt.outside, "Once you know none is"))
        return text


# `ARGV_CAP` and `_capped` are `cmdline.ARGV_CAP` and `cmdline.capped`.
# A GUID reference in a text file of the target: `guid: <hex>` as Unity writes it in YAML (scenes, prefabs, assets,
# the import settings of a .meta), `m_GUID: <hex>` (Addressables), `"guid": "<hex>"`, escaped or not (JSON assets
# such as a Shader Graph), and `guid=<hex>` (UI Toolkit's `project://database/...` URLs in .uss, .uxml and .tss: the
# one grep of the whole target, not one pattern a GUID.
# Missed by design: a GUID written bare, after no `guid` key: a Shader Graph's `"m_FunctionSource": "<hex>"` (an
# .hlsl), a baking set's list of scenes (`- <hex>`), prose and commands; on a large project, a small share of the GUIDs another
# file names, none of a kind the allow list skips (an .hlsl, a scene).
GUID_REFERENCE = r'[gG][uU][iI][dD][\"\\]*[:=][ \"\\]*[0-9a-fA-F]{32}'
_HEX32 = re.compile(r"[0-9a-f]{32}")  # a GUID as `_guids_named` keeps it (lower case)


@dataclass(frozen=True)
class GrepResult:
    """What one `Flow._grep` gave: git's exit code, its stdout ("" when read line by line) and its stderr."""
    returncode: int
    stdout: str
    stderr: str


def _pathspec_env() -> dict[str, str]:
    """This process's environment without git's pathspec variables: with
    `GIT_GLOB_PATHSPECS=1` the `*` of `Assets/*.meta` no longer crosses `/`, and with `GIT_LITERAL_PATHSPECS=1` it
    matches nothing, nor would a `:(glob)` magic. The greps whose pathspecs the checks rest on run with this, and
    the calls that pass `--literal-pathspecs`, which git refuses beside two of the variables."""
    return pathspec_free_env()


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _strings(path: Path, data: dict, key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise FlowError(f"{path}: {key} must be a list of strings, like [\"Assets/X\"]")
    return tuple(value)


def _project_paths(path: Path, data: dict, key: str) -> tuple[str, ...]:
    """A list of paths inside the project (`test_folders`), with forward slashes; `..`, an absolute path or
    an empty one is refused, as a claim is (`claims.normalize`)."""
    from .claims import ClaimError, normalize

    values = _strings(path, data, key)
    for value in values:
        try:
            normalize(value)
        except ClaimError:
            raise FlowError(f"{path}: {key}: {value!r} is not a path inside the project; give it relative to repo, "
                            "like \"Assets/Tests/\"") from None
    return tuple(v.strip().replace("\\", "/") for v in values)


TEST_FOLDER_NAMES = ("test", "tests")
TEST_FOLDERS_SHOWN = 10


def test_folders_in(paths: Sequence[str]) -> list[str]:
    """The folders named Test or Tests, any case, that hold a file of `paths` (a commit's tree), each to its first such
    part with a trailing `/`, in order, at most TEST_FOLDERS_SHOWN (`setup --check` reads them at the
    target, so an agent finds them before it claims)."""
    found: list[str] = []
    for path in paths:
        parts = path.split("/")[:-1]
        at = next((i for i, part in enumerate(parts) if part.lower() in TEST_FOLDER_NAMES), None)
        if at is not None:
            folder = "/".join(parts[:at + 1]) + "/"
            if folder not in found:
                found.append(folder)
    return found[:TEST_FOLDERS_SHOWN]


def _flag(path: Path, data: dict, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise FlowError(f"{path}: {key} must be true or false, without quotes")
    return value


def _under(path: Path, folder: Path) -> bool:
    """`path` is `folder` or inside it, compared the way Windows compares paths."""
    import os

    p, f = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(folder))
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def _quoted(paths: Sequence[str]) -> str:
    """Each path in double quotes, so a name that holds ", " reads back as one. git does not
    allow `"` in a path on Windows, where the tool runs."""
    return ", ".join('"' + p + '"' for p in paths)


def _sh(text: str) -> str:
    """One word of a printed command, in single quotes: `$`, a backtick, a space, `"` and `\\` are then
    plain text in Git Bash and in PowerShell alike. A `'` in it is written as POSIX shells read it (`'\\''`), which
    PowerShell does not; the step says so where one occurs."""
    return "'" + text.replace("'", "'\\''") + "'"


def _slot_word(path: Path | str) -> str:
    """A slot's path in a printed step, in double quotes: Git Bash, PowerShell and cmd.exe
    all read it as one word, since `setup` and `setup --check` refuse a root holding `$`, a backtick, `'`, `"` or `%`, or
    starting with `\\\\` (`setup.root_problem`). The dead-start steps write it the same way."""
    return f'"{path}"'


def render_checks(report: CheckReport) -> str:
    lines = ["## Findings"] + ([f"- {f.path} [{f.check}] {f.message}" for f in report.findings] or ["- none"])
    lines += ["", "## Notes"] + ([f"- {n}" for n in report.notes] or ["- none"])
    return "\n".join(lines) + "\n"


def report_from_json(text: str) -> MergeReport:
    data: Mapping[str, Any] = json.loads(text)
    report = MergeReport(data["task_id"], data["branch"], data["target"], data["candidate"], data["base"])
    report.steps = [Step(**s) for s in data["steps"]]
    report.regenerate = list(data["regenerate"])
    report.replays = list(data["replays"])
    report.committed = data["committed"]
    return report
