using System;

namespace Vampire
{
    /// <summary>
    /// Level timings that every other QA timing is derived from.
    /// </summary>
    /// <remarks>
    /// The level phase thresholds and the chest spawn delay used to be written out as
    /// separate literals, so changing the level duration silently left them behind. They
    /// are derived here instead: the mid phase starts halfway to the miniboss, and the
    /// final boss phase starts when the level duration elapses and the boss spawns.
    /// </remarks>
    public readonly struct QaLevelTimings
    {
        public const float DefaultDurationSeconds = 90f;
        public const float DefaultMinibossSpawnSeconds = 45f;

        public static readonly QaLevelTimings Default =
            new QaLevelTimings(DefaultDurationSeconds, DefaultMinibossSpawnSeconds);

        public QaLevelTimings(float durationSeconds, float minibossSpawnSeconds)
        {
            if (durationSeconds <= 0f)
                throw new ArgumentOutOfRangeException(nameof(durationSeconds), durationSeconds, "Level duration must be positive.");
            if (minibossSpawnSeconds <= 0f)
                throw new ArgumentOutOfRangeException(nameof(minibossSpawnSeconds), minibossSpawnSeconds, "Miniboss spawn time must be positive.");
            if (minibossSpawnSeconds >= durationSeconds)
                throw new ArgumentOutOfRangeException(nameof(minibossSpawnSeconds), minibossSpawnSeconds, "The miniboss must spawn before the level ends.");

            DurationSeconds = durationSeconds;
            MinibossSpawnSeconds = minibossSpawnSeconds;
        }

        public float DurationSeconds { get; }
        public float MinibossSpawnSeconds { get; }

        /// <summary>Level time at which the mid phase begins, halfway to the miniboss.</summary>
        public float MidPhaseSeconds => MinibossSpawnSeconds / 2f;

        /// <summary>Scale the source chest delay so it keeps its position within the level.</summary>
        public float ScaleChestSpawnDelay(float sourceDelay, float sourceDurationSeconds)
        {
            if (sourceDurationSeconds <= 0f)
                throw new ArgumentOutOfRangeException(nameof(sourceDurationSeconds), sourceDurationSeconds, "Source level duration must be positive.");
            return sourceDelay * (DurationSeconds / sourceDurationSeconds);
        }
    }
}
