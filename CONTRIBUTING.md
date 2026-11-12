# Contributing

Thank you for looking. One person maintains this project in spare time.

## What helps most

- **Reviews, design critique and field reports.** These are worth more than pull requests right now. Use the [feedback form](https://github.com/pomoxxi/unity-parallel-editors/issues/new?template=feedback.yml) or [Discussions](https://github.com/pomoxxi/unity-parallel-editors/discussions).
- **Bug reports** with the command, what you expected and what happened. Use the bug form.
- **Bug fixes** are welcome as pull requests.
- **Features:** start a thread in Discussions → Ideas first, so we can agree on the approach before you write code.

## Set up

Windows, Python 3.11 or later, and Git.

```
git clone https://github.com/pomoxxi/unity-parallel-editors
cd unity-parallel-editors
python -m venv .venv
.venv\Scripts\pip install -e ".[test]"
```

The unit tests need no Unity and no `upe.toml`.

## Run the tests

A full run takes 40 minutes or more, so run the module you changed first:

```
.venv\Scripts\python -m unittest discover -s tests -p "test_merge.py"   # one module
.venv\Scripts\python -m unittest discover -s tests                       # the full suite
```

CI runs a fast set of modules on Windows for every pull request. The full suite runs on `main`, or on a pull request with the `full-tests` label. CI has no Unity Editor, so the tests that need UnityYAMLMerge are skipped there and the C# in `unity/com.upe.agent` is not compiled.

## Pull requests

<a id="one-problem"></a>
- **One problem per pull request.** Clean-ups and refactors go in their own pull request.

<a id="evidence"></a>
- **Say how you checked it, and what you could not check.** "Tests pass" alone is not evidence. Name the modules you ran, and for Unity-side changes the Unity version and what you did in the Editor.

<a id="titles"></a>
- **Titles use [Conventional Commits](https://www.conventionalcommits.org/) with a scope**: `fix(merge): loss check no longer flags moved prefabs`. Scopes: `cli`, `merge`, `council`, `workspace`, `unity`, `docs`, `ci`, `tests`. Pull requests are squash-merged and the title becomes the commit on `main`.

<a id="agents"></a>
- **If an agent wrote part of it, name the model and the tool.** Agent-written pull requests are welcome when a person has read and checked them.

## Style

- Docs: plain, short sentences.
- Comments say why, not what. Leave out where an idea came from.
- No private paths, machine details or project names in code, docs, tests or commit messages. Use neutral examples such as `C:/work/...` or `<root>/...`.

## Security

Report a vulnerability privately, not in an issue. See [`SECURITY.md`](SECURITY.md).
