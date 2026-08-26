using System;
using System.Collections.Generic;
using UnityEngine;

namespace Vampire.QA
{
    public readonly struct QaFaultOptions
    {
        private QaFaultOptions(
            string faultId,
            string scenarioId,
            bool isValid,
            string failureReason)
        {
            FaultId = faultId;
            ScenarioId = scenarioId;
            IsValid = isValid;
            FailureReason = failureReason;
        }

        public string FaultId { get; }
        public string ScenarioId { get; }
        public bool IsValid { get; }
        public bool IsRequested => !string.IsNullOrEmpty(FaultId);
        public bool IsActive => IsValid && !string.IsNullOrEmpty(FaultId);
        public string FailureReason { get; }

        public static QaFaultOptions Parse(string[] arguments)
        {
            string faultId = ReadArgument(arguments, "-qaFault", "");
            string scenarioId = ReadArgument(arguments, "-qaScenarioId", "");
            string bridgeDirectory = ReadArgument(arguments, "-qaBridgeDir", "");
            if (string.IsNullOrEmpty(faultId))
                return new QaFaultOptions("", scenarioId, true, "");
            if (!QaFaultInjection.IsRegistered(faultId))
                return new QaFaultOptions(
                    faultId,
                    scenarioId,
                    false,
                    "Unknown QA fault identifier: " + faultId);
            if (string.IsNullOrEmpty(bridgeDirectory))
                return new QaFaultOptions(
                    faultId,
                    scenarioId,
                    false,
                    "-qaFault requires -qaBridgeDir");
            string expectedScenario = QaFaultInjection.ExpectedScenario(faultId);
            if (!string.Equals(scenarioId, expectedScenario, StringComparison.Ordinal))
                return new QaFaultOptions(
                    faultId,
                    scenarioId,
                    false,
                    $"Fault {faultId} requires scenario {expectedScenario}");
            return new QaFaultOptions(faultId, scenarioId, true, "");
        }

        private static string ReadArgument(string[] arguments, string name, string fallback)
        {
            if (arguments == null)
                return fallback;
            string prefix = name + "=";
            for (int index = 0; index < arguments.Length; index++)
            {
                string argument = arguments[index];
                if (string.Equals(argument, name, StringComparison.Ordinal) && index + 1 < arguments.Length)
                    return arguments[index + 1] ?? fallback;
                if (argument != null && argument.StartsWith(prefix, StringComparison.Ordinal))
                    return argument.Substring(prefix.Length);
            }
            return fallback;
        }
    }

    public static class QaFaultInjection
    {
        public const string HealthRatioOutOfRange = "health_ratio_out_of_range";
        public const string RelativePositionMismatch = "relative_position_mismatch";
        public const string UpgradeAckWithoutEffect = "upgrade_ack_without_effect";
        public const string ChestCollectedWithoutStateTransition =
            "chest_collected_without_state_transition";
        public const string ExperienceLevelDrift = "experience_level_drift";
        public const string CurrencyLeakAcrossRestart = "currency_leak_across_restart";
        public const string HpNotDecreasedOnHit = "hp_not_decreased_on_hit";
        public const string HealthBarDesync = "health_bar_desync";
        public const string ItemEffectNotApplied = "item_effect_not_applied";
        public const string ItemHitRangeMismatch = "item_hit_range_mismatch";
        public const string ExperienceDisplayDrift = "experience_display_drift";
        public const string UpgradeDialogStuckOpen = "upgrade_dialog_stuck_open";
        public const string MovementInputInverted = "movement_input_inverted";
        public const string MonsterSpawningStops = "monster_spawning_stops";
        public const string WeaponCooldownStuckAfterFirstAttack =
            "weapon_cooldown_stuck_after_first_attack";
        public const string ContactDamageCooldownNotReset =
            "contact_damage_cooldown_not_reset";
        public const string ProjectilePassesThroughEnemies =
            "projectile_passes_through_enemies";

        public const float MonsterSpawnStopSeconds = 60f;

        private static readonly Dictionary<string, string> ScenarioByFault =
            new Dictionary<string, string>(StringComparer.Ordinal)
            {
                [HealthRatioOutOfRange] = "easy-health-ratio",
                [RelativePositionMismatch] = "easy-relative-position",
                [UpgradeAckWithoutEffect] = "medium-upgrade-effect",
                [ChestCollectedWithoutStateTransition] = "medium-chest-transition",
                [ExperienceLevelDrift] = "hard-experience-drift",
                [CurrencyLeakAcrossRestart] = "hard-restart-currency",
                [HpNotDecreasedOnHit] = "easy-hp-on-hit",
                [HealthBarDesync] = "easy-view-health",
                [ItemEffectNotApplied] = "medium-item-effect",
                [ItemHitRangeMismatch] = "medium-item-hit-range",
                [ExperienceDisplayDrift] = "hard-exp-conservation",
                [UpgradeDialogStuckOpen] = "medium-upgrade-dialog-close",
                [MovementInputInverted] = "easy-movement-input",
                [MonsterSpawningStops] = "hard-monster-spawn-continuity",
                [WeaponCooldownStuckAfterFirstAttack] = "easy-weapon-cooldown",
                [ContactDamageCooldownNotReset] = "medium-contact-cooldown",
                [ProjectilePassesThroughEnemies] = "medium-projectile-collision",
            };

        private static string activeFaultId = "";

        public static IReadOnlyCollection<string> AllFaultIds => ScenarioByFault.Keys;

        public static bool IsRegistered(string faultId)
        {
            return !string.IsNullOrEmpty(faultId) && ScenarioByFault.ContainsKey(faultId);
        }

        public static string ExpectedScenario(string faultId)
        {
            return ScenarioByFault.TryGetValue(faultId ?? "", out string scenarioId)
                ? scenarioId
                : "";
        }

        public static float HealthRatio(
            string activeFault,
            float health,
            float maximumHealth,
            float reportedRatio)
        {
            return activeFault == HealthRatioOutOfRange ? 1.25f : reportedRatio;
        }

        public static float RelativeX(string activeFault, float relativeX)
        {
            return activeFault == RelativePositionMismatch ? relativeX + 7f : relativeX;
        }

        public static float Experience(string activeFault, int level, float experience)
        {
            return activeFault == ExperienceLevelDrift && level >= 3
                ? experience + 3f
                : experience;
        }

        public static int RestartCoins(string activeFault, int currentCoins, int previousCoins)
        {
            return activeFault == CurrencyLeakAcrossRestart
                ? Math.Max(currentCoins, previousCoins)
                : currentCoins;
        }

        public static int ChestCount(string activeFault, int currentCount, int previousCount)
        {
            return activeFault == ChestCollectedWithoutStateTransition
                ? previousCount
                : currentCount;
        }

        public static bool ShouldApplyUpgrade(string activeFault)
        {
            return activeFault != UpgradeAckWithoutEffect;
        }

        public static void Activate(string faultId)
        {
            activeFaultId = faultId ?? "";
            QaFaultTelemetry.Reset();
        }

        public static bool IsActive(string faultId)
        {
            return string.Equals(activeFaultId, faultId, StringComparison.Ordinal);
        }

        public static bool SkipHealthDecrease => IsActive(HpNotDecreasedOnHit);

        public static bool SkipHealthBarUpdate => IsActive(HealthBarDesync);

        public static bool SuppressItemEffect => IsActive(ItemEffectNotApplied);

        public static bool RestrictItemRange => IsActive(ItemHitRangeMismatch);

        public static bool KeepUpgradeDialogOpen => IsActive(UpgradeDialogStuckOpen);

        public static bool StopWeaponAfterFirstAttack =>
            IsActive(WeaponCooldownStuckAfterFirstAttack);

        public static bool SkipContactDamageCooldownReset =>
            IsActive(ContactDamageCooldownNotReset);

        public static bool AllowProjectilesThroughEnemies =>
            IsActive(ProjectilePassesThroughEnemies);

        public static Vector2 MovementDirection(Vector2 requestedDirection)
        {
            return IsActive(MovementInputInverted)
                ? -requestedDirection
                : requestedDirection;
        }

        public static bool ShouldSpawnRegularMonster(float levelTime)
        {
            return !IsActive(MonsterSpawningStops) || levelTime < MonsterSpawnStopSeconds;
        }

        public static float DisplayedExperience(string activeFault, float experience, int level)
        {
            return activeFault == ExperienceDisplayDrift && level >= 3
                ? experience + 3f
                : experience;
        }
    }

    public static class QaFaultTelemetry
    {
        private const float ContactCooldownViolationRatio = 0.5f;
        private static readonly HashSet<int> PendingProjectileEnemyCollisions =
            new HashSet<int>();
        private static int primaryWeaponSourceId;

        public static int UpgradeCloseAttempts { get; private set; }
        public static int UpgradeCloseCompletions { get; private set; }
        public static int WeaponAttacks { get; private set; }
        public static int PrimaryWeaponAttacks { get; private set; }
        public static float PrimaryWeaponFirstAttackTime { get; private set; }
        public static float PrimaryWeaponLastAttackTime { get; private set; }
        public static float PrimaryWeaponExpectedCooldown { get; private set; }
        public static float PrimaryWeaponMaxIntervalRatio { get; private set; }
        public static int RegularMonstersSpawned { get; private set; }
        public static float RegularLastSpawnTime { get; private set; }
        public static float RegularExpectedSpawnDelay { get; private set; }
        public static bool RegularSpawnScheduleActive { get; private set; }
        public static int ContactDamageHits { get; private set; }
        public static int ContactCooldownResets { get; private set; }
        public static int ContactCooldownViolations { get; private set; }
        public static int ContactIntervalSamples { get; private set; }
        public static float ContactMinimumIntervalRatio { get; private set; }
        public static int ProjectileEnemyCollisions { get; private set; }
        public static int ProjectileEnemyHits { get; private set; }
        public static int ProjectileEnemyConsumptions { get; private set; }

        public static void Reset()
        {
            UpgradeCloseAttempts = 0;
            UpgradeCloseCompletions = 0;
            WeaponAttacks = 0;
            primaryWeaponSourceId = int.MinValue;
            PrimaryWeaponAttacks = 0;
            PrimaryWeaponFirstAttackTime = -1f;
            PrimaryWeaponLastAttackTime = -1f;
            PrimaryWeaponExpectedCooldown = 0f;
            PrimaryWeaponMaxIntervalRatio = 0f;
            RegularMonstersSpawned = 0;
            RegularLastSpawnTime = -1f;
            RegularExpectedSpawnDelay = 0f;
            RegularSpawnScheduleActive = false;
            ContactDamageHits = 0;
            ContactCooldownResets = 0;
            ContactCooldownViolations = 0;
            ContactIntervalSamples = 0;
            ContactMinimumIntervalRatio = -1f;
            ProjectileEnemyCollisions = 0;
            ProjectileEnemyHits = 0;
            ProjectileEnemyConsumptions = 0;
            PendingProjectileEnemyCollisions.Clear();
        }

        public static void RecordUpgradeCloseAttempt()
        {
            UpgradeCloseAttempts++;
        }

        public static void RecordUpgradeCloseCompletion()
        {
            UpgradeCloseCompletions++;
        }

        public static void RecordWeaponAttack(
            int sourceId,
            float timestamp,
            float expectedCooldown)
        {
            WeaponAttacks++;
            if (primaryWeaponSourceId == int.MinValue)
            {
                primaryWeaponSourceId = sourceId;
                PrimaryWeaponFirstAttackTime = timestamp;
            }
            if (sourceId != primaryWeaponSourceId)
                return;
            if (PrimaryWeaponAttacks > 0 && PrimaryWeaponExpectedCooldown > 0f)
            {
                float intervalRatio =
                    (timestamp - PrimaryWeaponLastAttackTime) / PrimaryWeaponExpectedCooldown;
                PrimaryWeaponMaxIntervalRatio = Mathf.Max(
                    PrimaryWeaponMaxIntervalRatio,
                    intervalRatio);
            }
            PrimaryWeaponAttacks++;
            PrimaryWeaponLastAttackTime = timestamp;
            PrimaryWeaponExpectedCooldown = expectedCooldown;
        }

        public static void ObserveRegularMonsterSpawnSchedule(
            float levelTime,
            bool active,
            float expectedDelay)
        {
            RegularSpawnScheduleActive = active;
            RegularExpectedSpawnDelay = active &&
                !float.IsNaN(expectedDelay) &&
                !float.IsInfinity(expectedDelay)
                ? expectedDelay
                : 0f;
        }

        public static void RecordRegularMonsterSpawn(float levelTime)
        {
            RegularMonstersSpawned++;
            RegularLastSpawnTime = levelTime;
        }

        public static void RecordContactDamage(
            float previousTimestamp,
            float timestamp,
            float expectedInterval)
        {
            if (previousTimestamp >= 0f &&
                timestamp >= previousTimestamp && expectedInterval > 0f)
            {
                float intervalRatio = (timestamp - previousTimestamp) / expectedInterval;
                ContactIntervalSamples++;
                ContactMinimumIntervalRatio = ContactIntervalSamples == 1
                    ? intervalRatio
                    : Mathf.Min(ContactMinimumIntervalRatio, intervalRatio);
                if (intervalRatio < ContactCooldownViolationRatio)
                    ContactCooldownViolations++;
            }
            ContactDamageHits++;
        }

        public static void RecordContactDamageCooldownReset()
        {
            ContactCooldownResets++;
        }

        public static void RecordProjectileEnemyCollision(int projectileId)
        {
            ProjectileEnemyCollisions++;
            PendingProjectileEnemyCollisions.Add(projectileId);
        }

        public static void RecordProjectileEnemyHit(int projectileId)
        {
            ProjectileEnemyHits++;
        }

        public static void RecordProjectileConsumedAfterEnemyCollision(int projectileId)
        {
            if (PendingProjectileEnemyCollisions.Remove(projectileId))
                ProjectileEnemyConsumptions++;
        }
    }
}
