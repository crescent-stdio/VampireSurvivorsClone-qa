using System.Collections.Generic;
using UnityEngine;

namespace Vampire
{
    public readonly struct QaRewardSnapshot
    {
        public QaRewardSnapshot(int level, int killCount, int stateBucket, float damageTaken, float elapsedSeconds)
        {
            Level = level;
            KillCount = killCount;
            StateBucket = stateBucket;
            DamageTaken = damageTaken;
            ElapsedSeconds = elapsedSeconds;
        }

        public int Level { get; }
        public int KillCount { get; }
        public int StateBucket { get; }
        public float DamageTaken { get; }
        public float ElapsedSeconds { get; }
    }

    /// <summary>
    /// Rewards only monotonic counter increases. Damage is penalized by
    /// -DamagePenaltyMagnitude * clamp01(deltaDamage / DamagePenaltyScale).
    /// A stalled episode receives one penalty per full interval after the grace period.
    /// TimedOut is terminal with zero terminal reward; stalled remains non-terminal.
    /// </summary>
    public sealed class QaRewardTracker
    {
        public const float PassedReward = 10f;
        public const float FailureReward = -2f;
        public const float LevelIncrementReward = 0.1f;
        public const float KillReward = 0.01f;
        public const float NewStateBucketReward = 0.02f;
        public const float DamagePenaltyMagnitude = 0.1f;
        public const float DamagePenaltyScale = 25f;
        public const float StalledProgressPenalty = -0.02f;
        public const float StalledProgressGraceSeconds = 10f;
        public const float StalledProgressPenaltyIntervalSeconds = 5f;

        private readonly HashSet<int> rewardedStateBuckets = new HashSet<int>();
        private bool hasSnapshot;
        private int highestLevel;
        private int highestKillCount;
        private float highestDamageTaken;
        private float lastMeaningfulProgressSeconds;
        private int lastStallPenaltyInterval;

        public void Reset()
        {
            rewardedStateBuckets.Clear();
            hasSnapshot = false;
            highestLevel = 0;
            highestKillCount = 0;
            highestDamageTaken = 0f;
            lastMeaningfulProgressSeconds = 0f;
            lastStallPenaltyInterval = 0;
        }

        public float Evaluate(QaRewardSnapshot snapshot)
        {
            var elapsedSeconds = FiniteNonNegative(snapshot.ElapsedSeconds);
            if (!hasSnapshot)
            {
                hasSnapshot = true;
                highestLevel = Mathf.Max(0, snapshot.Level);
                highestKillCount = Mathf.Max(0, snapshot.KillCount);
                highestDamageTaken = FiniteNonNegative(snapshot.DamageTaken);
                lastMeaningfulProgressSeconds = elapsedSeconds;
                rewardedStateBuckets.Add(snapshot.StateBucket);
                return 0f;
            }

            var reward = 0f;
            var madeProgress = false;
            reward += RewardIncrease(snapshot.Level, ref highestLevel, LevelIncrementReward, ref madeProgress);
            reward += RewardIncrease(snapshot.KillCount, ref highestKillCount, KillReward, ref madeProgress);
            if (rewardedStateBuckets.Add(snapshot.StateBucket))
            {
                reward += NewStateBucketReward;
                madeProgress = true;
            }

            var damageTaken = FiniteNonNegative(snapshot.DamageTaken);
            if (damageTaken > highestDamageTaken)
            {
                reward -= DamagePenaltyMagnitude * Mathf.Clamp01((damageTaken - highestDamageTaken) / DamagePenaltyScale);
                highestDamageTaken = damageTaken;
            }

            if (madeProgress)
            {
                lastMeaningfulProgressSeconds = elapsedSeconds;
                lastStallPenaltyInterval = 0;
                return reward;
            }

            var stalledSeconds = Mathf.Max(0f, elapsedSeconds - lastMeaningfulProgressSeconds - StalledProgressGraceSeconds);
            var penaltyIntervals = Mathf.FloorToInt(stalledSeconds / StalledProgressPenaltyIntervalSeconds);
            if (penaltyIntervals > lastStallPenaltyInterval)
            {
                reward += (penaltyIntervals - lastStallPenaltyInterval) * StalledProgressPenalty;
                lastStallPenaltyInterval = penaltyIntervals;
            }

            return reward;
        }

        public static float TerminalReward(QaEpisodeOutcome outcome)
        {
            if (outcome == QaEpisodeOutcome.Passed)
                return PassedReward;

            return outcome == QaEpisodeOutcome.PlayerDied || outcome == QaEpisodeOutcome.Error
                ? FailureReward
                : 0f;
        }

        private static float RewardIncrease(int value, ref int highestValue, float rewardPerIncrease, ref bool madeProgress)
        {
            if (value <= highestValue)
                return 0f;

            var delta = value - highestValue;
            highestValue = value;
            madeProgress = true;
            return delta * rewardPerIncrease;
        }

        private static float FiniteNonNegative(float value)
        {
            return float.IsNaN(value) || float.IsInfinity(value) ? 0f : Mathf.Max(0f, value);
        }
    }
}
