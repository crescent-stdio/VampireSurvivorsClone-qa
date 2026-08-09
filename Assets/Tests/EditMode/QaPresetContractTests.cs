using System.IO;
using System.Linq;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;

namespace Vampire.Tests.EditMode
{
    public class QaPresetContractTests
    {
        private const string SourceCharacterPath = "Assets/Blueprints/Characters/Main Character Blueprint.asset";
        private const string QaCharacterPath = "Assets/Blueprints/QA/QA Main Character.asset";
        private const string QaAgentCharacterPath = "Assets/Blueprints/QA/QA Agent Character.asset";
        private const string QaLevelPath = "Assets/Blueprints/QA/QA Level 1.asset";
        private const string QaPresetFolder = "Assets/Blueprints/QA/Presets";

        private static QaPresetDefinition[] LoadDefinitions()
        {
            var projectRoot = Directory.GetParent(Application.dataPath).FullName;
            var path = Path.Combine(projectRoot, QaPresetDefinitions.DefinitionPath);
            Assert.That(File.Exists(path), "Missing preset definitions: " + QaPresetDefinitions.DefinitionPath);
            return QaPresetDefinitions.Parse(File.ReadAllText(path));
        }

        private static QaPresetBlueprint LoadPreset(string name)
        {
            var path = QaPresetFolder + "/QA Preset " + name + ".asset";
            var preset = AssetDatabase.LoadAssetAtPath<QaPresetBlueprint>(path);
            Assert.That(preset, Is.Not.Null, "Missing generated preset asset: " + path);
            return preset;
        }

        [Test]
        public void Preset_definitions_declare_smoke_train_and_eval()
        {
            var names = LoadDefinitions().Select(definition => definition.name).ToArray();

            Assert.That(names, Is.EquivalentTo(new[] { "smoke", "train", "eval" }));
        }

        [Test]
        public void Generated_presets_match_every_definition()
        {
            foreach (var definition in LoadDefinitions())
            {
                var preset = LoadPreset(definition.name);
                Assert.That(preset.PresetName, Is.EqualTo(definition.name));
                Assert.That(preset.Timings.DurationSeconds, Is.EqualTo(definition.Timings.DurationSeconds));
                Assert.That(preset.CharacterHealthMultiplier, Is.EqualTo(definition.character.healthMultiplier));
                Assert.That(preset.CharacterArmor, Is.EqualTo(definition.character.armor));
                Assert.That(preset.DeadlineSeconds, Is.EqualTo(definition.episode.deadlineSeconds));
                Assert.That(preset.TimeScale, Is.EqualTo(definition.episode.timeScale));
                Assert.That(preset.MaximumTimeScale, Is.EqualTo(definition.episode.maximumTimeScale));
                Assert.That(preset.ElapsedSecondsScale, Is.EqualTo(definition.observation.elapsedSecondsScale));
            }
        }

        [Test]
        public void Smoke_preset_preserves_the_existing_regression_baseline()
        {
            var smoke = LoadPreset("smoke");

            Assert.That(smoke.Timings.DurationSeconds, Is.EqualTo(90f));
            Assert.That(smoke.Timings.MinibossSpawnSeconds, Is.EqualTo(45f));
            Assert.That(smoke.CharacterHealthMultiplier, Is.EqualTo(10f));
            Assert.That(smoke.CharacterArmor, Is.EqualTo(100));
            Assert.That(smoke.DeadlineSeconds, Is.EqualTo(150f));
            Assert.That(smoke.TimeScale, Is.EqualTo(4f));
            Assert.That(smoke.MaximumTimeScale, Is.EqualTo(4f));
            Assert.That(smoke.ElapsedSecondsScale, Is.EqualTo(600f));
        }

        [Test]
        public void Smoke_keeps_the_durable_character_and_agents_keep_source_durability()
        {
            var source = AssetDatabase.LoadAssetAtPath<CharacterBlueprint>(SourceCharacterPath);
            var durable = AssetDatabase.LoadAssetAtPath<CharacterBlueprint>(QaCharacterPath);
            var agent = AssetDatabase.LoadAssetAtPath<CharacterBlueprint>(QaAgentCharacterPath);

            Assert.That(durable.hp, Is.EqualTo(source.hp * 10f));
            Assert.That(durable.armor, Is.EqualTo(100));
            Assert.That(agent.hp, Is.EqualTo(source.hp));
            Assert.That(agent.armor, Is.EqualTo(0));
        }

        [Test]
        public void Training_and_evaluation_share_a_character_so_their_distributions_match()
        {
            Assert.That(LoadPreset("train").Character, Is.SameAs(LoadPreset("eval").Character));
            Assert.That(LoadPreset("smoke").Character, Is.Not.SameAs(LoadPreset("train").Character));
        }

        [Test]
        public void Training_and_evaluation_differ_only_in_time_scale()
        {
            var train = LoadPreset("train");
            var evaluation = LoadPreset("eval");

            Assert.That(train.TimeScale, Is.EqualTo(20f));
            Assert.That(evaluation.TimeScale, Is.EqualTo(1f));
            Assert.That(train.DeadlineSeconds, Is.EqualTo(evaluation.DeadlineSeconds));
            Assert.That(train.ElapsedSecondsScale, Is.EqualTo(evaluation.ElapsedSecondsScale));
        }

        [Test]
        public void Trained_policies_transfer_between_training_and_evaluation()
        {
            Assert.That(LoadPreset("train").Fingerprint, Is.EqualTo(LoadPreset("eval").Fingerprint));
        }

        [Test]
        public void Smoke_assets_are_rejected_for_evaluation()
        {
            Assert.That(LoadPreset("smoke").Fingerprint, Is.Not.EqualTo(LoadPreset("eval").Fingerprint));
        }

        [Test]
        public void Canonical_environment_stays_reproducible_in_other_runtimes()
        {
            var expected = string.Join("\n", new[]
            {
                "qa-preset-environment/v1",
                "level.durationSeconds=90",
                "level.minibossSpawnSeconds=45",
                "character.healthMultiplier=1",
                "character.armor=0",
                "episode.deadlineSeconds=150",
                "observation.elapsedSecondsScale=600"
            });

            Assert.That(QaPresetFingerprint.Canonicalize(LoadPreset("train")), Is.EqualTo(expected));
        }

        [Test]
        public void Fingerprints_match_the_values_pinned_in_the_python_test_suite()
        {
            // qa_agent_runtime/tests/test_presets.py pins the same literals. Changing one
            // implementation without the other must fail here or there, never silently.
            Assert.That(LoadPreset("smoke").Fingerprint, Is.EqualTo("a75a0e8505ba6431"));
            Assert.That(LoadPreset("train").Fingerprint, Is.EqualTo("4ed498f8f2386317"));
            Assert.That(LoadPreset("eval").Fingerprint, Is.EqualTo("4ed498f8f2386317"));
        }

        [Test]
        public void Presets_point_at_the_generated_qa_level()
        {
            var level = AssetDatabase.LoadAssetAtPath<LevelBlueprint>(QaLevelPath);

            foreach (var definition in LoadDefinitions())
                Assert.That(LoadPreset(definition.name).Level, Is.SameAs(level));
        }

        [Test]
        public void Preset_parsing_rejects_a_deadline_that_precedes_the_final_boss()
        {
            var json = "{\"schema\":\"qa-presets/v1\",\"presets\":[{\"name\":\"smoke\"," +
                       "\"level\":{\"durationSeconds\":90,\"minibossSpawnSeconds\":45}," +
                       "\"character\":{\"healthMultiplier\":1,\"armor\":0}," +
                       "\"episode\":{\"deadlineSeconds\":60,\"timeScale\":1,\"maximumTimeScale\":1}," +
                       "\"observation\":{\"elapsedSecondsScale\":600}}]}";

            Assert.That(
                () => QaPresetDefinitions.Parse(json),
                Throws.InvalidOperationException.With.Message.Contains("defeat the final boss"));
        }

        [Test]
        public void Preset_parsing_rejects_a_missing_default_preset()
        {
            var json = "{\"schema\":\"qa-presets/v1\",\"presets\":[{\"name\":\"train\"," +
                       "\"level\":{\"durationSeconds\":90,\"minibossSpawnSeconds\":45}," +
                       "\"character\":{\"healthMultiplier\":1,\"armor\":0}," +
                       "\"episode\":{\"deadlineSeconds\":150,\"timeScale\":1,\"maximumTimeScale\":1}," +
                       "\"observation\":{\"elapsedSecondsScale\":600}}]}";

            Assert.That(
                () => QaPresetDefinitions.Parse(json),
                Throws.InvalidOperationException.With.Message.Contains("must include smoke"));
        }

        [Test]
        public void Preset_parsing_rejects_an_unsupported_schema()
        {
            Assert.That(
                () => QaPresetDefinitions.Parse("{\"schema\":\"qa-presets/v2\",\"presets\":[]}"),
                Throws.InvalidOperationException.With.Message.Contains("must declare schema"));
        }
    }
}
