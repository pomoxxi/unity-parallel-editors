# 06 · Security review

2026-10-28. The tool had just stopped calling any model (DESIGN.md decision 11), and agents were about to call it themselves (decision 13). A fresh reviewer agent read the code and DESIGN.md without being told what to look for. Its findings, and those found later, are below with what was done. DESIGN.md "Security" is the summary.

## The setting

Every agent runs as the same Windows user as the person, on the same machine, with the same rights. The project's `.git` is shared by the user's checkout and every slot. Three things common to Unity projects used with agents make some findings worse:

- **Agent hooks are tracked.** A project often commits `.claude/settings.json`, whose hooks run scripts from the project folder, and `CLAUDE.md` or `AGENTS.md`.
- **A pre-commit hook may run a tracked script.** `.git/hooks/pre-commit` often calls a script in the repository, a file branches can change.
- **The user's working branch is checked out.** It is in the main checkout and tracks `origin`.

## How Boat.dev handles the same question

Boat.dev ([docs](https://docs.boat.dev/security.md)) gives each agent a VM of its own. Its docs say "anyone with a shell in a sandbox is root in that sandbox": isolation is per machine, not per agent inside one. Agents learn the tool from a skill pack and an AGENTS.md snippet. The CLI takes `--json`, IDs are always explicit, and long jobs run with `--detach`, then `--status`. This tool borrowed the CLI shape and the skill. It cannot borrow the VM, so it is a guardrail, not a boundary (decision 15).

## Findings

| # | Finding | Severity | Done |
|---|---|---|---|
| 1 | One agent could be the whole council: vote twice under two names, or through its own subagents. `decide` was open to agents. Verdicts were not tied to a commit. | high | A verdict from the process that ran `land`, or from the other voter's process, is refused (`identity.py`). Verdicts must name the merged commit. `decide` needs a person at a terminal. |
| 2 | Landing trusted `merge.json`, which any agent can edit: a forged `target` could fast-forward the user's working branch, inside the user's own checkout. | high | The target comes from the config. The landed commit must be the candidate's tip, with the task branch inside it. A target tracking a remote, or checked out outside the slots, is refused. |
| 3 | `python -m upe` and the default `./upe.toml` both came from the current folder, so a branch could ship its own `upe/` or config. | high | Config from `UPE_CONFIG` or the tool's folder, and refused inside the project, root or golden. Agents run the `upe` console script; the skill says so. |
| 4 | Branch code runs in batch Editors with the user's rights before any council, and could fake a pass by exiting 0 before the tests, or by defining `Upe.Agent.SceneCheck`. | high | No results report is no pass. Code in `Upe.Agent` stops the merge. Refs, git config and hooks are fingerprinted around each Unity run. Running the code itself stays open (needs another user or a sandbox). |
| 5 | The brief sent members into the merged worktree, whose `CLAUDE.md` and `.claude/` hooks come from the branch. | high | Members are told not to start a session or run anything there. Changes to agent instructions and hooks stop the merge (`checks.PROTECTED`). |
| 6 | Prompt injection through the brief: backticks in a diff could close its fence; padding could push code out of the cut. | medium | Fences longer than any backtick run; evidence labelled as data and followed by the rules; a full file list first. |
| 7 | Races: two verdicts at once, `land` during a verdict, half-written state files, a lock broken by age while held. | medium | A per-task lock around `land`, `verdict`, `decide`, `finish`; verdict files created exclusively; atomic writes; OS file locks. |
| 8 | A reused slot kept what the dirty check cannot see: index flags, ignored files (an ignored Editor script still runs), per-worktree config. | medium | A reused slot is scrubbed of all three, keeping only its Library. The test for it caught a git quirk: `update-index` with both `--no-skip-worktree` and `--no-assume-unchanged` applies only one. |
| 9 | Slot names were not validated, and `reset --hard` / `clean` followed them. | medium | Names must be `s<number>`; destructive commands only in a slot git confirms, never the main checkout. |
| 10 | `robocopy /MIR` and `rmtree` follow junctions and symlinks. | medium | Copies refuse links under the tool's root; the tool's git runs with `core.symlinks=false`. |
| 11 | Hooks and `core.fsmonitor` in the shared `.git` ran with the tool's git; `gather_diffs` ran diff drivers. | medium | The tool's git runs with no hooks and no fsmonitor; diffs with `--no-ext-diff --no-textconv` (`safegit.py`). |
| 12 | Claims were advisory; quoted non-ASCII names skipped the checks. | medium | Unclaimed changes stop the merge; file lists are NUL-separated. |
| 13 | `upe activity ../claims ...` wrote outside the activity folder; ids were not checked on `verdict`, `decide`, `finish`. | low | Ids checked wherever they name a file. |
| 14 | `--base` went to `git rev-parse` as given, so it could be read as an option. | low | `--end-of-options`, and a leading `-` is refused. |
| 15 | Two paths that differ only in case are one file on Windows. | low | Stops the merge. |
| 16 | A credential helper such as Git Credential Manager can store a GitHub token where plain command-line git finds it. Every agent running as that user can then push to every repository the token reaches, the user's working branch included. | high | DESIGN.md says to keep no credential where command-line git finds it, and to push from somewhere else, such as an app that keeps its own. `cmdkey /list` shows what Windows holds. |
| 17 | An antivirus exclusion of the whole root, made to speed up imports, means nothing in a slot is scanned, including anything an agent downloads there. | medium | Advice: if you exclude anything for speed, exclude only the `Library` folders of the golden and the slots. Source files, packages and downloads in a slot stay scanned; only the Library files Unity reads on a first open are not. |
| 18 | Found in a later review, 2026-10-31: a `land` cut short after its merge left the task `awaiting-council` with `merge.json` on the new commit. One old GO and one new GO would land a merge that never finished its checks. | high | `land` voids the earlier merge and votes as it starts, before any merge. |
| 19 | Found in the same review: a formatting comparison that ignored all spaces and quotes reverted a real rename such as `Boss Room` to `BossRoom`, reported it merged, and hid it from the loss check. It was added in the same work. | high | Only quotes around a whole value and trailing space count as formatting. Tests cover changes inside a value. |
| 20 | Found in the same review: regenerate and builder runs ran branch code outside the fingerprint guard. A report with no tests counted as a pass. | medium | Those runs are guarded. No tests, or a failing exit with no failures listed, is a finding. |

## Still open

These need the agents to run as another Windows user, in a sandbox, or without credentials the user holds:

- **Git and state.** Any agent can run `git update-ref`, edit `.upe/`, or forge a test baseline itself. The tool cannot stop a deliberate agent.
- **Branch code in the Editor.** It runs with the user's rights and can read the environment it inherits.
- **Credentials.** An agent that can reach a stored credential can use it.
- **Antivirus.** Files an agent puts in an excluded `Library` folder are not scanned (finding 17).
