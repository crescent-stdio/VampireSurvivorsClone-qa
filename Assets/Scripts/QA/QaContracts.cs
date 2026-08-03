using System;
using UnityEngine;

[Serializable]
public sealed class QaAction
{
    public Vector2 Movement { get; }
    public int AbilityChoice { get; }

    public QaAction(Vector2 movement, int abilityChoice = -1)
    {
        if (!IsValidAbilityChoice(abilityChoice))
        {
            throw new ArgumentOutOfRangeException(nameof(abilityChoice), abilityChoice, "Ability choice must be -1 or an index from 0 through 3.");
        }

        Movement = movement.sqrMagnitude > 1f ? movement.normalized : movement;
        AbilityChoice = abilityChoice;
    }

    public static bool IsValidAbilityChoice(int abilityChoice)
    {
        return abilityChoice >= -1 && abilityChoice <= 3;
    }
}

[Serializable]
public sealed class QaObservation
{
    public const int MaxNearestEnemies = 8;
    public const int MaxAbilityChoices = 4;

    public Vector2 PlayerPosition { get; set; }
    public float PlayerHealth { get; set; }
    public float PlayerMaxHealth { get; set; }
    public float PlayerExperience { get; set; }
    public int PlayerLevel { get; set; }
    public bool IsPlayerAlive { get; set; }
    public Vector2[] NearestEnemyPositions { get; }
    public int EnemyCount { get; set; }
    public bool HasCollectibleTarget { get; set; }
    public Vector2 CollectiblePosition { get; set; }
    public bool HasChestTarget { get; set; }
    public Vector2 ChestPosition { get; set; }
    public bool[] AbilityChoices { get; }
    public QaLevelPhase LevelPhase { get; set; }
    public int KillCount { get; set; }
    public float ElapsedSeconds { get; set; }
    public float DamageTaken { get; set; }
    public bool IsAbilitySelectionOpen { get; set; }

    public QaObservation()
    {
        NearestEnemyPositions = new Vector2[MaxNearestEnemies];
        AbilityChoices = new bool[MaxAbilityChoices];
    }
}

public enum QaLevelPhase
{
    Early,
    Mid,
    Miniboss,
    FinalBoss,
    Completed
}

public enum QaEpisodeOutcome
{
    InProgress,
    Passed,
    PlayerDied,
    TimedOut,
    Error
}

[Serializable]
public sealed class QaEpisodeResult
{
    public int Seed;
    public QaEpisodeOutcome Outcome;
    public float ElapsedSeconds;
    public int KillCount;
    public int FinalLevel;
    public string FailureReason;
}

public interface IQaPolicy
{
    QaAction Decide(QaObservation observation);
}
