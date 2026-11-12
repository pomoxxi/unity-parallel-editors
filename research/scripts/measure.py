"""Open one or more Unity projects in batch Editors at once and record what it costs.

Used for research/05-measurements.md. Each project is opened the way the tool
opens one (src/upe/editor.py: -batchmode -nographics -accept-apiupdate
-job-worker-count N -quit), all at the same time. Once a second it samples
every Editor's process tree (import workers and shader compilers included)
and the machine's memory and CPU. When all have exited it reads each log for
signs of a full reimport and prints one JSON report.

    python research/scripts/measure.py --editor <Unity.exe> --out report.json <project> [<project> ...]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

import psutil

PATTERNS = {
    # Unity prints this when Library/ has no usable asset database and starts over.
    "rebuilding_library": r"Rebuilding Library because",
    "start_importing": r"Start importing .+ using Guid",
    "compile_started": r"\[ScriptCompilation\] Requested script compilation",
    "errors_cs": r"error CS\d{4}",
}
REFRESH = re.compile(r"Asset Pipeline Refresh \(id=\w+\): Total: ([\d.]+) seconds - Initiated by (\w+)")
DOMAIN_RELOAD = re.compile(r"Domain Reload Profiling: (\d+)ms")


def tree(proc: psutil.Process) -> list[psutil.Process]:
    try:
        return [proc, *proc.children(recursive=True)]
    except psutil.Error:
        return [proc]


def read_log(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    out = {name: len(re.findall(rx, text)) for name, rx in PATTERNS.items()}
    out["refreshes"] = [{"seconds": float(s), "by": by} for s, by in REFRESH.findall(text)]
    out["domain_reloads_ms"] = [int(ms) for ms in DOMAIN_RELOAD.findall(text)]
    out["log_bytes"] = len(text)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("projects", nargs="+", type=Path)
    ap.add_argument("--editor", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--job-workers", type=int, default=4)
    ap.add_argument("--stagger", type=float, default=0.0, help="seconds between starts")
    ap.add_argument("--timeout", type=float, default=7200.0)
    args = ap.parse_args()

    psutil.cpu_percent(None)
    mem0 = psutil.virtual_memory()
    runs = []
    for project in args.projects:
        log = project / "Logs" / "measure.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists():
            log.unlink()
        argv = [args.editor, "-batchmode", "-nographics", "-accept-apiupdate",
                "-job-worker-count", str(args.job_workers),
                "-projectPath", str(project), "-logFile", str(log), "-quit"]
        popen = subprocess.Popen(argv)
        runs.append({"project": str(project), "log": log, "popen": popen, "ps": psutil.Process(popen.pid),
                     "start": time.monotonic(), "end": None, "peak_rss": 0, "peak_procs": 0, "cpu_s": 0.0})
        if args.stagger:
            time.sleep(args.stagger)

    t0 = time.monotonic()
    peak_used = 0
    cpu_samples = []
    seen_cpu: dict[int, float] = {}
    while any(r["end"] is None for r in runs):
        if time.monotonic() - t0 > args.timeout:
            for r in runs:
                if r["end"] is None:
                    r["popen"].kill()
            break
        for r in runs:
            if r["end"] is not None:
                continue
            if r["popen"].poll() is not None:
                r["end"] = time.monotonic()
                continue
            rss, procs = 0, tree(r["ps"])
            for p in procs:
                try:
                    rss += p.memory_info().rss
                    t = p.cpu_times()
                    seen_cpu[p.pid] = t.user + t.system
                    r.setdefault("pids", set()).add(p.pid)
                except psutil.Error:
                    pass
            r["peak_rss"] = max(r["peak_rss"], rss)
            r["peak_procs"] = max(r["peak_procs"], len(procs))
        vm = psutil.virtual_memory()
        peak_used = max(peak_used, vm.total - vm.available)
        cpu_samples.append(psutil.cpu_percent(None))
        time.sleep(1.0)

    gb = 1024 ** 3
    report = {
        "machine": {"logical_cpus": psutil.cpu_count(), "cores": psutil.cpu_count(logical=False),
                    "ram_gb": round(mem0.total / gb, 1), "used_before_gb": round((mem0.total - mem0.available) / gb, 1),
                    "peak_used_gb": round(peak_used / gb, 1)},
        "cpu_percent": {"mean": round(sum(cpu_samples) / max(len(cpu_samples), 1), 1),
                        "peak": max(cpu_samples, default=0)},
        "job_workers": args.job_workers,
        "runs": [],
    }
    for r in runs:
        report["runs"].append({
            "project": r["project"],
            "exit_code": r["popen"].returncode,
            "wall_s": round((r["end"] or time.monotonic()) - r["start"], 1),
            "peak_tree_rss_gb": round(r["peak_rss"] / gb, 2),
            "peak_processes": r["peak_procs"],
            "cpu_s": round(sum(seen_cpu.get(pid, 0) for pid in r.get("pids", ())), 1),
            "log": read_log(r["log"]),
        })
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
