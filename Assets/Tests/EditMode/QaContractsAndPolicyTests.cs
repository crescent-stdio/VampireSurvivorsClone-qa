using System;
using NUnit.Framework;
using UnityEngine;

namespace Vampire.Tests.EditMode
{
    public class QaContractsAndPolicyTests
    {
        [Test]
        public void QaAction_clamps_movement_to_unit_circle()
        {
            var action = new QaAction(new Vector2(3f, 4f));

            Assert.That(action.Movement.x, Is.EqualTo(0.6f).Within(0.0001f));
            Assert.That(action.Movement.y, Is.EqualTo(0.8f).Within(0.0001f));
            Assert.That(action.AbilityChoice, Is.EqualTo(-1));
        }

        [Test]
        public void QaAction_preserves_analog_movement_within_unit_circle()
        {
            var action = new QaAction(new Vector2(0.3f, 0.4f));

            Assert.That(action.Movement, Is.EqualTo(new Vector2(0.3f, 0.4f)));
        }

        [TestCase(-2)]
        [TestCase(4)]
        public void QaAction_rejects_invalid_ability_choices(int invalidChoice)
        {
            Assert.That(
                () => new QaAction(Vector2.zero, invalidChoice),
                Throws.TypeOf<ArgumentOutOfRangeException>());
        }

        [Test]
        public void QaObservation_initializes_fixed_size_defaults()
        {
            var observation = new QaObservation();

            Assert.That(observation.NearestEnemyPositions, Has.Length.EqualTo(8));
            Assert.That(observation.AbilityChoices, Has.Length.EqualTo(4));
            Assert.That(observation.EnemyCount, Is.EqualTo(0));
            Assert.That(observation.HasCollectibleTarget, Is.False);
            Assert.That(observation.HasChestTarget, Is.False);
            Assert.That(observation.IsAbilitySelectionOpen, Is.False);
        }

        [Test]
        public void ScriptedQaPolicy_retreats_from_the_nearest_immediate_danger()
        {
            var observation = new QaObservation
            {
                PlayerPosition = Vector2.zero,
                EnemyCount = 2,
                HasCollectibleTarget = true,
                CollectiblePosition = Vector2.right * 10f
            };
            observation.NearestEnemyPositions[0] = Vector2.right * 1.5f;
            observation.NearestEnemyPositions[1] = Vector2.left;

            var action = new ScriptedQaPolicy().Decide(observation);

            Assert.That(action.Movement, Is.EqualTo(Vector2.right));
            Assert.That(action.AbilityChoice, Is.EqualTo(-1));
        }

        [Test]
        public void ScriptedQaPolicy_approaches_nearest_target_and_breaks_equal_distance_ties_by_collectible()
        {
            var observation = new QaObservation
            {
                PlayerPosition = Vector2.zero,
                HasCollectibleTarget = true,
                CollectiblePosition = Vector2.up * 3f,
                HasChestTarget = true,
                ChestPosition = Vector2.right * 3f
            };

            var action = new ScriptedQaPolicy().Decide(observation);

            Assert.That(action.Movement, Is.EqualTo(Vector2.up));
            Assert.That(action.AbilityChoice, Is.EqualTo(-1));
        }

        [Test]
        public void ScriptedQaPolicy_selects_lowest_available_ability_when_modal_is_open()
        {
            var observation = new QaObservation
            {
                IsAbilitySelectionOpen = true
            };
            observation.AbilityChoices[1] = true;
            observation.AbilityChoices[3] = true;

            var action = new ScriptedQaPolicy().Decide(observation);

            Assert.That(action.Movement, Is.EqualTo(Vector2.zero));
            Assert.That(action.AbilityChoice, Is.EqualTo(1));
        }

        [Test]
        public void ScriptedQaPolicy_returns_the_same_action_for_the_same_observation()
        {
            var observation = new QaObservation
            {
                PlayerPosition = new Vector2(1f, 1f),
                HasChestTarget = true,
                ChestPosition = new Vector2(4f, 5f)
            };

            var policy = new ScriptedQaPolicy();
            var firstAction = policy.Decide(observation);
            var secondAction = policy.Decide(observation);

            Assert.That(secondAction.Movement, Is.EqualTo(firstAction.Movement));
            Assert.That(secondAction.AbilityChoice, Is.EqualTo(firstAction.AbilityChoice));
        }

        [Test]
        public void QaEpisodeResult_round_trips_through_unity_json()
        {
            var result = new QaEpisodeResult
            {
                Seed = 12345,
                Outcome = QaEpisodeOutcome.Passed,
                ElapsedSeconds = 91.5f,
                KillCount = 42,
                FinalLevel = 7,
                FailureReason = string.Empty
            };

            var roundTripped = JsonUtility.FromJson<QaEpisodeResult>(JsonUtility.ToJson(result));

            Assert.That(roundTripped.Seed, Is.EqualTo(12345));
            Assert.That(roundTripped.Outcome, Is.EqualTo(QaEpisodeOutcome.Passed));
            Assert.That(roundTripped.ElapsedSeconds, Is.EqualTo(91.5f));
            Assert.That(roundTripped.KillCount, Is.EqualTo(42));
            Assert.That(roundTripped.FinalLevel, Is.EqualTo(7));
            Assert.That(roundTripped.FailureReason, Is.EqualTo(string.Empty));
        }
    }
}
