using System;
using System.Linq;
using Unity.MLAgents;
using Unity.MLAgents.Policies;
using UnityEditor.AddressableAssets.Settings;
using UnityEditor.Build.Reporting;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Vampire.Editor.QA
{
    /// <summary>
    /// Recreates QA-only gameplay assets without modifying production Level 1 assets.
    /// </summary>
    public static class QaAssetGenerator
    {
        public const string SourceLevelPath = "Assets/Blueprints/Levels/Level 1.asset";
        public const string QaLevelPath = "Assets/Blueprints/QA/QA Level 1.asset";
        public const string SourceChestPath = "Assets/Blueprints/Chests/Default Chest.asset";
        public const string QaChestPath = "Assets/Blueprints/QA/QA Default Chest.asset";
        public const string SourceScenePath = "Assets/Scenes/Game/Level 1.unity";
        public const string QaScenePath = "Assets/Scenes/QA/QA Gameplay.unity";
        private const string QaCharacterPath = "Assets/Blueprints/Characters/Main Character Blueprint.asset";
        private const string QaArtifactDirectory = "QAArtifacts";
        private const int QaDecisionPeriod = 5;

        [MenuItem("QA/Generate QA Gameplay Assets")]
        public static void Generate()
        {
            EnsureFolder("Assets/Blueprints", "QA");
            EnsureFolder("Assets/Scenes", "QA");
            CopyIfMissing(SourceLevelPath, QaLevelPath);
            CopyIfMissing(SourceChestPath, QaChestPath);
            AssetDatabase.Refresh();

            ConfigureQaChest();
            ConfigureQaLevel();
            CopyIfMissing(SourceScenePath, QaScenePath);
            AssetDatabase.Refresh();
            ConfigureQaScene();
            ConfigureBuildSettings();
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();
        }

        public static void GenerateForBatchMode()
        {
            Generate();
        }

        public static void BuildAddressablesForBatchMode()
        {
            AddressableAssetSettings.BuildPlayerContent(out var result);
            if (!string.IsNullOrEmpty(result.Error))
                throw new InvalidOperationException("Addressables build failed: " + result.Error);
        }

        public static void BuildMacPlayerForBatchMode()
        {
            var buildDirectory = "QAArtifacts/player";
            if (!System.IO.Directory.Exists(buildDirectory))
                System.IO.Directory.CreateDirectory(buildDirectory);

            var report = BuildPipeline.BuildPlayer(
                EditorBuildSettings.scenes.Where(scene => scene.enabled).Select(scene => scene.path).ToArray(),
                buildDirectory + "/QaGameplay.app",
                BuildTarget.StandaloneOSX,
                BuildOptions.None);
            if (report.summary.result != BuildResult.Succeeded)
                throw new InvalidOperationException("QA player build failed: " + report.summary.result);
        }

        private static void ConfigureQaChest()
        {
            var source = RequireAsset<ChestBlueprint>(SourceChestPath);
            var qa = RequireAsset<ChestBlueprint>(QaChestPath);
            var sourceLoot = source.lootTable.lootTable;
            var qaLoot = qa.lootTable.lootTable;
            if (sourceLoot == null || qaLoot == null || sourceLoot.Length != qaLoot.Length)
                throw new InvalidOperationException("QA chest copy does not match the source chest loot table.");

            var positiveTotal = sourceLoot.Where(loot => loot.dropChance > 0f).Sum(loot => loot.dropChance);
            if (positiveTotal <= 0f)
                throw new InvalidOperationException("The source chest has no positive loot probability to normalize.");

            for (var index = 0; index < qaLoot.Length; index++)
                qaLoot[index].dropChance = sourceLoot[index].dropChance <= 0f
                    ? 0f
                    : sourceLoot[index].dropChance / positiveTotal;

            EditorUtility.SetDirty(qa);
        }

        private static void ConfigureQaLevel()
        {
            var source = RequireAsset<LevelBlueprint>(SourceLevelPath);
            var qa = RequireAsset<LevelBlueprint>(QaLevelPath);
            if (source.levelTime <= 0f || qa.miniBosses == null || qa.miniBosses.Length == 0)
                throw new InvalidOperationException("Level 1 must have a positive duration and one miniboss.");

            qa.levelTime = 90f;
            qa.miniBosses[0].spawnTime = 45f;
            qa.chestSpawnDelay = source.chestSpawnDelay * (qa.levelTime / source.levelTime);
            qa.chestSpawnAmount = source.chestSpawnAmount;
            qa.chestBlueprint = RequireAsset<ChestBlueprint>(QaChestPath);
            EditorUtility.SetDirty(qa);
        }

        private static void ConfigureQaScene()
        {
            var scene = EditorSceneManager.OpenScene(QaScenePath, OpenSceneMode.Additive);
            try
            {
                var levelManager = FindSingle<LevelManager>(scene);
                var playerCharacter = FindSingle<Character>(scene);
                var abilityDialog = FindSingle<AbilitySelectionDialog>(scene);
                var entityManager = FindSingle<EntityManager>(scene);
                var statsManager = FindSingle<StatsManager>(scene);

                SetObjectReference(levelManager, "levelBlueprint", RequireAsset<LevelBlueprint>(QaLevelPath));
                var root = FindOrCreateQaRoot(scene);
                var controller = GetOrAdd<QaEpisodeController>(root);
                var agent = GetOrAdd<QaGameplayAgent>(root);
                var behavior = GetOrAdd<BehaviorParameters>(root);
                var requester = GetOrAdd<DecisionRequester>(root);

                SetObjectReference(controller, "qaCharacter", RequireAsset<CharacterBlueprint>(QaCharacterPath));
                SetString(controller, "qaSceneName", "QA Gameplay");
                SetString(controller, "artifactDirectory", QaArtifactDirectory);
                SetObjectReference(controller, "playerCharacter", playerCharacter);
                SetObjectReference(controller, "levelManager", levelManager);
                SetObjectReference(controller, "abilitySelectionDialog", abilityDialog);
                SetBoolean(controller, "disableAbilityPause", true);
                SetObjectReference(controller, "entityManager", entityManager);
                SetObjectReference(controller, "statsManager", statsManager);
                SetObjectReference(agent, "controller", controller);

                behavior.BehaviorName = QaGameplayAgent.BehaviorName;
                behavior.BrainParameters.VectorObservationSize = QaGameplayObservationEncoder.ObservationSize;
                behavior.BrainParameters.ActionSpec = QaGameplayAgent.ExpectedActionSpec;
                behavior.BehaviorType = BehaviorType.Default;
                behavior.Model = null;
                requester.DecisionPeriod = QaDecisionPeriod;
                requester.TakeActionsBetweenDecisions = true;
                abilityDialog.PauseOnOpen = false;

                EditorUtility.SetDirty(root);
                EditorUtility.SetDirty(levelManager);
                EditorUtility.SetDirty(controller);
                EditorUtility.SetDirty(agent);
                EditorUtility.SetDirty(behavior);
                EditorUtility.SetDirty(requester);
                EditorSceneManager.MarkSceneDirty(scene);
                EditorSceneManager.SaveScene(scene);
            }
            finally
            {
                EditorSceneManager.CloseScene(scene, true);
            }
        }

        private static void ConfigureBuildSettings()
        {
            var expectedPaths = new[]
            {
                "Assets/Scenes/Game/Main Menu.unity",
                "Assets/Scenes/Game/Level 1.unity",
                QaScenePath
            };
            var existing = EditorBuildSettings.scenes
                .Where(scene => scene.path != QaScenePath)
                .ToArray();
            if (existing.Length != 2 || existing[0].path != expectedPaths[0] || existing[1].path != expectedPaths[1])
                throw new InvalidOperationException("Existing build scene order must remain Main Menu then Level 1.");

            EditorBuildSettings.scenes = new[]
            {
                new EditorBuildSettingsScene(expectedPaths[0], true),
                new EditorBuildSettingsScene(expectedPaths[1], true),
                new EditorBuildSettingsScene(expectedPaths[2], true)
            };
        }

        private static GameObject FindOrCreateQaRoot(Scene scene)
        {
            var root = scene.GetRootGameObjects().SingleOrDefault(gameObject => gameObject.name == "QA Episode");
            if (root != null)
                return root;

            root = new GameObject("QA Episode");
            SceneManager.MoveGameObjectToScene(root, scene);
            return root;
        }

        private static T GetOrAdd<T>(GameObject gameObject) where T : Component
        {
            return gameObject.GetComponent<T>() ?? gameObject.AddComponent<T>();
        }

        private static T FindSingle<T>(Scene scene) where T : Component
        {
            var matches = scene.GetRootGameObjects()
                .SelectMany(root => root.GetComponentsInChildren<T>(true))
                .ToArray();
            if (matches.Length != 1)
                throw new InvalidOperationException("Expected exactly one " + typeof(T).Name + " in the QA scene.");
            return matches[0];
        }

        private static T RequireAsset<T>(string path) where T : UnityEngine.Object
        {
            var asset = AssetDatabase.LoadAssetAtPath<T>(path);
            if (asset == null)
                throw new InvalidOperationException("Missing required asset: " + path);
            return asset;
        }

        private static void CopyIfMissing(string sourcePath, string destinationPath)
        {
            if (AssetDatabase.LoadMainAssetAtPath(destinationPath) != null)
                return;
            if (!AssetDatabase.CopyAsset(sourcePath, destinationPath))
                throw new InvalidOperationException("Unable to copy " + sourcePath + " to " + destinationPath + ".");
        }

        private static void EnsureFolder(string parent, string child)
        {
            if (!AssetDatabase.IsValidFolder(parent + "/" + child))
                AssetDatabase.CreateFolder(parent, child);
        }

        private static void SetObjectReference(UnityEngine.Object target, string propertyName, UnityEngine.Object value)
        {
            var serialized = new SerializedObject(target);
            var property = serialized.FindProperty(propertyName);
            if (property == null)
                throw new InvalidOperationException("Missing serialized property " + propertyName + ".");
            property.objectReferenceValue = value;
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        private static void SetString(UnityEngine.Object target, string propertyName, string value)
        {
            var serialized = new SerializedObject(target);
            var property = serialized.FindProperty(propertyName);
            if (property == null)
                throw new InvalidOperationException("Missing serialized property " + propertyName + ".");
            property.stringValue = value;
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        private static void SetBoolean(UnityEngine.Object target, string propertyName, bool value)
        {
            var serialized = new SerializedObject(target);
            var property = serialized.FindProperty(propertyName);
            if (property == null)
                throw new InvalidOperationException("Missing serialized property " + propertyName + ".");
            property.boolValue = value;
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }
    }
}
