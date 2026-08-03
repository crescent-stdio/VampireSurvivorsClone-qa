using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using NUnit.Framework;
using Unity.MLAgents;
using Unity.MLAgents.Policies;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Vampire.Tests.EditMode
{
    public class QaAssetContractTests
    {
        private const string SourceLevelPath = "Assets/Blueprints/Levels/Level 1.asset";
        private const string QaLevelPath = "Assets/Blueprints/QA/QA Level 1.asset";
        private const string SourceChestPath = "Assets/Blueprints/Chests/Default Chest.asset";
        private const string QaChestPath = "Assets/Blueprints/QA/QA Default Chest.asset";
        private const string QaScenePath = "Assets/Scenes/QA/QA Gameplay.unity";

        [Test]
        public void Qa_scene_is_the_third_enabled_build_scene_without_changing_existing_order()
        {
            var scenes = EditorBuildSettings.scenes;

            Assert.That(scenes.Where(scene => scene.enabled).Select(scene => scene.path), Is.EqualTo(new[]
            {
                "Assets/Scenes/Game/Main Menu.unity",
                "Assets/Scenes/Game/Level 1.unity",
                QaScenePath
            }));
        }

        [Test]
        public void Qa_level_copies_source_content_and_uses_shortened_timing()
        {
            var source = Load<LevelBlueprint>(SourceLevelPath);
            var qa = Load<LevelBlueprint>(QaLevelPath);

            Assert.That(qa, Is.Not.SameAs(source));
            Assert.That(qa.levelTime, Is.EqualTo(90f));
            Assert.That(qa.miniBosses, Has.Length.EqualTo(source.miniBosses.Length));
            Assert.That(qa.miniBosses[0].spawnTime, Is.EqualTo(45f));
            Assert.That(qa.finalBoss.bossBlueprint, Is.Not.Null);
            Assert.That(JsonUtility.ToJson(qa.monsterSpawnTable), Is.EqualTo(JsonUtility.ToJson(source.monsterSpawnTable)));
            Assert.That(qa.monsters, Has.Length.EqualTo(source.monsters.Length));
            Assert.That(qa.chestSpawnDelay, Is.EqualTo(source.chestSpawnDelay * (90f / source.levelTime)).Within(0.0001f));
            Assert.That(qa.chestSpawnAmount, Is.EqualTo(source.chestSpawnAmount));
            Assert.That(qa.chestBlueprint, Is.EqualTo(Load<ChestBlueprint>(QaChestPath)));

            Assert.That(source.levelTime, Is.EqualTo(600f));
            Assert.That(source.miniBosses[0].spawnTime, Is.EqualTo(300f));
            Assert.That(source.chestBlueprint, Is.EqualTo(Load<ChestBlueprint>(SourceChestPath)));
        }

        [Test]
        public void Qa_generator_repairs_source_derived_assets_and_scene_without_changing_qa_guids()
        {
            using (QaFixtureSnapshot.Capture())
            {
                var levelGuid = AssetDatabase.AssetPathToGUID(QaLevelPath);
                var sceneGuid = AssetDatabase.AssetPathToGUID(QaScenePath);
                var source = Load<LevelBlueprint>(SourceLevelPath);
                var qa = Load<LevelBlueprint>(QaLevelPath);
                qa.initialExpGemCount = -99;
                qa.monsters = Array.Empty<LevelBlueprint.MonstersContainer>();
                EditorUtility.SetDirty(qa);

                var scene = EditorSceneManager.OpenScene(QaScenePath, OpenSceneMode.Additive);
                try
                {
                    var sentinel = new GameObject("QA source synchronization sentinel");
                    SceneManager.MoveGameObjectToScene(sentinel, scene);
                    EditorSceneManager.SaveScene(scene);
                }
                finally
                {
                    EditorSceneManager.CloseScene(scene, true);
                }

                InvokeQaGenerator("Generate");

                Assert.That(AssetDatabase.AssetPathToGUID(QaLevelPath), Is.EqualTo(levelGuid));
                Assert.That(AssetDatabase.AssetPathToGUID(QaScenePath), Is.EqualTo(sceneGuid));
                Assert.That(qa.initialExpGemCount, Is.EqualTo(source.initialExpGemCount));
                Assert.That(qa.monsters, Has.Length.EqualTo(source.monsters.Length));
                scene = EditorSceneManager.OpenScene(QaScenePath, OpenSceneMode.Additive);
                try
                {
                    Assert.That(scene.GetRootGameObjects().Select(root => root.name), Does.Not.Contain("QA source synchronization sentinel"));
                }
                finally
                {
                    EditorSceneManager.CloseScene(scene, true);
                }
            }
        }

        [Test]
        public void Qa_chest_normalizes_relative_positive_loot_probabilities()
        {
            var source = Load<ChestBlueprint>(SourceChestPath);
            var qa = Load<ChestBlueprint>(QaChestPath);
            var sourceLoot = source.lootTable.lootTable;
            var qaLoot = qa.lootTable.lootTable;

            Assert.That(qa, Is.Not.SameAs(source));
            Assert.That(qaLoot, Has.Length.EqualTo(sourceLoot.Length));
            Assert.That(qaLoot.Sum(loot => loot.dropChance), Is.EqualTo(1f).Within(0.0001f));

            var sourcePositiveTotal = sourceLoot.Where(loot => loot.dropChance > 0f).Sum(loot => loot.dropChance);
            var cumulative = 0f;
            for (var index = 0; index < qaLoot.Length; index++)
            {
                Assert.That(qaLoot[index].item, Is.EqualTo(sourceLoot[index].item));
                if (sourceLoot[index].dropChance <= 0f)
                {
                    Assert.That(qaLoot[index].dropChance, Is.Zero);
                    continue;
                }

                Assert.That(qaLoot[index].dropChance, Is.EqualTo(sourceLoot[index].dropChance / sourcePositiveTotal).Within(0.0001f));
                cumulative += qaLoot[index].dropChance;
                Assert.That(cumulative, Is.LessThanOrEqualTo(1f + 0.0001f));
            }

            Assert.That(sourceLoot.Sum(loot => loot.dropChance), Is.EqualTo(1.91f).Within(0.0001f), "Known source risk: do not alter the original chest asset.");
        }

        [Test]
        public void Qa_asset_references_and_monster_spawn_arrays_are_complete()
        {
            var qa = Load<LevelBlueprint>(QaLevelPath);
            var monsterCount = qa.monsters.Sum(container => container.monsterBlueprints.Length);

            Assert.That(qa.abilityPrefabs, Is.All.Not.Null);
            Assert.That(qa.monsters, Is.All.Matches<LevelBlueprint.MonstersContainer>(container =>
                container != null && container.monstersPrefab != null && container.monsterBlueprints.All(blueprint => blueprint != null)));
            Assert.That(qa.miniBosses, Is.All.Matches<LevelBlueprint.MiniBossContainer>(boss =>
                boss != null && boss.bossPrefab != null && boss.bossBlueprint != null));
            Assert.That(qa.finalBoss, Is.Not.Null);
            Assert.That(qa.finalBoss.bossPrefab, Is.Not.Null);
            Assert.That(qa.finalBoss.bossBlueprint, Is.Not.Null);
            Assert.That(qa.chestBlueprint.closedChest, Is.Not.Null);
            Assert.That(qa.chestBlueprint.openingChest, Is.Not.Null);
            Assert.That(qa.chestBlueprint.openChest, Is.Not.Null);
            Assert.That(qa.chestBlueprint.lootTable.lootTable.Where(loot => loot.dropChance > 0f), Is.All.Matches<Loot<GameObject>>(loot => loot.item != null));
            Assert.That(qa.monsterSpawnTable.spawnChanceKeyframes, Is.All.Matches<MonsterSpawnTable.SpawnChanceKeyframe>(keyframe => keyframe.spawnChances.Length == monsterCount));
            Assert.That(qa.monsterSpawnTable.hpMultiplierKeyframes, Is.All.Matches<MonsterSpawnTable.HPMultiplierKeyframe>(keyframe => keyframe.healthBuffs.Length == monsterCount));
        }

        [Test]
        public void Qa_scene_wires_controller_agent_and_non_pausing_ability_dialog()
        {
            var scene = EditorSceneManager.OpenScene(QaScenePath, OpenSceneMode.Additive);
            try
            {
                var levelManager = FindInScene<LevelManager>(scene);
                var controller = FindInScene<QaEpisodeController>(scene);
                var agent = FindInScene<QaGameplayAgent>(scene);
                var behavior = agent.GetComponent<BehaviorParameters>();
                var requester = agent.GetComponent<DecisionRequester>();
                var dialog = FindInScene<AbilitySelectionDialog>(scene);

                Assert.That(levelManager, Is.Not.Null);
                Assert.That(GetObjectReference<LevelBlueprint>(levelManager, "levelBlueprint"), Is.EqualTo(Load<LevelBlueprint>(QaLevelPath)));
                Assert.That(controller, Is.Not.Null);
                Assert.That(agent, Is.Not.Null);
                Assert.That(GetObjectReference<CharacterBlueprint>(controller, "qaCharacter"), Is.Not.Null);
                Assert.That(GetObjectReference<AbilitySelectionDialog>(controller, "abilitySelectionDialog"), Is.EqualTo(dialog));
                Assert.That(GetString(controller, "qaSceneName"), Is.EqualTo("QA Gameplay"));
                Assert.That(GetString(controller, "artifactDirectory"), Is.EqualTo("QAArtifacts"));
                Assert.That(controller.GetType().GetCustomAttributes(typeof(DefaultExecutionOrder), true)
                    .Cast<DefaultExecutionOrder>().Single().order, Is.LessThan(-100));
                Assert.That(behavior.BehaviorName, Is.EqualTo(QaGameplayAgent.BehaviorName));
                Assert.That(behavior.BrainParameters.VectorObservationSize, Is.EqualTo(36));
                Assert.That(behavior.BrainParameters.ActionSpec.NumContinuousActions, Is.EqualTo(2));
                Assert.That(behavior.BrainParameters.ActionSpec.BranchSizes, Is.EqualTo(new[] { 5 }));
                Assert.That(requester.DecisionPeriod, Is.EqualTo(5));
                Assert.That(GetBoolean(controller, "disableAbilityPause"), Is.True);
            }
            finally
            {
                EditorSceneManager.CloseScene(scene, true);
            }
        }

        [Test]
        public void Localization_string_tables_have_matching_keys_for_english_simplified_and_traditional_chinese()
        {
            var tables = AssetDatabase.FindAssets(string.Empty, new[] { "Assets/Localization/Tables" })
                .Select(AssetDatabase.GUIDToAssetPath)
                .Where(path => path.EndsWith("_en.asset", StringComparison.Ordinal)
                    || path.EndsWith("_zh.asset", StringComparison.Ordinal)
                    || path.EndsWith("_zh-Hant.asset", StringComparison.Ordinal))
                .Select(path => new { Path = path, Table = AssetDatabase.LoadAssetAtPath<ScriptableObject>(path) })
                .Where(item => item.Table != null && new SerializedObject(item.Table).FindProperty("m_TableData") != null)
                .GroupBy(item => item.Path.Substring(0, item.Path.LastIndexOf('_')))
                .ToList();

            Assert.That(tables, Is.Not.Empty);
            foreach (var collection in tables)
            {
                var byLocale = collection.ToDictionary(item => ExtractLocale(item.Table), item => item.Table);
                Assert.That(byLocale.ContainsKey("en"), Is.True, collection.Key + " is missing English.");
                Assert.That(byLocale.ContainsKey("zh"), Is.True, collection.Key + " is missing Simplified Chinese.");
                Assert.That(byLocale.ContainsKey("zh-Hant"), Is.True, collection.Key + " is missing Traditional Chinese.");

                var englishKeys = ExtractEntryIds(byLocale["en"]);
                Assert.That(ExtractEntryIds(byLocale["zh"]), Is.EquivalentTo(englishKeys), collection.Key + " Simplified Chinese keys differ.");
                Assert.That(ExtractEntryIds(byLocale["zh-Hant"]), Is.EquivalentTo(englishKeys), collection.Key + " Traditional Chinese keys differ.");
            }
        }

        [Test]
        public void Addressables_player_build_integration_remains_an_explicit_operator_step()
        {
            var settings = AssetDatabase.LoadAssetAtPath<ScriptableObject>("Assets/AddressableAssetsData/AddressableAssetSettings.asset");
            var serializedSettings = new SerializedObject(settings);

            Assert.That(serializedSettings.FindProperty("m_BuildAddressablesWithPlayerBuild").boolValue, Is.False);
        }

        [Test]
        public void Qa_player_build_starts_in_qa_gameplay_without_reordering_general_build_settings()
        {
            var paths = (string[])GetQaGeneratorType().GetMethod("GetQaPlayerBuildScenePaths", BindingFlags.Public | BindingFlags.Static)
                .Invoke(null, null);

            Assert.That(paths[0], Is.EqualTo(QaScenePath));
            Assert.That(paths.Skip(1), Is.EqualTo(new[] { "Assets/Scenes/Game/Main Menu.unity", "Assets/Scenes/Game/Level 1.unity" }));
            Assert.That(EditorBuildSettings.scenes.Where(scene => scene.enabled).Select(scene => scene.path).First(), Is.EqualTo("Assets/Scenes/Game/Main Menu.unity"));
        }

        private static T Load<T>(string path) where T : UnityEngine.Object
        {
            var asset = AssetDatabase.LoadAssetAtPath<T>(path);
            Assert.That(asset, Is.Not.Null, "Missing required QA asset: " + path);
            return asset;
        }

        private static T FindInScene<T>(Scene scene) where T : Component
        {
            return scene.GetRootGameObjects().SelectMany(root => root.GetComponentsInChildren<T>(true)).SingleOrDefault();
        }

        private static T GetObjectReference<T>(UnityEngine.Object target, string propertyName) where T : UnityEngine.Object
        {
            return new SerializedObject(target).FindProperty(propertyName).objectReferenceValue as T;
        }

        private static string GetString(UnityEngine.Object target, string propertyName)
        {
            return new SerializedObject(target).FindProperty(propertyName).stringValue;
        }

        private static bool GetBoolean(UnityEngine.Object target, string propertyName)
        {
            return new SerializedObject(target).FindProperty(propertyName).boolValue;
        }

        private static string ExtractLocale(ScriptableObject table)
        {
            return new SerializedObject(table).FindProperty("m_LocaleId.m_Code").stringValue;
        }

        private static IEnumerable<long> ExtractEntryIds(ScriptableObject table)
        {
            var entries = new SerializedObject(table).FindProperty("m_TableData");
            for (var index = 0; index < entries.arraySize; index++)
                yield return entries.GetArrayElementAtIndex(index).FindPropertyRelative("m_Id").longValue;
        }

        private static void InvokeQaGenerator(string methodName)
        {
            GetQaGeneratorType().GetMethod(methodName, BindingFlags.Public | BindingFlags.Static).Invoke(null, null);
        }

        private static Type GetQaGeneratorType()
        {
            var generator = AppDomain.CurrentDomain.GetAssemblies()
                .Select(assembly => assembly.GetType("Vampire.Editor.QA.QaAssetGenerator"))
                .FirstOrDefault(type => type != null);
            Assert.That(generator, Is.Not.Null, "QA asset generator must be available in the Editor assembly.");
            return generator;
        }
    }
}
