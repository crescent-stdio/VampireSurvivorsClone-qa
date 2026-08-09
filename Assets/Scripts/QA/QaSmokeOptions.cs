using System;
using System.Globalization;

namespace Vampire
{
    public readonly struct QaSmokeOptions
    {
        public const float MaximumGameTimeSeconds = 150f;
        public const float MaximumSupportedTimeScale = 4f;
        private const float DefaultTimeScale = 1f;

        private QaSmokeOptions(bool isRequested, bool isLlmRequested, bool isEvaluationRequested, float timeScale, string failureReason)
        {
            IsRequested = isRequested;
            IsLlmRequested = isLlmRequested;
            IsEvaluationRequested = isEvaluationRequested;
            TimeScale = timeScale;
            FailureReason = failureReason;
        }

        public bool IsRequested { get; }
        public bool IsLlmRequested { get; }
        public bool IsEvaluationRequested { get; }
        public float TimeScale { get; }
        public string FailureReason { get; }
        public bool IsValid => string.IsNullOrEmpty(FailureReason);

        public static QaSmokeOptions Parse(string[] arguments)
        {
            var requested = false;
            var llmRequested = false;
            var evaluationRequested = false;
            var timeScale = DefaultTimeScale;
            var failureReason = string.Empty;
            if (arguments == null)
                return new QaSmokeOptions(false, false, false, timeScale, failureReason);

            foreach (var argument in arguments)
            {
                if (string.Equals(argument, "-qaMode=smoke", StringComparison.Ordinal))
                {
                    requested = true;
                    continue;
                }

                if (string.Equals(argument, "-qaMode=llm", StringComparison.Ordinal))
                {
                    llmRequested = true;
                    continue;
                }

                if (string.Equals(argument, "-qaMode=evaluate", StringComparison.Ordinal))
                {
                    evaluationRequested = true;
                    continue;
                }

                const string prefix = "-qaTimeScale=";
                if (argument == null || !argument.StartsWith(prefix, StringComparison.Ordinal))
                    continue;

                var value = argument.Substring(prefix.Length);
                if (!float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed) ||
                    parsed < DefaultTimeScale || parsed > MaximumSupportedTimeScale)
                {
                    failureReason = "UnsupportedSmokeTimeScale";
                    continue;
                }

                timeScale = parsed;
            }

            if (llmRequested && (failureReason.Length > 0 || Math.Abs(timeScale - DefaultTimeScale) > float.Epsilon))
            {
                timeScale = DefaultTimeScale;
                failureReason = "UnsupportedLlmTimeScale";
            }

            else if (evaluationRequested && (failureReason.Length > 0 || Math.Abs(timeScale - DefaultTimeScale) > float.Epsilon))
            {
                timeScale = DefaultTimeScale;
                failureReason = "UnsupportedEvaluationTimeScale";
            }

            return new QaSmokeOptions(requested, llmRequested, evaluationRequested, timeScale, failureReason);
        }
    }

    public static class QaLevelPhaseResolver
    {
        public static QaLevelPhase Resolve(float levelTime)
        {
            return Resolve(levelTime, QaLevelTimings.Default);
        }

        public static QaLevelPhase Resolve(float levelTime, QaLevelTimings timings)
        {
            if (levelTime >= timings.DurationSeconds)
                return QaLevelPhase.FinalBoss;
            if (levelTime >= timings.MinibossSpawnSeconds)
                return QaLevelPhase.Miniboss;
            if (levelTime >= timings.MidPhaseSeconds)
                return QaLevelPhase.Mid;
            return QaLevelPhase.Early;
        }
    }
}
