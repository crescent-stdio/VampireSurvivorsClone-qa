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
        private const string SourceCharacterPath = "Assets/Blueprints/Characters/Main Character Blueprint.asset";
        private const string QaCharacterPath = "Assets/Blueprints/QA/QA Main Character.asset";
        public const string QaAgentCharacterPath = "Assets/Blueprints/QA/QA Agent Character.asset";
        public const string QaPresetFolder = "Assets/Blueprints/QA/Presets";
        private const string QaArtifactDirectory = "QAArtifacts";
        private const int QaDecisionPeriod = 5;

        public static string GetPresetAssetPath(string presetName)
        {
            return QaPresetFolder + "/QA Preset " + presetName + ".asset";
        }

        /// <summary>Read the committed preset definitions from the repository.</summary>
        public static QaPresetDefinition[] LoadPresetDefinitions()
        {
            var projectRoot = System.IO.Directory.GetParent(Application.dataPath).FullName;
            var path = System.IO.Path.Combine(projectRoot, QaPresetDefinitions.DefinitionPath);
            if (!System.IO.File.Exists(path))
                throw new InvalidOperationException("Preset definitions are missing: " + QaPresetDefinitions.DefinitionPath);
            return QaPresetDefinitions.Parse(System.IO.File.ReadAllText(path));
        }

        [MenuItem("QA/Generate QA Gameplay Assets")]
        public static void Generate()
        {
            EnsureFolder("Assets/Blueprints", "QA");
            EnsureFolder("Assets/Blueprints/QA", "Presets");
            EnsureFolder("Assets/Scenes", "QA");
            SynchronizeAssetCopy(SourceLevelPath, QaLevelPath);
            SynchronizeAssetCopy(SourceChestPath, QaChestPath);
            SynchronizeAssetCopy(SourceCharacterPath, QaCharacterPath);
            SynchronizeAssetCopy(SourceCharacterPath, QaAgentCharacterPath);

            var definitions = LoadPresetDefinitions();
            ConfigureQaChest();
            ConfigureQaLevel(definitions);
            ConfigureCharacters(definitions);
            ConfigureQaPresets(definitions);
            SynchronizeQaSceneCopy();
            ConfigureQaScene(definitions);
            ConfigureBuildSettings();
            AssetDatabase.SaveAssets();
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

        /// <summary>Local development build. Every shell wrapper expects this path.</summary>
        public static void BuildMacPlayerForBatchMode()
        {
            BuildPlayer(BuildTarget.StandaloneOSX, "QAArtifacts/player", "QaGameplay.app");
        }

        /// <summary>Distributable Linux build.</summary>
        public static void BuildLinuxPlayerForBatchMode()
        {
            BuildPlayer(BuildTarget.StandaloneLinux64, "QAArtifacts/dist/linux", "QaGameplay.x86_64");
        }

        /// <summary>Distributable Windows build.</summary>
        public static void BuildWindowsPlayerForBatchMode()
        {
            BuildPlayer(BuildTarget.StandaloneWindows64, "QAArtifacts/dist/windows", "QaGameplay.exe");
        }

        /// <summary>
        /// Switch to a build target, rebuild Addressables for it, then build the player.
        /// </summary>
        /// <remarks>
        /// The three steps have to happen in this order inside one editor session.
        /// <see cref="AddressableAssetSettings.BuildPlayerContent"/> builds bundles for
        /// whichever target is active, so building content before switching would ship
        /// another platform's bundles. That failure is quiet: the player launches and then
        /// cannot load its content.
        ///
        /// Each target also gets its own directory, because Unity writes a companion
        /// <c>&lt;name&gt;_Data</c> directory beside the executable and a shared directory
        /// would have the targets overwrite one another. A distributed build is that whole
        /// directory, not the executable alone.
        /// </remarks>
        private static void BuildPlayer(BuildTarget target, string buildDirectory, string playerName)
        {
            var group = BuildPipeline.GetBuildTargetGroup(target);
            if (!BuildPipeline.IsBuildTargetSupported(group, target))
                throw new InvalidOperationException(
                    "Unity is missing build support for " + target +
                    ". Install the matching module from Unity Hub before building.");

            if (EditorUserBuildSettings.activeBuildTarget != target &&
                !EditorUserBuildSettings.SwitchActiveBuildTarget(group, target))
                throw new InvalidOperationException("Unable to switch the active build target to " + target + ".");

            BuildAddressablesForBatchMode();

            if (!System.IO.Directory.Exists(buildDirectory))
                System.IO.Directory.CreateDirectory(buildDirectory);

            var report = BuildPipeline.BuildPlayer(
                GetQaPlayerBuildScenePaths(),
                buildDirectory + "/" + playerName,
                target,
                BuildOptions.None);
            if (report.summary.result != BuildResult.Succeeded)
                throw new InvalidOperationException(
                    "QA player build failed for " + target + ": " + report.summary.result);
        }

        public static string[] GetQaPlayerBuildScenePaths()
        {
            var enabledPaths = EditorBuildSettings.scenes.Where(scene => scene.enabled).Select(scene => scene.path).ToArray();
            if (!enabledPaths.Contains(QaScenePath))
                throw new InvalidOperationException("QA Gameplay must be an enabled build scene before building the QA player.");
            return new[] { QaScenePath }.Concat(enabledPaths.Where(path => path != QaScenePath)).ToArray();
        }

        public static string GetSourceSceneFingerprint()
        {
            var projectRoot = System.IO.Directory.GetParent(Application.dataPath).FullName;
            return Hash128.Compute(System.IO.File.ReadAllText(System.IO.Path.Combine(projectRoot, SourceScenePath))).ToString();
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

            qa.name = "QA Default Chest";
            EditorUtility.SetDirty(qa);
        }

        private static void ConfigureQaLevel(QaPresetDefinition[] definitions)
        {
            var source = RequireAsset<LevelBlueprint>(SourceLevelPath);
            var qa = RequireAsset<LevelBlueprint>(QaLevelPath);
            if (source.levelTime <= 0f || qa.miniBosses == null || qa.miniBosses.Length == 0)
                throw new InvalidOperationException("Level 1 must have a positive duration and one miniboss.");

            // Every preset currently shares one level schedule, so one level asset serves
            // them all. Diverging timings would need a level asset per distinct schedule.
            var timings = RequireSharedTimings(definitions);
            qa.levelTime = timings.DurationSeconds;
            qa.miniBosses[0].spawnTime = timings.MinibossSpawnSeconds;
            qa.chestSpawnDelay = timings.ScaleChestSpawnDelay(source.chestSpawnDelay, source.levelTime);
            qa.chestSpawnAmount = source.chestSpawnAmount;
            qa.chestBlueprint = RequireAsset<ChestBlueprint>(QaChestPath);
            qa.name = "QA Level 1";
            EditorUtility.SetDirty(qa);
        }

        private static QaLevelTimings RequireSharedTimings(QaPresetDefinition[] definitions)
        {
            var timings = definitions[0].Timings;
            foreach (var definition in definitions)
                if (Math.Abs(definition.Timings.DurationSeconds - timings.DurationSeconds) > float.Epsilon ||
                    Math.Abs(definition.Timings.MinibossSpawnSeconds - timings.MinibossSpawnSeconds) > float.Epsilon)
                    throw new InvalidOperationException(
                        "Preset " + definition.name + " uses different level timings. Generating a level asset per " +
                        "distinct schedule is not implemented yet.");
            return timings;
        }

        private static void ConfigureCharacters(QaPresetDefinition[] definitions)
        {
            // The smoke character is deliberately durable so scripted episodes reach the
            // final-boss phase deterministically. The agent character keeps the source
            // durability so death stays reachable and the failure reward can fire.
            ConfigureCharacter(QaCharacterPath, "QA Blue", QaPresetDefinitions.DefaultPresetName, definitions);
            ConfigureCharacter(QaAgentCharacterPath, "QA Agent", "train", definitions);
        }

        private static void ConfigureCharacter(
            string path,
            string assetName,
            string presetName,
            QaPresetDefinition[] definitions)
        {
            var definition = QaPresetDefinitions.Require(definitions, presetName);
            var source = RequireAsset<CharacterBlueprint>(SourceCharacterPath);
            var qa = RequireAsset<CharacterBlueprint>(path);
            qa.hp = source.hp * definition.character.healthMultiplier;
            qa.armor = definition.character.armor;
            qa.name = assetName;
            EditorUtility.SetDirty(qa);
        }

        private static void ConfigureQaPresets(QaPresetDefinition[] definitions)
        {
            var level = RequireAsset<LevelBlueprint>(QaLevelPath);
            foreach (var definition in definitions)
            {
                var path = GetPresetAssetPath(definition.name);
                var preset = AssetDatabase.LoadAssetAtPath<QaPresetBlueprint>(path);
                if (preset == null)
                {
                    preset = ScriptableObject.CreateInstance<QaPresetBlueprint>();
                    AssetDatabase.CreateAsset(preset, path);
                }

                var characterPath = string.Equals(definition.name, QaPresetDefinitions.DefaultPresetName, StringComparison.Ordinal)
                    ? QaCharacterPath
                    : QaAgentCharacterPath;
                preset.Configure(
                    definition.name,
                    definition.description,
                    definition.Timings,
                    definition.character.healthMultiplier,
                    definition.character.armor,
                    definition.episode.deadlineSeconds,
                    definition.episode.timeScale,
                    definition.episode.maximumTimeScale,
                    definition.observation.elapsedSecondsScale,
                    level,
                    RequireAsset<CharacterBlueprint>(characterPath));
                preset.name = "QA Preset " + definition.name;
                EditorUtility.SetDirty(preset);
            }
        }


        private static void ConfigureQaScene(QaPresetDefinition[] definitions)
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
                SetObjectReferenceArray(
                    controller,
                    "presets",
                    definitions.Select(definition => (UnityEngine.Object)RequireAsset<QaPresetBlueprint>(GetPresetAssetPath(definition.name))).ToArray());
                SetString(controller, "qaSceneName", "QA Gameplay");
                SetString(controller, "sourceSceneFingerprint", GetSourceSceneFingerprint());
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

        private static void SynchronizeAssetCopy(string sourcePath, string destinationPath)
        {
            var source = AssetDatabase.LoadMainAssetAtPath(sourcePath);
            var destination = AssetDatabase.LoadMainAssetAtPath(destinationPath);
            if (source == null)
                throw new InvalidOperationException("Missing source asset: " + sourcePath + ".");
            if (destination == null)
            {
                if (!AssetDatabase.CopyAsset(sourcePath, destinationPath))
                    throw new InvalidOperationException("Unable to copy " + sourcePath + " to " + destinationPath + ".");
                return;
            }

            EditorUtility.CopySerialized(source, destination);
            EditorUtility.SetDirty(destination);
        }

        private static void SynchronizeQaSceneCopy()
        {
            if (AssetDatabase.LoadMainAssetAtPath(QaScenePath) == null)
            {
                if (!AssetDatabase.CopyAsset(SourceScenePath, QaScenePath))
                    throw new InvalidOperationException("Unable to copy " + SourceScenePath + " to " + QaScenePath + ".");
                return;
            }

            if (IsQaSceneSynchronized(GetSourceSceneFingerprint()))
                return;

            const string temporaryScenePath = "Assets/Scenes/QA/QA Gameplay Source Sync.unity";
            if (AssetDatabase.LoadMainAssetAtPath(temporaryScenePath) != null)
                AssetDatabase.DeleteAsset(temporaryScenePath);
            if (!AssetDatabase.CopyAsset(SourceScenePath, temporaryScenePath))
                throw new InvalidOperationException("Unable to stage the QA scene source synchronization.");
            FileUtil.ReplaceFile(temporaryScenePath, QaScenePath);
            AssetDatabase.ImportAsset(QaScenePath, ImportAssetOptions.ForceUpdate);
            if (AssetDatabase.LoadMainAssetAtPath(temporaryScenePath) != null)
                AssetDatabase.DeleteAsset(temporaryScenePath);
        }

        private static bool IsQaSceneSynchronized(string sourceFingerprint)
        {
            var scene = EditorSceneManager.OpenScene(QaScenePath, OpenSceneMode.Additive);
            try
            {
                var controller = scene.GetRootGameObjects()
                    .SelectMany(root => root.GetComponentsInChildren<QaEpisodeController>(true))
                    .SingleOrDefault();
                if (controller == null)
                    return false;

                var property = new SerializedObject(controller).FindProperty("sourceSceneFingerprint");
                return property != null && property.stringValue == sourceFingerprint;
            }
            finally
            {
                EditorSceneManager.CloseScene(scene, true);
            }
        }

        private static void EnsureFolder(string parent, string child)
        {
            if (!AssetDatabase.IsValidFolder(parent + "/" + child))
                AssetDatabase.CreateFolder(parent, child);
        }

        private static void SetObjectReferenceArray(
            UnityEngine.Object target,
            string propertyName,
            UnityEngine.Object[] values)
        {
            var serialized = new SerializedObject(target);
            var property = serialized.FindProperty(propertyName);
            if (property == null)
                throw new InvalidOperationException("Missing serialized property " + propertyName + ".");
            property.arraySize = values.Length;
            for (var index = 0; index < values.Length; index++)
                property.GetArrayElementAtIndex(index).objectReferenceValue = values[index];
            serialized.ApplyModifiedPropertiesWithoutUndo();
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
