"""`upe`: the flow from the command line. See flow.py, and skill/upe/SKILL.md for how agents use it.

Run it as the `upe` console script or `python -I -m upe`: plain `python -m upe`
puts the current folder first on the import path, so a branch that commits an
`upe/` package would run instead of the tool (DESIGN.md, "Security").
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .agent_package import AgentPackageError
from .claims import ClaimError, write_atomic
from .council import CouncilError
from .editor import EditorError
from .flow import Config, Flow, FlowError, next_step, process_alive, process_created, start_fields
from .identity import agent_process
from .judge import ACTIVITIES, BASES, JudgeError, activities, declare, set_priority_class
from .merge import MergeError
from .setup import SetupError, check as check_setup, checks_json, interactive as setup_interactive, render_checks
from .unity_cli import UnityCliError
from .workspace import TASK_ID, WorkspaceError

ERRORS = (FlowError, ClaimError, WorkspaceError, MergeError, AgentPackageError, EditorError, UnityCliError, JudgeError,
          CouncilError, SetupError)
HEAVY_COMMANDS = ("start", "land", "verdict", "decide", "finish", "golden", "editor")  # copy Libraries, run Editors
TOOL_HOME = Path(__file__).resolve().parents[2]  # this repo, where upe.toml lives
CLAIMS_WAIT = 5.0  # seconds `status` waits for the claims' lock; a change holds it for well under one
WAIT_POLL_S = 5.0  # `job --wait` reads the job's record this often


def default_config() -> Path:
    """UPE_CONFIG, else the upe.toml next to the tool. Never the current folder, which may be an agent's slot."""
    return Path(os.environ.get("UPE_CONFIG") or TOOL_HOME / "upe.toml")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="upe", description="Several agents on one Unity project.")
    ap.add_argument("--config", type=Path, default=None, help="path to upe.toml (default: UPE_CONFIG, else the tool's own)")
    ap.add_argument("--json", action="store_true", help="print one JSON object, for agents")
    ap.add_argument("--detach", action="store_true", help="run in the background; prints a job id for `upe job`")
    ap.add_argument("--job-id", dest="job_child", help=argparse.SUPPRESS)  # set on the background process
    sub = ap.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="claim paths and get a slot (a worktree with its own Library) for a task")
    start.add_argument("task_id")
    start.add_argument("--brief", required=True, help="the brief, or @file to read it from a file")
    start.add_argument("--claim", action="append", required=True, help="a path, folder/ or glob; repeat for more")
    start.add_argument("--base", help="start from this ref instead of the target")

    for name, text in (("land", "merge, check, write the council brief"), ("finish", "park the task's slot")):
        sub.add_parser(name, help=text).add_argument("task_id")

    verdict = sub.add_parser("verdict", help="a council member's GO or NO-GO, as JSON (see the council brief)")
    verdict.add_argument("task_id")
    verdict.add_argument("--member", required=True, help="who is voting: the agent's name and model")
    verdict.add_argument("--file", required=True, help="the verdict JSON, or - to read it from stdin")

    decide = sub.add_parser("decide", help="the user's call after a split council (asks at the terminal)")
    decide.add_argument("task_id")
    decide.add_argument("choice", choices=("land", "refuse"))

    status = sub.add_parser("status", help="list tasks and their states, or one task's whole history")
    status.add_argument("task_id", nargs="?")

    job = sub.add_parser("job", help="the state of a --detach job, and the end of its log")
    job.add_argument("job_id")
    job.add_argument("--wait", type=float, nargs="?", const=540.0, default=None, metavar="SECONDS",
                     help="wait until the job is done, at most SECONDS (540, under a 10-minute shell limit)")

    editor = sub.add_parser("editor", help="the Editor in a task's own slot")
    esub = editor.add_subparsers(dest="action", required=True)
    for name, text in (("open", "open a batch-mode Editor and wait until it answers"),
                       ("close", "close it"), ("status", "is it open")):
        e = esub.add_parser(name, help=text)
        e.add_argument("task_id")
        if name == "close":
            e.add_argument("--force", action="store_true")
    etest = esub.add_parser("test", help="EditMode tests in the slot (its Editor must be closed)")
    etest.add_argument("task_id")
    etest.add_argument("--filter", help="only tests whose names match")
    erun = esub.add_parser("run", help="run one static Editor method in a batch Editor, then quit")
    erun.add_argument("task_id")
    erun.add_argument("method", help="Namespace.Class.Method")
    erun.add_argument("args", nargs="*", help="extra command-line arguments for the Editor")
    erun.add_argument("--graphics", action="store_true", help="keep a GPU device (loading scenes with terrain needs one)")

    activity = sub.add_parser("activity", help="say what a session is doing, for the priority judge")
    activity.add_argument("session", help="the task id")
    activity.add_argument("kind", choices=ACTIVITIES)
    activity.add_argument("--note", default="", help="anything the judge should know, e.g. a person is waiting")

    judge = sub.add_parser("judge", help="share the CPU among the running Editors")
    judge.add_argument("--watch", action="store_true", help="keep going: on every activity change, and every judge_every_s")

    golden = sub.add_parser("golden", help="bring the golden Library up to the target")
    golden.add_argument("--create", action="store_true", help="make the golden worktree first")

    setup = sub.add_parser("setup", help="the user names this machine's paths and the tool writes upe.toml (at a terminal)")
    setup.add_argument("--check", action="store_true", help="read every path in upe.toml again; changes nothing")

    skill = sub.add_parser("skill", help="install the agents' skill into an app's skills folder")
    skill.add_argument("--to", action="append", required=True, type=Path,
                       help="a skills folder, e.g. ~/.claude/skills or ~/.codex/skills; repeat for more")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    utf8_output()
    if "--json" in argv[1:]:  # agents put it after the subcommand too; it means the same there
        argv = ["--json", *(a for a in argv if a != "--json")]
    args = parser().parse_args(argv)
    args.config = args.config or default_config()
    if args.command == "setup":
        return setup(args)
    if not args.config.exists():
        emit(args, {"error": f"no {args.config}"}, f"upe: no {args.config}: ask the user to run `upe setup` in a terminal; "
             "it asks for this machine's paths and writes it", stream=sys.stderr)
        return 2
    try:
        flow = Flow(Config.load(args.config), job_id=args.job_child or "")
        flow._say = lambda message: progress(flow, args, message)
        if args.detach:
            return detach(flow, args, argv)
        if args.command in HEAVY_COMMANDS:
            lower_own_priority(flow.config.editor_priority)
        if args.job_child:
            _job_update(flow, args.job_child, state="running", pid=os.getpid(), created=process_created(os.getpid()))
        code = run(flow, args)
    except ERRORS as exc:
        code = 2
        emit(args, {"error": _said(exc)}, f"upe: {_said(exc)}", stream=sys.stderr)
    except KeyboardInterrupt as exc:  # a code the skill reads; the command's clean-up ran first
        code = 2
        message = _interrupted(args, " ".join(getattr(exc, "__notes__", ())))
        emit(args, {"error": message}, f"upe: {message}", stream=sys.stderr)
    except Exception as exc:
        if not args.job_child:
            raise
        code = 3  # a background job records how it ended, even on a bug; the traceback is in its log
        import traceback

        traceback.print_exc()
        _job_update(Flow(Config.load(args.config)), args.job_child, state="done", exit_code=code,
                    error=f"{type(exc).__name__}: {exc}")
        return code
    if args.job_child:
        _job_update(Flow(Config.load(args.config)), args.job_child, state="done", exit_code=code)
    return code


def run(flow: Flow, args: argparse.Namespace) -> int:
    if args.command == "start":
        brief = Path(args.brief[1:]).read_text(encoding="utf-8") if args.brief.startswith("@") else args.brief
        task = flow.start(args.task_id, brief, args.claim, args.base)
        path = flow.workspaces.workspace(task.task_id).path
        tests = flow.tests_answer(path)
        emit(args, {"task": task.task_id, "state": task.state, "slot": str(path), "branch": f"upe/{task.task_id}",
                    "tests": tests},
             f"{task.task_id}: working in {path}\ntests: "
             + (f"{', '.join(tests['folders'])} (test_folders in upe.toml)" if tests["folders"] else
                f"no folder named in upe.toml (test_folders); this lists the files in the folders named Test or Tests: "
                f"{tests['find']}"))
    elif args.command == "activity":
        emit(args, declare(flow.state_dir, args.session, args.kind, args.note), f"{args.session}: {args.kind}")
    elif args.command == "judge":
        if args.watch:
            watch(flow)
        else:
            decision = flow.judge_once()
            emit(args, {"priorities": decision.priorities, "reasons": decision.reasons}, _render_judge(decision))
    elif args.command == "golden":
        text = flow.create_golden() if args.create else flow.refresh_golden()
        emit(args, {"golden": text}, text)
    elif args.command == "verdict":
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8-sig")  # PowerShell writes a BOM
        return outcome(args, flow.verdict(args.task_id, args.member, text))
    elif args.command == "decide":
        if not confirmed_by_a_person(args.task_id, args.choice):
            emit(args, {"error": "decide needs a person"}, "upe: decide is the user's call: run it yourself, "
                 "in a terminal, and type the task id when asked", stream=sys.stderr)
            return 2
        return outcome(args, flow.decide(args.task_id, args.choice))
    elif args.command == "status" and args.task_id:
        if not flow.has_record(args.task_id):  # only then read the claims: a task being started has none yet
            claim = flow.starting(CLAIMS_WAIT).get(args.task_id)
            facts = flow.start_facts(args.task_id, claim)  # with no claim: a `start --detach` still beginning, or None
            # A start that ends writes its record, then frees the lock: when the lock reads free, the record is looked
            # for again, so a start that ended in between is shown as its record, not as one that died.
            if facts is not None and not (facts.running is False and flow.has_record(args.task_id)):
                step = flow.starting_step(args.task_id, facts)
                emit(args, {"task": args.task_id, "state": "starting", **start_fields(claim, facts), "next": step},
                     f"{args.task_id}: starting ({_render_start(claim, facts)})\nnext: {step}")
                return 0
        seen = flow.situation_of(flow.task(args.task_id))
        task, state, land, step = seen.task, seen.state, seen.land, seen.step  # one read: the record judged
        emit(args, {"task": task.task_id, "state": state, "claim": task.claim, "history": task.history,
                    "stamps": task.stamps, "run_folder": str(flow.run_dir(task.task_id)), "next": step,
                    **(land.to_json() if land else {})},
             f"{task.task_id}: {state}\n" + (f"{_render_land(land)}\n" if land else "")
             + f"run folder: {flow.run_dir(task.task_id)}\nnext: {step}\n" + "\n".join(task.dated_history()))
    elif args.command == "status":
        # The claims first, then the records: a `start` that writes its record in between shows as a record.
        claims_error = ""
        try:
            starting = flow.starting(CLAIMS_WAIT)
        except ClaimError as exc:
            starting, claims_error = {}, f"the claims could not be read, so tasks being started are not listed: {exc}"
        records, broken = flow._task_records()  # a record that does not read is listed, not a crash
        rows = []
        for read in records:
            seen = flow.situation_of(read)
            t, state, land, step = seen.task, seen.state, seen.land, seen.step  # `last` and `since` of the record judged
            rows.append({"task": t.task_id, "state": state, "last": t.history[-1] if t.history else "",
                         "since": t.stamps[-1] if t.stamps else "",  # stamps are matched from the end
                         "next": step, **(land.to_json() if land else {})})
        rows += [{"task": name, "state": "unreadable", "last": "its task record does not read", "since": "",
                  "next": next_step(name, "unreadable")} for name in broken]
        listed = {r["task"] for r in rows}
        for task_id, claim in starting.items():
            if task_id not in listed:
                facts = flow.start_facts(task_id, claim)
                if facts.running is False and flow.has_record(task_id):  # it ended between the reads
                    try:
                        seen = flow.situation_of(flow.task(task_id))
                    except FlowError:  # shown as `broken` rows are, not dropped
                        rows.append({"task": task_id, "state": "unreadable", "last": "its task record does not read",
                                     "since": "", "next": next_step(task_id, "unreadable")})
                        continue
                    rows.append({"task": task_id, "state": seen.state, "last": seen.task.history[-1] if seen.task.history
                                 else "", "since": seen.task.stamps[-1] if seen.task.stamps else "", "next": seen.step,
                                 **(seen.land.to_json() if seen.land else {})})
                    continue
                rows.append({"task": task_id, "state": "starting", "last": "", "since": claim.since,
                             **start_fields(claim, facts), "next": flow.starting_step(task_id, facts)})
        listed = {r["task"] for r in rows}
        for task_id, facts in flow.starts_beginning().items():  # a `start --detach` not yet at its claim
            if task_id not in listed:
                rows.append({"task": task_id, "state": "starting", "last": f"job {facts.job} beginning", "since": "",
                             **start_fields(None, facts), "next": flow.starting_step(task_id, facts)})
        emit(args, {"tasks": rows, **({"claims_error": claims_error} if claims_error else {})},
             "\n".join([f"{r['task']:24} {r['state']:16} {r['since']:25} {r['last']}"
                        + (f"\n{'':24} next: {r['next']}" if _misleads(r) else "") for r in rows]
                       + ([f"upe: {claims_error}"] if claims_error else [])))
    elif args.command == "skill":
        written = install_skill(args.to, args.config, flow.config)
        emit(args, {"installed": [str(p) for p in written]}, "\n".join(f"installed {p}" for p in written))
    elif args.command == "job":
        record = flow.job_state(args.job_id)
        waited = {}
        if args.wait is not None:
            # How the wait ended, so a `timeout` (wait again) reads apart from a job that ended.
            began = time.monotonic()
            deadline = began + args.wait
            while record.get("state") != "done" and time.monotonic() < deadline:
                time.sleep(min(WAIT_POLL_S, max(0.0, deadline - time.monotonic())))
                record = flow.job_state(args.job_id)
            waited = {"wait": {"asked_s": args.wait, "waited_s": round(time.monotonic() - began, 1),
                               "ended": "done" if record.get("state") == "done" else "timeout"}}
        log = Path(record["log"])
        tail = log.read_text(encoding="utf-8", errors="replace")[-4000:] if log.exists() else ""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")  # UTC, as every time the tool writes
        line = (f"\nwaited {waited['wait']['waited_s']:g} s of {args.wait:g}: "
                f"{'done' if waited['wait']['ended'] == 'done' else 'not done, wait again'}" if waited else "")
        emit(args, {**record, **waited, "now": now, "log_tail": tail}, f"{args.job_id}: {record['state']}"
             + (f", exit {record['exit_code']}" if "exit_code" in record else "") + f" (now {now}){line}\n{tail}")
    elif args.command == "editor":
        if args.action == "open":
            result = flow.editor_open(args.task_id)
        elif args.action == "close":
            result = flow.editor_close(args.task_id, args.force)
        elif args.action == "status":
            result = flow.editor_status(args.task_id)
        elif args.action == "test":
            result = flow.editor_test(args.task_id, args.filter)
        else:
            result = flow.editor_run(args.task_id, args.method, args.args, args.graphics)
        emit(args, result, json.dumps(result, indent=2))
        if args.action == "test":
            return 0 if result["finished"] and not result["failed"] and result["tests"] else 1
        if args.action == "run":
            return 0 if result["exit_code"] == 0 else 1
    else:
        return outcome(args, getattr(flow, args.command)(args.task_id))
    return 0


def _render_land(land) -> str:
    state = land.job_state or ("its record is missing" if land.job_record == "missing" else "its record does not read")
    job = (f"job: {land.job} ({state}" + (f", exit {land.exit_code}" if land.exit_code is not None else "") + ")"
           ) if land.job else "job: none"
    later = ""
    if land.later_job:
        how = (f"exit {land.later_exit_code}" if land.later_exit_code is not None
               else land.later_job_state or "its record does not read")
        later = f"; later job: {land.later_job} ({how})"
    return (f"{job}; running: {'unknown' if land.running is None else 'yes' if land.running else 'no'}"
            + (f"; since {land.since}" if land.since else "") + later)


def _render_start(claim, facts) -> str:
    running = "unknown" if facts.running is None else "yes" if facts.running else "no"
    if claim is None:
        return f"job {facts.job} beginning, not yet claimed; running: {running}"
    return f"claimed {claim.since}; running: {running}" + ("; a claim from an older version" if facts.unmarked else "")


def _interrupted(args: argparse.Namespace, said: str = "") -> str:
    """What a Ctrl-C means, by command: a wait or a read changed nothing; a command that changes a task
    may have left something, which `status` names."""
    if args.command == "job" and args.wait is not None:
        return f"stopped waiting (Ctrl-C); job {args.job_id} runs on as before: `upe job {args.job_id}` gives its state"
    if args.command == "judge":
        return "the judge stopped (Ctrl-C); the priorities it set stay until it runs again"
    if args.command in ("status", "job", "activity", "skill"):
        return f"`upe {args.command}` was interrupted (Ctrl-C); it changes no task, so nothing was left: run it again"
    task = getattr(args, "task_id", None)
    return (f"the command was interrupted (Ctrl-C) before it ended{f'. {said}' if said else ''}. Run "
            f"`upe status{f' {task}' if task else ''}` to see what it left, and do what its `next` says")


def _said(exc: BaseException) -> str:
    """The error, with any notes added to it (the clean-up's, after a `start` that failed)."""
    return " ".join([str(exc), *getattr(exc, "__notes__", ())]).strip()


def _misleads(row: dict) -> bool:
    """The list's text gives `next` only where the state alone misleads: a land that does not run (it looks like
    a land in flight), a command that works on a task in another state, or may (it looks idle), and a record
    that does not read. `status <task>` and `--json` give it for every task. A `starting` row whose start does not
    run, or may not (a dead start looks like one being made), gives it too."""
    if row["state"] in ("landing", "starting"):
        return row.get("running") is not True
    return row["state"] == "unreadable" or ("running" in row and row["running"] is not False)


def setup(args: argparse.Namespace) -> int:
    """`setup --check` reads the config again, for anyone; `setup` asks the user, so it needs a person at a terminal."""
    if args.check:
        try:
            checks = check_setup(args.config)
        except OSError as exc:  # git itself could not be started
            emit(args, {"error": str(exc)}, f"upe: {exc}", stream=sys.stderr)
            return 2
        emit(args, checks_json(checks), render_checks(checks))
        return 0 if checks_json(checks)["ok"] else 1
    if args.json or not at_a_terminal():
        emit(args, {"error": "setup needs a person"}, "upe: setup asks the user for this machine's paths: ask the user "
             "to run `upe setup` in a terminal (PowerShell or cmd; Git Bash's own window is not seen as one). "
             "`upe setup --check` reads them and changes nothing", stream=sys.stderr)
        return 2
    try:
        setup_interactive(args.config)
    except (SetupError, EditorError, OSError, ValueError, KeyboardInterrupt, EOFError) as exc:  # the config is written last
        print(f"\nupe: setup stopped{f': {exc}' if str(exc) else ''}; nothing was written", file=sys.stderr)
        return 2
    return 0


def install_skill(folders: list[Path], config: Path, settings: Config | None = None) -> list[Path]:
    """Copy skill/upe/SKILL.md into each skills folder, with this machine's command to run the tool filled in, and
    the project and its target, for the read-only `git` command the skill gives before a claim."""
    from .flow import _sh

    settings = settings or Config.load(config)
    exe = Path(sys.executable).with_name("upe.exe" if sys.platform == "win32" else "upe")
    command = f'"{exe}"' if exe.exists() else f'"{sys.executable}" -I -m upe'
    if Path(config).resolve() != default_config().resolve():
        command += f' --config "{Path(config).resolve()}"'
    text = ((TOOL_HOME / "skill" / "upe" / "SKILL.md").read_text(encoding="utf-8").replace("{{UPE}}", command)
            .replace("{{REPO}}", _sh(str(settings.repo))).replace("{{TARGET}}", _sh(settings.target)))
    written = []
    for folder in folders:
        dest = Path(folder).expanduser() / "upe" / "SKILL.md"
        write_atomic(dest, text)
        written.append(dest)
    return written


def outcome(args: argparse.Namespace, out) -> int:
    emit(args, {"task": out.task_id, "state": out.state, "message": out.message}, f"{out.task_id}: {out.state} - {out.message}")
    return 0 if out.state in ("landed", "finished", "refused", "awaiting-council") else 1


def progress(flow: Flow, args: argparse.Namespace, message: str) -> None:
    """A step of a long command: a line on stderr (the job's log), and the job record's `stage`."""
    if args.job_child or not args.json:  # with --json, stderr holds only the error object
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # the date, and the zone (UTC)
        print(f"{stamp} {message}", file=sys.stderr, flush=True)
    if args.job_child:
        _job_update(flow, args.job_child, stage=_stage(message))


STAGE_CHARS = 160  # a job's `stage` is one short line; the log keeps the whole message


def _stage(message: str) -> str:
    return message if len(message) <= STAGE_CHARS else message[:STAGE_CHARS - 3] + "..."


def utf8_output() -> None:
    """Write stdout and stderr as UTF-8: in the locale's code page (a pipe or a detached job's log on
    Windows), a CJK path raised UnicodeEncodeError after the state was saved, and a non-ASCII name came out garbled
    in the log, which the tool and `upe job` read back as UTF-8. A stream with no `reconfigure` is left as it is."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def emit(args: argparse.Namespace, data: dict, text: str, stream=None) -> None:
    print(json.dumps(data) if args.json else text, file=stream or sys.stdout, flush=True)


def at_a_terminal() -> bool:
    """A person can answer: both stdin and stdout are a console."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def confirmed_by_a_person(task_id: str, choice: str) -> bool:
    """A split council goes to the user, not to an agent: ask at a real terminal for the task id."""
    if not at_a_terminal():
        return False
    answer = input(f"The council split on {task_id}. Type the task id to {choice} it: ")
    return answer.strip() == task_id


# Background jobs -----------------------------------------------------------


def detach(flow: Flow, args: argparse.Namespace, argv: list[str]) -> int:
    """Start the same command in a background process and return at once with its job id."""
    task = getattr(args, "task_id", None) or args.command
    if not TASK_ID.match(task):
        task = args.command
    base = f"{args.command}-{task}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    jobs = flow.state_dir / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    rest = [a for a in argv if a != "--detach"]
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    env = {**os.environ, "UPE_AGENT": json.dumps(agent_process())}  # the job's own parent chain ends here
    out, n = None, 1
    while out is None:  # the log is made new ("x"): a second job of one command and task in one second gets `-2`
        job_id = base if n == 1 else f"{base}-{n}"
        log = jobs / f"{job_id}.log"
        try:
            out = open(log, "x", encoding="utf-8")
        except FileExistsError:
            n += 1
    child = [sys.executable, "-I", "-m", "upe", "--config", str(args.config), "--job-id", job_id, *rest]
    with out:
        proc = subprocess.Popen(child, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=flags, close_fds=True, env=env)
    # The start time goes with every pid written, so a later process given the same id is not read as this job.
    _job_update(flow, job_id, state="started", pid=proc.pid, created=process_created(proc.pid), argv=rest, log=str(log))
    emit(args, {"job": job_id, "log": str(log)}, f"{job_id}: started; `upe job {job_id}` for its state, log in {log}")
    return 0


# The reads live in Flow (`status` needs them too); these names are kept for callers of before.


def _job_path(flow: Flow, job_id: str) -> Path:
    return flow.job_path(job_id)


def _job_read(flow: Flow, job_id: str) -> dict:
    return flow.job_read(job_id)


def _job_state(flow: Flow, job_id: str) -> dict:
    return flow.job_state(job_id)


_alive = process_alive


def _job_update(flow: Flow, job_id: str, **fields) -> None:
    path = _job_path(flow, job_id)
    record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"job": job_id}
    if record.get("state") == "done":  # a job that ended before its parent wrote `started`
        fields.pop("state", None)
    record.update(fields, updated=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    write_atomic(path, json.dumps(record, indent=2) + "\n")


# Priority ----------------------------------------------------------------


def lower_own_priority(base: str) -> None:
    """Run this process at `editor_priority`. Windows children inherit the class: git, robocopy, the CLI, Editors."""
    if sys.platform == "win32" and base != "normal":
        set_priority_class(os.getpid(), BASES[base])


def _render_judge(decision) -> str:
    return "\n".join(f"{name:24} {priority:7} {decision.reasons.get(name, '')}" for name, priority in decision.priorities.items())


def watch(flow: Flow) -> None:
    """Judge again when any session declares a new activity, and at least every judge_every_s."""
    seen, last = None, 0.0
    while True:
        current = activities(flow.state_dir)
        if current != seen or time.monotonic() - last >= flow.config.judge_every_s:
            print(_render_judge(flow.judge_once()), flush=True)
            seen, last = current, time.monotonic()
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
