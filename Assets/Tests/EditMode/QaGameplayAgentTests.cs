using System;
using System.IO;
using System.Reflection;
using NUnit.Framework;
using Unity.MLAgents.Actuators;
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
            StringAssert.Contains("-qaSeed=1234", instructions);
            StringAssert.Contains("Time.timeScale = 1", instructions);
        }

        [Test]
        public void EpisodeController_exposes_an_external_agent_control_mode_with_a_single_submission_path()
        {
            var controlModeType = typeof(QaEpisodeController).Assembly.GetType("Vampire.QaControlMode");
            var enableExternal = typeof(QaEpisodeController).GetMethod("EnableExternalAgentControl");
            var submitExternal = typeof(QaEpisodeController).GetMethod("SubmitExternalAction");

            Assert.That(controlModeType, Is.Not.Null, "External control must be an explicit controller mode.");
            Assert.That(enableExternal, Is.Not.Null, "The Agent must explicitly acquire the controller driver.");
            Assert.That(submitExternal, Is.Not.Null, "External actions must use the controller acknowledgement path.");
        }

        [Test]
        public void GameplayAgent_initialize_fails_fast_when_no_controller_is_bound()
        {
            var gameObject = new GameObject("Unbound QA Agent");
            gameObject.SetActive(false);
            var agent = gameObject.AddComponent<QaGameplayAgent>();

            Assert.Throws<InvalidOperationException>(() => agent.Initialize());
            UnityEngine.Object.DestroyImmediate(gameObject);
        }

        [Test]
        public void GameplayAgent_submits_each_received_action_once_through_external_controller_contract()
        {
            var controller = new RecordingController { Observation = new QaObservation { PlayerLevel = 1 } };
            var agent = CreateAgent(controller);

            agent.OnActionReceived(new ActionBuffers(new[] { 3f, 4f }, new[] { 3 }));

            Assert.That(controller.EnableExternalCalls, Is.EqualTo(1));
            Assert.That(controller.SubmittedActions, Has.Count.EqualTo(1));
            Assert.That(controller.SubmittedActions[0].Movement.x, Is.EqualTo(0.6f).Within(0.0001f));
            Assert.That(controller.SubmittedActions[0].Movement.y, Is.EqualTo(0.8f).Within(0.0001f));
            Assert.That(controller.SubmittedActions[0].AbilityChoice, Is.EqualTo(2));
            DestroyAgent(agent);
        }

        [Test]
        public void GameplayAgent_applies_progress_and_terminal_rewards_once_and_resets_episode_state()
        {
            var controller = new RecordingController { Observation = new QaObservation { PlayerLevel = 1, ElapsedSeconds = 0f } };
            var agent = CreateAgent(controller);

            agent.OnActionReceived(new ActionBuffers(new float[2], new[] { 0 }));
            controller.Observation = new QaObservation { PlayerLevel = 2, KillCount = 1, LevelPhase = QaLevelPhase.Mid, ElapsedSeconds = 1f };
            agent.OnActionReceived(new ActionBuffers(new float[2], new[] { 0 }));
            controller.Outcome = QaEpisodeOutcome.Passed;
            agent.OnActionReceived(new ActionBuffers(new float[2], new[] { 0 }));
            agent.OnActionReceived(new ActionBuffers(new float[2], new[] { 0 }));

            Assert.That(agent.Rewards, Is.EqualTo(new[] { 0.13f, 10f }));
            Assert.That(agent.EndEpisodeCalls, Is.EqualTo(1));

            controller.Outcome = QaEpisodeOutcome.InProgress;
            controller.Observation = new QaObservation { PlayerLevel = 1, ElapsedSeconds = 0f };
            agent.OnEpisodeBegin();
            agent.OnActionReceived(new ActionBuffers(new float[2], new[] { 0 }));
            Assert.That(controller.SubmittedActions, Has.Count.EqualTo(3));
            Assert.That(agent.EndEpisodeCalls, Is.EqualTo(1));
            DestroyAgent(agent);
        }

        [TestCase(QaEpisodeOutcome.Passed, 10f)]
        [TestCase(QaEpisodeOutcome.PlayerDied, -2f)]
        [TestCase(QaEpisodeOutcome.Error, -2f)]
        public void GameplayAgent_rejects_actions_already_terminal_before_receipt(QaEpisodeOutcome outcome, float expectedReward)
        {
            var controller = new RecordingController
            {
                Observation = new QaObservation { PlayerLevel = 1 },
                Outcome = outcome
            };
            var agent = CreateAgent(controller);

            agent.OnActionReceived(new ActionBuffers(new[] { 1f, 0f }, new[] { 1 }));
            agent.OnActionReceived(new ActionBuffers(new[] { 1f, 0f }, new[] { 1 }));

            Assert.That(controller.SubmittedActions, Is.Empty);
            Assert.That(agent.Rewards, Is.EqualTo(new[] { expectedReward }));
            Assert.That(agent.EndEpisodeCalls, Is.EqualTo(1));
            DestroyAgent(agent);
        }

        [TestCase(QaEpisodeOutcome.Passed, 10f)]
        [TestCase(QaEpisodeOutcome.PlayerDied, -2f)]
        public void External_terminal_notifies_the_agent_once_before_the_controller_reloads(QaEpisodeOutcome outcome, float expectedReward)
        {
            var controllerObject = new GameObject("Integrated Terminal Controller");
            var reloader = new OrderedReloader();
            var controller = controllerObject.AddComponent<QaEpisodeController>();
            controller.ConfigureForTesting(8123, null, "QA Level", "QAArtifacts", new FixedPolicy(new QaAction(Vector2.zero)), reloader);

            var agentObject = new GameObject("Integrated Terminal Agent");
            agentObject.SetActive(false);
            var agent = agentObject.AddComponent<RecordingAgent>();
            agent.ConfigureControllerForTesting(controller);
            reloader.Agent = agent;
            agent.Initialize();
            agent.OnEpisodeBegin();

            Assert.That(controller.CompleteForTesting(outcome, outcome.ToString()), Is.True);
            Assert.That(controller.CompleteForTesting(QaEpisodeOutcome.Error, "duplicate"), Is.False);

            Assert.That(agent.Rewards, Is.EqualTo(new[] { expectedReward }));
            Assert.That(agent.EndEpisodeCalls, Is.EqualTo(1));
            Assert.That(reloader.RewardCountAtReload, Is.EqualTo(1));
            Assert.That(reloader.EndEpisodeCallsAtReload, Is.EqualTo(1));
            Assert.That(reloader.ReloadCalls, Is.EqualTo(1));
            DestroyAgent(agent);
            UnityEngine.Object.DestroyImmediate(controllerObject);
        }

        [Test]
        public void GameplayAgent_heuristic_writes_deterministic_actions_with_the_discrete_branch_offset()
        {
            var observation = new QaObservation { IsAbilitySelectionOpen = true };
            observation.AbilityChoices[1] = true;
            var agent = CreateAgent(new RecordingController { Observation = observation });
            var continuous = new float[2];
            var discrete = new int[1];
            var actions = new ActionBuffers(continuous, discrete);

            agent.Heuristic(in actions);
            var first = (float[])continuous.Clone();
            var firstBranch = discrete[0];
            agent.Heuristic(in actions);

            Assert.That(continuous, Is.EqualTo(first));
            Assert.That(continuous, Is.EqualTo(new[] { 0f, 0f }));
            Assert.That(discrete[0], Is.EqualTo(firstBranch));
            Assert.That(discrete[0], Is.EqualTo(2));
            DestroyAgent(agent);
        }

        [Test]
        public void EpisodeController_external_mode_suppresses_scripted_ticks_and_acknowledges_the_single_external_action()
        {
            var gameObject = new GameObject("External Controller");
            var controller = gameObject.AddComponent<QaEpisodeController>();
            controller.ConfigureForTesting(17, null, "QA Level", "QAArtifacts", new FixedPolicy(new QaAction(Vector2.right)), new NoOpReloader());
            controller.EnableExternalAgentControl();

            controller.AdvanceForTesting(QaEpisodeController.ControlIntervalSeconds, QaEpisodeController.ControlIntervalSeconds, 1f);
            Assert.That(controller.ActionTrace, Is.Empty);
            Assert.That(controller.SubmitExternalAction(new QaAction(Vector2.left, 1)), Is.True);
            Assert.That(controller.ActionTrace, Has.Count.EqualTo(1));
            Assert.That(controller.LastActionAcknowledgement.Sequence, Is.EqualTo(1));
            Assert.That(controller.LastActionAcknowledgement.Tick, Is.EqualTo(1));
            UnityEngine.Object.DestroyImmediate(gameObject);
        }

        [Test]
        public void EpisodeController_rejects_external_actions_after_level_outcome_before_controller_completion()
        {
            var levelObject = new GameObject("Terminal Level Manager");
            var levelManager = levelObject.AddComponent<LevelManager>();
            Assert.That(levelManager.TryTransitionToOutcome(QaEpisodeOutcome.Passed), Is.True);

            var controllerObject = new GameObject("Terminal External Controller");
            var controller = controllerObject.AddComponent<QaEpisodeController>();
            typeof(QaEpisodeController).GetField("levelManager", BindingFlags.Instance | BindingFlags.NonPublic).SetValue(controller, levelManager);
            controller.ConfigureForTesting(23, null, "QA Level", "QAArtifacts", new FixedPolicy(new QaAction(Vector2.right)), new NoOpReloader());
            controller.EnableExternalAgentControl();

            Assert.That(controller.SubmitExternalAction(new QaAction(Vector2.left)), Is.False);
            Assert.That(controller.ActionTrace, Is.Empty);
            Assert.That(controller.LastActionAcknowledgement.Sequence, Is.Zero);
            Assert.That(controller.LastActionAcknowledgement.Tick, Is.Zero);
            UnityEngine.Object.DestroyImmediate(controllerObject);
            UnityEngine.Object.DestroyImmediate(levelObject);
        }

        [Test]
        public void QaEpisodeSeedParser_prefers_only_valid_explicit_qa_seed_arguments()
        {
            Assert.That(QaEpisodeSeedParser.TryParseQaSeed(new[] { "game", "-qaSeed=1234" }, out var seed), Is.True);
            Assert.That(seed, Is.EqualTo(1234));
            Assert.That(QaEpisodeSeedParser.TryParseQaSeed(new[] { "-qaSeed=bad" }, out _), Is.False);
            Assert.That(QaEpisodeSeedParser.TryParseQaSeed(new string[0], out _), Is.False);
        }

        private static RecordingAgent CreateAgent(RecordingController controller)
        {
            var gameObject = new GameObject("QA Agent");
            gameObject.SetActive(false);
            var agent = gameObject.AddComponent<RecordingAgent>();
            agent.ConfigureControllerForTesting(controller);
            agent.Initialize();
            agent.OnEpisodeBegin();
            return agent;
        }

        private static void DestroyAgent(RecordingAgent agent)
        {
            UnityEngine.Object.DestroyImmediate(agent.gameObject);
        }

        private sealed class RecordingAgent : QaGameplayAgent
        {
            public readonly System.Collections.Generic.List<float> Rewards = new System.Collections.Generic.List<float>();
            public int EndEpisodeCalls { get; private set; }

            protected override void ApplyQaReward(float reward) { Rewards.Add(reward); }
            protected override void EndQaEpisode() { EndEpisodeCalls++; }
        }

        private sealed class RecordingController : IQaGameplayController
        {
            public event Action<QaEpisodeOutcome> TerminalReached { add { } remove { } }
            public readonly System.Collections.Generic.List<QaAction> SubmittedActions = new System.Collections.Generic.List<QaAction>();
            public QaObservation Observation { get; set; }
            public QaEpisodeOutcome Outcome { get; set; }
            public int EnableExternalCalls { get; private set; }
            public QaEpisodeOutcome CurrentOutcome => Outcome;
            public QaObservation CaptureAgentObservation() => Observation;
            public void EnableExternalAgentControl() { EnableExternalCalls++; }
            public bool SubmitExternalAction(QaAction action) { SubmittedActions.Add(action); return true; }
            public bool AcknowledgeTerminal() { return true; }
        }

        private sealed class FixedPolicy : IQaPolicy
        {
            private readonly QaAction action;
            public FixedPolicy(QaAction action) { this.action = action; }
            public QaAction Decide(QaObservation observation) { return action; }
        }

        private sealed class NoOpReloader : IQaSceneReloader
        {
            public void Reload(string sceneName) { }
        }

        private sealed class OrderedReloader : IQaSceneReloader
        {
            public RecordingAgent Agent { get; set; }
            public int RewardCountAtReload { get; private set; }
            public int EndEpisodeCallsAtReload { get; private set; }
            public int ReloadCalls { get; private set; }

            public void Reload(string sceneName)
            {
                RewardCountAtReload = Agent.Rewards.Count;
                EndEpisodeCallsAtReload = Agent.EndEpisodeCalls;
                ReloadCalls++;
            }
        }
    }
}
