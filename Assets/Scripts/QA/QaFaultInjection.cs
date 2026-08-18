using System;
using System.Collections.Generic;

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

        private static readonly Dictionary<string, string> ScenarioByFault =
            new Dictionary<string, string>(StringComparer.Ordinal)
            {
                [HealthRatioOutOfRange] = "easy-health-ratio",
                [RelativePositionMismatch] = "easy-relative-position",
                [UpgradeAckWithoutEffect] = "medium-upgrade-effect",
                [ChestCollectedWithoutStateTransition] = "medium-chest-transition",
                [ExperienceLevelDrift] = "hard-experience-drift",
                [CurrencyLeakAcrossRestart] = "hard-restart-currency"
            };

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
    }
}
