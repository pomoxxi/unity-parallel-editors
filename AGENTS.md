# Notes for coding agents

Read `CONTRIBUTING.md` first; its rules apply to you too. In short:

- One problem per change. Keep the scope to what was asked.
- Run the test module for what you changed: `.venv\Scripts\python -m unittest discover -s tests -p "test_<module>.py"`. The full suite takes 40 minutes or more.
- Comments say why, not what, and never where an idea came from (no ticket, review or session references).
- Never write private paths, machine details, people's names or emails, or names from a user's own Unity project into code, docs, tests or commit messages. Use `C:/work/...` or `<root>/...`.
- Pull request titles: Conventional Commits with a scope (`fix(merge): ...`). Fill in the pull request template, including what you could not check, and name the model and tool you are.
- Windows only: the tool uses robocopy, Windows process APIs and `%TEMP%`.

Where things are: `src/upe/` (the CLI; `flow.py` runs a task from start to land, `merge.py` and `checks.py` the merge and its checks), `tests/`, `skill/upe/SKILL.md` (what an agent using the tool reads), `unity/com.upe.agent/` (the Editor-side helper), `DESIGN.md` (decisions and the flow).
