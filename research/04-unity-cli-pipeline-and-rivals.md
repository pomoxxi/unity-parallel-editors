# 04 - Unity CLI + Pipeline package, and rival projects

Date: 2026-10-25. Method: web search and repo landing pages (README level). Source code was NOT read, so "what it does" is as documented, not code-verified.

## 1. Unity CLI and `com.unity.pipeline`

**What it is.** Two pieces:
- **Unity CLI** (`unity` binary): official standalone native binary, replaces/extends Hub CLI. Manages editors/modules/projects/auth/licensing, builds, tests, and talks to editors.
- **Unity Pipeline package** (`com.unity.pipeline`): experimental Editor package that starts a loopback-only HTTP server inside the Editor (example port 7800). The CLI sends commands to it. A runtime component can also target dev Player builds (`--runtime`).

**Status/versions.** Both experimental/beta. CLI latest in release notes: 1.0.0-beta.14 (2026-10-21). Pipeline needs Unity 6.0 LTS+ (so 6000.3.0f1 is fine). 0.9.0 of the package announced breaking command renames, so pin versions. Free; Unity says CLI and MCP are free with no MCP concurrency limits. Unity calls CLI the "recommended path forward" over Unity MCP (which stays supported); CLI has an `unity mcp` mode (with `--tools/--exclude-tools`). Installed via PowerShell script on the beta channel (`UNITY_CLI_CHANNEL=beta`); winget "coming soon".

**Commands relevant to us.**
- `unity install <ver> [-d dir]`, `unity editors`, `unity doctor`, `unity upgrade`.
- `unity open [path] [--batchmode] [--until-ready] [--accept-upgrades]`: opens project; `--until-ready` blocks until ready and fails fast with `OPEN_BLOCKED_BY_DIALOG`; reopening an open project returns `alreadyOpen:true` exit 0. `unity status` reports ready / dialog-blocked / Safe Mode (exit 6).
- `unity test` (`--filter`, `--shard`, `--rerun-failed`, `--affected`, JUnit output, `--allow-install`), `unity watch test`.
- `unity build` (profiles, `build run`).
- `unity pipeline install|list`.
- `unity command` (list editor commands), `unity command <name> args`, `eval "<C#>"` / `eval_file` (Roslyn, main thread, no domain reload, gated by a security token), built-ins like `run_tests` (10 min default timeout). Custom commands via `[CliCommand]`/`[CliArg]`.
- `unity job`, `unity list`, `unity mcp`.
- JSON/TSV output, stable exit codes (e.g. 11 not a project, 12 pin matched nothing), service-account auth for CI.

**Talks to an already-running editor?** Yes, that is the Pipeline's purpose. Targeting: auto-discovery from cwd's project; `--project-path`; since beta.14 `--port/--pid/--instance` pins, and running inside a project with no editor open no longer reaches another project's editor. Good for us: one editor per worktree is addressable by project path.

**Gaps/risks.** Requires `unity auth login` (Unity account/licence) in the install flow; docs do not say if it is needed per call or for headless CI. Docs do not explain how the editor picks a port when several run (UAX hashes the path to pick ports, see below). Unity staff gave no official guidance on worktrees/parallel editors. Windows issue reported: Hub log-dir `EEXIST` when launched from some hosts; calling from PowerShell worked. Package is installed into the project manifest, so `Packages/manifest.json` changes need to be committed or injected per worktree.

Docs: https://docs.unity.com/en-us/unity-cli/unity-pipeline/unity-pipeline-package ; https://docs.unity.com/en-us/unity-cli/release-notes ; https://unity.com/blog/meet-the-unity-cli ; https://discussions.unity.com/t/announcing-the-unity-cli-a-new-way-to-connect-your-tools-and-agents/1731104 ; package ref https://docs.unity3d.com/Packages/com.unity.pipeline@latest

## 2. Repos

| Project | What it does | Multi-agent/editor | Scene/prefab merge | Maturity / license |
|---|---|---|---|---|
| TomMoore515/UnityCLI.AgenticExtensions (UAX) https://github.com/TomMoore515/UnityCLI.AgenticExtensions | Package extending Pipeline with commands: profiling, UI Toolkit inspect/interact, simulated input, scene camera, macros (`IUaxMacro`). Auto-installs a SKILL.md into `.claude/skills`, `.codex/skills`, AGENTS.md. Editor dashboard. | UAC-MCP server in each editor (`127.0.0.1:<port>/mcp`): `whos_here`, `post_status`, `read_board`, `hold`/`release`. Advisory claims, 300 s lease, queue with `waitSeconds`, persists across domain reload, idle reap 15 min. Port hashed from project path (47100-47899) pinned in `ProjectSettings/UacSettings.json` so several editors coexist. Coordinates agents sharing one editor/project, not separate copies. | None | 4 stars, 9 commits, MIT. Needs Pipeline 0.3.x. |
| ManageXR/claude-unity-bridge https://github.com/ManageXR/claude-unity-bridge | Python CLI `unity-bridge` writes `.unity-bridge/command.json`; editor polls, runs tests/compile/refresh/console logs/play control, writes `response-{id}.json`. | Claims multi-agent friendly; per-project `.unity-bridge/` dir so one editor per worktree works. Single command slot per project. | None | 21 stars, 108 commits, CI+PyPI, Apache-2.0. Predates and is superseded in spirit by Unity CLI. |
| Final-Factory/ff-factory https://github.com/Final-Factory/ff-factory | Self-hosted web portal: orchestrator + dispatcher + workers (Claude Agent SDK) running many agents on a Unity 6 DOTS game. Remote Win/Mac/Linux machines via daemon. Standing scheduled agents. | Each task gets a git-worktree sandbox on its own branch with a warm `Library/` copy (optional ReFS Dev Drive block-clone on Windows). Own editor per sandbox, launched natively `Unity.exe -projectPath`, instance name = folder name for Unity MCP. Work ledger prevents duplicates; guard hook blocks destructive git. Watchdog dismisses harmless dialogs. 8-12 GB RAM per editor. | Not described in the page I read | 2,300 commits, 2 stars; README: "used daily on one project, changes often, no compatibility promises". MIT. Windows-first. Most complete real-world precedent. Uses Unity MCP, not the Unity CLI. |
| weslo/upool https://github.com/weslo/upool | Pool of pre-warmed Unity workspaces under `.claude/worktrees/`; Claude Code session gets a ready slot in seconds; pool refills in background; caps running editors by memory; slot parked (never reset) if it has uncommitted/unbranched work; `upool refresh/prune`. | Each slot is a full copy including Library; `unity` commands from a session reach only that slot's editor. Waits 20 s then fails if pool empty. | None | Pre-alpha v0.3.0, unsigned binary, macOS/APFS only for now, needs Unity CLI >= 1.0.0-beta.13, MIT. Closest to our design, and built on the Unity CLI. |
| Satura Space (saturadev) https://saturaspace.com/about , https://github.com/saturadev | Closed desktop app, early access: run coding agents on a Unity project in parallel, stream the game live, "Tuner" for hand-tuning feel. Public: `saturaspace-sdk` (Unity package the app installs), a bug tracker, a demo repo. | Isolation mechanism not stated publicly. | Not stated | Proprietary app; no license shown on public repos; pricing/platform not published. Nothing to borrow beyond ideas. |
| Diversion https://www.diversion.dev/blog/your-second-coding-agent-needs-its-own-workspace-heres-the-fast-way | Cloud VCS (alternative to Git/Perforce). Blog: one branch + one workspace per agent; Claude Code and Codex ran in parallel on a Unity project; plugin links commits to Claude conversations. | Full working copy per workspace (disk + initial sync); Library/editors left to you. | Only says text files from both branches three-way merged; no Unity YAML/GUID handling shown. Same-file scene conflicts not tested. | Commercial SaaS. Proves branch-per-agent model, solves binary-asset storage, not Unity semantic merge. |

## 3. Other 2026 projects found
- zaffre001/unity-claude-template (https://github.com/zaffre001/unity-claude-template): MIT, ARCHIVED 2026-08-19 (successor "Unity Agent Kit"). Worktree scripts + MCP bridge; README inconsistent on Library symlink vs per-worktree copies of ArtifactDB/SourceAssetDB/ScriptAssemblies/Bee/lock files. Useful list of what must not be shared between editors.
- Zenn article (https://zenn.dev/ohbashunsuke/articles/20260310_unity-parallel-dev-with-claude-code?locale=en): practical worktree guidance; asmdefs to decouple, scenes/prefabs single-owner.
- GaussianGuaicai/com.unity.pipeline (https://github.com/GaussianGuaicai/com.unity.pipeline): mirror/fork of the package; not evaluated.
- Generic worktree managers (Worktrunk, Orca) exist but are not Unity-aware.
- Unity Agent Kit: successor named by zaffre001; not investigated.

## 4. Build on / borrow

- **Build on: Unity CLI** as the per-worktree editor driver: `unity open --until-ready` (with `--project-path` pinning), `unity status`, `unity test`, `unity command eval/run_tests`, `unity build`. Replaces custom file-polling bridges and Unity MCP for our purposes. Pin CLI beta version; expect churn.
- **Borrow (MIT/Apache): ff-factory** patterns: sandbox lifecycle, warm Library copy (ReFS block clone on Windows), dialog watchdog, destructive-git guard hook, per-editor RAM cap. **upool**: slot pool/park-never-reset policy, editor cap, 20 s wait semantics (but it is macOS-only; Windows port of the idea is ours). **UAX**: advisory hold/lease board design for shared resources (scene/prefab ownership could reuse the model), port-hash-from-path trick, skill auto-installer. **claude-unity-bridge** only as a fallback for pre-Unity-6 projects.
- **Reference only:** Diversion (commercial), Satura (closed), zaffre template (archived but MIT, useful lock-file list).

## 5. What none of them do
- None performs an actual scene/prefab/ScriptableObject **semantic merge** (YAML + fileID + GUID aware) or integrates UnityYAMLMerge/uymerge into an automated merge step. They avoid conflicts by single-owner rules or hope for text merges.
- None runs a **post-merge integration gate** (merge all branches, reimport in a clean editor, run tests, detect missing references / duplicate GUIDs / broken `.meta`).
- None gives a Windows-first, Unity-CLI-based pipeline end to end (upool is macOS; ff-factory uses Unity MCP and is bespoke).
- None detects **cross-feature asset collisions up front** (two agents creating same-path or same-GUID assets, editing the same scene) and schedules/serialises around them automatically; UAX locks are advisory and per-editor.
- None combines worktree isolation + Library warm-copy + CLI-driven verification + conflict-free merge as a reusable open-source library with tests.
