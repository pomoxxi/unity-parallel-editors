# 03 - Merge strategy and local tooling

Date: 2026-10-25. Unity 6000.3.0f1, Windows, git worktrees, one Editor per working copy.
Evidence tags: [src] = read in a cited page; [bg] = background knowledge, not verified in this pass.

## Part A - How Unity asset merges go wrong, and what makes them safe

### A1. Format facts that drive merge behaviour
- Every object is its own YAML document: `--- !u!<classID> &<fileID>`. The fileID is unique only inside the file and "assigned arbitrarily". References are `{fileID: n}` (same file) or `{fileID, guid, type}` (cross file). [src] https://docs.unity3d.com/Manual/FormatDescription.html
- Prefab instances are a `PrefabInstance` document (classID 1001) with `m_Modification` holding `m_Modifications` (entries keyed by `target{fileID,guid}` + `propertyPath`), plus `m_AddedGameObjects`, `m_AddedComponents`, `m_RemovedComponents`, `m_RemovedGameObjects`. `m_SourcePrefab` is `{fileID: 100100000, guid: <prefab guid>}`. [src] https://docs.unity.com/en-us/engine/6000.3/manual/working-with-scenes/text-scene-format/yaml-prefab-serialization.md
- `stripped` documents are placeholders in the containing file so other objects (a parent Transform's `m_Children`, a reference to a child) can point inside a prefab instance. They carry `m_CorrespondingSourceObject` and `m_PrefabInstance`. Same page. Consequence: an edit that touches a nested instance can change the instance block, a stripped doc, AND a parent's `m_Children`: three places that must stay consistent.
- Document order is not semantically meaningful for lookups, but array order (Transform `m_Children`, `m_Component`) is. [bg]
- Floats may be written as hex plus decimal in parentheses; only hex parses. [src] FormatDescription page. A tool that rewrites YAML must preserve Unity's exact formatting or every save shows churn.
- fileIDs of objects produced by prefab instancing are derived from the source object's fileID combined with the instance's (an XOR scheme); new Editor-created objects get large pseudo-random 64-bit IDs. So two branches that each add objects rarely collide on fileID, but a tool that assigns small sequential IDs by hand can. [bg]

### A2. .meta files and GUIDs
- Every asset has a `.meta` with a `guid`; scenes/prefabs reference assets only by GUID. If a branch adds an asset without its `.meta`, the other side regenerates a different GUID and references break ("missing prefab after merge"). [src] https://discussions.unity.com/t/unity-github-missing-prefabs-after-merge/249352 , https://discussions.unity.com/t/how-to-resolve-different-guids-in-versioning/89093
- Two branches creating different assets get different random GUIDs: no collision. Real collisions come from (a) the same asset created independently on both branches (same path, different GUID: keep exactly one, preferably the one already referenced), (b) copying folders with `.meta` files, (c) git rename detection mangling `.meta` pairs (workaround `merge.renames false`). [src] https://bugnet.io/blog/how-to-fix-unity-meta-file-merge-conflict-in-git , https://discussions.unity.com/t/git-conflict-markers-in-meta-files-usually-guid-conflicts-sourcetree/1496722
- Rule for the tool: never text-merge a `.meta`; compare GUID per path and fail loudly on mismatch. Check every added asset arrived with its `.meta`, and no GUID appears twice in the merged tree.

### A3. What conflicts, by file type
| File | Mechanically mergeable | Truly conflicting |
|---|---|---|
| Scene `.unity` | Different GameObjects/components added, deleted or edited (disjoint documents). Different fields of one component if the tool is field-aware. | Same property of same object changed on both sides. Both sides change the same Transform `m_Children` or `m_Component` order. Delete vs edit of one object. Singletons (RenderSettings, LightmapSettings, OcclusionCullingSettings) that many edits touch. |
| Prefab / variant | Disjoint objects; disjoint `m_Modifications` keys (target+propertyPath). | Same target+propertyPath, different values; one side removes a component the other overrides. |
| `.asset` (ScriptableObject) | Disjoint fields, or simply disjoint files. UnityYAMLMerge does NOT merge `.asset`/`.mat`/`.controller` unless `--force`. [src] | Same field; list ordering. |
| ProjectSettings/*.asset | Different files merge (TagManager, InputManager, GraphicsSettings are separate). | TagManager layers/tags (positional), InputManager axes (long list), EditorBuildSettings scene list (order = build index), TimeManager (silently dirtied by tools). [bg] |
| Packages/manifest.json | Different dependency keys: JSON/text merge works. | Same key, different version. |
| Packages/packages-lock.json | Treat as derived. | Delete or take either side, merge manifest, let Unity regenerate. Docs: Package Manager recomputes it; deleting forces a refresh. [src] https://docs.unity3d.com/6000.1/Documentation/Manual/upm-conflicts-auto.html |
| `.cs`, `.asmdef`, shaders | Plain git merge. | Normal text conflicts. |
| Binary (fbx, png, wav, LFS) | Never merged. | Only disjoint paths are safe. |

### A4. UnityYAMLMerge (the official tool)
Facts [src] https://docs.unity.com/en-us/engine/6000.0/manual/get-started/project-configuration/version-control/smart-merge.md :
- Merges `.unity`, `.prefab`, `.meta` semantically; other files and unresolved conflicts go to fallback tools listed in `mergespecfile.txt`. `--force` merges other YAML types. Lives in `Editor/Data/Tools/` next to `mergespecfile.txt` and `mergerules.txt`.
- CLI: `UnityYAMLMerge merge [-p] [-l|-r] [-h] [-i f] [-o f] [--rules f] [--fallback f|none] [--force] [--describe] <base> <theirs> <mine> [<output>]`. `-h` headless (no dialogs); `--fallback none` disables fallback tools; `-o` writes the conflicts to a file; `--describe` explains the merge; `--nomappinginoneline` must match `EditorSettings.serializeInlineMappingsOnOneLine` or output formatting drifts. Non-zero exit on unresolved conflict [bg].
- `mergerules.txt`: arrays `set` (keyed) or `plain`; default is a hybrid heuristic. Defaults key `GameObject.m_Component` by `fileID` and `Prefab.m_Modification.m_Modifications` by `target.fileID target.guid propertyPath`. Exclusions force a conflict when both sides touch a path (MeshRenderer.m_Materials, SpriteRenderer.m_Color, ParticleSystem, MonoBehaviour x/y/z and r/g/b). Float compare can use a relative epsilon.
- Limits: same property changed on both sides is a conflict [src]. Community and vendor claims say it gives up when two people edit the same GameObject (anecdotal): https://github.com/junomation/UnityYamlMergeDemo , https://gist.github.com/maxahn . Hybrid array heuristics can mis-merge reordered entries. It does not validate reference integrity or that Unity can open the result [bg].
- Git wiring: documented as a mergetool. It can also be a merge driver in `.gitattributes` (`*.unity merge=unityyamlmerge`, driver `"<path>" merge -h -p --force %O %B %A %A`) [bg; community pattern, see https://thoughtbot.com/blog/how-to-git-with-unity ].

### A5. Third-party and open-source
- `unityparser` (PyPI, Python 3): load/edit/dump Unity YAML preserving Unity's format; `UnityDocument.load_yaml / .get / .filter / .dump_yaml`. A good base for an own merger or an op applier. https://pypi.org/project/unityparser
- Setup guide only, not an engine: https://github.com/anacat/unity-mergetool
- Commercial: MergeSight (Editor extension + CLI 3-way merge for Git/Perforce/UVCS) https://discussions.unity.com/t/mergesight-visual-unity-asset-review-and-merge-tool-for-git-and-other-vcs-workflows/1722629 ; Conflux (Fab listing; merge driver for same-object conflicts, validates by re-parsing). Closed source.
- I did not find a maintained open-source semantic 3-way merger for Unity YAML. Writing one is feasible (documents keyed by fileID, field-level for properties, `set`-keyed arrays) but component-specific ordering is a long tail.

### A6. Workflow patterns that avoid conflicts
1. One feature = its own additive scene or prefab; do not edit the shared level scene. Most repeated advice. https://discussions.unity.com/t/how-to-avoid-merge-conflicts-git/766124 , https://docs.diversion.dev/unity/unity-best-practices.md
2. Prefab per feature, with variants/nesting so scene files hold only instance overrides. https://blogs.unity3d.com/es/technology/production-workflow-improvements
3. Data in ScriptableObjects, one file per entity (not one big table asset): disjoint files merge by git for free. (Reasoned from the format; no source found.)
4. Assembly definitions per feature: code in disjoint folders that compile separately, so one agent's half-finished code does not break another's compile (see Part B).
5. Shared registries (scene lists, enums, tag/layer lists, input actions, build settings) are hot spots: make them additive per-feature files aggregated by a generator, or union-merge them.
6. Exclusive ownership: each agent claims scenes/prefabs and the tool checks claims before work starts.
7. Operation log / replay. Record each agent's Editor changes as commands (create GameObject at path P, add component T, set field F=V, add asset A) and apply them in order to the merged project via a headless Editor `-executeMethod` using `SerializedObject` / `PrefabUtility`. Pros: Unity writes the files, so format, fileIDs and references are always valid; conflicts become semantic ("both set X.y") and are detectable before writing; matches a "builder is safe to run twice" rule. Cons: ops must be captured (agents already write C# builder methods, so builder + args is the log); hand edits in the Editor are not captured unless diffed; replay needs a Unity run per merge; order dependence. CRDT/OT ideas are similar but overkill for sequential, mostly disjoint, single-user work; I found no mature Unity CRDT. [bg]
8. Semantic-diff middle path: parse base/ours/theirs to an object graph keyed by (fileID or hierarchy path + component type), compute per-object property diffs vs base, apply non-overlapping diffs, write YAML via unityparser or let Unity re-save. Hierarchy-path keys survive objects created independently on both sides.

### A7. Recommendation, ranked
1. Prevent: ownership + additive layout (A6.1-5) enforced by the tool. Each task declares the scene/prefab/asset paths it writes; overlapping claims are refused; shared registries become per-feature files. Cheapest, and turns most merges into plain git.
2. Merge: plain git first. For same-file `.unity/.prefab/.meta` conflicts run UnityYAMLMerge headless (`-h -p`, `--fallback none`, own `--rules` copy). Any non-zero exit means "needs semantic resolution", never a silent pick of one side.
3. Validate every merged result: re-parse YAML; every local `{fileID}` resolves; no duplicate fileID per file; each `.meta` GUID unique with its asset present; Transform parent/`m_Children` consistent; then a batch Unity run that loads the scenes and logs missing references/scripts, compared to the baseline log.
4. On semantic conflict, replay: where a change came from a builder/op method, re-run it on top of the merged project (idempotent builders make this safe) instead of resolving YAML.
5. Own semantic merger on `unityparser` (A6.8) only if ranks 1-4 leave frequent conflicts. Count conflicts per file type to decide.
6. Derived/registry files: regenerate `packages-lock.json`; merge `manifest.json` as JSON by key; ProjectSettings lists (tags, layers, inputs, build scenes) by per-key union script, flagged for review.
Do not rely on: binary merges, fileID renumbering, text-merging `.meta`.

Design notes: each worktree needs its own `Library/` (first import is slow; seeding from a clean copy is a common trick [bg]). One Unity per project folder is enforced by `Temp/UnityLockfile` and the process check, so one Editor per worktree is allowed. Set `merge.renames false` for `.meta` safety.

## Part B - One checkout, one Editor, many agents

The usual setup before worktrees is one checkout, one Unity project and one Editor, shared by every agent session. It serialises them. Only one Unity process may hold the project (`Temp/UnityLockfile`), so batch runs and the open Editor exclude each other, and agents queue behind a lock. Where it hurts:
- Waiting. A long test or builder run holds the project for everyone, and the user's open Editor blocks every agent.
- One broken compile stops all. One agent's half-written script fails every other agent's compile and test run.
- Domain reloads. A script saved during another agent's batch run reloads the domain and kills that run.
- The shared index. Sessions staging in one git index pick up each other's files, so commits need careful tooling.
- Noise. Unity rewrites files it was not asked to (line endings, settings, whole-prefab re-saves), and with one tree nobody can tell whose change it is.

A worktree and a `Library` per agent removes the lock, the shared compile and the shared index; what is left is the merge (Part A).

## Sources
- https://docs.unity.com/en-us/engine/6000.0/manual/get-started/project-configuration/version-control/smart-merge.md
- https://docs.unity.com/en-us/engine/6000.3/manual/working-with-scenes/text-scene-format/yaml-prefab-serialization.md
- https://docs.unity3d.com/Manual/FormatDescription.html
- https://docs.unity3d.com/6000.1/Documentation/Manual/upm-conflicts-auto.html
- https://support.unity.com/hc/en-us/articles/207332543-What-are-the-solutions-to-scene-prefab-conflict-merging
- https://pypi.org/project/unityparser
- https://github.com/anacat/unity-mergetool , https://github.com/junomation/UnityYamlMergeDemo , https://gist.github.com/maxahn
- https://discussions.unity.com/t/unity-github-missing-prefabs-after-merge/249352 , https://discussions.unity.com/t/how-to-resolve-different-guids-in-versioning/89093 , https://discussions.unity.com/t/git-conflict-markers-in-meta-files-usually-guid-conflicts-sourcetree/1496722
- https://bugnet.io/blog/how-to-fix-unity-meta-file-merge-conflict-in-git , https://bugnet.io/blog/how-to-fix-unity-scene-and-prefab-merge-conflicts
- https://discussions.unity.com/t/how-to-avoid-merge-conflicts-git/766124 , https://docs.diversion.dev/unity/unity-best-practices.md , https://blogs.unity3d.com/es/technology/production-workflow-improvements
- https://thoughtbot.com/blog/how-to-git-with-unity
- https://discussions.unity.com/t/mergesight-visual-unity-asset-review-and-merge-tool-for-git-and-other-vcs-workflows/1722629
