"""One mutation harness: remove one check at a time, run the tests that should catch it, and
list the functions the work changed in `src/` that no mutation touches.

    python tests/mutate.py --base <commit> --tmp <folder> [--list <mutations file>] [--commit HEAD] [--keep]

It never edits the checkout it runs from: a land may be running the tool from that checkout's `src/`. It refuses to
start when `git status` shows any change under `src` or `tests`, so the commit it copies is the code under test. It
copies the commit with `git archive` into a folder of its own under `--tmp`, and each mutation into a fresh copy of
that, deleted after; nothing is put back anywhere. `--tmp` has no default (not the system's temp folder), and one
inside the repo is refused. The tests run with `PYTHONPATH` set to the copy's `src`, and a control checks that `upe`
is imported from there before any mutation runs, and that the named tests pass on the copy unmutated.

The list file is Python that defines `M = [(name, file, old, new, tests), ...]`: `file` relative to the repo, `old`
text that occurs once in it at the commit, `new` its replacement, `tests` the unittest names to run (relative to
`tests/`, for example `test_flow.LandTests.test_x` or `test_report_shapes`). Each prints `red` (a test failed, and
none ended in an error: the check is guarded), `LIVES` (they passed: the check has no test) or `void` (a timeout, an
error such as a broken import, or any other end: it proves nothing either way).

After the mutations it prints the functions that `git diff <base> <commit> -- src` changes, how many hold a mutation's
`old` text, and those that hold none. That list is read by hand: for each function on the path from a report or a kept
file to a verdict, the work adds a mutation or says in one line why none is needed. It works by
function, not by line: a function with a mutation of one line is not listed, whatever its other lines do.

Exit: 0 when every mutation is red, 1 when one lives or is void, 2 when it refuses to start.
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def git(repo: Path, *args: str, text: bool = True):
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, stdin=subprocess.DEVNULL, check=True)
    return out.stdout.decode("utf-8") if text else out.stdout


def dirty(repo: Path) -> list[str]:
    """The changes `git status` shows under `src` and `tests`, untracked files with them."""
    return git(repo, "status", "--porcelain", "--untracked-files=all", "--", "src", "tests").splitlines()


def functions(text: str) -> list[tuple[str, int, int]]:
    """(qualified name, first line, last line) of every function and method."""
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((prefix + child.name, child.lineno, child.end_lineno))
                walk(child, prefix + child.name + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")
    walk(ast.parse(text), "")
    return out


def holder(funcs, line: int) -> str | None:
    """The innermost function that holds `line`."""
    inner = [f for f in funcs if f[1] <= line <= f[2]]
    return min(inner, key=lambda f: f[2] - f[1])[0] if inner else None


def changed_functions(repo: Path, base: str, commit: str) -> dict[tuple[str, str], int]:
    """(file, function) -> how many of its lines `git diff -U0 <base> <commit> -- src` adds or changes."""
    changed: dict[tuple[str, str], int] = {}
    for rel in git(repo, "diff", "--name-only", base, commit, "--", "src").split():
        if not rel.endswith(".py"):
            continue
        funcs = functions(git(repo, "show", f"{commit}:{rel}"))
        lines = set()
        for hunk in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", git(repo, "diff", "-U0", base, commit, "--", rel), re.M):
            start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
            lines.update(range(start, start + count))
        for line in lines:
            name = holder(funcs, line)
            if name:
                changed[(rel, name)] = changed.get((rel, name), 0) + 1
    return changed


def where(text: str, old: str) -> int:
    """The line of `old` in `text` (its first non-blank line)."""
    at = text.find(old)
    lead = len(old) - len(old.lstrip("\n"))
    return text.count("\n", 0, at) + 1 + lead


def run_tests(copy: Path, tests, timeout_s: int) -> tuple[int, float, str]:
    env = {**os.environ, "PYTHONPATH": str(copy / "src")}
    began = time.monotonic()
    try:
        out = subprocess.run([sys.executable, "-m", "unittest", *tests], cwd=copy / "tests", env=env,
                             capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout_s)
        code, tail = out.returncode, (out.stderr.strip().splitlines() or [""])[-1]
    except subprocess.TimeoutExpired:
        code, tail = -1, f"timed out after {timeout_s} s"
    return code, time.monotonic() - began, tail


def outcome(code: int, tail: str) -> str:
    """`red` only when the tests ran and failed with no error; a timeout, an error or any other end is `void`. A skipped
    test or an expected failure beside the failures does not change that (unittest names them after the failures, and
    `errors=` between the two, so an error never matches)."""
    if code == 0:
        return "LIVES"
    red = r"FAILED \(failures=\d+(, skipped=\d+)?(, expected failures=\d+)?(, unexpected successes=\d+)?\)"
    return "red" if code == 1 and re.fullmatch(red, tail) else "void"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True, help="the commit the work started from, e.g. main's")
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--list", dest="mutations", help="a Python file that defines M")
    parser.add_argument("--tmp", required=True, help="where the copies go, outside the repo")
    parser.add_argument("--repo", default=str(REPO))
    parser.add_argument("--timeout", type=int, default=1800, help="seconds for one mutation's tests")
    parser.add_argument("--keep", action="store_true", help="keep the unmutated copy")
    args = parser.parse_args(argv)
    repo = Path(args.repo)

    if Path(args.tmp).resolve().is_relative_to(repo.resolve()):
        print(f"refused: --tmp {args.tmp} is inside {repo}: the copies would sit in the checkout. Give a folder "
              "outside it")
        return 2
    changes = dirty(repo)
    if changes:
        print(f"refused: {repo} has changes under src or tests, so a copy of {args.commit} is not the code under test. "
              "Commit them first:\n  " + "\n  ".join(changes))
        return 2
    shas = {}
    for flag, name in (("--base", args.base), ("--commit", args.commit)):
        found = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"],
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if found.returncode != 0:
            print(f"refused: {flag} {name} is not a commit in {repo}")
            return 2
        shas[flag] = found.stdout.strip()
    commit, base = shas["--commit"], shas["--base"]
    mutations = runpy.run_path(args.mutations)["M"] if args.mutations else []

    Path(args.tmp).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"upe-mutate-{commit[:8]}-", dir=args.tmp))
    pristine = work / "copy"
    pristine.mkdir()
    with tarfile.open(fileobj=io.BytesIO(git(repo, "archive", "--format=tar", commit, text=False))) as tar:
        tar.extractall(pristine, filter="data")
    print(f"copy: {pristine} (git archive {commit[:12]})")
    texts: dict[str, str] = {}
    lived = void = 0
    try:
        found = subprocess.run([sys.executable, "-c", "import upe; print(upe.__file__)"], cwd=pristine / "tests",
                               env={**os.environ, "PYTHONPATH": str(pristine / "src")}, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL).stdout.strip()
        if not found or not Path(found).resolve().is_relative_to(pristine.resolve()):
            print(f"void: upe is imported from {found or 'nowhere'}, not from the copy")
            return 1
        print(f"control: upe from {found}")
        named = sorted({t for *_, tests in mutations for t in tests})
        if named:
            code, took, tail = run_tests(pristine, named, args.timeout)
            print(f"control: the named tests on the copy unmutated: exit {code} ({took:.1f} s; {tail})")
            if code != 0:
                print("void: the named tests do not pass unmutated")
                return 1
        for name, rel, old, new, tests in mutations:
            text = texts.setdefault(rel, (pristine / rel).read_text(encoding="utf-8"))
            if text.count(old) != 1:
                print(f"void   {name}: its old text occurs {text.count(old)} times in {rel}")
                void += 1
                continue
            copy = work / "mutant"
            shutil.copytree(pristine, copy)
            try:
                (copy / rel).write_text(text.replace(old, new), encoding="utf-8", newline="")
                code, took, tail = run_tests(copy, tests, args.timeout)
            finally:
                shutil.rmtree(copy, ignore_errors=True)
            result = outcome(code, tail)
            lived += result == "LIVES"
            void += result == "void"
            print(f"{result:5}  {name}  (exit {code}, {took:.1f} s; {tail})")
        if mutations:
            print(f"mutations: {len(mutations)}; red: {len(mutations) - lived - void}; LIVES: {lived}; void: {void}")

        changed = changed_functions(repo, base, commit)
        mutated = set()
        for name, rel, old, new, tests in mutations:
            text = texts.get(rel) or (pristine / rel).read_text(encoding="utf-8")
            if text.count(old) == 1:
                mutated.add((rel, holder(functions(text), where(text, old))))
        have = sorted(k for k in changed if k in mutated)
        lack = sorted(k for k in changed if k not in mutated)
        print(f"{base[:7]}..{commit[:7]}: {len(changed)} functions changed in src; with a mutation: {len(have)}; "
              f"with none: {len(lack)}")
        print("Read by hand: for each function below on the path from a report or a kept file to a verdict, add a "
              "mutation or say in one line why none is needed.")
        for rel, name in lack:
            print(f"  none  {rel}  {name}  ({changed[(rel, name)]} changed lines)")
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
    return 1 if lived or void else 0


if __name__ == "__main__":
    sys.exit(main())
