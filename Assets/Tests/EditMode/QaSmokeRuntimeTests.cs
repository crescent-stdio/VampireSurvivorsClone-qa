using System.Collections.Generic;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;

namespace Vampire.Tests.EditMode
{
    public sealed class QaSmokeRuntimeTests
    {
        [Test]
        public void Smoke_options_require_explicit_mode_and_accept_a_bounded_time_scale()
        {
            var options = QaSmokeOptions.Parse(new[] { "player", "-qaMode=smoke", "-qaTimeScale=10" });

            Assert.That(options.IsRequested, Is.True);
            Assert.That(options.TimeScale, Is.EqualTo(10f));
            Assert.That(QaSmokeOptions.Parse(new[] { "player" }).IsRequested, Is.False);
            Assert.That(QaSmokeOptions.Parse(new[] { "player", "-qaMode=smoke", "-qaTimeScale=0" }).TimeScale, Is.EqualTo(1f));
            Assert.That(QaSmokeOptions.Parse(new[] { "player", "-qaMode=smoke", "-qaTimeScale=101" }).TimeScale, Is.EqualTo(1f));
        }

        [Test]
        public void Qa_level_phase_resolver_marks_the_miniboss_and_final_boss_boundaries()
        {
            Assert.That(QaLevelPhaseResolver.Resolve(0f), Is.EqualTo(QaLevelPhase.Early));
            Assert.That(QaLevelPhaseResolver.Resolve(22.5f), Is.EqualTo(QaLevelPhase.Mid));
            Assert.That(QaLevelPhaseResolver.Resolve(45f), Is.EqualTo(QaLevelPhase.Miniboss));
            Assert.That(QaLevelPhaseResolver.Resolve(90f), Is.EqualTo(QaLevelPhase.FinalBoss));
        }

        [Test]
        public void Observation_capture_selects_the_nearest_collectible_and_chest_targets()
        {
            var collectibleNear = new GameObject("Collectible near").AddComponent<Coin>();
            var collectibleFar = new GameObject("Collectible far").AddComponent<Coin>();
            var chestNear = new GameObject("Chest near").AddComponent<Chest>();
            var chestFar = new GameObject("Chest far").AddComponent<Chest>();
            collectibleNear.transform.position = new Vector3(2f, 0f);
            collectibleFar.transform.position = new Vector3(4f, 0f);
            chestNear.transform.position = new Vector3(0f, 3f);
            chestFar.transform.position = new Vector3(0f, 5f);
            var observation = new QaObservation();

            QaObservationCapture.PopulateNearestTargets(
                observation,
                Vector2.zero,
                new[] { collectibleFar, collectibleNear },
                new[] { chestFar, chestNear });

            Assert.That(observation.HasCollectibleTarget, Is.True);
            Assert.That(observation.CollectiblePosition, Is.EqualTo(new Vector2(2f, 0f)));
            Assert.That(observation.HasChestTarget, Is.True);
            Assert.That(observation.ChestPosition, Is.EqualTo(new Vector2(0f, 3f)));
            Object.DestroyImmediate(collectibleNear.gameObject);
            Object.DestroyImmediate(collectibleFar.gameObject);
            Object.DestroyImmediate(chestNear.gameObject);
            Object.DestroyImmediate(chestFar.gameObject);
        }

        [Test]
        public void Qa_character_is_an_isolated_durable_copy_for_long_running_smoke_episodes()
        {
            var source = AssetDatabase.LoadAssetAtPath<CharacterBlueprint>("Assets/Blueprints/Characters/Main Character Blueprint.asset");
            var qaCharacter = AssetDatabase.LoadAssetAtPath<CharacterBlueprint>("Assets/Blueprints/QA/QA Main Character.asset");

            Assert.That(qaCharacter, Is.Not.Null);
            Assert.That(qaCharacter.hp, Is.GreaterThanOrEqualTo(source.hp * 10f));
            Assert.That(qaCharacter.armor, Is.GreaterThanOrEqualTo(100));
            Assert.That(source.hp, Is.EqualTo(100f));
            Assert.That(source.armor, Is.Zero);
        }

        [Test]
        public void Smoke_mode_exits_once_and_captures_an_anomaly_screenshot_only_for_failures()
        {
            var failureController = CreateController(7001);
            var failureExit = new RecordingProcessExit();
            var screenshots = new RecordingScreenshotCapture();
            failureController.ConfigureSmokeForTesting(failureExit, screenshots);

            Assert.That(failureController.CompleteForTesting(QaEpisodeOutcome.Error, "oracle"), Is.True);
            Assert.That(failureExit.ExitCodes, Is.EqualTo(new[] { 1 }));
            Assert.That(screenshots.Paths, Is.EqualTo(new[] { "QAArtifacts/screenshots/seed-00007001.png" }));
            Assert.That(failureController.CompleteForTesting(QaEpisodeOutcome.Error, "duplicate"), Is.False);
            Assert.That(failureExit.ExitCodes, Has.Count.EqualTo(1));

            Object.DestroyImmediate(failureController.gameObject);

            var passedController = CreateController(7002);
            var passedExit = new RecordingProcessExit();
            var passedScreenshots = new RecordingScreenshotCapture();
            passedController.ConfigureSmokeForTesting(passedExit, passedScreenshots);

            Assert.That(passedController.CompleteForTesting(QaEpisodeOutcome.Passed, "passed"), Is.True);
            Assert.That(passedExit.ExitCodes, Is.EqualTo(new[] { 0 }));
            Assert.That(passedScreenshots.Paths, Is.Empty);

            Object.DestroyImmediate(passedController.gameObject);
        }

        [Test]
        public void Smoke_mode_classifies_the_episode_deadline_as_a_timeout()
        {
            var controller = CreateController(7003);
            var processExit = new RecordingProcessExit();
            var screenshots = new RecordingScreenshotCapture();
            controller.ConfigureSmokeForTesting(processExit, screenshots);

            controller.AdvanceForTesting(0.1f, QaSmokeOptions.MaximumGameTimeSeconds, 1f);

            Assert.That(controller.TerminalResult.Outcome, Is.EqualTo(QaEpisodeOutcome.TimedOut));
            Assert.That(controller.TerminalResult.FailureReason, Is.EqualTo("SmokeDeadline"));
            Assert.That(processExit.ExitCodes, Is.EqualTo(new[] { 1 }));
            Assert.That(screenshots.Paths, Has.Count.EqualTo(1));
            Object.DestroyImmediate(controller.gameObject);
        }

        [Test]
        public void Smoke_mode_keeps_the_control_cadence_in_game_time_when_accelerated()
        {
            var controller = CreateController(7004);
            controller.ConfigureSmokeForTesting(new RecordingProcessExit(), new RecordingScreenshotCapture());

            controller.AdvanceForTesting(0.1f, 1f, 10f);

            Assert.That(controller.ActionTrace, Has.Count.EqualTo(QaEpisodeController.MaxCatchUpSteps));
            Assert.That(controller.PendingControlSeconds, Is.EqualTo(0.6f).Within(0.0001f));
            Object.DestroyImmediate(controller.gameObject);
        }

        private static QaEpisodeController CreateController(int seed)
        {
            var gameObject = new GameObject("Smoke controller");
            var controller = gameObject.AddComponent<QaEpisodeController>();
            controller.ConfigureForTesting(seed, null, "QA Gameplay", "QAArtifacts", new ScriptedQaPolicy(), new RecordingSceneReloader());
            return controller;
        }

        private sealed class RecordingProcessExit : IQaProcessExit
        {
            public List<int> ExitCodes { get; } = new List<int>();
            public void Exit(int code) { ExitCodes.Add(code); }
        }

        private sealed class RecordingScreenshotCapture : IQaFailureScreenshotCapture
        {
            public List<string> Paths { get; } = new List<string>();
            public void Capture(string relativePath) { Paths.Add(relativePath); }
        }

        private sealed class RecordingSceneReloader : IQaSceneReloader
        {
            public void Reload(string sceneName) { }
        }
    }
}
