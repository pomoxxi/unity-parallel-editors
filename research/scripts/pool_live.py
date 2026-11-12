"""Live check of the worktree pool on a real project (research/05).

Makes one slot from a golden, opens it, parks it, gives it to a second task
at another commit and opens it again. Prints the time of each step.
Reports go to research/data/ (made if missing, and git-ignored).
The Editor is the one named in UPE_UNITY_EDITOR; nothing else is searched (DESIGN decision 16).

    python research/scripts/pool_live.py <repo> <pool-root> <golden> <commit-a> <commit-b> <local-copy>...
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


def timed(label, fn):
    t = time.monotonic()
    out = fn()
    print(f"{label}: {time.monotonic() - t:.1f} s", flush=True)
    return out


def open_once(path: Path, out: Path) -> dict:
    subprocess.run([sys.executable, str(MEASURE), "--editor", str(find_editor(path)), "--out", str(out), str(path)],
                   check=True, capture_output=True)
    run = json.loads(out.read_text())["runs"][0]
    print(f"  open: {run['wall_s']} s, exit {run['exit_code']}, imports {run['log']['start_importing']}, "
          f"refresh {[r['seconds'] for r in run['log']['refreshes']]}", flush=True)
    return run


def main() -> None:
    repo, root, golden, commit_a, commit_b, *local = sys.argv[1:]
    ws = Workspaces(repo, root, golden, local_copies=local)
    data = Path(__file__).resolve().parents[1] / "data"
    data.mkdir(parents=True, exist_ok=True)
    a = timed("acquire, new slot (worktree, local copies, Library copy)",
              lambda: ws.acquire("pool-a", "upe/pool-a", commit_a))
    open_once(a, data / "pool-first-open.json")
    timed("release (Unity's rewrites discarded)", lambda: ws.release("pool-a", discard_changes=True))
    b = timed("acquire, parked slot (checkout of the other commit)",
              lambda: ws.acquire("pool-b", "upe/pool-b", commit_b))
    print(f"  same slot: {a == b}", flush=True)
    open_once(b, data / "pool-reused-open.json")
    timed("release", lambda: ws.release("pool-b", discard_changes=True))
    for branch in ("upe/pool-a", "upe/pool-b"):
        subprocess.run(["git", "-C", repo, "branch", "-D", branch], capture_output=True)


if __name__ == "__main__":
    main()
