"""The scan of `tests/test_argv_capped.py`: every expression in `src/upe` that puts a list of unknown length
on a command line, and whether a cap bounds it. Kept apart from the test so the test's self-checks can feed it snippets.

A growth expression is:
- a starred argument (`*x`) inside a call's arguments or inside a list or tuple, whose value is not a literal and not
  an ALL_CAPS constant (`*SAFE_GIT`);
- `";".join(x)` (Unity's and the `unity` CLI's list separator);
- `argv += x`, `argv.extend(x)` for a name `argv`, `cmd`, `command` or `args`, with a value that is not a literal.

It is `capped` when a name in it is bound, in the same function, by a `for` (or a comprehension) over an expression
that calls `capped` or `_capped`, or by an assignment from such an expression (`groups = list(capped(...))`; then
`groups[0]` and `for k, group in enumerate(groups)` are capped too).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

CAPPERS = ("capped", "_capped")
GROWING_NAMES = ("argv", "cmd", "command", "args")


@dataclass(frozen=True)
class Growth:
    file: str
    function: str
    expr: str
    line: int
    capped: bool


def _calls_capper(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in CAPPERS:
                return True
    return False


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _constant(node: ast.AST) -> bool:
    """A literal; a list or tuple written out (its length is fixed, and a starred item in it is scanned on its own);
    a conditional between two such; or an ALL_CAPS module constant (a name or an attribute)."""
    if isinstance(node, (ast.Constant,)):
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return True
    if isinstance(node, ast.IfExp):
        return _constant(node.body) and _constant(node.orelse)
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
    return bool(name) and name.isupper()


def _capped_names(function: ast.AST) -> set[str]:
    """The names a function binds from a capper: loop and comprehension targets over one, and assignments from one.
    Repeated until nothing new, so a loop over a name assigned from a capper counts too."""
    found: set[str] = set()
    while True:
        before = len(found)
        for node in ast.walk(function):
            if isinstance(node, (ast.For, ast.comprehension)):
                if _calls_capper(node.iter) or _names(node.iter) & found:
                    found |= _names(node.target)
            elif isinstance(node, ast.Assign):
                if _calls_capper(node.value) or _names(node.value) & found:
                    for target in node.targets:
                        found |= _names(target)
        if len(found) == before:
            return found


def scan_source(text: str, file: str = "<snippet>") -> list[Growth]:
    tree = ast.parse(text)
    out: list[Growth] = []

    def visit(node: ast.AST, qual: str, capped: set[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{qual}.{child.name}" if qual else child.name
                visit(child, name, capped | _capped_names(child))
                continue
            if isinstance(child, ast.ClassDef):
                visit(child, f"{qual}.{child.name}" if qual else child.name, capped)
                continue
            growth = _growth(child, node)
            if growth is not None:
                out.append(Growth(file, qual or "<module>", ast.unparse(growth), child.lineno,
                                  bool(_names(growth) & capped)))
            visit(child, qual, capped)

    visit(tree, "", set())
    return out


def _growth(node: ast.AST, parent: ast.AST) -> ast.AST | None:
    """The growing value `node` puts on a command line, or None."""
    if isinstance(node, ast.Starred) and isinstance(parent, (ast.Call, ast.List, ast.Tuple)):
        return None if _constant(node.value) else node.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join"
            and isinstance(node.func.value, ast.Constant) and node.func.value.value == ";" and node.args):
        return node.args[0]
    if (isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add) and isinstance(node.target, ast.Name)
            and node.target.id in GROWING_NAMES and not _constant(node.value)):
        return node.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "extend"
            and isinstance(node.func.value, ast.Name) and node.func.value.id in GROWING_NAMES and node.args
            and not _constant(node.args[0])):
        return node.args[0]
    return None


def scan(src: Path) -> list[Growth]:
    out: list[Growth] = []
    for path in sorted(src.glob("*.py")):
        out += scan_source(path.read_text(encoding="utf-8"), path.name)
    return out


if __name__ == "__main__":
    import sys

    for g in scan(Path(sys.argv[1])):
        print(f"{'CAPPED' if g.capped else '      '} {g.file}:{g.line} {g.function} :: {g.expr}")
