# 05 · Measurements

A case study: the tool measured on one real project, 2026-10-26 to 2026-11-12. The scripts are in [`scripts/`](scripts/): `measure.py` opens projects in batch Editors and samples what they cost, `pool_live.py` times a slot made and then reused, and `repeat_runs.py` repeats the open runs in the tool's own slots. The raw reports are not published; the figures below are taken from them.

## The project and the machines

- **The project.** A Unity 6 HDRP project on Unity 6000.3, with about 17,000 tracked files. A worktree checkout is about 7.5 GB, Git LFS content included, and took 1:51 to 2:44 minutes to make. A long-used `Library` was 8.5 GB in about 71,000 files; one imported from nothing was 5.8 GB in about 57,000 files. The log counts about 3,000 scripts and 9,400 other assets.
- **The 16 GB machine.** 15.7 GB usable, with other programs running.
- **The 32 GB machine.** 12 threads and 31.4 GB usable, with other programs running; the CPU was often busy before any Editor ran.
- Both ran Windows 11 on NVMe disks, with the `unity` CLI 1.0.0-beta.14 under a Unity Personal licence.

**Method.** Every open used the tool's own launch: `-batchmode -nographics -accept-apiupdate -job-worker-count 4 -quit`. `measure.py` samples once a second. "Peak tree" is the working set of the Editor and its child processes (import workers, shader compilers) added together. Machine RAM and CPU are the whole machine's. Refresh, compile and domain reload times come from the `Asset Pipeline Refresh` summary in the Editor log. GUI Editors, PlayMode and builds were not measured.

**A project may need git-ignored files to compile.** The project uses a third-party package that is kept out of git (about 1 GB) from tracked scripts. Without it, a worktree's first batch Editor stopped on `error CS0246` and exited 1 after 82 s. Such files are listed in `local_copies` and copied into every worktree.

## The `unity` CLI

- `unity upgrade --channel beta` moved it from 1.0.0-beta.13 to 1.0.0-beta.14. `unity doctor` passed every check.
- Auth: the CLI was already signed in with OAuth from the keyring. No `unity auth login` was needed. Three batch Editors at once ran under one Personal licence with no licence errors.
- `--help` on beta.14 settled the flags the docs left out:

  | Command | How the project is given |
  |---|---|
  | `close` | positional (`close <project> [--force]`) |
  | `test` | positional (`test [project]`); `--mode EditMode`, `--output <path>` |
  | `command` | `--project-path` (`command [--project-path P] <name> [args...]`) |
  | `pipeline install` | `--project-path` |

- `unity open --batchmode --until-ready` on a worktree **without** the Pipeline package timed out after 300 s (`OPEN_READY_TIMEOUT`, `lastSeen: noServer`). Plain `close` then refused (`PROJECTS_CLOSE_PIPELINE_MISSING`); `close --force` worked.
- With the packages installed, the Editor was ready in 146 s on the first open after install, compiling the new packages included. `status --project-path` found it, and a graceful `close` took 4.6 s.
- **`unity test` starts its own Editor** (`-batchmode -runTests`). Run while the same project is open, it exits 6 (`COMMAND_FAILED`) because the project is locked. The checks run `unity test` alone. Its results file goes to the project folder unless `--output` says otherwise; the checks write it to `Logs/`, which git ignores.
- `unity pipeline install` added one line to `Packages/manifest.json` (`"com.unity.pipeline": "0.9.1-exp.1"`). Both `com.unity.pipeline` and the embedded `com.upe.agent` got lock entries. Both package files stayed skip-worktree while Unity rewrote them.

## Copying the Library

| Copy | Time | Notes |
|---|---|---|
| main checkout to a new worktree, 16 GB machine | 118 s | 8.5 GB, about 82 MB/s |
| that worktree to two more, empty, 16 GB machine | 197 to 202 s | |
| over a Library copied before | 4 to 18 s | `robocopy /MIR` copies only what differs |
| a new slot from the golden (worktree, local copies, Library), 32 GB machine | 53 to 59 s | each of four in turn |

## Opening a worktree

**The 16 GB machine.**

| Run | Wall | Imports | Peak tree | Machine RAM used |
|---|---|---|---|---|
| one worktree, Library from the main checkout, first open | 237 s | 705 | 3.78 GB | 13.6 to 15.3 GB |
| the same worktree again, nothing changed | 20 s | 0 | 2.54 GB | 9.3 to 11.5 GB |
| a second worktree, Library from the first (same commit), first open | 200 s | 0 (1 changed file) | 3.65 GB | 12.0 to 15.2 GB |
| three at once, each Library from another path | 257, 257, 263 s | 0, 0, 1 | 1.9 to 2.3 GB each | 8.7 to 15.6 GB of 15.7 |
| three at once, reopened | 22.8 s each | 0 | 2.2 GB each | 9.0 to 15.7 GB |

The CPU averaged 41 to 45% with one Editor and 43% with three. The three-at-once working sets are lower than the single runs only because Windows trimmed them under memory pressure.

**Where a first open at a new path spends its time** (16 GB machine):

| Part | one alone | each of three at once |
|---|---|---|
| Scan (hashing source files whose path and timestamps are new) | 50 s | 44 s |
| Script compile | 69 s | 118 to 121 s |
| Domain reload | 39 s | 45 to 46 s |
| Imports | 11 s | |
| Refresh total | 191 s | 244 to 251 s |

**The 32 GB machine**, four new slots from the golden at one commit (`repeat_runs.py`):

| Run | Wall | Imports | Peak tree | Machine RAM used | Machine CPU |
|---|---|---|---|---|---|
| one slot, first open | 68 s (refresh 57 s) | 8 | 3.16 GB | 17.5 to 21.1 GB | 63% |
| the same slot again | 11 s (refresh 7 s) | 0 | 2.31 GB | 16.8 to 19.0 GB | 38% |
| three at once, first open | 71 s each (refresh 64 to 65 s) | 2, 8, 2 | 2.9 to 3.0 GB each | 17.0 to 27.5 GB of 31.4 | 90% |
| three at once, reopened | 23 to 24 s each (refresh 14 to 16 s) | 0 | 2.2 to 2.3 GB each | 17.1 to 23.4 GB | 97% |

No run rebuilt its Library or hit a compile error.

| | 32 GB machine | 16 GB machine |
|---|---|---|
| New slot to an open Editor, alone | about 2 minutes | about 7 minutes |
| First open at a new path, alone | 68 s | 200 s |
| Same, three at once | 71 s each | 257 to 263 s each |
| Reopen, alone | 11 s | 20 s |
| Reopen, three at once | 23 to 24 s each | 22.8 s each |
| Peak tree, first open | 3.2 GB | 3.8 GB |
| Three at once | fit, 4 GB left | filled memory, working sets trimmed |

## Reusing a slot

`pool_live.py` on the 32 GB machine, with the golden as the Library source. The new slot was made at the golden's own commit, so its first open measures the cost of a new path alone. The parked slot was then given to a commit that differs in two scripts and one `.meta`. The machine was 90% busy with other work during this run, so its first open is slower than in the table above.

| Step | Time |
|---|---|
| New slot: worktree, local copies, Library copy | 75 s |
| First open at the new path | 157 s: refresh 133 s, 2 imports |
| Park the slot (Unity's rewrites discarded) | 2.3 s |
| Check out the other commit in the parked slot | 0.8 s |
| Open the reused slot | 38 s: refresh 32.5 s, of which scan 0.5 s, compile 20 s, domain reload 2.2 s; 0 imports |

A new slot took 232 s from nothing to an open Editor; a parked slot took 39 s. The reused open is 38 s rather than a plain reopen because the two changed scripts had to be compiled again.

## What it means

1. **A copied Library skips the full reimport.** Once the source Library matched the commit, the copy at a new path imported nothing. With the Library from the main checkout, Unity imported the 705 files where that checkout differed (upgraded and uncommitted files). So the golden is a checkout the tool owns, kept at the target.
2. **A new path still costs one slow first open:** about 200 s alone on the 16 GB machine and about 70 s on the 32 GB one, against 11 to 20 s for a reopen. Most of it is scanning, recompiling and reloading, not importing. So tasks reuse parked slots instead of making a new path each time.
3. **Memory per Editor.** A batch Editor uses 2.2 to 2.5 GB once ready and up to 3.2 to 3.8 GB during a first open.
4. **Which limit counts depends on the machine.** On the 16 GB machine memory was the limit: three Editors filled it while the CPU averaged 43%. On the 32 GB machine three fit with 4 GB left, and the CPU was the limit: 90% on first opens and 97% on reopens. Three at once there barely slowed a first open (68 s to 71 s) but doubled a reopen (11 s to 23 s). So the judge sets CPU priority, which decides who gets the CPU, and counts memory as short below `memory_short_mb` (4096 MB, one first-open peak).
5. **A golden refresh after each land is cheap** when little changed: a reopen at the same path costs about 20 s plus the import of the changed files.

## Files Unity rewrites on open

- **Upgraded files.** Unity rewrote three tracked files on every open: a texture's `.meta`, a package's `Settings.json` and `ShaderGraphSettings.asset`. The project's committed versions were older, and Unity upgrades them when it opens them. Until a project commits the upgraded versions, every task shows them as changed outside its claim. `upe editor test` and `upe editor run` put back tracked files a run changed outside the claim, and `land` names the files it refuses.
- **Line endings only.** With `core.autocrlf` on, git checks files out with CRLF, and Unity rewrote several ProjectSettings files with LF. Each still hashes to the blob in HEAD, but its size no longer matches the index, and git then reports it as modified without comparing content. So `git status` calls the slot dirty. The tool's dirty check uses `git diff --name-only HEAD` plus untracked files, which compares content.
- **IDE integration.** Unity's IDE integration sets `dotnet.defaultSolution` in `.vscode/settings.json` to `<folder name>.slnx`, and writes that `.slnx`, untracked. Left alone, every slot Unity opens would count as dirty, and an agent that commits everything would carry them into its branch. The tool marks `.vscode/settings.json` skip-worktree, like the package files, and adds `/<folder>.slnx` to `info/exclude`. That file is shared with the main checkout, and a project may track `.slnx` files of its own, so the line names only that folder's file.
- **Empty folders.** Unity keeps empty folders for the folder `.meta` files git tracks. An early fix ran `git clean -fd` in a slot, which deleted them, and the next land failed on 23 "orphan .meta" findings. The tool now deletes only the files a branch is about to add, and the `.meta` checks read the merge's commit through git, not the slot's disk.

## The scene check and the tests

- With `-nographics`, the scene check crashed Unity (0xC0000005) inside `EditorSceneManager.OpenScene`. Loading a scene with a Terrain renders its splat maps, which needs a graphics device. The scene check runs with one.
- Run in batch mode, it loaded all of the project's scenes and prefabs (about 90 and 800) in 435 s, and found about 1,100 problems already in the project, nearly all missing references. So the check compares against the target's own problems.
- `unity test --mode EditMode` on a closed project ran about 1,700 tests in 243 s. 15 failed, all already failing on the target. Counted as findings, they would stop every merge, so the land tests the target too and counts only new failures. With the Editor's start and compile, one full run took 865 to 927 s on the 16 GB machine.
- **Licensing.** Once Unity's licensing did not start (`Licensing initialization failed after 75.80s`), its packages went unentitled, and the project logged over a thousand compile errors in code that had not changed. 11 minutes later the same project compiled. The tool tries a scene check again when its log shows this, and if it fails twice, says it is not about the code.

## Two agents at once

Fresh agents, each given one task and told only to use the `upe` skill, got a slot, wrote and tested their work, and landed. Their claims did not overlap and no slot touched another. Council members ran as separate processes; a member started as a subagent of the other member's process was refused, as designed.

**Land times** up to the council, 32 GB machine, the full EditMode suite run on the target and on the merge unless a kept result spared it:

| Land | Up to the council |
|---|---|
| alone, one scene changed, the target's tests run | 62 min |
| alone, every scene checked, the target's tests run | 69 min |
| alone, the target's tests and scene check read from kept results | 41 min |
| two lands at once | 89 to 107 min each |

A test run on the merge took 22 to 28 minutes alone and up to 47 minutes on a busy machine. The full scene check took 9 to 13 minutes alone; a check of one changed scene took under a minute.

**Losing the race.** Two merges built on one target can both get two GO votes. The first lands; the second finds the target moved, and nothing lands, so an unchecked merge cannot reach the target. Its next land, alone, took 69 minutes and needed a new council, for a change that touched nothing the other task did. The tool cannot know a change is safe without running the checks.

**Claims serialise one scene.** When a second task claimed a scene a running task had claimed, its `start` was refused. Two tasks never edit one scene at once.

## Merging real files

**The trial.** Real files of the project (a small scene, a prefab, `TagManager`, `EditorBuildSettings`, the manifest and the lock) went through the tool's merger and the real UnityYAMLMerge, in a throwaway repository, with no Editor. It takes seconds.

**The first run found two bugs:**
- **The scene merge had never worked.** UnityYAMLMerge chooses how to merge by the file's extension, and the tool gave it files named `base`, `mine` and `theirs`. It answered "Don't know how to merge files" for every scene and prefab. A fake merger in the unit tests hid it. A test now runs the real tool.
- **UnityYAMLMerge can lose a change and exit 0.** With one side deleting an object and the other renaming it, it exited 0 with the object gone. The tool now compares the result with both sides, object by object, and inside an object both sides changed, line by line and by place (DESIGN.md, "The flow").

**The loss check, tested.** Reviews found more losses the real UnityYAMLMerge makes with exit 0: a dropped `stripped` flag, one side's swap of two children dropped when the other adds a child, a swap against a rewrite of a swapped line. Each now stops. On random edits to `{fileID}` lists through the real UnityYAMLMerge, with `git merge-file` as referee, every result equal to git's clean merge passed, and the check stopped most results git also found a conflict in. Every scene and 150 prefabs of the project, each side adding an object through the real tool, stop none. UnityYAMLMerge also rewrites formatting: it folds long references and names at another column than the Editor, and changes the quoting of values. The check reads those as equal; a value it cannot read for sure is compared exactly as written, which can stop a good merge but loses nothing. Known open: a deletion inside a replace (children `10, 11, 12` made `20, 12` on one side and `20, 21, 12` on the other; the tool keeps `20, 21, 12`), one null child lost from a run of `{fileID: 0}`, and close edits among lines that repeat in one object.

**After the fixes:**

| Both sides | Result |
|---|---|
| Added an object to one scene | Merged, both kept |
| Renamed different objects | Merged by git, both kept |
| Changed different properties of one object | Merged, both kept |
| Added a tag at the same place, or a build scene | Merged as a list, both kept |
| Added different packages | Merged, both kept |
| Changed the lock | Deleted, for Unity to regenerate |
| Added an object to a 12 MB scene | Merged, both kept, 32 added lines in the diff |
| Overrode one nested prefab instance, or renamed different objects in that prefab | Merged, both kept |
| Renamed one object two ways, in a scene or a prefab, large or small | Stopped |
| One deleted an object the other changed | Stopped |
| One deleted an object the other now refers to | Merged by git; the static check reports the dangling fileID, which fails the land |
| Gave one asset two GUIDs | Stopped |
| Named one layer slot two ways, or one package at two versions | Stopped |
| Changed two fields of one `.meta` | Stopped: UnityYAMLMerge exits 1. Stricter than it needs to be, and safe |
| Wrote one script line two ways | Stopped |

**Live lands.**
- **Both sides added an object and a tag** to one scene. UnityYAMLMerge merged the scene and the settings merge took the tags. Unity loaded the merged scene with no new problems, the council voted GO, and it landed.
- **Both sides changed one component**, on adjacent lines. `git merge-file` calls it a conflict; UnityYAMLMerge merged it, the loss check found nothing lost, and it landed.
- **A real conflict**, one object renamed two ways, stopped in 6 seconds, since `land` tries the merge before the target's baseline.
- **Formatting.** For a two-line merge in the project's largest scene (12 MB, tens of thousands of objects), UnityYAMLMerge rewrote 700 lines, mostly quoting. Each object the merge did not change now keeps the target's exact text, so the merge shows only its real changes. The merge and the check together take about 5 seconds on that scene.

### An agent resolving a conflict

A fresh agent, knowing only the skill, was asked to rename an object in the 12 MB scene that the target had already renamed another way. The land stopped in 12 seconds and named the object. The agent ran `git merge`, kept both meanings in one name, committed and landed again. Nobody told it there would be a conflict. The council voted GO. One member found the resolution only through `git log`, so the brief now shows each merge on the branch and what its resolution chose.

## Tests under load

- **Lands failed on time limits.** The project's tests have time limits, and the tool runs at BELOW_NORMAL priority, so on a busy machine lands failed on a different test each time, or the suite did not finish in 3,600 s. The same test classes, run alone afterwards, passed: 172 s of test time, against 180 to 297 s for one test during a failed land.
- **`unity test --retries`** re-runs failing tests in a new Editor. The report named by `--output` keeps the first attempt; each retry writes `<name>.attempt-N.xml`; `<name>.retries.json` lists `flaky` and `failed`; it exits 0 if every failure passes on a retry, 8 if not. The tool passes `--retries` (`test_retries`, 1 by default), and a test that passes on a retry does not stop the land; `checks.md` names it for the council.
- **Parameterised tests are not retried.** The CLI lists them as failed after every attempt without running them again. The tool runs their classes once more after a full run.
- **A parked slot far behind the target is slow.** A class-only run on the target took 22 minutes, 14 of them before the first test: the parked slot's `Library` was 361 commits behind, and its Editor compiled and imported the difference. How far a parked slot is behind the target decides the cost of a run there.
