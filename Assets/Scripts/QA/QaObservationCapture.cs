using System;
using System.Collections.Generic;
using UnityEngine;

namespace Vampire
{
    public static class QaObservationCapture
    {
        /// <summary>
        /// Captures at most the nearest fixed observation capacity; EnemyCount uses the same bounded capacity.
        /// </summary>
        public static void PopulateNearestEnemies(QaObservation observation, Vector2 playerPosition, IEnumerable<Monster> livingMonsters)
        {
            Array.Clear(observation.NearestEnemyPositions, 0, observation.NearestEnemyPositions.Length);
            if (livingMonsters == null)
            {
                observation.EnemyCount = 0;
                return;
            }

            var monsters = new List<Monster>();
            foreach (var monster in livingMonsters)
                if (monster != null) monsters.Add(monster);

            monsters.Sort((left, right) => CompareMonsters(left, right, playerPosition));
            var count = Mathf.Min(monsters.Count, QaObservation.MaxNearestEnemies);
            observation.EnemyCount = count;
            for (var index = 0; index < count; index++)
                observation.NearestEnemyPositions[index] = monsters[index].Position;
        }

        private static int CompareMonsters(Monster left, Monster right, Vector2 playerPosition)
        {
            var leftPosition = left.Position;
            var rightPosition = right.Position;
            var distance = (leftPosition - playerPosition).sqrMagnitude.CompareTo((rightPosition - playerPosition).sqrMagnitude);
            if (distance != 0) return distance;
            var x = leftPosition.x.CompareTo(rightPosition.x);
            return x != 0 ? x : leftPosition.y.CompareTo(rightPosition.y);
        }
    }
}
