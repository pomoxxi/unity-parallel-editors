"""The open measurements of research/05, repeated with the tool's own slots.

Makes four new slots from the golden at one commit, then opens:
one slot alone, the same slot again, the other three at once, and those three again.
The slots are parked afterwards, Library kept, so the tool's next tasks reuse them.

    python research/scripts/repeat_runs.py <repo> <root> <golden> <commit> <prefix> <local-copy>...

Reports go to research/data/<prefix>-*.json (made if missing, and git-ignored). The Editor is found as in pool_live.py.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from upe.editor import find_editor  # noqa: E402
from upe.workspace import Workspaces  # noqa: E402

MEASURE = Path(__file__).with_name("measure.py")
DATA = Path(__file__).resolve().parents[1] / "data"


def measure(name: str, paths: list[Path]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    out = DATA / f"{name}.json"
    subprocess.run([sys.executable, str(MEASURE), "--editor", str(find_editor(paths[0])), "--out", str(out),
                    *map(str, paths)], check=True, capture_output=True)
    report = json.loads(out.read_text())
    print(f"{name}: cpu mean {report['cpu_percent']['mean']}%, machine {report['machine']['used_before_gb']} -> "
          f"{report['machine']['peak_used_gb']} GB", flush=True)
    for run in report["runs"]:
        print(f"  {Path(run['project']).name}: {run['wall_s']} s, exit {run['exit_code']}, imports "
              f"{run['log']['start_importing']}, refresh {[r['seconds'] for r in run['log']['refreshes']]}, "
              f"peak tree {run['peak_tree_rss_gb']} GB", flush=True)


def main() -> None:
    repo, root, golden, commit, prefix, *local = sys.argv[1:]
    ws = Workspaces(repo, root, golden, local_copies=local)
    owners = [f"measure-{i}" for i in range(1, 5)]
    slots = []
    for owner in owners:
        t = time.monotonic()
        slots.append(ws.acquire(owner, f"upe/{owner}", commit))
        print(f"new slot {slots[-1].name} (worktree, local copies, Library copy): {time.monotonic() - t:.1f} s", flush=True)
    try:
        measure(f"{prefix}-m1-first-open", slots[:1])
        measure(f"{prefix}-m1-reopen", slots[:1])
        measure(f"{prefix}-x3-first-open", slots[1:])
        measure(f"{prefix}-x3-reopen", slots[1:])
    finally:
        for owner in owners:
            ws.release(owner, discard_changes=True)
            subprocess.run(["git", "-C", repo, "branch", "-D", f"upe/{owner}"], capture_output=True)


if __name__ == "__main__":
    main()
