using System;
using System.Collections.Generic;
using Unity.MLAgents;
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
        public const int MaximumConsecutiveBacklogFrames = 8;

        [SerializeField] private int episodeSeed = 1;
        [SerializeField] private CharacterBlueprint qaCharacter;
        [SerializeField] private QaPresetBlueprint[] presets;
        [SerializeField] private string qaSceneName = "QA Level";
        [SerializeField] private string sourceSceneFingerprint;
        [SerializeField] private string artifactDirectory = "QAArtifacts";
        [SerializeField] private Character playerCharacter;
        [SerializeField] private LevelManager levelManager;
        [SerializeField] private AbilitySelectionDialog abilitySelectionDialog;
        [SerializeField] private bool disableAbilityPause = true;
        [SerializeField] private EntityManager entityManager;
        [SerializeField] private StatsManager statsManager;

        private readonly List<QaActionTraceEntry> actionTrace = new List<QaActionTraceEntry>();
        private readonly List<QaTelemetryEntry> telemetry = new List<QaTelemetryEntry>();
        private readonly QaEpisodeOracles oracles = new QaEpisodeOracles();
        private readonly QaEpisodeRecorder recorder = new QaEpisodeRecorder();
        private IQaPolicy policy;
        private IQaSceneReloader sceneReloader;
        private IQaArtifactWriter artifactWriter;
        private IQaProcessExit processExit;
        private IQaFailureScreenshotCapture failureScreenshotCapture;
        private QaRecordedEpisode expectedReplay;
        private bool replayRequested;
        private string replayLoadFailure;
        private bool episodeStarted;
        private bool logSubscribed;
        private bool reloadRequested;
        private bool coinsExisted;
        private int coinsValue;
        private bool coinsSnapshotTaken;
        private int actionSequence;
        private int controlTick;
        private float pendingControlSeconds;
        private int consecutiveBacklogFrames;
        private float elapsedUnscaledSeconds;
        private bool smokeRequested;
        private bool llmRequested;
        private bool evaluationRequested;
        private float originalTimeScale = 1f;
        private bool terminalNotificationInProgress;
        private bool terminalAcknowledged;
        private bool randomDecisionSubscribed;
        private QaPresetBlueprint activePreset;
        private bool inferenceSourceChecked;
        private bool? inferenceSourceOverride;
        private static bool invalidSeedWarningLogged;

        /// <summary>
        /// Report whether a trained policy can actually drive this episode.
        /// </summary>
        /// <remarks>
        /// BehaviorType.Default degrades quietly: with no communicator and no assigned
        /// model it returns a HeuristicPolicy, so an evaluation with a broken trainer
        /// connection measures ScriptedQaPolicy and reports it as the model's result.
        /// Evaluation wants InferenceOnly's failure mode instead.
        /// </remarks>
        public bool HasInferenceSource()
        {
            if (inferenceSourceOverride.HasValue)
                return inferenceSourceOverride.Value;

            var behavior = GetComponent<Unity.MLAgents.Policies.BehaviorParameters>();
            if (behavior != null && behavior.Model != null)
                return true;
            return Academy.IsInitialized && Academy.Instance.IsCommunicatorOn;
        }

        public void ConfigureInferenceSourceForTesting(bool? available)
        {
            inferenceSourceOverride = available;
            inferenceSourceChecked = false;
        }

        /// <summary>Preset resolved from <c>-qaPreset=</c>, or null when running outside a preset.</summary>
        public QaPresetBlueprint ActivePreset => activePreset;

        /// <summary>
        /// True while an ML-Agents trainer drives the episode with no <c>-qaMode</c> flag.
        /// </summary>
        /// <remarks>
        /// Training needs a deadline of its own. The agent carries no step cap, the QA
        /// character survives long enough that death is not a practical terminal, and a
        /// pass requires killing the boss that spawns once the level duration elapses.
        /// Without this an untrained policy never reaches a terminal at all, so PPO sees
        /// neither the pass reward nor the failure reward and bootstraps forever.
        /// Replay is excluded: it is bounded by the recorded action trace.
        /// </remarks>
        public bool IsTrainingMode => !smokeRequested && !llmRequested && !evaluationRequested && !replayRequested;

        /// <summary>Game-time deadline for this episode.</summary>
        public float DeadlineSeconds =>
            activePreset == null ? QaSmokeOptions.MaximumGameTimeSeconds : activePreset.DeadlineSeconds;

        public float ObservationElapsedSecondsScale =>
            activePreset == null
                ? QaGameplayObservationEncoder.ElapsedSecondsScale
                : activePreset.ElapsedSecondsScale;

        public event Action<QaEpisodeOutcome> TerminalReached;

        public IReadOnlyList<QaActionTraceEntry> ActionTrace => actionTrace;
        public IReadOnlyList<QaTelemetryEntry> Telemetry => telemetry;
        public QaActionAcknowledgement LastActionAcknowledgement { get; private set; }
        public QaObservation LastObservation { get; private set; }
        public QaEpisodeResult TerminalResult { get; private set; }
        public QaArtifactWriteResult LastArtifactWrite { get; private set; }
        public QaReplayComparison LastReplayComparison { get; private set; }
        public QaRecordedEpisode RecordedEpisode => recorder.Create(TerminalResult == null ? QaEpisodeOutcome.InProgress : TerminalResult.Outcome);
        public int DuplicateTerminalCount { get; private set; }
        public float PendingControlSeconds => pendingControlSeconds;
        public bool IsLogSubscribed => logSubscribed;
        public QaControlMode ControlMode { get; private set; } = QaControlMode.Scripted;
        public bool IsSmokeMode => smokeRequested;
        public bool IsLlmMode => llmRequested;
        public bool IsEvaluationMode => evaluationRequested;
        public QaEpisodeOutcome CurrentOutcome => TerminalResult == null
            ? (levelManager == null ? QaEpisodeOutcome.InProgress : levelManager.Outcome)
            : TerminalResult.Outcome;

        private void Awake()
        {
            if (disableAbilityPause && abilitySelectionDialog != null)
                abilitySelectionDialog.PauseOnOpen = false;
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
            UnsubscribeRandomDecisions();
            RestoreCoins();
            RestoreTimeScale();
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
            UnsubscribeRandomDecisions();
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
            LastReplayComparison = null;
            expectedReplay = null;
            replayRequested = false;
            replayLoadFailure = null;
            processExit = null;
            failureScreenshotCapture = null;
            smokeRequested = false;
            llmRequested = false;
            evaluationRequested = false;
            terminalNotificationInProgress = false;
            terminalAcknowledged = false;
            DuplicateTerminalCount = 0;
            actionSequence = 0;
            controlTick = 0;
            pendingControlSeconds = 0f;
            consecutiveBacklogFrames = 0;
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

        public void ConfigureReplayForTesting(QaReplayTrace trace, IQaProcessExit testProcessExit)
        {
            if (trace == null)
                throw new ArgumentNullException(nameof(trace));

            replayRequested = true;
            episodeSeed = trace.Seed;
            QaEpisodeBootstrap.Prepare(episodeSeed, qaCharacter);
            expectedReplay = trace.ToRecordedEpisode();
            policy = new QaReplayPolicy(trace.Actions);
            processExit = testProcessExit;
        }

        public void ConfigureSmokeForTesting(IQaProcessExit testProcessExit, IQaFailureScreenshotCapture testScreenshotCapture)
        {
            smokeRequested = true;
            processExit = testProcessExit;
            failureScreenshotCapture = testScreenshotCapture;
        }

        public void ConfigureLlmForTesting(IQaProcessExit testProcessExit, IQaFailureScreenshotCapture testScreenshotCapture)
        {
            llmRequested = true;
            processExit = testProcessExit;
            failureScreenshotCapture = testScreenshotCapture;
        }

        public void ConfigureEvaluationForTesting(IQaProcessExit testProcessExit, IQaFailureScreenshotCapture testScreenshotCapture)
        {
            evaluationRequested = true;
            processExit = testProcessExit;
            failureScreenshotCapture = testScreenshotCapture;
            // Tests stand in for a connected trainer unless they say otherwise; there is no
            // Academy in edit mode. ConfigureInferenceSourceForTesting(false) opts out.
            inferenceSourceOverride = inferenceSourceOverride ?? true;
            inferenceSourceChecked = false;
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
            if (replayRequested)
                return;
            ControlMode = QaControlMode.ExternalAgent;
        }

        public bool SubmitExternalAction(QaAction action)
        {
            if (ControlMode != QaControlMode.ExternalAgent || TerminalResult != null || CurrentOutcome != QaEpisodeOutcome.InProgress || action == null)
                return false;

            RecordAppliedAction(action);
            return true;
        }

        public bool AcknowledgeTerminal()
        {
            if (!terminalNotificationInProgress || terminalAcknowledged || TerminalResult == null)
                return false;

            terminalAcknowledged = true;
            return true;
        }

        private void BeginEpisode()
        {
            if (episodeStarted)
                return;

            var arguments = Environment.GetCommandLineArgs();
            // Resolve the preset from a first pass so its ceiling governs the real parse.
            activePreset = FindPreset(QaSmokeOptions.Parse(arguments).PresetName);
            var maximumTimeScale = activePreset == null
                ? QaSmokeOptions.MaximumSupportedTimeScale
                : activePreset.MaximumTimeScale;
            var smokeOptions = QaSmokeOptions.Parse(arguments, maximumTimeScale);
            var presetFailure = ResolvePresetFailure(smokeOptions);
            smokeRequested = smokeRequested || smokeOptions.IsRequested;
            llmRequested = llmRequested || smokeOptions.IsLlmRequested;
            evaluationRequested = evaluationRequested || smokeOptions.IsEvaluationRequested;
            if (smokeOptions.IsRequested || smokeOptions.IsLlmRequested || smokeOptions.IsEvaluationRequested)
            {
                originalTimeScale = Time.timeScale;
                Time.timeScale = smokeOptions.TimeScale;
            }
            replayRequested = QaReplayTrace.IsReplayRequested(arguments);
            if (replayRequested && expectedReplay == null)
            {
                if (QaReplayTrace.TryLoadFromCommandLine(arguments, out var trace, out var error))
                {
                    episodeSeed = trace.Seed;
                    expectedReplay = trace.ToRecordedEpisode();
                    policy = new QaReplayPolicy(trace.Actions);
                }
                else
                {
                    replayLoadFailure = error;
                }
            }
            else
            {
                episodeSeed = ResolveEpisodeSeed(episodeSeed);
            }
            QaEpisodeBootstrap.Prepare(episodeSeed, ResolveCharacter());
            SnapshotCoins();
            policy = policy ?? new ScriptedQaPolicy();
            sceneReloader = sceneReloader ?? new UnityQaSceneReloader();
            processExit = processExit ?? new UnityQaProcessExit();
            failureScreenshotCapture = failureScreenshotCapture ?? new UnityQaFailureScreenshotCapture();
            Application.logMessageReceived += HandleLogMessage;
            logSubscribed = true;
            QaRandomDecisionRecorder.DecisionRecorded += RecordDiscreteEvent;
            randomDecisionSubscribed = true;
            episodeStarted = true;
            if (!string.IsNullOrEmpty(presetFailure))
                Complete(QaEpisodeOutcome.Error, presetFailure);
            else if ((smokeRequested || llmRequested || evaluationRequested) && !smokeOptions.IsValid)
                Complete(QaEpisodeOutcome.Error, smokeOptions.FailureReason);
            else if (!string.IsNullOrEmpty(replayLoadFailure))
                Complete(QaEpisodeOutcome.Error, replayLoadFailure);
        }

        /// <summary>Locate the requested preset among the ones wired into the scene.</summary>
        private QaPresetBlueprint FindPreset(string presetName)
        {
            if (presets == null || string.IsNullOrEmpty(presetName))
                return null;
            foreach (var preset in presets)
                if (preset != null && string.Equals(preset.PresetName, presetName, StringComparison.Ordinal))
                    return preset;
            return null;
        }

        /// <summary>
        /// Report a preset that was named but not found.
        /// </summary>
        /// <remarks>
        /// Scenes built before presets existed carry none, so an unresolved default is not
        /// an error; silently ignoring an explicitly requested name would be.
        /// </remarks>
        private string ResolvePresetFailure(QaSmokeOptions options)
        {
            if (activePreset != null)
                return string.Empty;
            if (presets == null || presets.Length == 0)
                return string.Empty;
            return string.Equals(options.PresetName, QaSmokeOptions.DefaultPresetName, StringComparison.Ordinal)
                ? string.Empty
                : "UnknownQaPreset";
        }

        /// <summary>Prefer the active preset's character so durability follows the preset.</summary>
        private CharacterBlueprint ResolveCharacter()
        {
            return activePreset != null && activePreset.Character != null ? activePreset.Character : qaCharacter;
        }

        private void Advance(float unscaledDeltaSeconds, float gameTime, float currentTimeScale)
        {
            if (!episodeStarted || TerminalResult != null)
                return;

            elapsedUnscaledSeconds += Mathf.Max(0f, unscaledDeltaSeconds);
            if (smokeRequested && gameTime >= DeadlineSeconds)
            {
                Complete(QaEpisodeOutcome.TimedOut, "SmokeDeadline");
                return;
            }
            if (llmRequested && gameTime >= DeadlineSeconds)
            {
                Complete(QaEpisodeOutcome.TimedOut, "LlmDeadline");
                return;
            }
            if (evaluationRequested && gameTime >= DeadlineSeconds)
            {
                Complete(QaEpisodeOutcome.TimedOut, "EvaluationDeadline");
                return;
            }
            if (IsTrainingMode && gameTime >= DeadlineSeconds)
            {
                Complete(QaEpisodeOutcome.TimedOut, "TrainingDeadline");
                return;
            }
            if (evaluationRequested && !inferenceSourceChecked)
            {
                inferenceSourceChecked = true;
                if (!HasInferenceSource())
                {
                    Complete(QaEpisodeOutcome.Error, "NoInferenceSource");
                    return;
                }
            }
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

            var controlDeltaSeconds = smokeRequested
                ? unscaledDeltaSeconds * Mathf.Max(1f, currentTimeScale)
                : unscaledDeltaSeconds;
            pendingControlSeconds += Mathf.Max(0f, controlDeltaSeconds);
            var steps = 0;
            while (pendingControlSeconds + 0.000001f >= ControlIntervalSeconds && steps < MaxCatchUpSteps)
            {
                pendingControlSeconds -= ControlIntervalSeconds;
                if (ControlMode == QaControlMode.Scripted)
                    RunControlStep();
                steps++;
            }

            if (pendingControlSeconds + 0.000001f >= ControlIntervalSeconds)
            {
                consecutiveBacklogFrames++;
                if (consecutiveBacklogFrames >= MaximumConsecutiveBacklogFrames)
                    Complete(QaEpisodeOutcome.Error, "ControlBacklogExceeded");
            }
            else
            {
                consecutiveBacklogFrames = 0;
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
                LevelPhase = QaLevelPhaseResolver.Resolve(levelManager == null ? elapsedUnscaledSeconds : levelManager.LevelTime),
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
            QaObservationCapture.PopulateNearestTargets(
                observation,
                observation.PlayerPosition,
                entityManager == null ? null : entityManager.MagneticCollectables,
                entityManager == null ? null : entityManager.chests);

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
            var requiresExternalAcknowledgement = ControlMode == QaControlMode.ExternalAgent && TerminalReached != null;
            if (requiresExternalAcknowledgement)
            {
                terminalNotificationInProgress = true;
                TerminalReached.Invoke(outcome);
                terminalNotificationInProgress = false;
            }
            telemetry.Add(new QaTelemetryEntry(controlTick, elapsedUnscaledSeconds, "terminal"));
            recorder.RecordDiscreteEvent("terminal:" + outcome);
            UnsubscribeLogs();
            UnsubscribeRandomDecisions();
            RestoreCoins();
            WriteArtifacts();

            if (smokeRequested || llmRequested || evaluationRequested)
            {
                if (outcome != QaEpisodeOutcome.Passed)
                    failureScreenshotCapture.Capture("QAArtifacts/screenshots/seed-" + episodeSeed.ToString("D8") + ".png");
                RestoreTimeScale();
                processExit.Exit(outcome == QaEpisodeOutcome.Passed ? 0 : 1);
                return true;
            }

            if (replayRequested)
            {
                LastReplayComparison = expectedReplay == null
                    ? new QaReplayComparison(false, "trace-load")
                    : QaReplayComparator.Compare(expectedReplay, RecordedEpisode);
                processExit.Exit(LastReplayComparison.IsMatch ? 0 : 1);
                return true;
            }

            if (requiresExternalAcknowledgement && !terminalAcknowledged)
                return true;

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

        private void UnsubscribeRandomDecisions()
        {
            if (!randomDecisionSubscribed)
                return;

            QaRandomDecisionRecorder.DecisionRecorded -= RecordDiscreteEvent;
            randomDecisionSubscribed = false;
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

        private void RestoreTimeScale()
        {
            if (smokeRequested || llmRequested || evaluationRequested)
                Time.timeScale = originalTimeScale;
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

    public interface IQaProcessExit
    {
        void Exit(int code);
    }

    public interface IQaFailureScreenshotCapture
    {
        void Capture(string relativePath);
    }

    public sealed class UnityQaFailureScreenshotCapture : IQaFailureScreenshotCapture
    {
        public void Capture(string relativePath)
        {
            var directory = System.IO.Path.GetDirectoryName(relativePath);
            if (!string.IsNullOrEmpty(directory))
                System.IO.Directory.CreateDirectory(directory);

            var camera = Camera.main ?? UnityEngine.Object.FindObjectOfType<Camera>();
            if (camera == null)
                throw new InvalidOperationException("The QA scene has no camera for an anomaly screenshot.");

            const int width = 1280;
            const int height = 720;
            var renderTexture = new RenderTexture(width, height, 24);
            var texture = new Texture2D(width, height, TextureFormat.RGB24, false);
            var previousTarget = camera.targetTexture;
            var previousActive = RenderTexture.active;
            try
            {
                camera.targetTexture = renderTexture;
                camera.Render();
                RenderTexture.active = renderTexture;
                texture.ReadPixels(new Rect(0, 0, width, height), 0, 0);
                texture.Apply();
                System.IO.File.WriteAllBytes(relativePath, texture.EncodeToPNG());
            }
            finally
            {
                camera.targetTexture = previousTarget;
                RenderTexture.active = previousActive;
                UnityEngine.Object.Destroy(renderTexture);
                UnityEngine.Object.Destroy(texture);
            }
        }
    }

    public sealed class UnityQaProcessExit : IQaProcessExit
    {
        public void Exit(int code)
        {
            Application.Quit(code);
        }
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
