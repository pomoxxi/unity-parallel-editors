"""`upe setup`: the user names this machine's paths once, at a terminal, and `upe setup --check` reads them again.

Setup asks; it does not search (DESIGN.md decisions 16 and 17). It reads the paths the
user types and what is inside them (the project's ProjectVersion.txt, and git's answers
about that project: its branches, worktrees and tracked files), the free space on the
drives those paths are on, the existing config and the tool's own upe.example.toml. It
writes only the config: `upe.toml`, or `upe.toml.new` beside one that exists, with the
difference shown. It makes no branch, runs no Unity and changes nothing in the project.

`--check` changes nothing at all, so an agent may run it; it catches a moved Editor,
a new Unity version, a missing target branch or a full drive before a land fails on it.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .editor import EditorError, project_version, yaml_merge_for
from .flow import Config, FlowError, _under, test_folders_in
from .merge import program_there
from .safegit import SAFE_GIT

TOOL_HOME = Path(__file__).resolve().parents[2]
EXAMPLE = TOOL_HOME / "upe.example.toml"
LOW_SPACE_GB = 20  # below this on the drive of root or golden, a new slot may not fit; one slot of a large project is ~16 GB
ASKED = ("repo", "root", "golden", "target", "local_copies", "unity_editor", "unity_cli", "yaml_merge")
CONFIG_ERRORS = (FlowError, EditorError, tomllib.TOMLDecodeError, OSError, ValueError, TypeError)


class SetupError(Exception):
    pass


# Every step the tool prints puts a slot's path in double quotes, which Git Bash, PowerShell and cmd.exe all read as one
# word only when it holds none of these characters, and does not start with `\\` (Git Bash's double quotes read `\\` as
# one backslash, so a UNC path loses one). The slots live under `root`.
ROOT_CHARS = {"$": "a dollar sign", "`": "a backtick", "'": "a single quote", '"': "a double quote", "%": "a percent sign"}


def root_problem(root: Path) -> str | None:
    """Why `root` cannot hold the slots, or None: a character the printed steps' double quotes do not keep in every shell,
    or a start of `\\`."""
    text = str(root)
    for char, name in ROOT_CHARS.items():
        if char in text:
            return (f"{text} holds {name} ({char}), which the git steps the tool prints, in double quotes, do not keep in "
                    "Git Bash, PowerShell and cmd alike: choose a folder whose path has none of $ ` ' \" %")
    if text.startswith(("\\\\", "//")):
        return (f"{text} starts with \\\\ (a network path), which Git Bash's double quotes read as one backslash: "
                "choose a folder on a drive of this machine")
    return None


@dataclass
class Check:
    name: str
    level: str  # "ok", "warn" or "fail"
    message: str


def _git(repo: Path, *args: str, runner=subprocess.run) -> subprocess.CompletedProcess:
    return runner(["git", *SAFE_GIT, "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                  errors="replace")


def _free_gb(path: Path) -> float | None:
    """Free space on the drive of `path`, from its nearest folder that exists."""
    for folder in (path, *path.parents):
        if folder.exists():
            return shutil.disk_usage(folder).free / 1e9
    return None


def _is_project(repo: Path) -> str | None:
    """Why `repo` is not a Unity project's git checkout, or None if it is."""
    if not (repo / "ProjectSettings" / "ProjectVersion.txt").is_file():
        return f"{repo} has no ProjectSettings/ProjectVersion.txt: give the Unity project's folder"
    if not (repo / ".git").exists():
        return f"{repo} is not a git checkout (no .git in it)"
    try:
        project_version(repo)
    except (EditorError, OSError, UnicodeDecodeError) as exc:
        return str(exc)
    return None


def _apart(a: Path, b: Path) -> bool:
    return not (_under(a, b) or _under(b, a))


def _target_problem(repo: Path, target: str, root: Path | None, protect: bool = True,
                    runner=subprocess.run) -> tuple[str, str] | None:
    """(level, message) when the target branch cannot be landed on as it is, else None. The same rules as `land`."""
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{target}", runner=runner).returncode != 0:
        current = _git(repo, "branch", "--show-current", runner=runner).stdout.strip() or "<your branch>"
        return "fail", (f"no branch {target} in {repo}. Make it from the branch you work on: "
                        f"git -C \"{repo}\" branch --no-track {target} {current}")
    if _git(repo, "config", "--get", f"branch.{target}.remote", runner=runner).stdout.strip():
        return "fail", f"{target} tracks a remote: the tool lands only on a local branch (git branch --unset-upstream {target})"
    if not protect:
        return None
    path = None
    for line in _git(repo, "worktree", "list", "--porcelain", runner=runner).stdout.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{target}" and path is not None and not (root and _under(path, root / "slots")):
            return "fail", f"{target} is checked out in {path}; the tool does not move a branch someone works on. Name another"
    return None


def _copy_problem(repo: Path, rel: str, runner=subprocess.run) -> str | None:
    """Why `rel` cannot be a local copy: outside the project, missing, tracked by git (a worktree has it already),
    or not git-ignored (an agent's `git add -A` would put it on a branch). Paths are matched without case, as Windows does."""
    if Path(rel).is_absolute() or not _under(repo / rel, repo) or (repo / rel).resolve() == repo.resolve():
        return f"{rel} is not a path inside the project; give it relative to {repo}"
    if not (repo / rel).exists():
        return f"{rel} is not in {repo}"
    spec = f":(icase,literal){rel}"
    tracked = _git(repo, "ls-files", "--", spec, runner=runner)
    if tracked.returncode != 0:
        return f"git could not say whether {rel} is tracked: {tracked.stderr.strip()}"
    if tracked.stdout.strip():
        return f"{rel} has files in git, so every worktree has them already"
    loose = _git(repo, "ls-files", "--others", "--exclude-standard", "--", spec, runner=runner)
    if loose.returncode != 0 or loose.stdout.strip():
        return (f"{rel} is not shown as git-ignored (or git could not say), so an agent's commit could take it onto a "
                f"branch; add it to .gitignore first")
    return None


# --check -------------------------------------------------------------------


def check(config_path: Path, env=None, runner=subprocess.run) -> list[Check]:
    """Every named path and setting, read again, as the tool would use it. Changes nothing."""
    env = os.environ if env is None else env
    if not config_path.exists():
        return [Check("config", "fail", f"no {config_path}: ask the user to run `upe setup` at a terminal")]
    try:
        config = Config.load(config_path)
    except CONFIG_ERRORS as exc:
        return [Check("config", "fail", f"{config_path}: {exc}")]
    out = [Check("config", "ok", str(config_path))]

    problem = _is_project(config.repo)
    if problem:
        return out + [Check("repo", "fail", problem)]
    version = project_version(config.repo)
    out.append(Check("repo", "ok", f"{config.repo}, Unity {version}"))

    # As the tool chooses them: upe.toml first, then the UPE_* variable (flow.py, _editor and _cli).
    editor = config.unity_editor or (Path(env["UPE_UNITY_EDITOR"]) if env.get("UPE_UNITY_EDITOR") else None)
    if editor is None:
        out.append(Check("unity_editor", "fail", f"not named: give Unity {version}'s Unity.exe as unity_editor"))
    elif not editor.is_file():
        out.append(Check("unity_editor", "fail", f"{editor} is not there"))
    elif version not in editor.as_posix():
        out.append(Check("unity_editor", "warn", f"{editor}: its path does not say {version}, the project's version. "
                                                 "Check it is that version"))
    else:
        out.append(Check("unity_editor", "ok", str(editor)))

    merge = config.yaml_merge or (yaml_merge_for(editor) if editor else None)
    if merge is None:
        out.append(Check("yaml_merge", "fail", "no Editor named, so no UnityYAMLMerge beside it"))
    elif not program_there(merge):
        out.append(Check("yaml_merge", "fail", f"{merge} is not there: name UnityYAMLMerge(.exe) as yaml_merge"))
    elif config.yaml_merge and editor and version not in merge.as_posix():
        out.append(Check("yaml_merge", "warn", f"{merge}: named in upe.toml, and its path does not say {version}; "
                                               "an older one may merge differently"))
    else:
        out.append(Check("yaml_merge", "ok", str(merge)))

    cli = config.unity_cli or (Path(env["UPE_UNITY_CLI"]) if env.get("UPE_UNITY_CLI") else None)
    if cli is None:
        out.append(Check("unity_cli", "fail", "not named: give the unity CLI's unity.exe as unity_cli"))
    elif not cli.is_file():
        out.append(Check("unity_cli", "fail", f"{cli} is not there"))
    else:
        out.append(Check("unity_cli", "ok", f"{cli} (its version is checked at `start`)"))

    problem = _target_problem(config.repo, config.target, config.root, config.protect_checked_out_target, runner)
    out.append(Check("target", *problem) if problem else Check("target", "ok", config.target))

    for name, place in (("root", config.root), ("golden", config.golden)):
        if name == "root" and root_problem(place):
            out.append(Check("root", "fail", root_problem(place)))
            continue
        free = _free_gb(place)
        notes = [f"{free:.0f} GB free on its drive" if free is not None else "its drive is not there"]
        level = "ok"
        if free is None or free < LOW_SPACE_GB:
            level = "warn"
            notes.append("a slot is a checkout plus a Library")
        if name == "golden" and not (place / ".git").exists():
            level = "warn"
            notes.append("not made yet: run `upe golden --create` (a full import; slow)")
        out.append(Check(name, level, f"{place}: " + "; ".join(notes)))

    for rel in config.local_copies:
        if Path(rel).is_absolute() or not _under(config.repo / rel, config.repo):
            out.append(Check(f"local_copies: {rel}", "fail", "not a path inside the project"))
        elif not (config.repo / rel).exists():
            out.append(Check(f"local_copies: {rel}", "fail", f"{config.repo / rel} is not there; every new worktree stops on it"))
        else:
            out.append(Check(f"local_copies: {rel}", "ok", str(config.repo / rel)))
    # Each test folder `start` names, read at the target through git (Config.load refused one outside the project): it
    # holds a tracked file there, or it misleads every agent that starts.
    for rel in config.test_folders:
        listed = _git(config.repo, "ls-tree", "-r", "--name-only", "-z", config.target, "--", rel, runner=runner)
        if listed.returncode == 0 and listed.stdout.strip("\0"):
            out.append(Check(f"test_folders: {rel}", "ok", f"tracked at {config.target}"))
        else:
            out.append(Check(f"test_folders: {rel}", "fail", f"no tracked file under it at {config.target}; `start` "
                                                              "names it to every agent as where the tests live"))
    # Where the tests live, read before any claim (the skill sends the agent here first): the named folders, or the
    # folders named Test or Tests at the target.
    if config.test_folders:
        out.append(Check("tests", "ok", f"{', '.join(config.test_folders)} (test_folders in upe.toml)"))
    else:
        tree = _git(config.repo, "-c", "core.quotepath=off", "ls-tree", "-r", "--name-only", "-z", config.target,
                    runner=runner)
        folders = test_folders_in([p for p in tree.stdout.split("\0") if p]) if tree.returncode == 0 else []
        out.append(Check("tests", "ok", f"{', '.join(folders)} (folders named Test or Tests at {config.target}; "
                                        "upe.toml names none)") if folders else
                   Check("tests", "warn", f"no folder named Test or Tests at {config.target}, and upe.toml names none "
                                          "(test_folders): ask the user where the tests live"))
    return out


def render_checks(checks: list[Check]) -> str:
    return "\n".join(f"{c.level:4}  {c.name:14} {c.message}" for c in checks)


def checks_json(checks: list[Check]) -> dict:
    return {"ok": all(c.level != "fail" for c in checks), "checks": [asdict(c) for c in checks]}


# Setup ---------------------------------------------------------------------


def _toml(value) -> str:
    if isinstance(value, Path):
        value = value.as_posix()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml(str(k))} = {_toml(v)}" for k, v in value.items()) + "}"
    # A TOML basic string has JSON's escapes, but TOML also refuses a raw DEL.
    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def _key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _toml(key)


def render(values: dict, template: str) -> str:
    """The example config with `values` in place of its lines; its comments are kept.

    A key's line is the example's uncommented one, else its commented one; a key the example lacks
    goes above the comments that open the [builders] table. `builders` is written as that table's entries.
    """
    builders = values.get("builders") or {}
    head, sep, table = template.partition("\n[builders]")
    extra = []
    for key, value in values.items():
        if key == "builders":
            continue
        line = f"{_key(key)} = {_toml(value)}"
        for pattern in (rf"^{re.escape(key)}\s*=.*$", rf"^#\s*{re.escape(key)}\s*=.*$"):
            head, n = re.subn(pattern, lambda _: line, head, count=1, flags=re.MULTILINE)
            if n:
                break
        else:
            extra.append(line)
    if extra:
        lines = head.rstrip("\n").split("\n")
        start = len(lines)
        while start > 0 and lines[start - 1].startswith("#"):
            start -= 1
        block = "\n".join(extra) + "\n"
        if start == 0 or start == len(lines):
            head = "\n".join(lines) + f"\n\n{block}"
        else:
            head = "\n".join(lines[:start]).rstrip("\n") + f"\n\n{block}\n" + "\n".join(lines[start:])
    if not sep:
        sep, table = "\n[builders]", "\n"
    table += "".join(f"{_toml(str(k))} = {_toml(v)}\n" for k, v in builders.items())
    return head + sep + table


def _load_kept(old: dict) -> None:
    """Load the old config's settings that setup does not ask about, with stand-ins for the ones it does,
    so a fault the questions are about to fix (a missing golden, a lone local_copies string) is not one here."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        here = Path(tmp)
        kept = {k: v for k, v in old.items() if k not in ASKED}
        if not isinstance(kept.get("builders", {}), dict):
            raise FlowError("builders must be a table: [builders] with \"path\" = \"Method\" lines")
        stand_in = {"repo": here / "repo", "root": here / "root", "golden": here / "golden"}
        probe = here / "config" / "upe.toml"
        probe.parent.mkdir()
        probe.write_text(render({**stand_in, **kept}, ""), encoding="utf-8")
        try:
            Config.load(probe)
        except CONFIG_ERRORS as exc:
            raise type(exc)(str(exc).replace(str(probe), "the old config")) if isinstance(exc, FlowError) else exc


class Asker:
    """The questions, one at a time, each asked again until its answer is usable."""

    def __init__(self, ask: Callable[[str], str], say: Callable[[str], None]):
        self.ask, self.say = ask, say

    def path(self, question: str, default: Path | None, valid: Callable[[Path], str | None]) -> Path:
        while True:
            suffix = f" [{default.as_posix()}]" if default else ""
            text = self.ask(f"{question}{suffix}: ").strip().strip('"')
            if not text and default:
                chosen = default
            elif not text:
                self.say("  an answer is needed")
                continue
            else:
                chosen = Path(os.path.expandvars(text)).expanduser()
            if not chosen.is_absolute():
                self.say("  give the whole path, from the drive")
                continue
            problem = valid(chosen)
            if problem is None:
                return chosen
            self.say(f"  {problem}")

    def text(self, question: str, default: str = "") -> str:
        answer = self.ask(f"{question}{f' [{default}]' if default else ''}: ").strip()
        return answer or default

    def yes(self, question: str) -> bool:
        return self.ask(f"{question} [y/N]: ").strip().lower() in ("y", "yes")


def interactive(config_path: Path, ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
                runner=subprocess.run) -> Path:
    """Ask for this machine's paths and write the config. Returns the file written."""
    q = Asker(ask, say)
    old = {}
    if config_path.exists():
        try:
            old = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            say(f"{config_path} does not read as TOML; its values are not offered")
    keep_rest = True  # the old config's settings that setup does not ask about
    if old:
        try:
            _load_kept(old)
        except CONFIG_ERRORS as exc:
            say(f"{config_path} does not load as it is: {exc}")
            if not q.yes("Leave its other settings out, and take the example's, with the paths you give now?"):
                raise SetupError(f"fix {config_path}, or move it away, then run setup again") from exc
            keep_rest = False

    def was(key: str) -> Path | None:
        return (config_path.parent / old[key]).resolve() if isinstance(old.get(key), str) and old[key] else None

    say("upe setup: the tool reads only the paths you give here, and searches nowhere else.\n"
        "Nothing is changed but the config file. Enter keeps the value in [brackets].\n")

    def project_ok(p: Path) -> str | None:
        if _under(config_path.resolve(), p):
            return f"the config ({config_path}) is inside it, where agents' branches could write it; keep it outside"
        return _is_project(p)

    repo = q.path("Your Unity project's git checkout (the main one)", was("repo"), project_ok)
    version = project_version(repo)
    say(f"  Unity {version}")

    def place_ok(place: Path, other: Path | None = None) -> str | None:
        if not _apart(place, repo):
            return f"not inside the project ({repo}), and not around it"
        if other is not None and not _apart(place, other):
            return f"not inside the slots' folder ({other}), and not around it"
        if _under(config_path.resolve(), place):
            return f"the config ({config_path}) is inside it, where agents' branches could write it; choose another folder"
        return None

    root = q.path("A folder for the agents' slots and the tool's state (a drive with room)", was("root"),
                  lambda p: root_problem(p) or place_ok(p))
    golden = q.path("A folder for the golden checkout, on the same drive",
                    was("golden") or root.parent / f"{root.name or 'upe'}-golden", lambda p: place_ok(p, root))
    if root.anchor.lower() != golden.anchor.lower():
        say(f"  {root} and {golden} are on different drives: every new slot's Library is then copied across them")
    for place in (root, golden):
        free = _free_gb(place)
        if free is not None:
            say(f"  {free:.0f} GB free on the drive of {place}" + ("; a slot is a checkout plus a Library" if free < LOW_SPACE_GB else ""))

    protect = old.get("protect_checked_out_target", True) is not False  # setting it needs true or false (Config.load)
    while True:
        target = q.text("The branch tasks land on (a branch of its own, not one you work on)", str(old.get("target", "upe/test-base")))
        problem = _target_problem(repo, target, root, protect, runner)
        if problem is None:
            break
        say(f"  {problem[1]}")
        if problem[1].startswith("no branch") and q.yes("Write it anyway, and make the branch yourself before the first task?"):
            break

    def editor_ok(exe: Path) -> str | None:
        if not exe.is_file():
            return f"{exe} is not there"
        if version not in exe.as_posix() and not q.yes(f"  The path does not say {version}. Is it that version?"):
            return f"give Unity {version}'s Unity.exe"
        return None

    editor = q.path(f"Unity {version}'s Editor (its Editor/Unity.exe)", was("unity_editor"), editor_ok)
    yaml_merge = None
    if not yaml_merge_for(editor).is_file():
        yaml_merge = q.path("UnityYAMLMerge.exe (it is not beside that Editor)", was("yaml_merge"),
                            lambda p: None if program_there(p) else f"{p} is not there")
    elif was("yaml_merge") and was("yaml_merge") != yaml_merge_for(editor).resolve():
        say(f"  yaml_merge ({was('yaml_merge')}) is left out: the one beside this Editor is used")
    cli = q.path("The unity CLI (its unity.exe, beta.14 or later)", was("unity_cli"),
                 lambda p: None if p.is_file() else f"{p} is not there")

    say("\nFiles outside git: git-ignored files or folders the project needs to compile, such as an asset\n"
        "kept out of the repository. They are copied from the project into each worktree.")
    earlier = old.get("local_copies", [])
    earlier = [earlier] if isinstance(earlier, str) else [c for c in earlier if isinstance(c, str)] if isinstance(earlier, list) else []
    answer = q.text("Their paths relative to the project, separated by ; (- for none)", "; ".join(earlier))
    copies = []
    for rel in ([] if answer.strip() == "-" else answer.split(";")):
        rel = rel.strip().strip('"').replace("\\", "/").rstrip("/")
        if not rel:
            continue
        problem = _copy_problem(repo, rel, runner)
        if problem:
            say(f"  {problem}; left out")
        else:
            copies.append(rel)

    values = {k: v for k, v in old.items() if k not in ASKED and keep_rest}  # the rest of the old config, as it was
    if "builders" in values and not (isinstance(values["builders"], dict)
                                     and all(isinstance(v, str) for v in values["builders"].values())):
        say("  builders in the old config is not a table of methods; left out")
        del values["builders"]
    values.update(repo=repo, root=root, golden=golden, target=target, local_copies=copies, unity_editor=editor, unity_cli=cli)
    if yaml_merge:
        values["yaml_merge"] = yaml_merge
    template = EXAMPLE.read_text(encoding="utf-8").replace(
        "# Copy to upe.toml and fill in your own paths.", "# Written by `upe setup`; run it again to change the paths.", 1)
    written = write(config_path, render(values, template), say)
    say(after_setup(config_path, written, golden))
    return written


def write(config_path: Path, text: str, say: Callable[[str], None]) -> Path:
    """Write `upe.toml`, or `upe.toml.new` beside one that exists, once the tool reads it.

    The text is tried in a file of its own first, so a text the tool refuses replaces nothing.
    """
    dest = config_path.with_name(config_path.name + ".new") if config_path.exists() else config_path
    trial = config_path.with_name(config_path.name + ".trying")  # beside it: relative paths and Config.load's checks see the same folder
    try:
        trial.write_text(text, encoding="utf-8")
        try:
            Config.load(trial)
        except CONFIG_ERRORS as exc:
            raise SetupError(f"not written: {str(exc).replace(str(trial), str(config_path))}") from exc
        diff = None
        if dest != config_path:
            before = config_path.read_text(encoding="utf-8", errors="replace").splitlines()
            diff = "\n".join(difflib.unified_diff(before, text.splitlines(), config_path.name, dest.name, lineterm=""))
        if dest.exists():
            say(f"replacing the earlier {dest}")
        os.replace(trial, dest)
    finally:
        trial.unlink(missing_ok=True)
    if diff is not None:
        say(diff or "(no change)")
        say(f"\n{config_path} exists, so this went to {dest}; it is made from upe.example.toml, so comments of\n"
            f"your own are not in it (the difference above shows them). If it is right, put it in place of the old one.")
    else:
        say(f"wrote {dest}")
    return dest


def after_setup(config_path: Path, written: Path, golden: Path) -> str:
    upe = f'upe --config "{config_path}"'
    first = f"  put {written.name} in place of {config_path.name}, then:\n" if written != config_path else ""
    return (
        "\nNext, yourself:\n" + first +
        f"  {upe} setup --check     reads every path again\n"
        f"  {upe} golden --create   the golden Library (a full import; slow)\n"
        f"  {upe} skill --to <an app's skills folder>   once per app the agents use\n"
        "Keep the slots and the golden out of cloud-synced folders. A virus-scanner exclusion for their\n"
        f"Library folders (for example {golden.as_posix()}/Library) makes imports faster; the tool does not make one.\n"
        "Any git credential this Windows user has, every agent can use to push: push from somewhere else."
    )
