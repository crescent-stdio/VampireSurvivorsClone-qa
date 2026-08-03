using System;
using System.Collections.Generic;
using System.IO;
using NUnit.Framework;
using UnityEngine;
using Vampire;
using Object = UnityEngine.Object;

namespace Vampire.Tests.EditMode
{
    public class QaEpisodeControllerTests
    {
        private const string ArtifactDirectory = ".superpowers/sdd/2026-08-03-ai-qa-poc/artifacts/task-4/tests";
        private const string CoinsKey = "Coins";

        [SetUp]
        public void SetUp()
        {
            PlayerPrefs.DeleteKey(CoinsKey);
            Time.timeScale = 1f;
        }

        [TearDown]
        public void TearDown()
        {
            PlayerPrefs.DeleteKey(CoinsKey);
            Time.timeScale = 1f;
        }

        [Test]
        public void Bootstrap_seeds_random_before_assigning_the_QA_character()
        {
            var blueprint = ScriptableObject.CreateInstance<CharacterBlueprint>();
            UnityEngine.Random.InitState(999);
            var expectedFirstValue = UnityEngine.Random.value;

            QaEpisodeBootstrap.Prepare(999, blueprint);

            Assert.That(CrossSceneData.CharacterBlueprint, Is.SameAs(blueprint));
            Assert.That(UnityEngine.Random.value, Is.EqualTo(expectedFirstValue));
            CrossSceneData.CharacterBlueprint = null;
            Object.DestroyImmediate(blueprint);
        }

        [Test]
        public void Controller_uses_a_ten_hertz_cadence_and_monotonic_action_acknowledgements()
        {
            var controller = CreateController(new QaAction(Vector2.right));

            controller.AdvanceForTesting(0.09f, 0f, 1f);
            controller.AdvanceForTesting(0.01f, 0f, 1f);
            controller.AdvanceForTesting(0.30f, 0f, 1f);

            Assert.That(controller.LastActionAcknowledgement.Sequence, Is.EqualTo(4));
            Assert.That(controller.LastActionAcknowledgement.Tick, Is.EqualTo(4));
            Assert.That(controller.ActionTrace.Count, Is.EqualTo(4));
            Assert.That(controller.ActionTrace[0].Time, Is.EqualTo(0.1f).Within(0.0001f));
            Object.DestroyImmediate(controller.gameObject);
        }

        [Test]
        public void Controller_limits_control_catch_up_without_losing_deterministic_remainder()
        {
            var controller = CreateController(new QaAction(Vector2.zero));

            controller.AdvanceForTesting(1f, 0f, 1f);

            Assert.That(controller.ActionTrace.Count, Is.EqualTo(QaEpisodeController.MaxCatchUpSteps));
            Assert.That(controller.PendingControlSeconds, Is.EqualTo(0.6f).Within(0.0001f));
            Object.DestroyImmediate(controller.gameObject);
        }

        [Test]
        public void Controller_captures_fixed_size_observations()
        {
            var controller = CreateController(new QaAction(Vector2.zero));

            controller.AdvanceForTesting(0.1f, 0f, 1f);

            Assert.That(controller.LastObservation.NearestEnemyPositions, Has.Length.EqualTo(QaObservation.MaxNearestEnemies));
            Assert.That(controller.LastObservation.AbilityChoices, Has.Length.EqualTo(QaObservation.MaxAbilityChoices));
            Object.DestroyImmediate(controller.gameObject);
        }

        [TestCase(float.NaN)]
        [TestCase(float.PositiveInfinity)]
        public void Oracles_reject_non_finite_observations(float invalidValue)
        {
            var oracles = new QaEpisodeOracles();
            var observation = new QaObservation { PlayerHealth = invalidValue };

            Assert.That(oracles.Evaluate(observation, 1f, 1f, false, false), Is.EqualTo(QaOracleFailure.NonFiniteValue));
        }

        [Test]
        public void Oracles_reject_unknown_pauses_but_allow_known_modal_and_terminal_pauses()
        {
            var oracles = new QaEpisodeOracles();
            var observation = new QaObservation();

            Assert.That(oracles.Evaluate(observation, 1f, 0f, false, false), Is.EqualTo(QaOracleFailure.UnknownTimeScalePause));
            Assert.That(oracles.Evaluate(observation, 1f, 0f, true, false), Is.EqualTo(QaOracleFailure.None));
            Assert.That(oracles.Evaluate(observation, 1f, 0f, false, true), Is.EqualTo(QaOracleFailure.None));
        }

        [Test]
        public void Oracles_detect_stalled_game_time_and_modal_timeout_using_unscaled_time()
        {
            var oracles = new QaEpisodeOracles();
            var observation = new QaObservation();

            Assert.That(oracles.Evaluate(observation, 10f, 1f, false, false), Is.EqualTo(QaOracleFailure.None));
            Assert.That(oracles.Evaluate(observation, 10f, 1f, false, false), Is.EqualTo(QaOracleFailure.None));
            Assert.That(oracles.Evaluate(observation, 10f, 1f, false, false), Is.EqualTo(QaOracleFailure.StalledGameTime));

            oracles.Reset();
            Assert.That(oracles.Evaluate(observation, 1f, 1f, true, false), Is.EqualTo(QaOracleFailure.None));
            Assert.That(oracles.Evaluate(observation, 1f, 1f, true, false), Is.EqualTo(QaOracleFailure.ModalTimeout));
        }

        [Test]
        public void Controller_emits_one_terminal_result_detects_duplicates_and_reloads_once()
        {
            var reloader = new RecordingSceneReloader();
            var controller = CreateController(new QaAction(Vector2.zero), reloader);

            Assert.That(controller.CompleteForTesting(QaEpisodeOutcome.Passed, "passed"), Is.True);
            Assert.That(controller.CompleteForTesting(QaEpisodeOutcome.Error, "duplicate"), Is.False);

            Assert.That(controller.TerminalResult.Outcome, Is.EqualTo(QaEpisodeOutcome.Passed));
            Assert.That(controller.DuplicateTerminalCount, Is.EqualTo(1));
            Assert.That(reloader.SceneNames, Is.EqualTo(new[] { "QA Level" }));
            Object.DestroyImmediate(controller.gameObject);
        }

        [TestCase(LogType.Error)]
        [TestCase(LogType.Assert)]
        [TestCase(LogType.Exception)]
        public void Controller_classifies_unity_error_logs_as_terminal_but_not_warnings(LogType terminalLogType)
        {
            var warningController = CreateController(new QaAction(Vector2.zero));
            warningController.CaptureLogForTesting(LogType.Warning);
            Assert.That(warningController.TerminalResult, Is.Null);
            Object.DestroyImmediate(warningController.gameObject);

            var controller = CreateController(new QaAction(Vector2.zero));
            controller.CaptureLogForTesting(terminalLogType);

            Assert.That(controller.TerminalResult.Outcome, Is.EqualTo(QaEpisodeOutcome.Error));
            Object.DestroyImmediate(controller.gameObject);
        }

        [Test]
        public void Artifact_writer_emits_valid_json_lines_and_a_summary()
        {
            var writer = new QaArtifactWriter(ArtifactDirectory, 7);
            var result = new QaEpisodeResult { Seed = 7, Outcome = QaEpisodeOutcome.Passed, ElapsedSeconds = 1.2f };
            var trace = new List<QaActionTraceEntry>
            {
                new QaActionTraceEntry(1, 1, 0.1f, new QaAction(Vector2.right))
            };
            var telemetry = new List<QaTelemetryEntry>
            {
                new QaTelemetryEntry(1, 0.1f, "observation")
            };

            var paths = writer.Write(result, trace, telemetry);

            Assert.That(JsonUtility.FromJson<QaArtifactLine>(File.ReadAllLines(paths.ActionTracePath)[0]).Schema, Is.EqualTo(QaArtifactWriter.SchemaVersion));
            Assert.That(JsonUtility.FromJson<QaArtifactLine>(File.ReadAllLines(paths.TelemetryPath)[0]).Tick, Is.EqualTo(1));
            Assert.That(JsonUtility.FromJson<QaEpisodeSummary>(File.ReadAllText(paths.SummaryPath)).Outcome, Is.EqualTo(QaEpisodeOutcome.Passed));
        }

        [Test]
        public void Coins_snapshot_is_restored_for_normal_and_abnormal_termination()
        {
            PlayerPrefs.SetInt(CoinsKey, 23);
            var normal = CreateController(new QaAction(Vector2.zero));
            PlayerPrefs.SetInt(CoinsKey, 99);
            normal.CompleteForTesting(QaEpisodeOutcome.Passed, "passed");
            Assert.That(PlayerPrefs.GetInt(CoinsKey), Is.EqualTo(23));
            Object.DestroyImmediate(normal.gameObject);

            PlayerPrefs.DeleteKey(CoinsKey);
            var abnormal = CreateController(new QaAction(Vector2.zero));
            PlayerPrefs.SetInt(CoinsKey, 77);
            abnormal.CompleteForTesting(QaEpisodeOutcome.Error, "error");
            Assert.That(PlayerPrefs.HasKey(CoinsKey), Is.False);
            Object.DestroyImmediate(abnormal.gameObject);
        }

        [Test]
        public void Replay_contract_requires_exact_discrete_events_and_outcome_with_position_tolerance()
        {
            var baseline = RecordedEpisode(QaEpisodeOutcome.Passed, "spawn:a", new Vector2(2f, 3f));
            for (var run = 0; run < 5; run++)
            {
                var candidate = RecordedEpisode(QaEpisodeOutcome.Passed, "spawn:a", new Vector2(2.04f, 3f));
                Assert.That(QaReplayComparator.Compare(baseline, candidate).IsMatch, Is.True, $"run {run}");
            }

            Assert.That(QaReplayComparator.Compare(baseline, RecordedEpisode(QaEpisodeOutcome.Passed, "spawn:b", new Vector2(2f, 3f))).IsMatch, Is.False);
            Assert.That(QaReplayComparator.Compare(baseline, RecordedEpisode(QaEpisodeOutcome.PlayerDied, "spawn:a", new Vector2(2f, 3f))).IsMatch, Is.False);
            Assert.That(QaReplayComparator.Compare(baseline, RecordedEpisode(QaEpisodeOutcome.Passed, "spawn:a", new Vector2(2.06f, 3f))).IsMatch, Is.False);
        }

        private static QaRecordedEpisode RecordedEpisode(QaEpisodeOutcome outcome, string discreteEvent, Vector2 position)
        {
            return new QaRecordedEpisode(outcome, new[] { discreteEvent }, new[] { position });
        }

        private static QaEpisodeController CreateController(QaAction action, IQaSceneReloader reloader = null)
        {
            var gameObject = new GameObject("QA Episode Controller");
            var controller = gameObject.AddComponent<QaEpisodeController>();
            controller.ConfigureForTesting(123, null, "QA Level", ArtifactDirectory, new FixedPolicy(action), reloader ?? new RecordingSceneReloader());
            return controller;
        }

        private sealed class FixedPolicy : IQaPolicy
        {
            private readonly QaAction action;
            public FixedPolicy(QaAction action) { this.action = action; }
            public QaAction Decide(QaObservation observation) { return action; }
        }

        private sealed class RecordingSceneReloader : IQaSceneReloader
        {
            public List<string> SceneNames { get; } = new List<string>();
            public void Reload(string sceneName) { SceneNames.Add(sceneName); }
        }
    }
}
