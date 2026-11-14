# Unity Parallel Editors (`upe`)

Several AI coding agents work on one Unity project at once. Each agent gets its own git worktree, its own `Library` and its own Unity Editor. Their work is merged back through checks. One of them, the loss check, stops a scene or prefab merge that silently drops a change. Then a council of two fresh agents, ideally on two different models, which did not do the work, judges the merge. The tool itself calls no model and needs no API key: agents run in their own apps and call the tool.

**Why.** Unity lets only one Editor open a project folder. With one checkout and one Editor, agents queue behind each other. And merging scenes and prefabs is risky: UnityYAMLMerge can drop a change and still report success.

**Status:** alpha. Windows only. Its author measured it on one large Unity 6 URP project (about 17,000 tracked files). No one else has reported running it yet, so expect rough edges, and [report what breaks](https://github.com/pomoxxi/unity-parallel-editors/issues/new?template=bug_report.yml).

## Requirements

- Windows 10 or 11.
- Python 3.11 or later. The tool has no runtime dependencies.
- Git. Git LFS too, if your project keeps assets in it.
- Unity 6, the version your project uses (`ProjectSettings/ProjectVersion.txt`). UnityYAMLMerge ships with it (`Editor/Data/Tools/UnityYAMLMerge.exe`).
- The `unity` CLI, `1.0.0-beta.14` or later: `unity upgrade --channel beta`, then `unity --version` and `unity doctor`. Beta.14 stops a command from reaching another project's Editor, which one Editor per worktree depends on.
- An agent app that loads skills, such as Claude Code or Codex.

## Install

```
git clone https://github.com/pomoxxi/unity-parallel-editors
cd unity-parallel-editors
python -m venv .venv
.venv\Scripts\pip install -e ".[test]"
```

The tool runs from the clone. It reads its config (`upe.toml`) and the agents' skill (`skill/upe/SKILL.md`) from the clone's own folder, never from the current folder, so install it editable (`-e`). `UPE_CONFIG` or `--config` names another config. The console script is `.venv\Scripts\upe.exe`. The `test` extra adds `ruamel.yaml`, which one test of the loss check uses; without it that test is skipped.

## Set up

1. Clone your Unity project and check out the branch you work on.
2. If the project needs git-ignored files to compile, such as a paid asset kept out of git, put them in its main checkout. The tool copies them into each worktree.
3. Make the branch the tool lands on, from your branch: `git -C <project> branch --no-track upe/test-base <your branch>`. Without `--no-track`, a pull moves it and a push from it could reach your branch.
4. Run `upe setup` in a terminal (PowerShell or cmd). It asks for the project, the folders for the slots and the golden, the target branch, the Editor, the `unity` CLI and the files from step 2. It checks each answer and writes `upe.toml`. It reads only the paths you give and changes nothing else. `upe setup --check` reads them all again at any time. Or copy `upe.example.toml` to `upe.toml` and fill it in by hand.
5. `upe golden --create`. This is a full import, so it is slow.
6. Install the skill into each app the agents use: `upe skill --to ~/.claude/skills --to ~/.codex/skills`.
7. Keep no git credential where command-line git finds it (`cmdkey /list`), or every agent can push. Push from an app that keeps its own credential.

## Words

- **Slot:** a git worktree with its own `Library` and Editor, under `<root>/slots/s1`, `s2`, ... A finished task parks its slot, and the next task reuses it.
- **Golden:** a worktree with a fully imported `Library`, which nobody works in. A new slot copies its `Library` from it.
- **Claim:** the files and folders a task says it will write. Claims may not overlap, and a change outside the claim stops the merge.
- **Land:** merge a task's branch onto the target branch, check it, and, after the council says GO, move the target by fast-forward.
- **Loss check:** after UnityYAMLMerge, the tool compares the merged scene or prefab with both sides, object by object. If a change one side made is missing, the merge stops.
- **Council:** two fresh agents that did not do the work. Each reads the council brief and records GO or NO-GO with `upe verdict`. Two GOs land the merge, two NO-GOs refuse it, and a split goes to the user.

## Commands

Every command takes `--json`, and long ones take `--detach`. Agents learn these from the skill.

```
upe setup                    # once per machine, by you at a terminal
upe golden --create          # once; later `upe golden` brings it up to the target
upe skill --to ~/.claude/skills

# What an agent does:
upe --json start dash --brief @briefs/dash.md --claim Assets/Features/Dash/ --claim Assets/Scenes/Arena.unity
#   it works and commits in the slot the answer names, on branch upe/dash
upe --json editor test dash --filter MyFeatureTests   # or: editor run / editor open / editor close
upe activity dash rendering                           # before heavy work, and `editing` after
upe --json --detach land dash                         # merge and check; writes the council brief
upe --json job <job id> --wait

# Two other agents each read <root>/.upe/runs/dash/council-brief.md and vote:
upe --json verdict dash --member "<model>" --file verdict.json

upe decide dash land         # the user, at a terminal, only after a split council
upe withdraw dash            # give up a pending land; the task keeps its slot, branch and claim
upe finish dash              # park the slot for the next task
upe status [<task>]
upe judge --watch            # shares the CPU among the Editors
```

Run the `upe` console script, or `python -I -m upe`. Never run plain `python -m upe` from inside a worktree: a branch could ship its own `upe` folder. `<root>/.upe/runs/<task>/` holds the merge report, the check results, the council brief, the verdicts and the Editor logs.

## How it works

1. **Claim.** `start` records the task's claim, refused if it overlaps another task's.
2. **Isolate.** The task gets a slot on branch `upe/<task>`. Its `Library` is copied from the golden, never linked, or kept from the slot's last task.
3. **Work.** The agent commits in its slot and reaches the Editor only through `upe editor`.
4. **Merge.** `land` merges onto a candidate branch in a slot of its own. UnityYAMLMerge resolves scenes and prefabs, then the loss check compares the result with both sides.
5. **Check.** Static checks (YAML, GUIDs, `.meta` files, claims), a scene check in a batch Editor for missing references, then the EditMode tests. Tests that already fail on the target do not count.
6. **Council.** `land` writes a brief. Two agents vote with `upe verdict`. Two GOs move the target by fast-forward.

[`DESIGN.md`](DESIGN.md) has the details and the decisions; [`research/`](research/) has the findings and measurements.

## Disk cost

On the test project each slot was about 16 GB: 7.5 GB of checkout and 8.5 GB of `Library`. The golden is one more of those. Put `root` and `golden` on the same local drive with room, and out of cloud-synced folders. If you exclude slot folders from antivirus scanning for speed, exclude only their `Library` folders.

## Limits and trust

- Agents run as your OS user, with your rights. The tool is a guardrail, not a sandbox. A deliberate agent can get around it. Branch code runs in batch Editors before the council sees it.
- Windows only. Measured on one project.
- A land runs the full EditMode suite on the target and on the merge, so it takes as long as your suite. On the test project a land took about 40 to 70 minutes alone, and up to about 1 h 50 min with two at once ([research/05](research/05-measurements.md)).

See [`SECURITY.md`](SECURITY.md) and [`research/06-security.md`](research/06-security.md).

## Tests

The unit tests need Git, not Unity. A few run UnityYAMLMerge when `upe.toml` or `UPE_YAML_MERGE` names it, and are skipped otherwise. A full run takes a long time. From the clone:

```
.venv\Scripts\python -m unittest discover -s tests -p "test_merge.py"   # one module
.venv\Scripts\python -m unittest discover -s tests                       # all of them
```

## Feedback wanted

- A design review of the merge checks, above all the loss check (`src/upe/merge.py`, DESIGN.md "The flow").
- Unity users trying it on their own project: what broke, and how long a land took.

Use the [feedback form](https://github.com/pomoxxi/unity-parallel-editors/issues/new?template=feedback.yml), or start a thread in [Discussions](https://github.com/pomoxxi/unity-parallel-editors/discussions). Critical feedback is the most useful kind.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Reviews and field reports are worth more than pull requests right now.

## License

MIT. See [`LICENSE`](LICENSE).
