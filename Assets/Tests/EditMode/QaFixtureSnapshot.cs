using System;
using System.IO;
using System.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Vampire.Tests.EditMode
{
    /// <summary>
    /// Restores QA fixtures even when an EditMode assertion or generator invocation throws.
    /// </summary>
    internal sealed class QaFixtureSnapshot : IDisposable
    {
        private static readonly string[] QaAssetPaths =
        {
            "Assets/Blueprints/QA/QA Level 1.asset",
            "Assets/Blueprints/QA/QA Default Chest.asset",
            "Assets/Scenes/QA/QA Gameplay.unity",
            "Assets/Blueprints/QA/QA Main Character.asset"
        };

        private readonly string projectRoot;
        private readonly byte[][] assetBytes;
        private readonly EditorBuildSettingsScene[] buildScenes;
        private bool disposed;

        private QaFixtureSnapshot(string projectRoot, byte[][] assetBytes, EditorBuildSettingsScene[] buildScenes)
        {
            this.projectRoot = projectRoot;
            this.assetBytes = assetBytes;
            this.buildScenes = buildScenes;
        }

        public static QaFixtureSnapshot Capture()
        {
            var root = Directory.GetParent(Application.dataPath).FullName;
            var bytes = QaAssetPaths.Select(path => File.ReadAllBytes(Path.Combine(root, path))).ToArray();
            var scenes = EditorBuildSettings.scenes
                .Select(scene => new EditorBuildSettingsScene(scene.path, scene.enabled))
                .ToArray();
            return new QaFixtureSnapshot(root, bytes, scenes);
        }

        public void Dispose()
        {
            if (disposed)
                return;
            disposed = true;

            var qaScene = SceneManager.GetSceneByPath(QaAssetPaths[2]);
            if (qaScene.IsValid() && qaScene.isLoaded)
                EditorSceneManager.CloseScene(qaScene, true);

            AssetDatabase.SaveAssets();
            for (var index = 0; index < QaAssetPaths.Length; index++)
            {
                File.WriteAllBytes(Path.Combine(projectRoot, QaAssetPaths[index]), assetBytes[index]);
                AssetDatabase.ImportAsset(QaAssetPaths[index], ImportAssetOptions.ForceUpdate);
            }
            EditorBuildSettings.scenes = buildScenes;
        }
    }
}
