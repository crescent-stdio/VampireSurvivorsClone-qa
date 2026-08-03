using System.Reflection;
using NUnit.Framework;
using UnityEngine;

namespace Vampire.Tests.EditMode
{
    public sealed class QaRandomDecisionRecordingTests
    {
        [Test]
        public void Real_monster_and_loot_selection_seams_record_stable_decision_ids()
        {
            var controller = CreateController();
            var spawnTable = new MonsterSpawnTable
            {
                spawnChanceKeyframes = new[]
                {
                    new MonsterSpawnTable.SpawnChanceKeyframe { t = 0f, spawnChances = new[] { 1f } },
                    new MonsterSpawnTable.SpawnChanceKeyframe { t = 1f, spawnChances = new[] { 1f } }
                }
            };
            var lootTable = new LootTable<string>
            {
                lootTable = new[] { new Loot<string> { item = "coin", dropChance = 1f } }
            };

            Random.InitState(3401);
            Assert.That(spawnTable.SelectMonster(0.5f), Is.EqualTo(0));
            Assert.That(lootTable.DropLoot(), Is.EqualTo("coin"));

            Assert.That(controller.RecordedEpisode.DiscreteEvents, Does.Contain("random:monster-spawn:0"));
            Assert.That(controller.RecordedEpisode.DiscreteEvents, Does.Contain("random:loot:0"));
            Object.DestroyImmediate(controller.gameObject);
        }

        [Test]
        public void Real_ability_selection_seam_records_the_selected_ability_type()
        {
            var controller = CreateController();
            var characterBlueprint = ScriptableObject.CreateInstance<CharacterBlueprint>();
            characterBlueprint.luck = 1f;
            characterBlueprint.startingAbilities = new GameObject[0];
            var levelBlueprint = ScriptableObject.CreateInstance<LevelBlueprint>();
            var abilityPrefab = new GameObject("QA Test Ability Prefab");
            abilityPrefab.SetActive(false);
            abilityPrefab.AddComponent<QaSelectableTestAbility>();
            levelBlueprint.abilityPrefabs = new[] { abilityPrefab };

            var characterObject = new GameObject("QA Selection Character");
            characterObject.SetActive(false);
            var character = characterObject.AddComponent<Character>();
            typeof(Character).GetField("characterBlueprint", BindingFlags.Instance | BindingFlags.NonPublic).SetValue(character, characterBlueprint);
            var managerObject = new GameObject("QA Selection Manager");
            managerObject.SetActive(false);
            var manager = managerObject.AddComponent<AbilityManager>();
            manager.Init(levelBlueprint, null, character, manager);

            Random.InitState(3402);
            var selected = manager.SelectAbilities();

            Assert.That(selected, Has.Count.EqualTo(1));
            Assert.That(controller.RecordedEpisode.DiscreteEvents,
                Does.Contain("random:ability:Vampire.Tests.EditMode.QaSelectableTestAbility"));

            Object.DestroyImmediate(managerObject);
            Object.DestroyImmediate(characterObject);
            Object.DestroyImmediate(abilityPrefab);
            Object.DestroyImmediate(levelBlueprint);
            Object.DestroyImmediate(characterBlueprint);
            Object.DestroyImmediate(controller.gameObject);
        }

        private static QaEpisodeController CreateController()
        {
            var gameObject = new GameObject("QA Random Decision Controller");
            var controller = gameObject.AddComponent<QaEpisodeController>();
            controller.ConfigureForTesting(3400, null, "QA Level", "QAArtifacts", new ScriptedQaPolicy(), new NoOpReloader());
            return controller;
        }

        private sealed class NoOpReloader : IQaSceneReloader
        {
            public void Reload(string sceneName) { }
        }
    }

    public sealed class QaSelectableTestAbility : Ability
    {
        public override bool RequirementsMet() { return true; }
    }
}
