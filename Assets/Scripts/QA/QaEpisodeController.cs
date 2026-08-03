using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Vampire
{
    /// <summary>
    /// Orchestrates one deterministic QA episode around existing gameplay components.
    /// </summary>
    [DefaultExecutionOrder(-1000)]
    public sealed class QaEpisodeController : MonoBehaviour, IQaGameplayController
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
        private readonly QaEpisodeRecorder recorder = new QaEpisodeRecorder();
        private IQaPolicy policy;
        private IQaSceneReloader sceneReloader;
        private IQaArtifactWriter artifactWriter;
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
        private static bool invalidSeedWarningLogged;

        public IReadOnlyList<QaActionTraceEntry> ActionTrace => actionTrace;
        public IReadOnlyList<QaTelemetryEntry> Telemetry => telemetry;
        public QaActionAcknowledgement LastActionAcknowledgement { get; private set; }
        public QaObservation LastObservation { get; private set; }
        public QaEpisodeResult TerminalResult { get; private set; }
        public QaArtifactWriteResult LastArtifactWrite { get; private set; }
        public QaRecordedEpisode RecordedEpisode => recorder.Create(TerminalResult == null ? QaEpisodeOutcome.InProgress : TerminalResult.Outcome);
        public int DuplicateTerminalCount { get; private set; }
        public float PendingControlSeconds => pendingControlSeconds;
        public bool IsLogSubscribed => logSubscribed;
        public QaControlMode ControlMode { get; private set; } = QaControlMode.Scripted;
        public QaEpisodeOutcome CurrentOutcome => TerminalResult == null
            ? (levelManager == null ? QaEpisodeOutcome.InProgress : levelManager.Outcome)
            : TerminalResult.Outcome;

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
            UnsubscribeLogs();
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
            artifactWriter = null;
            episodeStarted = false;
            reloadRequested = false;
            TerminalResult = null;
            LastArtifactWrite = null;
            DuplicateTerminalCount = 0;
            actionSequence = 0;
            controlTick = 0;
            pendingControlSeconds = 0f;
            elapsedUnscaledSeconds = 0f;
            actionTrace.Clear();
            telemetry.Clear();
            recorder.Reset();
            oracles.Reset();
            BeginEpisode();
        }

        public void SetArtifactWriterForTesting(IQaArtifactWriter writer)
        {
            artifactWriter = writer;
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

        public void RecordDiscreteEvent(string eventName)
        {
            recorder.RecordDiscreteEvent(eventName);
        }

        public QaObservation CaptureAgentObservation()
        {
            return CaptureObservation();
        }

        public void EnableExternalAgentControl()
        {
            ControlMode = QaControlMode.ExternalAgent;
        }

        public bool SubmitExternalAction(QaAction action)
        {
            if (ControlMode != QaControlMode.ExternalAgent || TerminalResult != null || action == null)
                return false;

            RecordAppliedAction(action);
            return true;
        }

        private void BeginEpisode()
        {
            if (episodeStarted)
                return;

            episodeSeed = ResolveEpisodeSeed(episodeSeed);
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
                if (ControlMode == QaControlMode.Scripted)
                    RunControlStep();
                steps++;
            }
        }

        private void RunControlStep()
        {
            var observation = CaptureObservation();
            var action = policy.Decide(observation);
            RecordAppliedAction(action);
        }

        private void RecordAppliedAction(QaAction action)
        {
            controlTick++;
            LastObservation = CaptureObservation();
            recorder.RecordObservation(LastObservation);
            Apply(action);
            actionSequence++;
            LastActionAcknowledgement = new QaActionAcknowledgement(actionSequence, controlTick);
            actionTrace.Add(new QaActionTraceEntry(actionSequence, controlTick, controlTick * ControlIntervalSeconds, action));
            telemetry.Add(new QaTelemetryEntry(controlTick, controlTick * ControlIntervalSeconds, "observation"));
            recorder.RecordDiscreteEvent("action-ack:" + actionSequence + ":" + controlTick);
            recorder.RecordDiscreteEvent("phase:" + (int)LastObservation.LevelPhase);
            recorder.RecordDiscreteEvent("modal:" + (LastObservation.IsAbilitySelectionOpen ? "1" : "0"));
        }

        private static int ResolveEpisodeSeed(int serializedSeed)
        {
            var arguments = Environment.GetCommandLineArgs();
            if (QaEpisodeSeedParser.TryParseQaSeed(arguments, out var commandLineSeed))
                return commandLineSeed;

            for (var index = 0; index < arguments.Length; index++)
            {
                if (arguments[index] != null && arguments[index].StartsWith("-qaSeed=", StringComparison.Ordinal) && !invalidSeedWarningLogged)
                {
                    invalidSeedWarningLogged = true;
                    Debug.LogWarning("Ignoring invalid -qaSeed argument and using the serialized episode seed.");
                }
            }

            return serializedSeed;
        }

        private QaObservation CaptureObservation()
        {
            var observation = new QaObservation
            {
                ElapsedSeconds = elapsedUnscaledSeconds,
                LevelPhase = QaLevelPhase.Early,
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
                observation.PlayerNextExperience = playerCharacter.NextExperience;
                observation.PlayerLevel = playerCharacter.CurrentLevel;
                observation.IsPlayerAlive = playerCharacter.IsAlive;
            }

            QaObservationCapture.PopulateNearestEnemies(
                observation,
                observation.PlayerPosition,
                entityManager == null ? null : entityManager.LivingMonsters);

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
            recorder.RecordDiscreteEvent("terminal:" + outcome);
            UnsubscribeLogs();
            RestoreCoins();
            WriteArtifacts();

            if (!reloadRequested)
            {
                reloadRequested = true;
                sceneReloader.Reload(qaSceneName);
            }

            return true;
        }

        private void WriteArtifacts()
        {
            try
            {
                var writer = artifactWriter ?? new QaArtifactWriter(artifactDirectory, episodeSeed);
                LastArtifactWrite = writer.Write(TerminalResult, actionTrace, telemetry, RecordedEpisode);
            }
            catch (Exception exception)
            {
                LastArtifactWrite = QaArtifactWriteResult.Failed(exception.Message);
            }
        }

        private void HandleLogMessage(string condition, string stackTrace, LogType type)
        {
            if (type == LogType.Error || type == LogType.Assert || type == LogType.Exception)
                Complete(QaEpisodeOutcome.Error, "UnityLog:" + type);
        }

        private void UnsubscribeLogs()
        {
            if (!logSubscribed)
                return;

            Application.logMessageReceived -= HandleLogMessage;
            logSubscribed = false;
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
}
