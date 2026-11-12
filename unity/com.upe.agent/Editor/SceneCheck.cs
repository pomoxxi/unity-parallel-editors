// Loads scenes and prefabs and reports what Unity can no longer resolve:
// missing scripts, missing prefab assets, and object references whose target
// is gone. Nothing is saved.
//
// Run in batch mode, with a graphics device (no -nographics: Unity crashes
// loading a scene with a Terrain without one):
//   Unity.exe -batchmode -accept-apiupdate -projectPath <worktree> -logFile <log>
//     -executeMethod Upe.Agent.SceneCheck.Run
//     -upeOut <report.json> [-upePaths "Assets/A.unity;Assets/B.prefab"]
//
// Without -upePaths every scene and prefab under Assets/ is checked.
// Exit code: 0 clean, 1 problems found, 2 the check itself failed.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;

namespace Upe.Agent
{
    public static class SceneCheck
    {
        [Serializable]
        public class Problem
        {
            public string path;
            public string kind;
            public string where;
            public string detail;
        }

        [Serializable]
        public class Report
        {
            public int scenes;
            public int prefabs;
            public List<Problem> problems = new List<Problem>();
        }

        public static void Run()
        {
            var outPath = Arg("-upeOut") ?? Path.Combine("Logs", "upe-scene-check.json");
            var only = (Arg("-upePaths") ?? "").Split(new[] { ';' }, StringSplitOptions.RemoveEmptyEntries);
            var report = new Report();
            var exit = 0;
            try
            {
                foreach (var path in Assets(only, ".unity", "t:Scene"))
                {
                    var scene = EditorSceneManager.OpenScene(path, OpenSceneMode.Single);
                    foreach (var root in scene.GetRootGameObjects())
                        CheckTree(path, root, report);
                    report.scenes++;
                }
                foreach (var path in Assets(only, ".prefab", "t:Prefab"))
                {
                    var root = PrefabUtility.LoadPrefabContents(path);
                    try
                    {
                        CheckTree(path, root, report);
                    }
                    finally
                    {
                        PrefabUtility.UnloadPrefabContents(root);
                    }
                    report.prefabs++;
                }
            }
            catch (Exception e)
            {
                report.problems.Add(new Problem { kind = "exception", detail = e.ToString() });
                exit = 2;
            }

            var dir = Path.GetDirectoryName(outPath);
            if (!string.IsNullOrEmpty(dir))
                Directory.CreateDirectory(dir);
            File.WriteAllText(outPath, JsonUtility.ToJson(report, true));
            if (exit == 0 && report.problems.Count > 0)
                exit = 1;
            EditorApplication.Exit(exit);
        }

        static IEnumerable<string> Assets(string[] only, string extension, string filter)
        {
            if (only.Length > 0)
                return only.Where(p => p.EndsWith(extension, StringComparison.OrdinalIgnoreCase));
            return AssetDatabase.FindAssets(filter, new[] { "Assets" })
                .Select(AssetDatabase.GUIDToAssetPath)
                .Where(p => p.EndsWith(extension, StringComparison.OrdinalIgnoreCase))
                .Distinct();
        }

        static void CheckTree(string asset, GameObject root, Report report)
        {
            foreach (var transform in root.GetComponentsInChildren<Transform>(true))
            {
                var go = transform.gameObject;
                var where = HierarchyPath(transform);

                if (PrefabUtility.IsPrefabAssetMissing(go))
                    report.problems.Add(new Problem { path = asset, kind = "missing-prefab", where = where });

                foreach (var component in go.GetComponents<Component>())
                {
                    if (component == null)
                    {
                        report.problems.Add(new Problem { path = asset, kind = "missing-script", where = where });
                        continue;
                    }
                    var so = new SerializedObject(component);
                    var property = so.GetIterator();
                    while (property.Next(true))
                    {
                        if (property.propertyType != SerializedPropertyType.ObjectReference)
                            continue;
                        // A null value with a non-zero id means the reference points at something that is gone.
                        if (property.objectReferenceValue == null && property.objectReferenceInstanceIDValue != 0)
                        {
                            report.problems.Add(new Problem
                            {
                                path = asset,
                                kind = "missing-reference",
                                where = where,
                                detail = component.GetType().Name + "." + property.propertyPath,
                            });
                        }
                    }
                }
            }
        }

        static string HierarchyPath(Transform t)
        {
            var parts = new List<string>();
            for (; t != null; t = t.parent)
                parts.Add(t.name);
            parts.Reverse();
            return string.Join("/", parts);
        }

        static string Arg(string name)
        {
            var args = Environment.GetCommandLineArgs();
            var i = Array.IndexOf(args, name);
            return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
        }
    }
}
