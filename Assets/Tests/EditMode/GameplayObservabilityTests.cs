using NUnit.Framework;
using UnityEngine;
using System.Reflection;

namespace Vampire.Tests.EditMode
{
    public class GameplayObservabilityTests
    {
        [Test]
        public void Character_exposes_read_only_runtime_state()
        {
            var gameObject = new GameObject("Character");
            gameObject.AddComponent<Rigidbody2D>();
            var visual = new GameObject("Visual");
            visual.transform.SetParent(gameObject.transform);
            visual.AddComponent<SpriteRenderer>();
            visual.AddComponent<SpriteAnimator>();
            var character = gameObject.AddComponent<Character>();
            var blueprint = ScriptableObject.CreateInstance<CharacterBlueprint>();
            blueprint.hp = 40f;
            SetPrivateField(character, "characterBlueprint", blueprint);
            SetPrivateField(character, "currentHealth", 17f);
            SetPrivateField(character, "currentExp", 8f);
            SetPrivateField(character, "nextLevelExp", 13f);

            Assert.That(character.CurrentHealth, Is.EqualTo(17f));
            Assert.That(character.MaxHealth, Is.EqualTo(40f));
            Assert.That(character.CurrentExperience, Is.EqualTo(8f));
            Assert.That(character.NextExperience, Is.EqualTo(13f));
            Assert.That(character.IsAlive, Is.True);

            Object.DestroyImmediate(blueprint);
            Object.DestroyImmediate(gameObject);
        }

        [Test]
        public void LevelManager_starts_uninitialized_with_an_in_progress_outcome()
        {
            var gameObject = new GameObject("Level Manager");
            var levelManager = gameObject.AddComponent<LevelManager>();

            Assert.That(levelManager.Initialized, Is.False);
            Assert.That(levelManager.LevelTime, Is.Zero);
            Assert.That(levelManager.Outcome, Is.EqualTo(QaEpisodeOutcome.InProgress));

            Object.DestroyImmediate(gameObject);
        }

        [Test]
        public void LevelManager_accepts_only_the_first_terminal_outcome()
        {
            var gameObject = new GameObject("Level Manager");
            var levelManager = gameObject.AddComponent<LevelManager>();

            Assert.That(levelManager.TryTransitionToOutcome(QaEpisodeOutcome.Passed), Is.True);
            Assert.That(levelManager.TryTransitionToOutcome(QaEpisodeOutcome.PlayerDied), Is.False);
            Assert.That(levelManager.Outcome, Is.EqualTo(QaEpisodeOutcome.Passed));

            Object.DestroyImmediate(gameObject);
        }

        [Test]
        public void EntityManager_exposes_zero_counts_before_initialization()
        {
            var gameObject = new GameObject("Entity Manager");
            var entityManager = gameObject.AddComponent<EntityManager>();

            Assert.That(entityManager.ChestCount, Is.Zero);
            Assert.That(entityManager.EntityCount, Is.Zero);

            Object.DestroyImmediate(gameObject);
        }

        private static void SetPrivateField(object target, string fieldName, object value)
        {
            var field = target.GetType().GetField(fieldName, BindingFlags.Instance | BindingFlags.NonPublic);
            Assert.That(field, Is.Not.Null, $"Missing private field: {fieldName}");
            field.SetValue(target, value);
        }
    }
}
