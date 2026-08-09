using System;
using System.Globalization;

namespace Vampire
{
    public readonly struct QaSmokeOptions
    {
        /// <summary>Deadline used when no preset supplies one. Matches the smoke preset.</summary>
        public const float MaximumGameTimeSeconds = 150f;

        /// <summary>Time-scale ceiling used when no preset supplies one. Matches the smoke preset.</summary>
        public const float MaximumSupportedTimeScale = 4f;

        public const string DefaultPresetName = "smoke";
        private const float DefaultTimeScale = 1f;

        private QaSmokeOptions(
            bool isRequested,
            bool isLlmRequested,
            bool isEvaluationRequested,
            float timeScale,
            string presetName,
            string failureReason)
        {
            IsRequested = isRequested;
            IsLlmRequested = isLlmRequested;
            IsEvaluationRequested = isEvaluationRequested;
            TimeScale = timeScale;
            PresetName = presetName;
            FailureReason = failureReason;
        }

        public bool IsRequested { get; }
        public bool IsLlmRequested { get; }
        public bool IsEvaluationRequested { get; }
        public float TimeScale { get; }

        /// <summary>Preset requested with <c>-qaPreset=</c>; defaults to the smoke baseline.</summary>
        public string PresetName { get; }

        public string FailureReason { get; }
        public bool IsValid => string.IsNullOrEmpty(FailureReason);

        /// <summary>Parse using the default smoke time-scale ceiling.</summary>
        public static QaSmokeOptions Parse(string[] arguments)
        {
            return Parse(arguments, MaximumSupportedTimeScale);
        }

        /// <summary>
        /// Parse using a preset's time-scale ceiling.
        /// </summary>
        /// <remarks>
        /// The ceiling is a parameter rather than a constant because training runs far
        /// above the scripted controller's four-tick catch-up limit: that limit only
        /// applies to the controller's own loop, and training is driven by the agent's
        /// DecisionRequester instead.
        /// </remarks>
        public static QaSmokeOptions Parse(string[] arguments, float maximumTimeScale)
        {
            var requested = false;
            var llmRequested = false;
            var evaluationRequested = false;
            var timeScale = DefaultTimeScale;
            var presetName = DefaultPresetName;
            var failureReason = string.Empty;
            if (arguments == null)
                return new QaSmokeOptions(false, false, false, timeScale, presetName, failureReason);

            foreach (var argument in arguments)
            {
                const string presetPrefix = "-qaPreset=";
                if (argument != null && argument.StartsWith(presetPrefix, StringComparison.Ordinal))
                {
                    var requestedPreset = argument.Substring(presetPrefix.Length);
                    if (string.IsNullOrEmpty(requestedPreset))
                        failureReason = "UnsupportedQaPreset";
                    else
                        presetName = requestedPreset;
                    continue;
                }

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
                    parsed < DefaultTimeScale || parsed > maximumTimeScale)
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

            return new QaSmokeOptions(requested, llmRequested, evaluationRequested, timeScale, presetName, failureReason);
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
