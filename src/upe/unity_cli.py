"""Every call to the official `unity` CLI goes through this module.

The CLI is in beta and renames commands between releases, so the argument
lists live in one table, `COMMANDS`. A rename is a one-line change there.

Targeting: each worktree has its own Editor. A call is aimed at one project
three ways at once: the process runs with the project as its working
directory, `UNITY_PROJECT_PATH` is set to it, and commands that document a
path argument get it explicitly. Beta.14 and later never let such a call
reach another project's Editor, which is why `MIN_VERSION` is beta.14.

Sources: https://docs.unity.com/en-us/unity-cli/unity-cli-reference and
https://docs.unity.com/en-us/unity-cli/release-notes (1.0.0-beta.14).
Flags not on those pages were checked with `unity <command> --help` on
beta.14. The field names inside `data` are still unverified.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


class ExitCode(IntEnum):
    """Exit codes documented in the CLI reference and beta.14 release notes."""

    OK = 0
    GENERAL = 1
    USAGE = 2
    AUTH = 3
    CONFIG_REQUIRED = 4
    FAILED = 6  # primary operation failed; for `test`, no results were produced
    UNREACHABLE = 7  # service unreachable, safe to retry
    TESTS_FAILED = 8
    NOT_A_PROJECT = 11
    PIN_MATCHED_NOTHING = 12  # also: Editor unreachable, from `status`
    INTERRUPTED = 130
    TERMINATED = 143


# Exit codes the CLI reference says a script may retry.
RETRYABLE = frozenset({ExitCode.FAILED, ExitCode.UNREACHABLE})


@dataclass(frozen=True, order=True)
class CliVersion:
    """A `major.minor.patch[-beta.N]` version. A release sorts after its betas."""

    major: int
    minor: int
    patch: int
    is_release: bool  # False for a prerelease, so prereleases sort first
    beta: int

    _PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?")

    @classmethod
    def parse(cls, text: str) -> "CliVersion":
        match = cls._PATTERN.search(text)
        if not match:
            raise ValueError(f"no unity CLI version in {text!r}")
        major, minor, patch, beta = match.groups()
        return cls(int(major), int(minor), int(patch), beta is None, int(beta or 0))

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return core if self.is_release else f"{core}-beta.{self.beta}"


MIN_VERSION = CliVersion.parse("1.0.0-beta.14")


class UnityCliError(Exception):
    """The CLI could not be found, or is older than MIN_VERSION."""


@dataclass(frozen=True)
class CliResult:
    """One CLI call. `envelope` is the parsed `--format json` output, if any."""

    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    envelope: Mapping[str, Any] | None = None

    @property
    def ok(self) -> bool:
        if self.exit_code != ExitCode.OK:
            return False
        return self.envelope is None or self.envelope.get("success", True) is not False

    @property
    def data(self) -> Any:
        return None if self.envelope is None else self.envelope.get("data")

    @property
    def errors(self) -> list[Mapping[str, Any]]:
        if self.envelope is None:
            return []
        return list(self.envelope.get("errors") or [])

    @property
    def error_code(self) -> str | None:
        """The stable code of the first error, such as `OPEN_BLOCKED_BY_DIALOG`."""
        for error in self.errors:
            if isinstance(error, Mapping) and error.get("code"):
                return str(error["code"])
        return None

    @property
    def retryable(self) -> bool:
        return self.exit_code in RETRYABLE


@dataclass(frozen=True)
class Spec:
    """How one logical operation maps onto CLI arguments."""

    args: tuple[str, ...]
    json: bool = True  # pass `--format json` and parse the envelope
    path_flag: str | None = None  # flag that takes the project path; None = positional or not taken
    path_positional: bool = False


# The one place CLI command names and flags are written down.
COMMANDS: dict[str, Spec] = {
    # `unity --version` prints only the version string.
    "version": Spec(("--version",), json=False),
    "doctor": Spec(("doctor",)),
    "auth_status": Spec(("auth", "status")),
    "editors_installed": Spec(("editors", "--installed")),
    # `unity open <path>`; --batchmode, --until-ready, --accept-upgrades are in beta.14 notes.
    "open": Spec(("open",), path_positional=True),
    # `unity status --project` became `--project-path` in beta.14.
    "status": Spec(("status",), path_flag="--project-path"),
    # Flags below were checked with `unity <command> --help` on beta.14 (2026-10-26).
    "close": Spec(("close",), path_positional=True),
    "test": Spec(("test",), path_positional=True),
    # `unity command [--project-path P] <name> [args...]`; the args are positional.
    "command": Spec(("command",), path_flag="--project-path"),
    "pipeline_install": Spec(("pipeline", "install"), path_flag="--project-path"),
}

Runner = Callable[..., subprocess.CompletedProcess]


def find_cli(env: Mapping[str, str] | None = None) -> Path:
    """The `unity` CLI the user named in UPE_UNITY_CLI. The tool looks nowhere the user has not named."""
    env = os.environ if env is None else env
    override = env.get("UPE_UNITY_CLI")
    if override:
        return Path(override)
    raise UnityCliError("no unity CLI named: give its unity.exe as unity_cli in upe.toml, or set UPE_UNITY_CLI")


class UnityCli:
    """Runs the `unity` CLI against one machine. Stateless apart from the exe path."""

    def __init__(
        self,
        exe: Path | str | None = None,
        runner: Runner = subprocess.run,
        default_timeout: float = 600.0,
    ) -> None:
        self.exe = Path(exe) if exe is not None else find_cli()
        self._runner = runner
        self.default_timeout = default_timeout

    # Low level -----------------------------------------------------------

    def run(
        self,
        operation: str,
        *extra: str,
        project: Path | str | None = None,
        timeout: float | None = None,
    ) -> CliResult:
        """Run one operation from COMMANDS, aimed at `project` if given."""
        spec = COMMANDS[operation]
        argv: list[str] = [str(self.exe), *spec.args]
        if project is not None:
            project = str(Path(project))
            if spec.path_positional:
                argv.append(project)
            elif spec.path_flag:
                argv += [spec.path_flag, project]
        argv += extra
        if spec.json:
            argv += ["--format", "json"]

        env = dict(os.environ)
        env["UNITY_NON_INTERACTIVE"] = "1"
        env["UNITY_NO_BANNER"] = "1"
        env["UNITY_NO_PAGER"] = "1"
        if project is not None:
            env["UNITY_PROJECT_PATH"] = project
        else:
            env.pop("UNITY_PROJECT_PATH", None)

        completed = self._runner(
            argv,
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout if timeout is not None else self.default_timeout,
        )
        stdout = completed.stdout or ""
        return CliResult(
            argv=tuple(argv),
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=completed.stderr or "",
            envelope=_parse_envelope(stdout) if spec.json else None,
        )

    # Operations ----------------------------------------------------------

    def version(self) -> CliVersion:
        result = self.run("version")
        if result.exit_code != ExitCode.OK:
            raise UnityCliError(f"`unity --version` exited {result.exit_code}: {result.stderr.strip()}")
        return CliVersion.parse(result.stdout)

    def check_version(self, minimum: CliVersion = MIN_VERSION) -> CliVersion:
        """Fail unless the installed CLI is at least `minimum`."""
        found = self.version()
        if found < minimum:
            raise UnityCliError(f"unity CLI {found} is older than the pinned {minimum}; run `unity upgrade`")
        return found

    def doctor(self) -> CliResult:
        return self.run("doctor")

    def auth_status(self) -> CliResult:
        return self.run("auth_status")

    def editors_installed(self) -> CliResult:
        return self.run("editors_installed")

    def open(
        self,
        project: Path | str,
        *,
        batchmode: bool = True,
        until_ready: bool = True,
        accept_upgrades: bool = False,
        timeout: float | None = None,
    ) -> CliResult:
        """Open the project's Editor. Already open returns success with data.alreadyOpen."""
        extra = []
        if batchmode:
            extra.append("--batchmode")
        if until_ready:
            extra.append("--until-ready")
        if accept_upgrades:
            extra.append("--accept-upgrades")
        return self.run("open", *extra, project=project, timeout=timeout)

    def status(self, project: Path | str) -> CliResult:
        return self.run("status", project=project)

    def close(self, project: Path | str, *, force: bool = False) -> CliResult:
        """Close the project's Editor. Neither mode saves unsaved work."""
        return self.run("close", *(["--force"] if force else []), project=project)

    def test(
        self,
        project: Path | str,
        *extra: str,
        cli_timeout_s: int | None = None,
        timeout: float | None = None,
    ) -> CliResult:
        """Run tests. Exit 8 = tests failed; exit 6 = the run produced no results."""
        args = list(extra)
        if cli_timeout_s is not None:
            args += ["--timeout", str(int(cli_timeout_s))]  # whole seconds only
            if timeout is None:  # wait as long as the CLI may take, not the 600 s default (a full run is ~15 min)
                timeout = cli_timeout_s + 300
        return self.run("test", *args, project=project, timeout=timeout)

    def pipeline_install(self, project: Path | str) -> CliResult:
        """Add the Pipeline package the CLI needs to the project's manifest."""
        return self.run("pipeline_install", project=project)

    def command(self, project: Path | str, name: str, *args: str, timeout: float | None = None) -> CliResult:
        """Forward an Editor command (from the Pipeline package) to the project's Editor."""
        return self.run("command", name, *args, project=project, timeout=timeout)


def _parse_envelope(stdout: str) -> Mapping[str, Any] | None:
    """Parse the JSON envelope. Accepts a single object or ndjson ending in a result frame."""
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
        for line in reversed(text.splitlines()):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return parsed if isinstance(parsed, dict) else None
