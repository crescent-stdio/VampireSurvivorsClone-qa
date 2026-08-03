using System;
using System.IO;
using NUnit.Framework;
using UnityEngine;

namespace Vampire.Tests.EditMode
{
    public class QaGameplayAgentTests
    {
        [Test]
        public void ActionMapper_clamps_two_continuous_actions_and_converts_discrete_branch_offset()
        {
            var mapperType = typeof(QaAction).Assembly.GetType("Vampire.QaAgentActionMapper");

            Assert.That(mapperType, Is.Not.Null, "The ML-Agents action adapter must be available.");

            var map = mapperType.GetMethod("Map");
            Assert.That(map, Is.Not.Null, "The ML-Agents action adapter must map action buffers.");

            var action = (QaAction)map.Invoke(null, new object[]
            {
                new[] { 3f, 4f },
                new[] { 3 }
            });

            Assert.That(action.Movement.x, Is.EqualTo(0.6f).Within(0.0001f));
            Assert.That(action.Movement.y, Is.EqualTo(0.8f).Within(0.0001f));
            Assert.That(action.AbilityChoice, Is.EqualTo(2));
        }

        [Test]
        public void ActionMapper_converts_missing_or_invalid_buffers_to_a_safe_no_op()
        {
            QaAction missing = null;
            Assert.DoesNotThrow(() => missing = QaAgentActionMapper.Map(null, null));

            var nonFinite = QaAgentActionMapper.Map(
                new[] { float.NaN },
                new[] { 9 });

            Assert.That(missing.Movement, Is.EqualTo(Vector2.zero));
            Assert.That(missing.AbilityChoice, Is.EqualTo(-1));
            Assert.That(nonFinite.Movement, Is.EqualTo(Vector2.zero));
            Assert.That(nonFinite.AbilityChoice, Is.EqualTo(-1));
        }

        [Test]
        public void ObservationEncoder_returns_the_documented_fixed_vector_order()
        {
            var observation = new QaObservation
            {
                PlayerPosition = new Vector2(10f, 20f), PlayerHealth = 50f, PlayerMaxHealth = 100f,
                PlayerExperience = 25f, PlayerNextExperience = 50f, PlayerLevel = 4, IsPlayerAlive = true,
                EnemyCount = 2, HasCollectibleTarget = true, CollectiblePosition = new Vector2(30f, 40f),
                LevelPhase = QaLevelPhase.Mid, KillCount = 12, ElapsedSeconds = 30f, DamageTaken = 25f,
                IsAbilitySelectionOpen = true
            };
            observation.NearestEnemyPositions[0] = new Vector2(30f, 0f);
            observation.NearestEnemyPositions[1] = new Vector2(5f, 30f);
            observation.AbilityChoices[0] = true;
            observation.AbilityChoices[2] = true;

            var values = QaGameplayObservationEncoder.Encode(observation);

            Assert.That(values, Has.Length.EqualTo(36));
            Assert.That(values[0], Is.EqualTo(0.5f));
            Assert.That(values[1], Is.EqualTo(0.5f));
            Assert.That(values[2], Is.EqualTo(0.04f));
            Assert.That(values[3], Is.EqualTo(1f));
            Assert.That(values[4], Is.EqualTo(0.25f));
            Assert.That(values[5], Is.EqualTo(1f));
            Assert.That(values[6], Is.EqualTo(-1f));
            Assert.That(values[7], Is.EqualTo(-0.25f));
            Assert.That(values[8], Is.EqualTo(0.5f));
            Assert.That(values[21], Is.EqualTo(1f));
            Assert.That(values[22], Is.EqualTo(1f));
            Assert.That(values[23], Is.EqualTo(1f));
            Assert.That(values[24], Is.EqualTo(0f));
            Assert.That(values[27], Is.EqualTo(1f));
            Assert.That(values[28], Is.EqualTo(0f));
            Assert.That(values[29], Is.EqualTo(1f));
            Assert.That(values[30], Is.EqualTo(0f));
            Assert.That(values[31], Is.EqualTo(0.25f));
            Assert.That(values[32], Is.EqualTo(0.012f));
            Assert.That(values[33], Is.EqualTo(0.05f));
            Assert.That(values[34], Is.EqualTo(0.25f));
            Assert.That(values[35], Is.EqualTo(1f));
        }

        [Test]
        public void RewardTracker_applies_exact_progress_damage_and_stall_deltas_without_rollback_exploits()
        {
            var tracker = new QaRewardTracker();

            Assert.That(tracker.Evaluate(new QaRewardSnapshot(1, 0, 0, 0f, 0f)), Is.EqualTo(0f));
            Assert.That(tracker.Evaluate(new QaRewardSnapshot(3, 2, 1, 12.5f, 3f)), Is.EqualTo(0.19f).Within(0.0001f));
            Assert.That(tracker.Evaluate(new QaRewardSnapshot(1, 1, 1, 2f, 4f)), Is.EqualTo(0f));
            Assert.That(tracker.Evaluate(new QaRewardSnapshot(1, 1, 1, 2f, 18f)), Is.EqualTo(-0.02f));
            Assert.That(tracker.Evaluate(new QaRewardSnapshot(1, 1, 2, 2f, 19f)), Is.EqualTo(0.02f));
        }

        [Test]
        public void RewardTracker_maps_terminal_outcomes_to_exact_rewards()
        {
            Assert.That(QaRewardTracker.TerminalReward(QaEpisodeOutcome.Passed), Is.EqualTo(10f));
            Assert.That(QaRewardTracker.TerminalReward(QaEpisodeOutcome.PlayerDied), Is.EqualTo(-2f));
            Assert.That(QaRewardTracker.TerminalReward(QaEpisodeOutcome.Error), Is.EqualTo(-2f));
            Assert.That(QaRewardTracker.TerminalReward(QaEpisodeOutcome.TimedOut), Is.EqualTo(0f));
        }

        [Test]
        public void GameplayAgent_exposes_the_release20_action_and_observation_contract()
        {
            Assert.That(typeof(QaGameplayAgent).BaseType.FullName, Is.EqualTo("Unity.MLAgents.Agent"));
            Assert.That(QaGameplayAgent.ContinuousActionCount, Is.EqualTo(2));
            Assert.That(QaGameplayAgent.AbilityBranchSize, Is.EqualTo(5));
            Assert.That(QaGameplayAgent.DiscreteBranchSizes, Is.EqualTo(new[] { 5 }));
            Assert.That(QaGameplayAgent.BehaviorName, Is.EqualTo("QaGameplay"));
            Assert.That(QaGameplayObservationEncoder.ObservationSize, Is.EqualTo(36));
        }

        [Test]
        public void PpoConfiguration_matches_the_agent_behavior_and_release20_required_fields()
        {
            var projectPath = Directory.GetParent(Application.dataPath).FullName;
            var yaml = File.ReadAllText(Path.Combine(projectPath, "config", "qa-ppo.yaml"));

            StringAssert.Contains(QaGameplayAgent.BehaviorName + ":", yaml);
            StringAssert.Contains("trainer_type: ppo", yaml);
            StringAssert.Contains("batch_size:", yaml);
            StringAssert.Contains("buffer_size:", yaml);
            StringAssert.Contains("learning_rate:", yaml);
            StringAssert.Contains("beta:", yaml);
            StringAssert.Contains("epsilon:", yaml);
            StringAssert.Contains("lambd:", yaml);
            StringAssert.Contains("num_epoch:", yaml);
            StringAssert.Contains("normalize:", yaml);
            StringAssert.Contains("extrinsic:", yaml);
            StringAssert.Contains("max_steps:", yaml);
            StringAssert.Contains("time_horizon:", yaml);
            StringAssert.Contains("summary_freq:", yaml);
            StringAssert.Contains("keep_checkpoints:", yaml);
            StringAssert.Contains("checkpoint_interval:", yaml);
            StringAssert.Contains("threaded:", yaml);
        }

        [Test]
        public void EvaluationInstructions_require_inference_with_a_fixed_seed_and_normal_time_scale()
        {
            var projectPath = Directory.GetParent(Application.dataPath).FullName;
            var instructions = File.ReadAllText(Path.Combine(projectPath, "config", "qa-ppo-evaluation.md"));

            StringAssert.Contains("--inference", instructions);
            StringAssert.Contains("--seed=1234", instructions);
            StringAssert.Contains("Time.timeScale = 1", instructions);
        }
    }
}
