using System;
using UnityEngine;

public sealed class ScriptedQaPolicy : IQaPolicy
{
    // Threats at or below this distance preempt all non-modal movement.
    public const float ImmediateDangerDistance = 2f;

    public QaAction Decide(QaObservation observation)
    {
        if (observation == null)
        {
            throw new ArgumentNullException(nameof(observation));
        }

        if (observation.IsAbilitySelectionOpen)
        {
            return new QaAction(Vector2.zero, SelectAbilityChoice(observation.AbilityChoices));
        }

        var immediateDanger = FindImmediateDanger(observation);
        if (immediateDanger.HasValue)
        {
            return new QaAction(observation.PlayerPosition - immediateDanger.Value);
        }

        return new QaAction(FindTargetDirection(observation));
    }

    private static Vector2? FindImmediateDanger(QaObservation observation)
    {
        var closestDangerDistanceSquared = ImmediateDangerDistance * ImmediateDangerDistance;
        Vector2? closestDanger = null;
        var enemyCount = Mathf.Min(observation.EnemyCount, observation.NearestEnemyPositions.Length);

        for (var index = 0; index < enemyCount; index++)
        {
            var enemyPosition = observation.NearestEnemyPositions[index];
            var distanceSquared = (enemyPosition - observation.PlayerPosition).sqrMagnitude;
            if (distanceSquared <= closestDangerDistanceSquared &&
                (!closestDanger.HasValue || distanceSquared < closestDangerDistanceSquared))
            {
                closestDanger = enemyPosition;
                closestDangerDistanceSquared = distanceSquared;
            }
        }

        return closestDanger;
    }

    private static Vector2 FindTargetDirection(QaObservation observation)
    {
        Vector2? target = observation.HasCollectibleTarget ? observation.CollectiblePosition : (Vector2?)null;
        var targetDistanceSquared = target.HasValue
            ? (target.Value - observation.PlayerPosition).sqrMagnitude
            : float.PositiveInfinity;

        if (observation.HasChestTarget)
        {
            var chestDistanceSquared = (observation.ChestPosition - observation.PlayerPosition).sqrMagnitude;
            // Equal distances preserve collectible priority for deterministic target selection.
            if (!target.HasValue || chestDistanceSquared < targetDistanceSquared)
            {
                target = observation.ChestPosition;
            }
        }

        return target.HasValue ? target.Value - observation.PlayerPosition : Vector2.zero;
    }

    private static int SelectAbilityChoice(bool[] abilityChoices)
    {
        // Lower indices win so modal selection stays stable across equivalent observations.
        for (var index = 0; index < abilityChoices.Length; index++)
        {
            if (abilityChoices[index])
            {
                return index;
            }
        }

        return -1;
    }
}
