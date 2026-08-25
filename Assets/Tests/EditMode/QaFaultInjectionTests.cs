using NUnit.Framework;
using UnityEngine;
using Vampire.QA;

namespace Vampire.Tests.EditMode
{
    public sealed class QaFaultInjectionTests
    {
        [TearDown]
        public void TearDown()
        {
            QaFaultInjection.Activate("");
        }

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

        [Test]
        public void Gameplay_faults_are_opt_in_and_preserve_unrelated_behavior()
        {
            Vector2 requested = new Vector2(0.75f, -0.25f);

            QaFaultInjection.Activate("");
            Assert.That(QaFaultInjection.MovementDirection(requested), Is.EqualTo(requested));
            Assert.That(QaFaultInjection.ShouldSpawnRegularMonster(60f), Is.True);
            Assert.That(QaFaultInjection.KeepUpgradeDialogOpen, Is.False);
            Assert.That(QaFaultInjection.StopWeaponAfterFirstAttack, Is.False);
            Assert.That(QaFaultInjection.SkipContactDamageCooldownReset, Is.False);
            Assert.That(QaFaultInjection.AllowProjectilesThroughEnemies, Is.False);

            QaFaultInjection.Activate(QaFaultInjection.MovementInputInverted);
            Assert.That(QaFaultInjection.MovementDirection(requested), Is.EqualTo(-requested));

            QaFaultInjection.Activate(QaFaultInjection.MonsterSpawningStops);
            Assert.That(QaFaultInjection.ShouldSpawnRegularMonster(59.99f), Is.True);
            Assert.That(QaFaultInjection.ShouldSpawnRegularMonster(60f), Is.False);

            QaFaultInjection.Activate(QaFaultInjection.UpgradeDialogStuckOpen);
            Assert.That(QaFaultInjection.KeepUpgradeDialogOpen, Is.True);

            QaFaultInjection.Activate(QaFaultInjection.WeaponCooldownStuckAfterFirstAttack);
            Assert.That(QaFaultInjection.StopWeaponAfterFirstAttack, Is.True);

            QaFaultInjection.Activate(QaFaultInjection.ContactDamageCooldownNotReset);
            Assert.That(QaFaultInjection.SkipContactDamageCooldownReset, Is.True);

            QaFaultInjection.Activate(QaFaultInjection.ProjectilePassesThroughEnemies);
            Assert.That(QaFaultInjection.AllowProjectilesThroughEnemies, Is.True);
        }

        [Test]
        public void Qa_telemetry_records_behavior_without_fault_identity()
        {
            QaFaultInjection.Activate("");

            QaFaultTelemetry.RecordUpgradeCloseAttempt();
            QaFaultTelemetry.RecordUpgradeCloseCompletion();
            QaFaultTelemetry.RecordWeaponAttack(11, 1.0f, 0.5f);
            QaFaultTelemetry.RecordWeaponAttack(11, 1.5f, 0.5f);
            QaFaultTelemetry.ObserveRegularMonsterSpawnSchedule(2.0f, true, 0.25f);
            QaFaultTelemetry.RecordRegularMonsterSpawn(2.0f);
            QaFaultTelemetry.RecordContactDamage(-1f, 1.0f, 0.5f);
            QaFaultTelemetry.RecordContactDamageCooldownReset();
            QaFaultTelemetry.RecordContactDamage(1.0f, 1.1f, 0.5f);
            QaFaultTelemetry.RecordContactDamageCooldownReset();
            QaFaultTelemetry.RecordProjectileEnemyCollision(23);
            QaFaultTelemetry.RecordProjectileEnemyHit(23);
            QaFaultTelemetry.RecordProjectileConsumedAfterEnemyCollision(23);

            Assert.That(QaFaultTelemetry.UpgradeCloseAttempts, Is.EqualTo(1));
            Assert.That(QaFaultTelemetry.UpgradeCloseCompletions, Is.EqualTo(1));
            Assert.That(QaFaultTelemetry.WeaponAttacks, Is.EqualTo(2));
            Assert.That(QaFaultTelemetry.PrimaryWeaponAttacks, Is.EqualTo(2));
            Assert.That(QaFaultTelemetry.PrimaryWeaponMaxIntervalRatio, Is.EqualTo(1f));
            Assert.That(QaFaultTelemetry.RegularMonstersSpawned, Is.EqualTo(1));
            Assert.That(QaFaultTelemetry.RegularLastSpawnTime, Is.EqualTo(2f));
            Assert.That(QaFaultTelemetry.RegularExpectedSpawnDelay, Is.EqualTo(0.25f));
            Assert.That(QaFaultTelemetry.RegularSpawnScheduleActive, Is.True);
            Assert.That(QaFaultTelemetry.ContactDamageHits, Is.EqualTo(2));
            Assert.That(QaFaultTelemetry.ContactCooldownResets, Is.EqualTo(2));
            Assert.That(QaFaultTelemetry.ContactCooldownViolations, Is.EqualTo(1));
            Assert.That(QaFaultTelemetry.ContactIntervalSamples, Is.EqualTo(1));
            Assert.That(QaFaultTelemetry.ContactMinimumIntervalRatio, Is.EqualTo(0.2f).Within(0.001f));
            Assert.That(QaFaultTelemetry.ProjectileEnemyCollisions, Is.EqualTo(1));
            Assert.That(QaFaultTelemetry.ProjectileEnemyHits, Is.EqualTo(1));
            Assert.That(QaFaultTelemetry.ProjectileEnemyConsumptions, Is.EqualTo(1));
        }
    }
}
