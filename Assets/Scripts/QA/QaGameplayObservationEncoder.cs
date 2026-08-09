using UnityEngine;

namespace Vampire
{
    /// <summary>
    /// Encodes a fixed 36-float vector in this order: player health, experience, level, alive,
    /// enemy count, eight relative enemy XY pairs, collectible presence/XY, chest presence/XY,
    /// four ability flags, phase, kills, elapsed time, damage, and ability-dialog state.
    /// </summary>
    public static class QaGameplayObservationEncoder
    {
        public const int ObservationSize = 36;
        public const float PositionScale = 20f;
        public const float LevelScale = 100f;
        public const float KillScale = 1000f;
        /// <summary>Elapsed-time saturation point used when no preset supplies one.</summary>
        public const float ElapsedSecondsScale = 600f;

        public static float[] Encode(QaObservation observation)
        {
            return Encode(observation, ElapsedSecondsScale);
        }

        /// <summary>
        /// Encode using a preset's elapsed-time scale.
        /// </summary>
        /// <remarks>
        /// This scale decides what index 33 means, so two presets that disagree on it
        /// produce vectors a policy cannot share. It is part of the preset fingerprint for
        /// exactly that reason.
        /// </remarks>
        public static float[] Encode(QaObservation observation, float elapsedSecondsScale)
        {
            var values = new float[ObservationSize];
            if (observation == null)
                return values;

            if (elapsedSecondsScale <= 0f)
                elapsedSecondsScale = ElapsedSecondsScale;

            var index = 0;
            values[index++] = Ratio(observation.PlayerHealth, observation.PlayerMaxHealth);
            values[index++] = Ratio(observation.PlayerExperience, observation.PlayerNextExperience);
            values[index++] = NormalizeNonNegative(observation.PlayerLevel, LevelScale);
            values[index++] = observation.IsPlayerAlive ? 1f : 0f;
            values[index++] = Mathf.Clamp01((float)Mathf.Clamp(observation.EnemyCount, 0, QaObservation.MaxNearestEnemies) / QaObservation.MaxNearestEnemies);

            for (var enemyIndex = 0; enemyIndex < QaObservation.MaxNearestEnemies; enemyIndex++)
            {
                var enemy = observation.NearestEnemyPositions[enemyIndex] - observation.PlayerPosition;
                values[index++] = NormalizeSigned(enemy.x, PositionScale);
                values[index++] = NormalizeSigned(enemy.y, PositionScale);
            }

            AddTarget(values, ref index, observation.HasCollectibleTarget, observation.CollectiblePosition, observation.PlayerPosition);
            AddTarget(values, ref index, observation.HasChestTarget, observation.ChestPosition, observation.PlayerPosition);

            for (var abilityIndex = 0; abilityIndex < QaObservation.MaxAbilityChoices; abilityIndex++)
                values[index++] = observation.AbilityChoices[abilityIndex] ? 1f : 0f;

            values[index++] = Mathf.Clamp01((float)Mathf.Clamp((int)observation.LevelPhase, 0, (int)QaLevelPhase.Completed) / (int)QaLevelPhase.Completed);
            values[index++] = NormalizeNonNegative(observation.KillCount, KillScale);
            values[index++] = NormalizeNonNegative(observation.ElapsedSeconds, elapsedSecondsScale);
            values[index++] = Ratio(observation.DamageTaken, observation.PlayerMaxHealth);
            values[index] = observation.IsAbilitySelectionOpen ? 1f : 0f;
            return values;
        }

        private static void AddTarget(float[] values, ref int index, bool hasTarget, Vector2 target, Vector2 player)
        {
            values[index++] = hasTarget ? 1f : 0f;
            var relative = hasTarget ? target - player : Vector2.zero;
            values[index++] = NormalizeSigned(relative.x, PositionScale);
            values[index++] = NormalizeSigned(relative.y, PositionScale);
        }

        private static float Ratio(float value, float denominator)
        {
            return denominator > 0f && IsFinite(value) ? Mathf.Clamp01(value / denominator) : 0f;
        }

        private static float NormalizeNonNegative(float value, float scale)
        {
            return IsFinite(value) ? Mathf.Clamp01(value / scale) : 0f;
        }

        private static float NormalizeSigned(float value, float scale)
        {
            return IsFinite(value) ? Mathf.Clamp(value / scale, -1f, 1f) : 0f;
        }

        private static bool IsFinite(float value)
        {
            return !float.IsNaN(value) && !float.IsInfinity(value);
        }
    }
}
