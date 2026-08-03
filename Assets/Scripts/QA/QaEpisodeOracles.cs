using UnityEngine;

namespace Vampire
{
    public enum QaOracleFailure
    {
        None,
        NonFiniteValue,
        UnknownTimeScalePause,
        StalledGameTime,
        ModalTimeout
    }

    public sealed class QaEpisodeOracles
    {
        public const float StalledGameTimeTimeoutSeconds = 2f;
        public const float ModalTimeoutSeconds = 2f;

        private bool hasGameTime;
        private float lastGameTime;
        private float stalledSeconds;
        private float modalSeconds;

        public QaOracleFailure Evaluate(QaObservation observation, float gameTime, float timeScale, bool knownModal, bool knownTerminal, float unscaledDeltaSeconds = 1f)
        {
            if (!IsFinite(observation))
                return QaOracleFailure.NonFiniteValue;

            if (Mathf.Approximately(timeScale, 0f) && !knownModal && !knownTerminal)
                return QaOracleFailure.UnknownTimeScalePause;

            var delta = Mathf.Max(0f, unscaledDeltaSeconds);
            if (knownModal)
            {
                modalSeconds += delta;
                if (modalSeconds >= ModalTimeoutSeconds)
                    return QaOracleFailure.ModalTimeout;
            }
            else
            {
                modalSeconds = 0f;
            }

            if (hasGameTime && Mathf.Approximately(gameTime, lastGameTime) && !knownModal && !knownTerminal)
            {
                stalledSeconds += delta;
                if (stalledSeconds >= StalledGameTimeTimeoutSeconds)
                    return QaOracleFailure.StalledGameTime;
            }
            else
            {
                stalledSeconds = 0f;
            }

            lastGameTime = gameTime;
            hasGameTime = true;
            return QaOracleFailure.None;
        }

        public void Reset()
        {
            hasGameTime = false;
            lastGameTime = 0f;
            stalledSeconds = 0f;
            modalSeconds = 0f;
        }

        private static bool IsFinite(QaObservation observation)
        {
            if (observation == null || !IsFinite(observation.PlayerPosition) || !IsFinite(observation.PlayerHealth) ||
                !IsFinite(observation.PlayerMaxHealth) || !IsFinite(observation.PlayerExperience) ||
                !IsFinite(observation.CollectiblePosition) || !IsFinite(observation.ChestPosition) ||
                !IsFinite(observation.ElapsedSeconds) || !IsFinite(observation.DamageTaken))
                return false;

            foreach (var position in observation.NearestEnemyPositions)
                if (!IsFinite(position)) return false;

            return true;
        }

        private static bool IsFinite(Vector2 value)
        {
            return IsFinite(value.x) && IsFinite(value.y);
        }

        private static bool IsFinite(float value)
        {
            return !float.IsNaN(value) && !float.IsInfinity(value);
        }
    }
}
