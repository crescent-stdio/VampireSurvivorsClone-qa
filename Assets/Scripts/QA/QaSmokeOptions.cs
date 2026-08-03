using System;
using System.Globalization;

namespace Vampire
{
    public readonly struct QaSmokeOptions
    {
        public const float MaximumGameTimeSeconds = 150f;
        private const float DefaultTimeScale = 1f;
        private const float MaximumTimeScale = 100f;

        private QaSmokeOptions(bool isRequested, float timeScale)
        {
            IsRequested = isRequested;
            TimeScale = timeScale;
        }

        public bool IsRequested { get; }
        public float TimeScale { get; }

        public static QaSmokeOptions Parse(string[] arguments)
        {
            var requested = false;
            var timeScale = DefaultTimeScale;
            if (arguments == null)
                return new QaSmokeOptions(false, timeScale);

            foreach (var argument in arguments)
            {
                if (string.Equals(argument, "-qaMode=smoke", StringComparison.Ordinal))
                {
                    requested = true;
                    continue;
                }

                const string prefix = "-qaTimeScale=";
                if (argument == null || !argument.StartsWith(prefix, StringComparison.Ordinal))
                    continue;

                var value = argument.Substring(prefix.Length);
                if (float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed) &&
                    parsed >= DefaultTimeScale && parsed <= MaximumTimeScale)
                    timeScale = parsed;
            }

            return new QaSmokeOptions(requested, timeScale);
        }
    }

    public static class QaLevelPhaseResolver
    {
        public static QaLevelPhase Resolve(float levelTime)
        {
            if (levelTime >= 90f)
                return QaLevelPhase.FinalBoss;
            if (levelTime >= 45f)
                return QaLevelPhase.Miniboss;
            if (levelTime >= 22.5f)
                return QaLevelPhase.Mid;
            return QaLevelPhase.Early;
        }
    }
}
