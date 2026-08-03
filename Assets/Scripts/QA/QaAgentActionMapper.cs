using UnityEngine;

namespace Vampire
{
    public static class QaAgentActionMapper
    {
        public static QaAction Map(float[] continuousActions, int[] discreteActions)
        {
            return new QaAction(
                new Vector2(ReadFiniteValue(continuousActions, 0), ReadFiniteValue(continuousActions, 1)),
                ReadAbilityChoice(discreteActions));
        }

        private static float ReadFiniteValue(float[] actions, int index)
        {
            if (actions == null || actions.Length <= index)
                return 0f;

            var value = actions[index];
            return float.IsNaN(value) || float.IsInfinity(value) ? 0f : value;
        }

        private static int ReadAbilityChoice(int[] actions)
        {
            if (actions == null || actions.Length == 0)
                return -1;

            var branchValue = actions[0];
            return branchValue >= 1 && branchValue <= QaObservation.MaxAbilityChoices
                ? branchValue - 1
                : -1;
        }
    }
}
