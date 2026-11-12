# 02 - Unity CLI, project lock, Library, licensing, external control

Target: Unity 6000.3.0f1, Windows 11. Researched 2026-10-25. Items marked [UNVERIFIED] came from forums, reasoning or general knowledge, not Unity docs.

## 1. Editor command-line arguments (6000.3 manual)
Source: https://docs.unity3d.com/6000.3/Documentation/Manual/EditorCommandLineArguments.html

| Flag | Notes / gotchas |
|---|---|
| `-batchmode` | No dialogs. Exit code 1 on script exception / failed op. Console output minimal; Editor.log has everything. |
| `-nographics` | No graphics device init. No GI baking. Logs off unless `-logFile` given. Rendering-dependent PlayMode tests won't work. |
| `-quit` | Quits after other commands finish. Exits BEFORE `-runTests` finishes (omit with tests); needs `-cacheServerWaitForUploadCompletion` with cache server; can hang on async code. `-quitTimeout <s>` (default 300). |
| `-projectPath <path>` | Absolute or relative; quote spaces. Also `-openfile`, `-createProject`, `-cloneFromTemplate`. |
| `-executeMethod Ns.Class.Method` | Static method in an `Editor` folder, runs once after project open/compile. Signal failure via exception or `EditorApplication.Exit(code)`. |
| `-logFile <path>` | `-` = stdout. Use a unique path per agent. `-timestamps` adds timestamp + thread id. |
| `-upmLogFile <path>` | Log of the UPM background process. `-noUpm` disables Package Manager. |
| `-accept-apiupdate` | Lets the API Updater run in batchmode; without it you may get compile errors. |
| `-disable-assembly-updater [asm ...]` | No names = ignore all. Can cause compile/runtime errors. |
| `-ignorecompilererrors` | Appears on the page with a non-standard hyphen character; verify before use. |
| `-buildTarget <name>` | win64, win, linux64, osxuniversal, android, ios, webgl, etc. Switching target on a cold Library triggers full reimport for that target. |
| `-activeBuildProfile`, `-build <path>` | Build-profile based builds (Unity 6). |
| `-refreshImportMode InProcess/OutOfProcessPerQueue/OutOfProcess`, `-desiredWorkerCount`, `-standbyWorkerCount` (2), `-idleWorkerShutdownDelay` (60000 ms) | Import worker tuning. Each editor spawns its own workers, so cap them with N editors. |
| `-job-worker-count N`, `-gc-helper-count N` | Limit CPU threads per instance. |
| `-rebuildLibrary` | Deletes Library, full reimport. Page says only when directed by Unity Support. |
| `-vcsMode "Visible Meta Files"` | Set version-control mode from CLI. |
| Accelerator: `-EnableCacheServer -cacheServerEndpoint 127.0.0.1:10080 -cacheServerNamespacePrefix <p> -cacheServerEnableDownload true -cacheServerEnableUpload true -cacheServerWaitForConnection <ms> -cacheServerWaitForUploadCompletion` | Default port 10080. Also `-cacheServerUploadExistingImports`, `-cacheServerDownloadBatchSize`. |
| Licensing: `-serial -username -password -returnlicense -createManualActivationFile -manualLicenseFile` | See section 4. |

The page does not label any flag as new in Unity 6. Recent-looking ones: `-activeBuildProfile`, `-build`, `-refreshImportMode` family, `-consistencyCheck`, `-quitTimeout`, `-timestamps`.

The page states only a single Unity instance can run per project (cannot batch-open a project the Editor already has open).

### Test Framework flags
Source: https://docs.unity3d.com/Packages/com.unity.test-framework@1.4/manual/reference-command-line.html
`-runTests`, `-testPlatform EditMode|PlayMode|<BuildTarget>` (default EditMode), `-testResults <path.xml>` (NUnit XML, default project root), `-testFilter "a;b"` (or regex, `!` excludes), `-testCategory`, `-assemblyNames`, `-testSettingsFile`, `-runSynchronously` (EditMode only, silently drops `[UnityTest]`), `-orderedTestListFile`, `-retry`, `-repeat`, `-playerHeartbeatTimeout`. Docs say there is no common exit-code definition; parse the XML. Use `-batchmode`, do not pass `-quit`.

### Unity Hub CLI
Sources: https://docs.unity.com/hub/use-hub-cli , https://docs.unity.com/hub/hub-cli-reference
Run `"C:\Program Files\Unity Hub\Unity Hub.exe" -- --headless <cmd>`. Commands: `editors -r`, `install --version 6000.3.0f1 [--changeset <hash>] [-m module ...] [--cm]`, `install-modules --version ... -m <ids>`. Marked experimental. Modules can only be added to Hub-installed editors. Exact Windows module IDs not verified.

## 2. Project lock
- Unity creates `<project>/Temp/UnityLockfile` while a project is open. A second instance (Hub, GUI or `-projectPath`) on the same folder fails: "It looks like another Unity instance is running with this project open" / "Multiple Unity instances cannot open the same project". Threads: https://discussions.unity.com/t/multiple-unity-instances-cannot-open-the-same-project/607546 , https://discussions.unity.com/t/error-it-looks-like-another-unity-instance-is-running-with-this-project-open/656599
- Stale lock after crash: kill all Unity processes (including lingering UnityShaderCompiler / JobProcess / AssetImportWorker), then delete `Temp/` (deleting only the lockfile sometimes insufficient per forum reports).
- Unity support article proposes symlinking Assets and ProjectSettings between copies, labelled "NOT SUPPORTED, USE AT YOUR OWN RISK": https://support.unity.com/hc/en-us/articles/115003118426 (login-gated; only seen via search snippet).
- Two instances on two DIFFERENT folders is the normal supported pattern. One forum report of the lock error across different projects on a build machine was attributed to a shared Temp location: https://discussions.unity.com/t/multiple-unity-instances-cannot-open-the-same-project-with-different-projects-on-build-machine/944045 . Separate worktrees give separate Temp, so fine as long as Temp is never linked.
- Precedent: Multiplayer Play Mode (Unity 6, 6000.3.0b4+) runs up to 4 editor players on one machine: https://docs.unity3d.com/Packages/com.unity.multiplayer.playmode@2.0/manual/index.html . How it clones projects is not stated there [UNVERIFIED].

## 3. Library folder / import cost
- Library is a regenerable local cache, not for version control. Unity reimports by comparing source metadata/content hashes.
- Cold import: no Unity-published figures. One third-party article says 5-15 minutes for a clean reimport depending on size [UNVERIFIED]: https://dev.to/attiliohimeki/reducing-assets-import-times-in-unity-2kn2 . Measure on the real project.
- Copying a warm Library into a new worktree: no official statement found. Expectation (Asset Database v2 keys artifacts by content hash and importer version, not by path) is that most artifacts are reused, but absolute paths in package cache entries may force partial reimports [UNVERIFIED]. Must be tested: check Import Activity window and Editor.log "Start importing" counts. Prior art: https://github.com/d12frosted/UnityLibrarySwitcher (archived, per-branch Library copies).
- Symlinks/junctions of Library: Unity issue tracker (Won't Fix) - symlinked Library on another drive gave Win32 error 1176 during UnityEngine.UI reimport; Unity said symlinks are not fully supported and cross-volume sharing is not supported: https://issuetracker-mig.prd.it.unity3d.com/issues/console-errors-appear-when-reimporting-unityengine-dot-ui-when-the-library-folder-is-symlinked . Editor warns symlinks can corrupt when an asset has multiple references: https://discussions.unity.com/t/using-symbolic-links-with-directory-monitoring/884475 . Regression: symlinked folders crashing refresh from 2021.2b14: https://discussions.unity.com/t/2021-2-beta14-and-higher-symlinks-produce-crash-when-loading-project/858756 . Sharing one Library between two live editors would corrupt it [reasoned]. Private Library per worktree is safe. Hard link anecdote on Windows only.
- Unity Accelerator / cache server: caches import artifacts only ("does not touch source assets"), default port 10080, Editor attaches via CLI flags above. Localhost use isn't documented but not prohibited. Docs (older versions seen): https://docs.unity3d.com/6000.3/Documentation/Manual/UnityAccelerator.html . Known bugs: double import on first import in Local mode; artifact downloaded yet still reimported: https://issuetracker.unity3d.com/issues/asset-artifact-downloaded-from-accelerator-but-the-asset-still-gets-reimported . Typically run via Docker or installer; Windows Home/Docker/WSL2 requirements unverified.
- The AssetDatabase overview page does not describe Library contents or artifact portability; subpages not retrieved.

## 4. Licensing
- ActivationFAQ: https://docs.unity3d.com/6000.3/Documentation/Manual/ActivationFAQ.html . Many editor versions may be installed but only ONE serial-based license can be activated per computer at a time. Paid commercial licenses: one person, two computers. Named-user licenses activate by Hub sign-in.
- Nothing there forbids multiple concurrent editor processes on one machine; license is per machine, not per process. No per-machine instance or seat cap found. EULA text not read.
- Batchmode: `-batchmode -quit -serial XXX -username -password` is documented for Plus/Pro serials. Personal has no serial: activate via Hub sign-in or manual .ulf. Batchmode editors on a machine with a valid activation normally reuse it (license file in `C:\ProgramData\Unity`) [UNVERIFIED]. Login rate limit: 10 per minute per user (only matters if each agent logs in).
- Not verified: whether Unity 6 batchmode on Personal needs the Hub running or a live session token.

## 5. Driving an already-open editor from outside
- Built-in: no CLI to send commands to a running editor; `-executeMethod` works only at launch and the lock blocks a second launch.
- Official Unity MCP bridge (package `com.unity.ai.assistant` 2.0, Unity 6000.0+): editor-side bridge opens local IPC (named pipes on Windows); relay binary in `~/.unity/relay/` is started by the MCP client with `--mcp`. External direct clients need approval via a dialog in Project Settings (awkward for headless). Docs: https://docs.unity3d.com/Packages/com.unity.ai.assistant@2.0/manual/unity-mcp-overview.html , https://docs.unity3d.com/Packages/com.unity.ai.assistant@2.0/manual/unity-mcp-get-started.html . Entitlement/beta terms not verified.
- Third-party bridges: CoplayDev unity-mcp, CoderGamester mcp-unity, AnkleBreaker (HTTP localhost:7890), AgentBridge (disk queue replayed after reload), Wasim bridge (socket in out-of-process helper). Shared caveat: in-editor TCP/WebSocket servers die on every domain reload and must restart; synchronous handlers block the main thread. https://github.com/AnkleBreaker-Studio/unity-mcp-plugin , https://glama.ai/mcp/servers/simonwittber/AgentBridge
- DIY: an `[InitializeOnLoad]` script running a `HttpListener` or named pipe on a per-instance port, queueing work onto `EditorApplication.update`, re-binding after `AssemblyReloadEvents`; or a file inbox folder polled by the editor (survives domain reload best). `AssetDatabase.Refresh()` from outside happens only via editor focus (auto-refresh) or your own listener; in batchmode call it explicitly [general knowledge].
- Editor Coroutines package lets you yield across editor frames but does not survive domain reload [general knowledge].

## 6. Resource cost of N editors
- No official numbers and no credible report found of 3-6 parallel Unity 6 editors. Forum: many batchmode player/server instances in parallel (players, not editors): https://discussions.unity.com/t/batchmode-multiple-server-instances/422156
- Guess [UNVERIFIED]: empty Unity 6 editor 1.5-3 GB RAM, medium project 3-8 GB, plus import workers, shader/Burst compilers. Disk: one full Library per worktree (often several GB). Cold import is the CPU-heavy phase; avoid N simultaneous cold imports.
- Measure with Task Manager or `Get-Process Unity*`.

## 7. Serialization and merging
- Project Settings > Editor > Asset Serialization Mode: default Force Text (https://docs.unity3d.com/6000.3/Documentation/Manual/class-EditorManager.html). Binary/Mixed makes scenes/prefabs unmergeable. "Visible Meta Files" (version control mode; CLI `-vcsMode "Visible Meta Files"`) keeps `.meta` files with GUIDs; commit them with assets. The fetched page did not cover VC mode. "Reduce version control noise" option reduces inline-mapping churn.
- UnityYAMLMerge: https://docs.unity3d.com/6000.3/Documentation/Manual/SmartMerge.html
  - Windows path: `C:\Program Files\Unity\Hub\Editor\6000.3.0f1\Editor\Data\Tools\UnityYAMLMerge.exe`
  - CLI: `UnityYAMLMerge merge [options] <base> <theirs> <mine> [<output>]`. `-p` premerge then fallback tool for leftovers, `-h` headless, `--force` for files other than .unity/.prefab/.meta (.asset/.mat/.controller NOT merged by default), `--fallback none`, `--rules <file>`, `--nomappinginoneline`. Fallback tools in `mergespecfile.txt` next to the exe.
  - Documented only as a mergetool:
    ```
    [merge]
      tool = unityyamlmerge
    [mergetool "unityyamlmerge"]
      trustExitCode = false
      cmd = '<path>' merge -p "$BASE" "$REMOTE" "$LOCAL" "$MERGED"
    ```
  - A real git merge driver (`.gitattributes: *.unity merge=unityyamlmerge`; `[merge "unityyamlmerge"] driver = "<path>" merge -h -p --force %O %B %A %A`) is a community recipe, not in Unity docs [UNVERIFIED]. Test on a conflicting prefab; non-zero exit = conflict.

## 8. What this means for the tool
1. One git worktree per agent, each with private `Library/`, `Temp/`, `Logs/`. Never link or share Library/Temp.
2. Seed Library: open the main project once to completion (same Unity version and build target) as a golden copy, then `robocopy golden\Library wt\Library /MIR /MT:16` before first launch of each worktree. Copy, do not link. Check Editor.log on first open for reimport counts; if it reimports everything, fall back to Accelerator. Refresh golden when packages/settings change substantially.
3. Optional phase 2: local Accelerator, `-EnableCacheServer -cacheServerEndpoint 127.0.0.1:10080 -cacheServerNamespacePrefix <proj>`.
4. Headless launch recipe:
   ```
   "C:\Program Files\Unity\Hub\Editor\6000.3.0f1\Editor\Unity.exe" ^
     -batchmode -nographics -projectPath "<worktree>" ^
     -logFile "<worktree>\Logs\agent.log" -accept-apiupdate ^
     -job-worker-count 4 -executeMethod Tool.AgentEntry.Run -quit
   ```
   Tests: replace `-executeMethod/-quit` with `-runTests -testPlatform EditMode -testResults <wt>\Logs\results.xml` (no `-quit`). Rendering PlayMode tests: drop `-nographics`. Judge success by exit code AND log scan for `error CS`.
5. Long-lived GUI editor per worktree: no `-batchmode`, unique `-logFile`, plus an in-editor inbox/pipe script (or a third-party MCP bridge on a per-instance port) for Refresh/compile/test requests. This amortises domain-reload and compile startup.
6. Pre-flight: if `<wt>\Temp\UnityLockfile` exists, check for a live Unity.exe whose command line contains the worktree path (`Get-CimInstance Win32_Process`); if none, delete `Temp\` and proceed.
7. Concurrency: stagger cold starts, budget 4-8 GB RAM per editor, cap worker counts, start with N=3.
8. Merge: Force Text + Visible Meta Files, `.gitattributes` merge driver for `.unity .prefab`, commit `.meta`, ignore Library/Temp/Logs/UserSettings. Run headless compile + EditMode tests after each merge before accepting.
9. Licensing: one machine activation covers all processes; run a smoke test with 3 concurrent batchmode editors on the real license.

## 9. Could not verify
- Whether a copied Library avoids full reimport in 6000.3 (and effect of a different absolute path).
- Real import times, RAM/CPU per instance, any 3-6 parallel editor report.
- Accelerator localhost specifics on Windows and actual speedup.
- Personal-license batchmode behaviour and EULA clauses on concurrent instances.
- Content of the login-gated Unity support article on symlinking Assets/ProjectSettings.
- Hub CLI Windows module IDs; Visible Meta Files doc page; Unity 6-specific flag deltas.
- UnityYAMLMerge as a git merge driver (only mergetool documented).
- How Multiplayer Play Mode clones virtual projects.
