using NUnit.Framework;
using UnityEngine;
using Vampire.QA;

namespace Vampire.Tests.EditMode
{
    public sealed class QaBridgeProtocolTests
    {
        [Test]
        public void Ready_and_observation_models_share_the_protocol_version()
        {
            Assert.That(new QAReadyState().protocol_version, Is.EqualTo(QABridgeModels.ProtocolVersion));
            Assert.That(new QAObservation().protocol_version, Is.EqualTo(QABridgeModels.ProtocolVersion));
            Assert.That(QABridgeModels.ProtocolVersion, Is.EqualTo("1.5"));
        }

        [Test]
        public void Launch_options_parse_explicit_run_and_scenario_identifiers()
        {
            var options = QABridgeLaunchOptions.Parse(new[]
            {
                "player",
                "-qaBridgeDir", "/tmp/bridge",
                "-qaRunId=run-42",
                "-qaScenarioId=scenario-7"
            });

            Assert.That(options.IsRequested, Is.True);
            Assert.That(options.BridgeDirectory, Is.EqualTo("/tmp/bridge"));
            Assert.That(options.RunId, Is.EqualTo("run-42"));
            Assert.That(options.ScenarioId, Is.EqualTo("scenario-7"));
        }

        [Test]
        public void Launch_options_generate_a_run_identifier_for_legacy_invocations()
        {
            var options = QABridgeLaunchOptions.Parse(new[] { "player" });

            Assert.That(options.IsRequested, Is.False);
            Assert.That(options.RunId, Is.Not.Empty);
            Assert.That(options.ScenarioId, Is.Empty);
        }

        [Test]
        public void Simulation_clock_keeps_a_sixty_hertz_gameplay_step_at_accelerated_time_scale()
        {
            int previousCaptureFramerate = Time.captureFramerate;
            try
            {
                QABridgeSimulationClock.Configure(4f);

                Assert.That(Time.captureFramerate, Is.EqualTo(240));
                Assert.That(Time.captureDeltaTime * 4f, Is.EqualTo(1f / 60f).Within(0.0001f));
            }
            finally
            {
                Time.captureFramerate = previousCaptureFramerate;
            }
        }
    }
}
