using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Vampire
{
    /// <summary>
    /// Prepares and drives a single deterministic QA episode without changing the normal gameplay composition root.
    /// </summary>
    [DefaultExecutionOrder(-1000)]
    public sealed class QaEpisodeController : MonoBehaviour
    {
        public const float ControlIntervalSeconds = 0.1f;
        public const int MaxCatchUpSteps = 4;

        [SerializeField] private int episodeSeed = 1;
        [SerializeField] private CharacterBlueprint qaCharacter;
        [SerializeField] private string qaSceneName = "QA Level";
        [SerializeField] private string artifactDirectory = "QAArtifacts";
        [SerializeField] private Character playerCharacter;
        [SerializeField] private LevelManager levelManager;
        [SerializeField] private AbilitySelectionDialog abilitySelectionDialog;
        [SerializeField] private EntityManager entityManager;
        [SerializeField] private StatsManager statsManager;

        private readonly List<QaActionTraceEntry> actionTrace = new List<QaActionTraceEntry>();
        private readonly List<QaTelemetryEntry> telemetry = new List<QaTelemetryEntry>();
        private readonly QaEpisodeOracles oracles = new QaEpisodeOracles();
        private IQaPolicy policy;
        private IQaSceneReloader sceneReloader;
        private bool episodeStarted;
        private bool logSubscribed;
        private bool reloadRequested;
        private bool coinsExisted;
        private int coinsValue;
        private bool coinsSnapshotTaken;
        private int actionSequence;
        private int controlTick;
        private float pendingControlSeconds;
        private float elapsedUnscaledSeconds;

        public IReadOnlyList<QaActionTraceEntry> ActionTrace => actionTrace;
        public IReadOnlyList<QaTelemetryEntry> Telemetry => telemetry;
        public QaActionAcknowledgement LastActionAcknowledgement { get; private set; }
        public QaObservation LastObservation { get; private set; }
        public QaEpisodeResult TerminalResult { get; private set; }
        public int DuplicateTerminalCount { get; private set; }
        public float PendingControlSeconds => pendingControlSeconds;

        private void Awake()
        {
            BeginEpisode();
        }

        private void Update()
        {
            var gameTime = levelManager == null ? elapsedUnscaledSeconds : levelManager.LevelTime;
            Advance(Time.unscaledDeltaTime, gameTime, Time.timeScale);
        }

        private void OnDestroy()
        {
            if (logSubscribed)
            {
                Application.logMessageReceived -= HandleLogMessage;
                logSubscribed = false;
            }

            RestoreCoins();
        }

        public void ConfigureForTesting(
            int seed,
            CharacterBlueprint character,
            string sceneName,
            string outputDirectory,
            IQaPolicy testPolicy,
            IQaSceneReloader testSceneReloader)
        {
            RestoreCoins();
            episodeSeed = seed;
            qaCharacter = character;
            qaSceneName = sceneName;
            artifactDirectory = outputDirectory;
            policy = testPolicy;
            sceneReloader = testSceneReloader;
            episodeStarted = false;
            reloadRequested = false;
            TerminalResult = null;
            DuplicateTerminalCount = 0;
            actionSequence = 0;
            controlTick = 0;
            pendingControlSeconds = 0f;
            elapsedUnscaledSeconds = 0f;
            actionTrace.Clear();
            telemetry.Clear();
            oracles.Reset();
            BeginEpisode();
        }

        public void AdvanceForTesting(float unscaledDeltaSeconds, float gameTime, float currentTimeScale)
        {
            Advance(unscaledDeltaSeconds, gameTime, currentTimeScale);
        }

        public bool CompleteForTesting(QaEpisodeOutcome outcome, string reason)
        {
            return Complete(outcome, reason);
        }

        public void CaptureLogForTesting(LogType type)
        {
            HandleLogMessage("test", string.Empty, type);
        }

        private void BeginEpisode()
        {
            if (episodeStarted)
                return;

            QaEpisodeBootstrap.Prepare(episodeSeed, qaCharacter);
            SnapshotCoins();
            policy = policy ?? new ScriptedQaPolicy();
            sceneReloader = sceneReloader ?? new UnityQaSceneReloader();
            Application.logMessageReceived += HandleLogMessage;
            logSubscribed = true;
            episodeStarted = true;
        }

        private void Advance(float unscaledDeltaSeconds, float gameTime, float currentTimeScale)
        {
            if (!episodeStarted || TerminalResult != null)
                return;

            elapsedUnscaledSeconds += Mathf.Max(0f, unscaledDeltaSeconds);
            var observation = CaptureObservation();
            var knownModal = abilitySelectionDialog != null && abilitySelectionDialog.MenuOpen;
            var knownTerminal = levelManager != null && levelManager.Outcome != QaEpisodeOutcome.InProgress;
            var failure = oracles.Evaluate(observation, gameTime, currentTimeScale, knownModal, knownTerminal, unscaledDeltaSeconds);
            if (failure != QaOracleFailure.None)
            {
                Complete(QaEpisodeOutcome.Error, failure.ToString());
                return;
            }

            if (knownTerminal)
            {
                Complete(levelManager.Outcome, levelManager.Outcome.ToString());
                return;
            }

            pendingControlSeconds += Mathf.Max(0f, unscaledDeltaSeconds);
            var steps = 0;
            while (pendingControlSeconds + 0.000001f >= ControlIntervalSeconds && steps < MaxCatchUpSteps)
            {
                pendingControlSeconds -= ControlIntervalSeconds;
                RunControlStep();
                steps++;
            }
        }

        private void RunControlStep()
        {
            controlTick++;
            LastObservation = CaptureObservation();
            var action = policy.Decide(LastObservation);
            Apply(action);
            actionSequence++;
            LastActionAcknowledgement = new QaActionAcknowledgement(actionSequence, controlTick);
            actionTrace.Add(new QaActionTraceEntry(actionSequence, controlTick, controlTick * ControlIntervalSeconds, action));
            telemetry.Add(new QaTelemetryEntry(controlTick, controlTick * ControlIntervalSeconds, "observation"));
        }

        private QaObservation CaptureObservation()
        {
            var observation = new QaObservation
            {
                ElapsedSeconds = elapsedUnscaledSeconds,
                LevelPhase = QaLevelPhase.Early,
                EnemyCount = entityManager == null ? 0 : entityManager.EntityCount,
                IsAbilitySelectionOpen = abilitySelectionDialog != null && abilitySelectionDialog.MenuOpen,
                KillCount = statsManager == null ? 0 : statsManager.MonstersKilled,
                DamageTaken = statsManager == null ? 0f : statsManager.DamageTaken
            };

            if (playerCharacter != null)
            {
                observation.PlayerPosition = playerCharacter.Position;
                observation.PlayerHealth = playerCharacter.CurrentHealth;
                observation.PlayerMaxHealth = playerCharacter.MaxHealth;
                observation.PlayerExperience = playerCharacter.CurrentExperience;
                observation.PlayerLevel = playerCharacter.CurrentLevel;
                observation.IsPlayerAlive = playerCharacter.IsAlive;
            }

            if (abilitySelectionDialog != null)
            {
                var choices = abilitySelectionDialog.DisplayedAbilities;
                for (var index = 0; index < choices.Count && index < QaObservation.MaxAbilityChoices; index++)
                    observation.AbilityChoices[index] = choices[index] != null;
            }

            return observation;
        }

        private void Apply(QaAction action)
        {
            if (playerCharacter != null)
                playerCharacter.Move(action.Movement);

            if (action.AbilityChoice >= 0 && abilitySelectionDialog != null)
                abilitySelectionDialog.TrySelectOption(action.AbilityChoice);
        }

        private bool Complete(QaEpisodeOutcome outcome, string reason)
        {
            if (TerminalResult != null)
            {
                DuplicateTerminalCount++;
                telemetry.Add(new QaTelemetryEntry(controlTick, elapsedUnscaledSeconds, "duplicate-terminal"));
                return false;
            }

            if (outcome == QaEpisodeOutcome.InProgress)
                return false;

            TerminalResult = new QaEpisodeResult
            {
                Seed = episodeSeed,
                Outcome = outcome,
                ElapsedSeconds = elapsedUnscaledSeconds,
                KillCount = statsManager == null ? 0 : statsManager.MonstersKilled,
                FinalLevel = playerCharacter == null ? 0 : playerCharacter.CurrentLevel,
                FailureReason = reason
            };
            telemetry.Add(new QaTelemetryEntry(controlTick, elapsedUnscaledSeconds, "terminal"));
            RestoreCoins();
            new QaArtifactWriter(artifactDirectory, episodeSeed).Write(TerminalResult, actionTrace, telemetry);

            if (!reloadRequested)
            {
                reloadRequested = true;
                sceneReloader.Reload(qaSceneName);
            }

            return true;
        }

        private void HandleLogMessage(string condition, string stackTrace, LogType type)
        {
            if (type == LogType.Error || type == LogType.Assert || type == LogType.Exception)
                Complete(QaEpisodeOutcome.Error, "UnityLog:" + type);
        }

        private void SnapshotCoins()
        {
            coinsExisted = PlayerPrefs.HasKey("Coins");
            coinsValue = coinsExisted ? PlayerPrefs.GetInt("Coins") : 0;
            coinsSnapshotTaken = true;
        }

        private void RestoreCoins()
        {
            if (!coinsSnapshotTaken)
                return;

            if (coinsExisted)
                PlayerPrefs.SetInt("Coins", coinsValue);
            else
                PlayerPrefs.DeleteKey("Coins");

            PlayerPrefs.Save();
            coinsSnapshotTaken = false;
        }
    }

    public static class QaEpisodeBootstrap
    {
        public static void Prepare(int seed, CharacterBlueprint character)
        {
            UnityEngine.Random.InitState(seed);
            CrossSceneData.CharacterBlueprint = character;
        }
    }

    public interface IQaSceneReloader
    {
        void Reload(string sceneName);
    }

    public sealed class UnityQaSceneReloader : IQaSceneReloader
    {
        public void Reload(string sceneName)
        {
            SceneManager.LoadScene(sceneName);
        }
    }

    public readonly struct QaActionAcknowledgement
    {
        public QaActionAcknowledgement(int sequence, int tick)
        {
            Sequence = sequence;
            Tick = tick;
        }

        public int Sequence { get; }
        public int Tick { get; }
    }

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

    [Serializable]
    public sealed class QaActionTraceEntry
    {
        public int Sequence;
        public int Tick;
        public float Time;
        public float MovementX;
        public float MovementY;
        public int AbilityChoice;

        public QaActionTraceEntry(int sequence, int tick, float time, QaAction action)
        {
            Sequence = sequence;
            Tick = tick;
            Time = time;
            MovementX = action.Movement.x;
            MovementY = action.Movement.y;
            AbilityChoice = action.AbilityChoice;
        }
    }

    [Serializable]
    public sealed class QaTelemetryEntry
    {
        public int Tick;
        public float Time;
        public string Event;

        public QaTelemetryEntry(int tick, float time, string eventName)
        {
            Tick = tick;
            Time = time;
            Event = eventName;
        }
    }

    [Serializable]
    public sealed class QaArtifactLine
    {
        public string Schema;
        public string Kind;
        public int Seed;
        public int Tick;
        public int Sequence;
        public float Time;
        public QaEpisodeOutcome Outcome;
        public string Event;
        public float MovementX;
        public float MovementY;
        public int AbilityChoice;
    }

    [Serializable]
    public sealed class QaEpisodeSummary
    {
        public string Schema;
        public int Seed;
        public QaEpisodeOutcome Outcome;
        public float ElapsedSeconds;
        public int KillCount;
        public int FinalLevel;
        public string FailureReason;
    }

    public sealed class QaArtifactPaths
    {
        public string ActionTracePath { get; }
        public string TelemetryPath { get; }
        public string SummaryPath { get; }

        public QaArtifactPaths(string actionTracePath, string telemetryPath, string summaryPath)
        {
            ActionTracePath = actionTracePath;
            TelemetryPath = telemetryPath;
            SummaryPath = summaryPath;
        }
    }

    public sealed class QaArtifactWriter
    {
        public const string SchemaVersion = "qa-episode/v1";

        private readonly string directory;
        private readonly int seed;

        public QaArtifactWriter(string directory, int seed)
        {
            this.directory = string.IsNullOrWhiteSpace(directory) ? "QAArtifacts" : directory;
            this.seed = seed;
        }

        public QaArtifactPaths Write(QaEpisodeResult result, IReadOnlyList<QaActionTraceEntry> trace, IReadOnlyList<QaTelemetryEntry> telemetry)
        {
            Directory.CreateDirectory(directory);
            var prefix = "episode-" + seed.ToString("D8");
            var actionPath = Path.Combine(directory, prefix + "-actions.jsonl");
            var telemetryPath = Path.Combine(directory, prefix + "-telemetry.jsonl");
            var summaryPath = Path.Combine(directory, prefix + "-summary.json");
            WriteLines(actionPath, trace, entry => new QaArtifactLine
            {
                Schema = SchemaVersion, Kind = "action", Seed = seed, Tick = entry.Tick, Sequence = entry.Sequence,
                Time = entry.Time, MovementX = entry.MovementX, MovementY = entry.MovementY, AbilityChoice = entry.AbilityChoice
            });
            WriteLines(telemetryPath, telemetry, entry => new QaArtifactLine
            {
                Schema = SchemaVersion, Kind = "telemetry", Seed = seed, Tick = entry.Tick, Time = entry.Time,
                Outcome = result.Outcome, Event = entry.Event
            });
            var summary = new QaEpisodeSummary
            {
                Schema = SchemaVersion, Seed = result.Seed, Outcome = result.Outcome, ElapsedSeconds = result.ElapsedSeconds,
                KillCount = result.KillCount, FinalLevel = result.FinalLevel, FailureReason = result.FailureReason
            };
            File.WriteAllText(summaryPath, JsonUtility.ToJson(summary), new UTF8Encoding(false));
            return new QaArtifactPaths(actionPath, telemetryPath, summaryPath);
        }

        private static void WriteLines<T>(string path, IReadOnlyList<T> entries, Func<T, QaArtifactLine> toLine)
        {
            var builder = new StringBuilder();
            for (var index = 0; index < entries.Count; index++)
                builder.Append(JsonUtility.ToJson(toLine(entries[index]))).Append('\n');

            File.WriteAllText(path, builder.ToString(), new UTF8Encoding(false));
        }
    }

    public sealed class QaRecordedEpisode
    {
        public QaRecordedEpisode(QaEpisodeOutcome outcome, IReadOnlyList<string> discreteEvents, IReadOnlyList<Vector2> positions)
        {
            Outcome = outcome;
            DiscreteEvents = discreteEvents;
            Positions = positions;
        }

        public QaEpisodeOutcome Outcome { get; }
        public IReadOnlyList<string> DiscreteEvents { get; }
        public IReadOnlyList<Vector2> Positions { get; }
    }

    public sealed class QaReplayComparison
    {
        public QaReplayComparison(bool isMatch, string reason)
        {
            IsMatch = isMatch;
            Reason = reason;
        }

        public bool IsMatch { get; }
        public string Reason { get; }
    }

    public static class QaReplayComparator
    {
        public const float PositionTolerance = 0.05f;

        public static QaReplayComparison Compare(QaRecordedEpisode expected, QaRecordedEpisode actual)
        {
            if (expected.Outcome != actual.Outcome)
                return new QaReplayComparison(false, "outcome");
            if (expected.DiscreteEvents.Count != actual.DiscreteEvents.Count)
                return new QaReplayComparison(false, "discrete-event-count");
            if (expected.Positions.Count != actual.Positions.Count)
                return new QaReplayComparison(false, "position-count");

            for (var index = 0; index < expected.DiscreteEvents.Count; index++)
                if (!string.Equals(expected.DiscreteEvents[index], actual.DiscreteEvents[index], StringComparison.Ordinal))
                    return new QaReplayComparison(false, "discrete-event-" + index);

            for (var index = 0; index < expected.Positions.Count; index++)
                if (Vector2.Distance(expected.Positions[index], actual.Positions[index]) > PositionTolerance)
                    return new QaReplayComparison(false, "position-" + index);

            return new QaReplayComparison(true, string.Empty);
        }
    }
}
