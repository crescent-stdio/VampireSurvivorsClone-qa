using System.Collections.Generic;
using System.IO;
using NUnit.Framework;
using UnityEngine;

namespace Vampire.Tests.EditMode
{
    public class QaReplayRuntimeTests
    {
        [Test]
        public void Replay_trace_feeds_actions_and_exits_after_a_matching_terminal_comparison()
        {
            var trace = new QaReplayTrace
            {
                Outcome = QaEpisodeOutcome.Passed,
                DiscreteEvents = new[] { "action-ack:1:1", "phase:0", "modal:0", "terminal:Passed" },
                Positions = new[] { Vector2.zero },
                Actions = new[] { new QaActionTraceEntry(1, 1, 0.1f, new QaAction(Vector2.right)) }
            };
            var gameObject = new GameObject("Replay controller");
            var controller = gameObject.AddComponent<QaEpisodeController>();
            var reloader = new RecordingSceneReloader();
            var exit = new RecordingProcessExit();
            controller.ConfigureForTesting(1, null, "QA Gameplay", "QAArtifacts", new ScriptedQaPolicy(), reloader);
            controller.ConfigureReplayForTesting(trace, exit);
            controller.EnableExternalAgentControl();

            controller.AdvanceForTesting(QaEpisodeController.ControlIntervalSeconds, 0f, 1f);
            Assert.That(controller.ControlMode, Is.EqualTo(QaControlMode.Scripted));
            Assert.That(controller.ActionTrace, Has.Count.EqualTo(1));
            Assert.That(controller.ActionTrace[0].MovementX, Is.EqualTo(1f));
            Assert.That(controller.CompleteForTesting(QaEpisodeOutcome.Passed, "passed"), Is.True);
            Assert.That(controller.LastReplayComparison.IsMatch, Is.True);
            Assert.That(exit.ExitCodes, Is.EqualTo(new[] { 0 }));
            Assert.That(reloader.SceneNames, Is.Empty);
            Object.DestroyImmediate(gameObject);
        }

        [Test]
        public void Replay_trace_parser_requires_replay_mode_and_a_json_trace_path()
        {
            Assert.That(QaReplayTrace.IsReplayRequested(new[] { "player", "-qaMode=replay" }), Is.True);
            Assert.That(QaReplayTrace.TryLoadFromCommandLine(new[] { "player", "-qaMode=replay" }, out _, out var error), Is.False);
            StringAssert.Contains("-qaReplayPath", error);
        }

        [Test]
        public void Replay_trace_loads_the_existing_summary_artifact_schema()
        {
            var projectRoot = Directory.GetParent(Application.dataPath).FullName;
            var artifactDirectory = Path.Combine(projectRoot, "QAArtifacts", "task6-replay-test");
            var summaryPath = Path.Combine(artifactDirectory, "summary.json");
            Directory.CreateDirectory(artifactDirectory);
            File.WriteAllText(summaryPath, JsonUtility.ToJson(new QaEpisodeSummary
            {
                Schema = QaArtifactWriter.SchemaVersion,
                Outcome = QaEpisodeOutcome.Passed,
                ReplayActions = new[] { new QaActionTraceEntry(1, 1, 0.1f, new QaAction(Vector2.left)) },
                ReplayDiscreteEvents = new[] { "terminal:Passed" },
                ReplayPositions = new[] { Vector2.zero }
            }));

            try
            {
                Assert.That(QaReplayTrace.TryLoad(summaryPath, out var trace, out var error), Is.True, error);
                Assert.That(new QaReplayPolicy(trace.Actions).Decide(new QaObservation()).Movement, Is.EqualTo(Vector2.left));
                Assert.That(trace.ToRecordedEpisode().Outcome, Is.EqualTo(QaEpisodeOutcome.Passed));
            }
            finally
            {
                File.Delete(summaryPath);
                Directory.Delete(artifactDirectory);
            }
        }

        private sealed class RecordingSceneReloader : IQaSceneReloader
        {
            public List<string> SceneNames { get; } = new List<string>();
            public void Reload(string sceneName) { SceneNames.Add(sceneName); }
        }

        private sealed class RecordingProcessExit : IQaProcessExit
        {
            public List<int> ExitCodes { get; } = new List<int>();
            public void Exit(int code) { ExitCodes.Add(code); }
        }
    }
}
