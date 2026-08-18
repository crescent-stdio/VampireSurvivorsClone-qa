using NUnit.Framework;
using Vampire.QA;

namespace Vampire.Tests.EditMode
{
    public sealed class QaFaultInjectionTests
    {
        [Test]
        public void Unknown_fault_identifier_is_rejected()
        {
            QaFaultOptions options = QaFaultOptions.Parse(new[]
            {
                "player",
                "-qaBridgeDir=/tmp/bridge",
                "-qaScenarioId=easy-health-ratio",
                "-qaFault=unknown-fault"
            });

            Assert.That(options.IsValid, Is.False);
            Assert.That(options.FailureReason, Does.Contain("unknown-fault"));
        }

        [Test]
        public void Fault_without_bridge_is_rejected()
        {
            QaFaultOptions options = QaFaultOptions.Parse(new[]
            {
                "player",
                "-qaScenarioId=easy-health-ratio",
                "-qaFault=health_ratio_out_of_range"
            });

            Assert.That(options.IsValid, Is.False);
            Assert.That(options.IsRequested, Is.True);
            Assert.That(options.FailureReason, Does.Contain("-qaBridgeDir"));
        }

        [Test]
        public void Faults_are_scoped_to_their_designated_scenarios()
        {
            foreach (string faultId in QaFaultInjection.AllFaultIds)
            {
                string expectedScenario = QaFaultInjection.ExpectedScenario(faultId);
                QaFaultOptions valid = QaFaultOptions.Parse(new[]
                {
                    "player",
                    "-qaBridgeDir=/tmp/bridge",
                    $"-qaScenarioId={expectedScenario}",
                    $"-qaFault={faultId}"
                });
                QaFaultOptions invalid = QaFaultOptions.Parse(new[]
                {
                    "player",
                    "-qaBridgeDir=/tmp/bridge",
                    "-qaScenarioId=control-valid-observation",
                    $"-qaFault={faultId}"
                });

                Assert.That(valid.IsValid, Is.True, faultId);
                Assert.That(valid.IsActive, Is.True, faultId);
                Assert.That(invalid.IsValid, Is.False, faultId);
            }
        }

        [Test]
        public void Missing_fault_preserves_observation_values_and_actions()
        {
            const string none = "";
            QaFaultOptions options = QaFaultOptions.Parse(new[]
            {
                "player",
                "-qaBridgeDir=/tmp/bridge",
                "-qaScenarioId=control-valid-observation"
            });

            Assert.That(options.IsRequested, Is.False);
            Assert.That(options.IsValid, Is.True);
            Assert.That(QaFaultInjection.HealthRatio(none, 50f, 100f, 0.5f), Is.EqualTo(0.5f));
            Assert.That(QaFaultInjection.RelativeX(none, 3f), Is.EqualTo(3f));
            Assert.That(QaFaultInjection.Experience(none, 3, 12f), Is.EqualTo(12f));
            Assert.That(QaFaultInjection.RestartCoins(none, 0, 7), Is.EqualTo(0));
            Assert.That(QaFaultInjection.ChestCount(none, 0, 1), Is.EqualTo(0));
            Assert.That(QaFaultInjection.ShouldApplyUpgrade(none), Is.True);
        }

        [Test]
        public void Each_fault_changes_only_its_declared_signal()
        {
            Assert.That(
                QaFaultInjection.HealthRatio(QaFaultInjection.HealthRatioOutOfRange, 50f, 100f, 0.5f),
                Is.GreaterThan(1f));
            Assert.That(
                QaFaultInjection.RelativeX(QaFaultInjection.RelativePositionMismatch, 3f),
                Is.Not.EqualTo(3f));
            Assert.That(
                QaFaultInjection.Experience(QaFaultInjection.ExperienceLevelDrift, 3, 12f),
                Is.Not.EqualTo(12f));
            Assert.That(
                QaFaultInjection.RestartCoins(QaFaultInjection.CurrencyLeakAcrossRestart, 0, 7),
                Is.EqualTo(7));
            Assert.That(
                QaFaultInjection.ChestCount(
                    QaFaultInjection.ChestCollectedWithoutStateTransition,
                    0,
                    1),
                Is.EqualTo(1));
            Assert.That(
                QaFaultInjection.ShouldApplyUpgrade(QaFaultInjection.UpgradeAckWithoutEffect),
                Is.False);
        }
    }
}
