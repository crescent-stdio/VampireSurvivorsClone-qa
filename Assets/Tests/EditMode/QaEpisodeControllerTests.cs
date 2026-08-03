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
            var writer = new QaArtifactWriter(ArtifactDirectory + "/round2-success-" + Guid.NewGuid().ToString("N"), 7, null, "schema");
            var result = new QaEpisodeResult { Seed = 7, Outcome = QaEpisodeOutcome.Passed, ElapsedSeconds = 1.2f };
            var trace = new List<QaActionTraceEntry>
            {
                new QaActionTraceEntry(1, 1, 0.1f, new QaAction(Vector2.right))
            };
            var telemetry = new List<QaTelemetryEntry>
            {
                new QaTelemetryEntry(1, 0.1f, "observation")
            };

            var write = writer.Write(result, trace, telemetry, new QaRecordedEpisode(QaEpisodeOutcome.Passed, new string[0], new Vector2[0]));
            Assert.That(write.Success, Is.True, write.FailureReason);
            var paths = write.Paths;

            Assert.That(JsonUtility.FromJson<QaArtifactLine>(File.ReadAllLines(paths.ActionTracePath)[0]).Schema, Is.EqualTo(QaArtifactWriter.SchemaVersion));
            Assert.That(JsonUtility.FromJson<QaArtifactLine>(File.ReadAllLines(paths.TelemetryPath)[0]).Tick, Is.EqualTo(1));
            Assert.That(JsonUtility.FromJson<QaEpisodeSummary>(File.ReadAllText(paths.SummaryPath)).Outcome, Is.EqualTo(QaEpisodeOutcome.Passed));
            Assert.That(Path.GetFileName(Path.GetDirectoryName(paths.ActionTracePath)), Is.EqualTo("episode-00000007-schema"));
            Assert.That(Directory.GetFiles(Path.GetDirectoryName(paths.ActionTracePath)), Has.Length.EqualTo(3));
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

        [Test]
        public void Controller_recorder_captures_observations_and_known_discrete_events_for_five_runs()
        {
            QaRecordedEpisode baseline = null;
            for (var run = 0; run < 5; run++)
            {
                var controller = CreateController(new QaAction(Vector2.right));
                controller.AdvanceForTesting(0.1f, 0f, 1f);
                controller.RecordDiscreteEvent("spawn:seeded");
                controller.CompleteForTesting(QaEpisodeOutcome.Passed, "passed");

                var recorded = controller.RecordedEpisode;
                Assert.That(recorded.Positions, Has.Count.EqualTo(1));
                Assert.That(recorded.DiscreteEvents, Does.Contain("action-ack:1:1"));
                Assert.That(recorded.DiscreteEvents, Does.Contain("phase:0"));
                Assert.That(recorded.DiscreteEvents, Does.Contain("modal:0"));
                Assert.That(recorded.DiscreteEvents, Does.Contain("terminal:Passed"));
                Assert.That(recorded.Outcome, Is.EqualTo(QaEpisodeOutcome.Passed));
                if (baseline != null)
                    Assert.That(QaReplayComparator.Compare(baseline, recorded).IsMatch, Is.True, $"run {run}");
                baseline = recorded;
                Object.DestroyImmediate(controller.gameObject);
            }
        }

        [Test]
        public void Replay_comparator_rejects_non_finite_values_and_length_mismatches_but_includes_the_tolerance_boundary()
        {
            var baseline = new QaRecordedEpisode(QaEpisodeOutcome.Passed, new[] { "event" }, new[] { Vector2.zero });

            Assert.That(QaReplayComparator.Compare(baseline, new QaRecordedEpisode(QaEpisodeOutcome.Passed, new string[0], new[] { Vector2.zero })).IsMatch, Is.False);
            Assert.That(QaReplayComparator.Compare(baseline, new QaRecordedEpisode(QaEpisodeOutcome.Passed, new[] { "event" }, new Vector2[0])).IsMatch, Is.False);
            Assert.That(QaReplayComparator.Compare(baseline, new QaRecordedEpisode(QaEpisodeOutcome.Passed, new[] { "other" }, new[] { Vector2.zero })).IsMatch, Is.False);
            Assert.That(QaReplayComparator.Compare(baseline, new QaRecordedEpisode(QaEpisodeOutcome.Passed, new[] { "event" }, new[] { new Vector2(float.NaN, 0f) })).IsMatch, Is.False);
            Assert.That(QaReplayComparator.Compare(baseline, new QaRecordedEpisode(QaEpisodeOutcome.Passed, new[] { "event" }, new[] { new Vector2(float.PositiveInfinity, 0f) })).IsMatch, Is.False);
            Assert.That(QaReplayComparator.Compare(baseline, new QaRecordedEpisode(QaEpisodeOutcome.Passed, new[] { "event" }, new[] { new Vector2(0.05f, 0f) })).IsMatch, Is.True);
        }

        [Test]
        public void Observation_capture_sorts_live_monsters_by_distance_and_uses_a_bounded_enemy_count()
        {
            var monsters = new FastList<Monster>();
            var objects = new List<GameObject>();
            foreach (var position in new[] { new Vector2(3f, 0f), new Vector2(1f, 0f), new Vector2(-1f, 0f), new Vector2(2f, 0f), new Vector2(4f, 0f), new Vector2(5f, 0f), new Vector2(6f, 0f), new Vector2(7f, 0f), new Vector2(8f, 0f) })
            {
                var gameObject = new GameObject("Monster");
                gameObject.SetActive(false);
                gameObject.transform.position = position;
                monsters.Add(gameObject.AddComponent<Monster>());
                objects.Add(gameObject);
            }

            var observation = new QaObservation { NearestEnemyPositions = { } };
            QaObservationCapture.PopulateNearestEnemies(observation, Vector2.zero, monsters);

            Assert.That(observation.EnemyCount, Is.EqualTo(QaObservation.MaxNearestEnemies));
            Assert.That(observation.NearestEnemyPositions[0], Is.EqualTo(new Vector2(-1f, 0f)));
            Assert.That(observation.NearestEnemyPositions[1], Is.EqualTo(new Vector2(1f, 0f)));
            Assert.That(observation.NearestEnemyPositions[7], Is.EqualTo(new Vector2(7f, 0f)));
            Assert.That(observation.NearestEnemyPositions, Has.Length.EqualTo(QaObservation.MaxNearestEnemies));
            QaObservationCapture.PopulateNearestEnemies(observation, Vector2.zero, null);
            Assert.That(observation.EnemyCount, Is.Zero);
            Assert.That(observation.NearestEnemyPositions[0], Is.EqualTo(Vector2.zero));
            foreach (var gameObject in objects)
                Object.DestroyImmediate(gameObject);
        }

        [Test]
        public void Artifact_writer_rejects_escaping_paths_and_reports_io_failures_without_throwing()
        {
            var result = new QaEpisodeResult { Seed = 3, Outcome = QaEpisodeOutcome.Error };
            var recorded = new QaRecordedEpisode(QaEpisodeOutcome.Error, new string[0], new Vector2[0]);
            var rejected = new QaArtifactWriter("../escape", 3).Write(result, new List<QaActionTraceEntry>(), new List<QaTelemetryEntry>(), recorded);
            var rooted = new QaArtifactWriter("/escape", 3).Write(result, new List<QaActionTraceEntry>(), new List<QaTelemetryEntry>(), recorded);
            var failed = new QaArtifactWriter("tests/focused-io", 3, new ThrowingArtifactFileSystem()).Write(result, new List<QaActionTraceEntry>(), new List<QaTelemetryEntry>(), recorded);

            Assert.That(rejected.Success, Is.False);
            Assert.That(rooted.Success, Is.False);
            Assert.That(failed.Success, Is.False);
        }

        [Test]
        public void Artifact_failures_do_not_block_terminal_cleanup_or_the_single_reload()
        {
            PlayerPrefs.SetInt(CoinsKey, 31);
            var reloader = new RecordingSceneReloader();
            var controller = CreateController(new QaAction(Vector2.zero), reloader);
            controller.SetArtifactWriterForTesting(new QaArtifactWriter("tests/focused-io", 123, new ThrowingArtifactFileSystem()));
            PlayerPrefs.SetInt(CoinsKey, 99);

            Assert.That(controller.CompleteForTesting(QaEpisodeOutcome.Error, "write-failure"), Is.True);
            Assert.That(controller.LastArtifactWrite.Success, Is.False);
            Assert.That(controller.TerminalResult.Outcome, Is.EqualTo(QaEpisodeOutcome.Error));
            Assert.That(PlayerPrefs.GetInt(CoinsKey), Is.EqualTo(31));
            Assert.That(controller.IsLogSubscribed, Is.False);
            Assert.That(reloader.SceneNames, Is.EqualTo(new[] { "QA Level" }));
            Assert.That(controller.CompleteForTesting(QaEpisodeOutcome.Passed, "duplicate"), Is.False);
            Assert.That(reloader.SceneNames, Has.Count.EqualTo(1));
            Object.DestroyImmediate(controller.gameObject);
        }

        [TestCase(2)]
        [TestCase(3)]
        public void Artifact_writer_does_not_publish_any_final_episode_file_when_a_post_action_write_fails(int failingWrite)
        {
            var fileSystem = new StagingArtifactFileSystem { FailOnWrite = failingWrite };
            var writer = new QaArtifactWriter("tests/round2-telemetry-failure", 22, fileSystem);

            var write = writer.Write(
                new QaEpisodeResult { Seed = 22, Outcome = QaEpisodeOutcome.Error },
                new List<QaActionTraceEntry> { new QaActionTraceEntry(1, 1, 0.1f, new QaAction(Vector2.zero)) },
                new List<QaTelemetryEntry> { new QaTelemetryEntry(1, 0.1f, "telemetry") },
                new QaRecordedEpisode(QaEpisodeOutcome.Error, new string[0], new Vector2[0]));

            Assert.That(write.Success, Is.False);
            Assert.That(fileSystem.PublishedFileCount, Is.Zero);
            Assert.That(fileSystem.MoveDirectoryCalls, Is.Zero);
            Assert.That(fileSystem.DeleteDirectoryCalls, Is.EqualTo(1));
        }

        [Test]
        public void Artifact_writer_rejects_an_existing_episode_directory_before_writing_or_overwriting()
        {
            var fileSystem = new StagingArtifactFileSystem { FinalDirectoryAlreadyExists = true };
            var writer = new QaArtifactWriter("tests/round2-collision", 23, fileSystem, "existing-run");

            var write = writer.Write(
                new QaEpisodeResult { Seed = 23, Outcome = QaEpisodeOutcome.Error },
                new List<QaActionTraceEntry>(),
                new List<QaTelemetryEntry>(),
                new QaRecordedEpisode(QaEpisodeOutcome.Error, new string[0], new Vector2[0]));

            Assert.That(write.Success, Is.False);
            Assert.That(fileSystem.CreateDirectoryCalls, Is.Zero);
            Assert.That(fileSystem.WriteCalls, Is.Zero);
            Assert.That(fileSystem.PublishedFileCount, Is.Zero);
        }

        [Test]
        public void Artifact_writer_publishes_five_same_seed_runs_to_distinct_default_episode_directories()
        {
            var fileSystem = new StagingArtifactFileSystem();
            var finalDirectories = new List<string>();
            for (var run = 0; run < 5; run++)
            {
                var write = new QaArtifactWriter("tests/round3-default-runs", 31, fileSystem).Write(
                    new QaEpisodeResult { Seed = 31, Outcome = QaEpisodeOutcome.Passed },
                    new List<QaActionTraceEntry>(),
                    new List<QaTelemetryEntry>(),
                    new QaRecordedEpisode(QaEpisodeOutcome.Passed, new string[0], new Vector2[0]));

                Assert.That(write.Success, Is.True, write.FailureReason);
                Assert.That(fileSystem.PublishedFileCount, Is.EqualTo(3));
                finalDirectories.Add(write.Paths.EpisodeDirectory);
            }

            Assert.That(new HashSet<string>(finalDirectories), Has.Count.EqualTo(5));
        }

        [Test]
        public void Artifact_writer_rejects_a_reused_explicit_episode_id_without_overwriting()
        {
            var fileSystem = new StagingArtifactFileSystem();
            var first = new QaArtifactWriter("tests/round3-explicit", 32, fileSystem, "run-a");
            var second = new QaArtifactWriter("tests/round3-explicit", 32, fileSystem, "run-a");
            var result = new QaEpisodeResult { Seed = 32, Outcome = QaEpisodeOutcome.Passed };
            var recorded = new QaRecordedEpisode(QaEpisodeOutcome.Passed, new string[0], new Vector2[0]);

            Assert.That(first.Write(result, new List<QaActionTraceEntry>(), new List<QaTelemetryEntry>(), recorded).Success, Is.True);
            var duplicate = second.Write(result, new List<QaActionTraceEntry>(), new List<QaTelemetryEntry>(), recorded);

            Assert.That(duplicate.Success, Is.False);
            Assert.That(fileSystem.MoveDirectoryCalls, Is.EqualTo(1));
        }

        [Test]
        public void Artifact_writer_rejects_unsafe_explicit_episode_ids()
        {
            var write = new QaArtifactWriter("tests/round3-id-validation", 33, new StagingArtifactFileSystem(), "bad/../run").Write(
                new QaEpisodeResult { Seed = 33, Outcome = QaEpisodeOutcome.Error },
                new List<QaActionTraceEntry>(),
                new List<QaTelemetryEntry>(),
                new QaRecordedEpisode(QaEpisodeOutcome.Error, new string[0], new Vector2[0]));

            Assert.That(write.Success, Is.False);
        }

        [Test]
        public void Controller_records_five_same_seed_episodes_without_artifact_collisions()
        {
            var outputDirectory = ArtifactDirectory + "/round3-controller-" + Guid.NewGuid().ToString("N");
            var episodeDirectories = new List<string>();
            for (var run = 0; run < 5; run++)
            {
                var controller = CreateController(new QaAction(Vector2.zero), null, outputDirectory);
                Assert.That(controller.CompleteForTesting(QaEpisodeOutcome.Passed, "passed"), Is.True);
                Assert.That(controller.LastArtifactWrite.Success, Is.True, controller.LastArtifactWrite.FailureReason);
                episodeDirectories.Add(controller.LastArtifactWrite.Paths.EpisodeDirectory);
                Object.DestroyImmediate(controller.gameObject);
            }

            Assert.That(new HashSet<string>(episodeDirectories), Has.Count.EqualTo(5));
        }

        private static QaRecordedEpisode RecordedEpisode(QaEpisodeOutcome outcome, string discreteEvent, Vector2 position)
        {
            return new QaRecordedEpisode(outcome, new[] { discreteEvent }, new[] { position });
        }

        private static QaEpisodeController CreateController(QaAction action, IQaSceneReloader reloader = null, string outputDirectory = ArtifactDirectory)
        {
            var gameObject = new GameObject("QA Episode Controller");
            var controller = gameObject.AddComponent<QaEpisodeController>();
            controller.ConfigureForTesting(123, null, "QA Level", outputDirectory, new FixedPolicy(action), reloader ?? new RecordingSceneReloader());
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

        private sealed class ThrowingArtifactFileSystem : IQaArtifactFileSystem
        {
            public bool DirectoryExists(string path) { return false; }
            public void CreateDirectory(string path) { throw new IOException("forced"); }
            public void WriteAllText(string path, string contents) { throw new IOException("forced"); }
            public void MoveReplace(string sourcePath, string destinationPath) { throw new IOException("forced"); }
            public void DeleteFile(string path) { }
            public void MoveDirectory(string sourcePath, string destinationPath) { throw new IOException("forced"); }
            public void DeleteDirectory(string path) { }
        }

        private sealed class StagingArtifactFileSystem : IQaArtifactFileSystem
        {
            public int FailOnWrite { get; set; }
            public bool FinalDirectoryAlreadyExists { get; set; }
            public int CreateDirectoryCalls { get; private set; }
            public int WriteCalls { get; private set; }
            public int PublishedFileCount { get; private set; }
            public int MoveDirectoryCalls { get; private set; }
            public int DeleteDirectoryCalls { get; private set; }

            public bool DirectoryExists(string path) { return FinalDirectoryAlreadyExists || PublishedDirectories.Contains(path); }
            public void CreateDirectory(string path) { CreateDirectoryCalls++; }
            public void WriteAllText(string path, string contents)
            {
                WriteCalls++;
                if (WriteCalls == FailOnWrite) throw new IOException("forced");
            }
            public void MoveReplace(string sourcePath, string destinationPath)
            {
                if (FinalDirectoryAlreadyExists) throw new IOException("collision");
                PublishedFileCount++;
            }
            public void DeleteFile(string path) { }
            public void MoveDirectory(string sourcePath, string destinationPath)
            {
                if (FinalDirectoryAlreadyExists) throw new IOException("collision");
                MoveDirectoryCalls++;
                PublishedFileCount = 3;
                PublishedDirectories.Add(destinationPath);
            }
            public void DeleteDirectory(string path) { DeleteDirectoryCalls++; }
            public List<string> PublishedDirectories { get; } = new List<string>();
        }
    }
}
