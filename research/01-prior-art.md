# 01 - Prior art: multiple agents / editors on one Unity project

Researched 2026-10-25. Method: web search plus page fetches. Nearly all facts below came from fetched pages; items I could not fetch or confirm are flagged "UNVERIFIED". Search-snippet-only claims are labelled as such.

## Related projects

A separate project, `unity-parallel-agents` by Mirven, works on the same problem: https://github.com/siiplasplas/unity-parallel-agents (MIT, 1 commit, 1 star; Mirven also sells a paid desktop app for the same workflow). It is unrelated to this repo, which is named Unity Parallel Editors. It is described in section 2 below.

## 1. Real-time multi-user Unity editing

**Scene Fusion 1 / 2 (KinematicSoup)**
- Live multi-user scene editing inside the Editor, intended to be used alongside source control (search-result summary of Asset Store listing, https://assetstore.unity.com/packages/slug/285247 ; forum thread https://discussions.unity.com/t/released-scene-fusion-real-time-multi-user-scene-editing/654155?page=2).
- Asset Store "Scene Fusion 2 Lite" (free): v2.0.5, released 2026-08-11, compatibility table lists only Unity 2022.3.51f1 (fetched: https://assetstore.unity.com/packages/slug/285247). So it is maintained, but I found no Unity 6 support statement. UNVERIFIED for 6000.3.
- Search snippets (not verified on the page): free tier is capped at 2 users; Scene Fusion 2 Indie costs $190 per seat; the session host must be on Windows (https://assetstore.unity.com/packages/slug/285234 , via search result).
- Gap: every participant needs a live Editor on the same scene. It does not help an AI agent that edits files or drives its own headless/automated session. It is a live-sync model, not an offline-branch-and-merge model.

**Unity Reflect**
- Deprecated, no longer available from December 2024, replaced by Unity Cloud Reference Project (search-result summary of https://docs.unity.com/reflect/en/manual/Review/Multi-user%20collaboration and https://blog.unity.com/es/aec/unity-reflect-evolves-into-a-suite-of-purpose-built-applications-for-aec). Its "multi-user" was review/presence, not co-authoring. I did not fetch the deprecation page itself.

**"Unity Multi-user" / Collaborate**
- I found no current Unity-first-party real-time co-editing product. Old Collaborate scene merging complaints exist (https://forum.unity3d.com/threads/scene-merging.453901/). Absence of evidence only.

**Unity Version Control (Plastic SCM) / semantic merge / DevOps**
- Plastic is now Unity Version Control, part of Unity DevOps. Its client delegates scene/prefab merge to a configured tool; Unity's docs say you must add UnityYAMLMerge ("Unity Smart Merge", client 11.0.16.10303+) as a custom tool for `.unity;.prefab` and place it above the `$text` rule (fetched: https://docs.unity.com/en-us/engine/6000.3/manual/get-started/project-configuration/version-control/smart-merge.md). So Plastic's own semantic merge is not YAML-scene-aware by default; it uses the same UnityYAMLMerge.
- Commercial, not open-source.

**NVIDIA Omniverse Unity connector**
- Unidirectional Live Sync from Unity Editor to Omniverse; connector docs say it is not compatible with Unity 6 and point to Unity's native USD packages (https://docs.omniverse.nvidia.com/connect/latest/unity/requirements.html , https://docs.omniverse.nvidia.com/connect/latest/unity/manual/live_sync.html). NVIDIA is stopping connector development as of 2025-10-01 (the Esri notice describes this as a general connector-program decision: https://support.esri.com/en-us/knowledge-base/deprecation-arcgis-cityengine-connector-for-nvidia-omni-000036969). Effectively dead for this use, and one-way anyway.

**Open-source real-time attempts**: none found. UNVERIFIED (search only).

## 2. Several editors on clones of one project

**ParrelSync** (https://github.com/VeriorPies/ParrelSync): MIT, 6.1k stars, not archived, repo `pushed_at` 2026-06-16 (GitHub API). Tested on 2020.3 / 2021.3 / 2022.3 only; README does not mention Unity 6; 45 open issues. Clone = copy of project folder with `Assets`, `Packages`, `ProjectSettings` as symlinks to the original; `Library`, `Temp`, `obj` per clone. Coherence docs describe it as working "prior to Unity 6" (https://docs.coherence.io/get-started/build-and-run/local-testing-via-parrelsync). Gap: all clones share the SAME Assets, so it is shared-working-tree editing, not isolation. Two editors writing the same asset "fight for it at serialization time" (forum, 2013: https://discussions.unity.com/t/running-a-project-in-multiple-editors/503828). Each clone pays a full Library import.

**Multiplayer Play Mode** (https://docs.unity3d.com/Packages/com.unity.multiplayer.playmode@2.0/manual/index.html, via search): v2.0 requires 6000.3.0b4+; up to 3 virtual players beyond the main editor; virtual players use the same assets on disk and cannot author (create/modify GameObjects). Gap: it is a playtesting aid, not an authoring one.

**Symlinks warning**: Unity's own forum thread reports symlinks can corrupt a project if the same asset is referenced twice, or links are recursive (search snippet from https://discussions.unity.com/t/using-links-sym-hard-to-create-a-common-folder-for-all-unity-projects/455577?page=2).

**Git worktrees + Unity**:
- Diversion blog (vendor marketing, one informal test, screenshots dated 2026-08-11): Claude Code and Codex in separate Diversion workspaces on the Unity 3D Game Kit, both merged to main without conflicts; mentions disk cost of a full sync per workspace and Unity-side package cache/licensing hiccups (https://www.diversion.dev/blog/your-second-coding-agent-needs-its-own-workspace-heres-the-fast-way).
- unity-parallel-agents (Mirven, MIT): worktree + branch per agent; optional `COPY_LIBRARY` to cut reimport; headless EditMode/PlayMode tests and player build; Smart Merge via `.gitattributes`; all scene/prefab/shared-asset jobs are serialized into ONE lane in the main checkout; merges only after human review (https://github.com/siiplasplas/unity-parallel-agents). This is the closest prior art, and its answer to our problem is "don't parallelize scenes."
- Library sharing: no authoritative source found for sharing a Library across clones. Only the Mirven repo (copy, not share) and the forum advice to keep Library per instance (https://discussions.unity.com/t/running-a-project-in-multiple-editors/503828). Unity Accelerator behaviour with worktrees: UNVERIFIED.

## 3. AI-agent + Unity tools

| Tool | Facts |
|---|---|
| Unity official MCP / AI Assistant (https://docs.unity3d.com/Packages/com.unity.ai.assistant@2.0/manual/unity-mcp-overview.html) | "Multiple MCP clients can connect to the same Unity instance simultaneously." Needs a running Editor; named-pipe relay. Open beta, needs a Unity AI subscription (https://unity.com/blog/unity-ai-mcp-how-to-get-started). Forum posts (search snippets, secondhand) mention per-plan caps of 3-5 concurrent MCP connections and a Personal-tier $10/month price; a thread title mentions an MCP "Capacity Limit" (https://discussions.unity.com/t/call-to-make-unity-mcp-usage-free/1724543). Docs now list a plugin for Claude Code, Codex and Grok (https://docs.unity.com/en-us/ai). Unity Muse "deprecated": UNVERIFIED. |
| CoplayDev/unity-mcp (https://github.com/CoplayDev/unity-mcp) | MIT, 15.4k stars, v10.3.0 dated 2026-10-04. Multi-instance routing (`Name@hash`, per-call `unity_instance`). justinpbarnett/unity-mcp resolves to this same repo (fetch showed CoplayDev, not archived). |
| Multi-instance guide (https://coplaydev.github.io/unity-mcp/guides/multi-instance) | Primary evidence on concurrency: one Editor runs one command at a time, concurrent calls queue; reads went from ~5s to ~17s under four-agent write load; no idempotency key, so retries after the 30s timeout can double-apply. This is the "agents queue behind one editor" problem, measured. |
| IvanMurzak/Unity-MCP (https://github.com/IvanMurzak/Unity-MCP) | Apache-2.0. Server runs stdio or streamableHttp (standalone/Docker). README says nothing on concurrent agents. |
| AnkleBreaker-Studio/unity-mcp-server (https://github.com/AnkleBreaker-Studio/unity-mcp-server) | 526 stars, custom "AnkleBreaker Open License v1.0" (restricts resale). Per-agent identity (`_meta.agentId`), per-editor queue, writes serialized within an editor, undo history. States plainly that "simultaneous agents can still make conflicting edits to the same object, so assign clear ownership for shared scene work." |
| UnityMCP VRC, realvirtual MCP | Also multi-instance (search snippets only, not fetched). |

Takeaway: all MCP tools multiplex agents onto one or more live Editors. None gives an agent its own isolated project state with a merge afterwards.

## 4. Scene / prefab merging

**UnityYAMLMerge** (shipped with Editor; docs: https://docs.unity.com/en-us/engine/6000.3/manual/get-started/project-configuration/version-control/smart-merge.md). Merges `.unity`, `.prefab`, `.meta`; `.asset/.mat/.controller` only with `--force`; unresolved conflicts go to a fallback tool; rules are configurable (arrays as `set`/`plain`, exclusions, float epsilon). Free, bundled. Reported failure modes (Unity issue tracker titles, from search results; I could not open the detail pages, so IDs, status and dates are UNVERIFIED beyond the titles):
- Wrong child hierarchy merging a prefab (Perforce repro; marked fixed 2018.1.x): https://issuetracker.unity3d.com/issues/yamlmerge-incorrect-merge-result-when-merging-prefab
- Not detecting conflicts when two users modify the same prefab instance (reported as UUM-6563 and still active in the search summary): https://issuetracker.unity3d.com/issues/unityyamlmerge-tool-is-not-detecting-conflicts-in-prefab-instances
- Keeps both `m_StringField` entries on prefab-variant overrides instead of conflicting (reproducible 2022.3 / Unity 6 per summary): https://issuetracker.unity.com/issues/11551/unityyamlmerge-exe-doesnt-correctly-handle-merge-conflicts-in-modified-properties-on-a-prefab-variant
- "Vector too long" popup on a 20k-line prefab: https://discussions.unity.com/t/unityyamlmerge-error-vector-when-trying-to-solve-merge-conflict-of-a-specific-prefab/826031
- Crash via git mergetool (2015 era): https://issuetracker.unity3d.com/issues/unityyamlmerge-unityyamlmerge-crashes-git-mergetool
- Same-GameObject edits need a fallback tool (community note: https://github.com/GamesResearchTUG/Maroon/wiki/Merge-conflicts).
Also, the official Git recipe uses `git mergetool` (manual, interactive), not an automatic merge driver.

**Third party:**
- uymerge (https://github.com/lgarczyn/uymerge): MIT, Rust, 4 stars, 10 commits, 1 open issue. Structural 3-way driver keyed on document anchor, string-table `m_Id`, SerializeReference `rid`; byte-exact reserialization; self-check (dropped/duplicate ids, dangling rids); a failed self-check becomes a conflict, not a silent merge. Claims UnityYAMLMerge "can drop, duplicate, or misalign records and still exit 0" (author assertion, no benchmarks). Non-keyed regions fall back to diff3. Closest thing to our merge layer; very young.
- MergeSight (https://discussions.unity.com/t/mergesight-visual-unity-asset-review-and-merge-tool-for-git-and-other-vcs-workflows/1722629): commercial Asset Store tool (released Aug 2026), CLI with merge-driver workflows, 3-way UI, "fails closed" on ambiguous structures, covers `.unity .prefab .asset .mat`. License/price not stated.
- PrefabLens #323 (https://github.com/hashiiiii/PrefabLens/issues/323): a proposal for fileID-keyed interactive 3-way merge, not built (search summary).
- UniMerge (old, wiki.unity3d.com/index.php/UniMerge), anacat/unity-mergetool (guide only, 79 stars, https://github.com/anacat/unity-mergetool), GUID Warden (pre-commit hook, https://csaf.itch.io/guid-warden).
- No tool found that remaps fileIDs/GUIDs when two branches independently CREATE objects (collision handling) or that merges semantically at the Unity-object level rather than YAML-document level. UNVERIFIED (search coverage limited).

## 5. "Multiple AI agents on one Unity project", 2024-2026

Found: Mirven's repo and app (above); Diversion's post (above); KatoSync issue #116, opened 2026-10-23, which designs separate Codex and Claude worktree lanes plus ONE exclusive Editor-writer lease and blocks autonomous writers until the lease is enforced (https://github.com/NMKato/KatoSync/issues/116); Claude Code agent view puts background sessions in `.claude/worktrees/` (https://code.claude.com/docs/en/agent-view). Generic worktree guides exist (e.g. https://betterstack.com/community/guides/ai/git-worktrees-claude/) but are not Unity-aware. I did not find relevant Reddit or HN threads (search tool did not index them). UNVERIFIED either way.

## Summary table

| Item | What it does | Maintained | License / cost | Gap vs our goal |
|---|---|---|---|---|
| Scene Fusion 2 | Live co-edit of scenes | Yes (v2.0.5, 2026-08) | Free Lite (2 users per snippet); Indie ~$190/seat (snippet) | Needs live editors; 2022.3 only listed; no offline agent merge |
| Unity Reflect | AEC review | Deprecated Dec 2024 | n/a | Not authoring |
| UVCS / Plastic | VCS, semantic merge | Yes | Commercial | Scene merge = UnityYAMLMerge |
| Omniverse Unity connector | One-way sync | Dead for Unity 6 | n/a | Not applicable |
| ParrelSync | Symlinked clones | Active repo, no Unity 6 claim | MIT | Shared Assets, no isolation, no merge |
| Multiplayer Play Mode | Virtual players | Yes (2.0, 6000.3+) | Free | No authoring |
| Git worktrees (+ Mirven repo) | Isolated checkouts | Repo 1 commit | MIT | Scenes serialized; manual merge; reimport per tree |
| Unity AI / MCP | Official MCP in Editor | Beta | Subscription | Many clients, one editor |
| CoplayDev unity-mcp | MCP, multi-instance | Very active | MIT | Queues in one editor; no merge |
| IvanMurzak Unity-MCP | MCP | Active (unverified date) | Apache-2.0 | Same |
| AnkleBreaker | MCP, per-agent queues | Active-ish | Custom open license | Says conflicts remain your problem |
| UnityYAMLMerge | Smart merge | Bundled | Free | Documented silent-misedit bugs; interactive |
| uymerge | Structural merge driver | New, tiny | MIT | Young; no fileID collision handling stated |
| MergeSight | Merge UI + CLI | New (2026-08) | Commercial | Closed; scope limited |

## Gap we would fill

1. Nobody lets agents work on isolated copies and then reliably merge scene/prefab changes automatically. The state of the art is "serialize scene work to one lane" (Mirven), "assign ownership" (AnkleBreaker), or "one exclusive editor writer" (KatoSync).
2. Measured evidence the single-editor queue is the bottleneck: Coplay's guide (5s reads become 17s under 4 agents).
3. Merge layer is the weak point: UnityYAMLMerge has reported silent-wrong merges; uymerge is the only open-source structural driver and is days-to-months old with 4 stars. A validated merge (verify afterward by loading in Unity / comparing object graphs) would be new.
4. Unaddressed corner cases (our opportunity, UNVERIFIED as unsolved): two branches both adding objects (fileID/GUID collisions), reordering of `m_Children`/`m_Component`, prefab-variant override conflicts, `.meta` GUID duplicates.
5. Sharing Library between clones has no documented safe recipe; a tool could manage per-agent Library copies cheaply (copy-on-write, junction of read-only packages cache) and run headless import/validation.

## Could not verify
Scene Fusion Unity 6 support and pricing page; Unity issue-tracker details and IDs; Unity MCP concurrent-connection limits per plan; Unity Reflect deprecation page directly; Reddit/HN discussions; Accelerator with worktrees; ParrelSync on Unity 6 in practice; IvanMurzak release dates.
