using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Vampire.QA
{
    public class QABridge : MonoBehaviour
    {
        private enum OperationKind
        {
            None,
            Run,
            Scene,
            Settle
        }

        private const int MaxRecentLogs = 24;
        private const int MaxQAEntities = 64;
        private const float ObservationThreatRadius = 8f;
        private string bridgeDirectory;
        private string commandPath;
        private string responseDirectory;
        private string eventPath;
        private string readyPath;
        private string mode = "player";
        private string lastCommandId = "";
        private int seed = 1337;
        private float runTimeScale = 4f;
        private float nextPollTime;
        private readonly List<string> recentLogs = new List<string>();

        private OperationKind operation = OperationKind.None;
        private QACommand activeCommand;
        private float targetLevelTime;
        private float operationDeadline;
        private int settleFrames;
        private string targetScene;
        private string operationResult;
        private bool steeringActive;
        private bool directControlActive;
        private bool planningHoldActive;
        private bool planningHoldInterrupted;
        private QACommand planningHoldCommand;
        private bool steeringCollectChests;
        private string currentSkill = "none";
        private Vector2 steeringHeading = Vector2.right;
        private Vector2 currentSteering = Vector2.zero;
        private string steeringTargetKind = "heading";
        private float steeringTargetDistance = -1f;
        private string currentPlannerIntent = "";
        private int currentTargetId;
        private int steeringInitialChestCount;
        private float steeringInitialHealthRatio;
        private float steeringInitialDangerScore;
        private float steeringInitialChestDistance;
        private Vector2 steeringCheckpointPosition;
        private float nextSteeringCheckpointTime;
        private int steeringStuckWindows;
        private string eventType = "";
        private string eventDetail = "";

        private void Awake()
        {
            DontDestroyOnLoad(gameObject);
            Application.runInBackground = true;

            bridgeDirectory = ReadArgument("-qaBridgeDir", "");
            mode = ReadArgument("-qaMode", "player").ToLowerInvariant();
            int.TryParse(ReadArgument("-qaSeed", "1337"), out seed);
            float parsedScale;
            if (float.TryParse(ReadArgument("-qaTimeScale", "4"), out parsedScale))
                runTimeScale = Mathf.Clamp(parsedScale, 0.1f, 20f);

            if (string.IsNullOrWhiteSpace(bridgeDirectory))
            {
                enabled = false;
                return;
            }

            Directory.CreateDirectory(bridgeDirectory);
            commandPath = Path.Combine(bridgeDirectory, "command.json");
            responseDirectory = Path.Combine(bridgeDirectory, "responses");
            Directory.CreateDirectory(responseDirectory);
            eventPath = Path.Combine(bridgeDirectory, "events.jsonl");
            readyPath = Path.Combine(bridgeDirectory, "ready.json");
            UnityEngine.Random.InitState(seed);
            Application.logMessageReceived += HandleLog;
            SceneManager.sceneLoaded += HandleSceneLoaded;
        }

        private void Start()
        {
            if (!enabled)
                return;

            Time.timeScale = 0;
            QAReadyState ready = new QAReadyState
            {
                ready = true,
                process_id = Process.GetCurrentProcess().Id,
                bridge_directory = bridgeDirectory,
                mode = mode,
                seed = seed
            };
            WriteAtomic(readyPath, JsonUtility.ToJson(ready, true));
            WriteObservation("startup", true, "QA Bridge ready");
        }

        private void OnDestroy()
        {
            Application.logMessageReceived -= HandleLog;
            SceneManager.sceneLoaded -= HandleSceneLoaded;
        }

        private void Update()
        {
            if (!enabled)
                return;

            if (operation != OperationKind.None)
            {
                UpdateOperation();
                return;
            }

            if (planningHoldActive)
                UpdatePlanningHold();

            if (Time.realtimeSinceStartup < nextPollTime)
                return;
            nextPollTime = Time.realtimeSinceStartup + 0.03f;
            PollCommand();
        }

        private void PollCommand()
        {
            if (!File.Exists(commandPath))
                return;

            try
            {
                QACommand command = JsonUtility.FromJson<QACommand>(File.ReadAllText(commandPath));
                if (command == null || string.IsNullOrWhiteSpace(command.id) || command.id == lastCommandId)
                    return;

                lastCommandId = command.id;
                ProcessCommand(command);
            }
            catch (Exception exception)
            {
                HandleLog("QA Bridge command read failed: " + exception, "", LogType.Exception);
            }
        }

        private void ProcessCommand(QACommand command)
        {
            activeCommand = command;
            string action = (command.action ?? "observe").ToLowerInvariant();

            if (planningHoldInterrupted && (action == "direct_steer" || action == "steer" || action == "move" || action == "wait"))
            {
                planningHoldInterrupted = false;
                Complete(command, true, "Continuous movement was interrupted by a live event; re-plan from this fresh observation");
                return;
            }
            if (planningHoldInterrupted)
                planningHoldInterrupted = false;
            if (action != "observe" && planningHoldActive)
            {
                bool seamlessControlReplacement = action == "direct_steer" || action == "steer" || action == "move" || action == "wait";
                CancelPlanningHold(!seamlessControlReplacement);
            }

            try
            {
                switch (action)
                {
                    case "observe":
                        Complete(command, true, "Observation captured");
                        break;
                    case "pause":
                        StopPlayer();
                        Time.timeScale = 0;
                        Complete(command, true, "Game paused");
                        break;
                    case "start_game":
                        StartGame(command);
                        break;
                    case "move":
                        RunSimulation(command, true);
                        break;
                    case "steer":
                        RunSteering(command);
                        break;
                    case "direct_steer":
                        RunDirectSteering(command);
                        break;
                    case "wait":
                        RunSimulation(command, false);
                        break;
                    case "select_upgrade":
                        SelectUpgrade(command);
                        break;
                    case "use_item":
                        UseItem(command);
                        break;
                    case "restart":
                        Restart(command);
                        break;
                    case "return_to_menu":
                        ReturnToMenu(command);
                        break;
                    case "shutdown":
                        Complete(command, true, "Game shutdown requested");
                        Application.Quit();
                        break;
                    default:
                        Complete(command, false, "Unknown action: " + action);
                        break;
                }
            }
            catch (Exception exception)
            {
                Time.timeScale = 0;
                Complete(command, false, exception.GetType().Name + ": " + exception.Message);
            }
        }

        private void StartGame(QACommand command)
        {
            CharacterSelector selector = FindObjectsOfType<CharacterSelector>(true).FirstOrDefault();
            if (selector == null)
            {
                Complete(command, false, "CharacterSelector not found in current scene");
                return;
            }

            int index = command.index;
            if (!selector.StartGameByIndex(index))
            {
                Complete(command, false, "Invalid character index: " + index);
                return;
            }

            BeginSceneOperation(command, "Level 1", "Started character " + index);
        }

        private void RunSimulation(QACommand command, bool applyMovement)
        {
            Character player = FindObjectOfType<Character>();
            LevelManager level = FindObjectOfType<LevelManager>();
            if (player == null || level == null)
            {
                Complete(command, false, "Gameplay objects are not ready");
                return;
            }

            float duration = Mathf.Clamp(command.duration <= 0 ? 1f : command.duration, 0.05f, 30f);
            Vector2 direction = applyMovement ? new Vector2(command.x, command.y).normalized : Vector2.zero;
            player.Move(direction);
            if (direction != Vector2.zero)
                player.StartWalkAnimation();
            else
                player.StopWalkAnimation();

            targetLevelTime = level.LevelTime + duration;
            operationDeadline = Time.realtimeSinceStartup + duration / runTimeScale + 10f;
            operation = OperationKind.Run;
            operationResult = applyMovement ? "Movement interval completed" : "Wait interval completed";
            steeringActive = false;
            directControlActive = false;
            steeringCollectChests = false;
            currentSkill = applyMovement ? "move" : "wait";
            currentPlannerIntent = "";
            currentTargetId = 0;
            currentSteering = direction;
            steeringHeading = direction == Vector2.zero ? Vector2.right : direction;
            steeringTargetKind = applyMovement ? "fixed_direction" : "wait";
            steeringTargetDistance = -1f;
            ClearEvent();
            Time.timeScale = runTimeScale;
        }

        private void RunSteering(QACommand command)
        {
            Character player = FindObjectOfType<Character>();
            EntityManager entities = FindObjectOfType<EntityManager>();
            LevelManager level = FindObjectOfType<LevelManager>();
            if (player == null || entities == null || level == null)
            {
                Complete(command, false, "Gameplay objects are not ready for steering");
                return;
            }

            float duration = Mathf.Clamp(command.duration <= 0 ? 5f : command.duration, 0.25f, 30f);
            Vector2 heading = new Vector2(command.x, command.y);
            if (heading.sqrMagnitude < 0.0001f)
                heading = Vector2.right;
            steeringHeading = heading.normalized;
            steeringActive = true;
            directControlActive = false;
            steeringCollectChests = command.collect_chests;
            currentSkill = "steer";
            currentPlannerIntent = "bridge_hybrid_baseline";
            currentTargetId = 0;
            steeringTargetKind = "heading";
            steeringTargetDistance = -1f;
            currentSteering = steeringHeading;
            steeringInitialChestCount = entities.chests != null ? entities.chests.Count : 0;
            steeringInitialHealthRatio = HealthRatio(player);
            steeringInitialDangerScore = CalculateDanger(player, entities, EffectiveThreatRadius(command), out _);
            steeringCheckpointPosition = player.Position;
            nextSteeringCheckpointTime = level.LevelTime + 1f;
            steeringStuckWindows = 0;
            ClearEvent();

            targetLevelTime = level.LevelTime + duration;
            operationDeadline = Time.realtimeSinceStartup + duration / runTimeScale + 10f;
            operation = OperationKind.Run;
            operationResult = "Steering horizon completed";
            UpdateSteering(player, entities, command);
            Time.timeScale = runTimeScale;
        }

        private void RunDirectSteering(QACommand command)
        {
            Character player = FindObjectOfType<Character>();
            EntityManager entities = FindObjectOfType<EntityManager>();
            LevelManager level = FindObjectOfType<LevelManager>();
            if (player == null || entities == null || level == null)
            {
                Complete(command, false, "Gameplay objects are not ready for direct LLM steering");
                return;
            }

            float duration = Mathf.Clamp(command.duration <= 0 ? 2f : command.duration, 0.25f, 10f);
            Vector2 direction = new Vector2(command.x, command.y);
            if (direction.sqrMagnitude > 1f)
                direction.Normalize();
            steeringHeading = direction.sqrMagnitude > 0.0001f ? direction.normalized : Vector2.right;
            currentSteering = direction;
            steeringActive = true;
            directControlActive = true;
            steeringCollectChests = false;
            currentSkill = "direct_steer";
            currentPlannerIntent = command.intent ?? "";
            currentTargetId = command.target_id;
            steeringTargetKind = "llm_direction";
            steeringTargetDistance = -1f;
            steeringInitialChestCount = entities.chests != null ? entities.chests.Count : 0;
            steeringInitialHealthRatio = HealthRatio(player);
            steeringInitialDangerScore = CalculateDanger(player, entities, EffectiveThreatRadius(command), out _);
            Chest nearestChest = NearestChest(player, entities, out _, out float nearestChestDistance);
            steeringInitialChestDistance = nearestChest != null ? nearestChestDistance : float.PositiveInfinity;
            steeringCheckpointPosition = player.Position;
            nextSteeringCheckpointTime = level.LevelTime + 1f;
            steeringStuckWindows = 0;
            ClearEvent();

            targetLevelTime = level.LevelTime + duration;
            operationDeadline = Time.realtimeSinceStartup + duration / runTimeScale + 10f;
            operation = OperationKind.Run;
            operationResult = "Direct LLM steering horizon completed";
            UpdateDirectSteering(player);
            Time.timeScale = runTimeScale;
        }

        private void UpdateDirectSteering(Character player)
        {
            player.Move(currentSteering);
            if (currentSteering != Vector2.zero)
                player.StartWalkAnimation();
            else
                player.StopWalkAnimation();
        }

        private void UpdateSteering(Character player, EntityManager entities, QACommand command)
        {
            float threatRadius = EffectiveThreatRadius(command);
            float danger = CalculateDanger(player, entities, threatRadius, out Vector2 escape);
            Vector2 desired = steeringHeading;
            float survivalScale = 1f;

            steeringTargetKind = "heading";
            steeringTargetDistance = -1f;
            if (command.collect_chests && HealthRatio(player) > EffectiveHealthInterrupt(command))
            {
                Chest chest = NearestChest(player, entities, out Vector2 chestVector, out float chestDistance);
                float chestRadius = command.chest_radius > 0 ? Mathf.Clamp(command.chest_radius, 1f, 100f) : 12f;
                if (chest != null && chestDistance <= chestRadius)
                {
                    float proximity = 1f - Mathf.Clamp01(chestDistance / chestRadius);
                    float attraction = 0.65f + proximity * 1.15f;
                    desired += chestVector.normalized * attraction;
                    steeringTargetKind = "chest";
                    steeringTargetDistance = chestDistance;
                    if (chestDistance <= 3f && Vector2.Dot(chestVector.normalized, steeringHeading) >= 0f)
                    {
                        // Once a reachable chest is close and still ahead, aim precisely enough to
                        // make collider contact. Retain a small safety term instead of abandoning it.
                        desired = chestVector.normalized * 4f + steeringHeading * 0.2f;
                        survivalScale = 0.2f;
                        steeringTargetKind = "chest_capture";
                    }
                }
            }

            float survivalWeight = command.survival_weight > 0 ? Mathf.Clamp(command.survival_weight, 0f, 5f) : 1.4f;
            if (escape.sqrMagnitude > 0.0001f)
                desired += escape.normalized * survivalWeight * survivalScale * Mathf.Clamp01(0.35f + danger);

            currentSteering = EnforceForwardProgress(
                desired,
                steeringHeading,
                command.min_forward_component > 0 ? Mathf.Clamp(command.min_forward_component, 0.01f, 1f) : 0.15f
            );
            player.Move(currentSteering);
            if (currentSteering != Vector2.zero)
                player.StartWalkAnimation();
            else
                player.StopWalkAnimation();
        }

        private Vector2 EnforceForwardProgress(Vector2 desired, Vector2 heading, float minimumForward)
        {
            if (desired.sqrMagnitude < 0.0001f)
                return heading;
            desired.Normalize();
            float forward = Vector2.Dot(desired, heading);
            if (forward >= minimumForward)
                return desired;

            Vector2 lateral = desired - heading * forward;
            float maxLateral = Mathf.Sqrt(Mathf.Max(0f, 1f - minimumForward * minimumForward));
            if (lateral.sqrMagnitude > 0.0001f)
                lateral = lateral.normalized * maxLateral;
            return (heading * minimumForward + lateral).normalized;
        }

        private void SelectUpgrade(QACommand command)
        {
            AbilitySelectionDialog dialog = FindObjectOfType<AbilitySelectionDialog>();
            if (dialog == null || !dialog.MenuOpen)
            {
                Complete(command, false, "Upgrade dialog is not open");
                return;
            }
            if (!dialog.TrySelectOption(command.index))
            {
                Complete(command, false, "Invalid upgrade index: " + command.index);
                return;
            }

            BeginSettleOperation(command, 3, "Selected upgrade " + command.index);
        }

        private void UseItem(QACommand command)
        {
            Inventory inventory = FindObjectOfType<Inventory>();
            if (inventory == null)
            {
                Complete(command, false, "Inventory not found");
                return;
            }

            bool used = inventory.UseItemAt(command.index);
            BeginSettleOperation(command, 2, used ? "Used inventory slot " + command.index : "Inventory slot is empty or invalid");
        }

        private void Restart(QACommand command)
        {
            LevelManager level = FindObjectOfType<LevelManager>();
            if (level == null)
            {
                Complete(command, false, "LevelManager not found");
                return;
            }

            string scene = SceneManager.GetActiveScene().name;
            level.Restart();
            BeginSceneOperation(command, scene, "Level restarted");
        }

        private void ReturnToMenu(QACommand command)
        {
            LevelManager level = FindObjectOfType<LevelManager>();
            if (level == null)
            {
                Complete(command, false, "LevelManager not found");
                return;
            }

            level.ReturnToMainMenu();
            BeginSceneOperation(command, "Main Menu", "Returned to main menu");
        }

        private void BeginSceneOperation(QACommand command, string sceneName, string result)
        {
            activeCommand = command;
            targetScene = sceneName;
            operationResult = result;
            settleFrames = 4;
            operationDeadline = Time.realtimeSinceStartup + 30f;
            operation = OperationKind.Scene;
        }

        private void BeginSettleOperation(QACommand command, int frames, string result)
        {
            activeCommand = command;
            settleFrames = frames;
            operationResult = result;
            operationDeadline = Time.realtimeSinceStartup + 5f;
            operation = OperationKind.Settle;
        }

        private void UpdateOperation()
        {
            if (operation == OperationKind.Run)
            {
                LevelManager level = FindObjectOfType<LevelManager>();
                Character player = FindObjectOfType<Character>();
                EntityManager entities = FindObjectOfType<EntityManager>();
                AbilitySelectionDialog dialog = FindObjectOfType<AbilitySelectionDialog>();
                if (steeringActive && player != null && entities != null && activeCommand != null)
                {
                    if (directControlActive)
                        UpdateDirectSteering(player);
                    else
                        UpdateSteering(player, entities, activeCommand);
                }
                bool interrupted = dialog != null && dialog.MenuOpen;
                bool dying = player != null && !player.IsAlive;
                bool gameOver = dying && Mathf.Approximately(Time.timeScale, 0);
                bool reachedTarget = !dying && level != null && level.LevelTime >= targetLevelTime;
                bool timedOut = Time.realtimeSinceStartup >= operationDeadline;

                if (steeringActive && player != null && entities != null && level != null && activeCommand != null)
                {
                    int chestCount = entities.chests != null ? entities.chests.Count : 0;
                    if (chestCount < steeringInitialChestCount)
                    {
                        SetEvent("chest_collected", "A tracked chest was opened during local steering.", level.LevelTime);
                        FinishOperation(true, "Steering interrupted after chest collection");
                        return;
                    }

                    if (directControlActive)
                    {
                        Chest nearestChest = NearestChest(player, entities, out _, out float nearestChestDistance);
                        float chestInterruptDistance = activeCommand.interrupt_chest_distance > 0
                            ? Mathf.Clamp(activeCommand.interrupt_chest_distance, 0.5f, 20f)
                            : 2.5f;
                        if (nearestChest != null && steeringInitialChestDistance > chestInterruptDistance && nearestChestDistance <= chestInterruptDistance)
                        {
                            SetEvent("chest_nearby", "A chest entered the close-control radius; the LLM must decide how to approach it.", level.LevelTime);
                            FinishOperation(true, "Direct steering interrupted by a nearby chest");
                            return;
                        }
                    }

                    float healthRatio = HealthRatio(player);
                    float healthInterrupt = EffectiveHealthInterrupt(activeCommand);
                    if (steeringInitialHealthRatio > healthInterrupt && healthRatio <= healthInterrupt)
                    {
                        SetEvent("low_health", "Health crossed the configured interrupt threshold.", level.LevelTime);
                        FinishOperation(true, "Steering interrupted by low health");
                        return;
                    }

                    float danger = CalculateDanger(player, entities, EffectiveThreatRadius(activeCommand), out _);
                    float dangerInterrupt = activeCommand.interrupt_danger_score > 0
                        ? Mathf.Clamp01(activeCommand.interrupt_danger_score)
                        : 0.85f;
                    if (steeringInitialDangerScore < dangerInterrupt && danger >= dangerInterrupt)
                    {
                        SetEvent("danger_spike", "Local danger crossed the configured interrupt threshold.", level.LevelTime);
                        FinishOperation(true, "Steering interrupted by danger spike");
                        return;
                    }

                    if (level.LevelTime >= nextSteeringCheckpointTime)
                    {
                        float displacement = Vector2.Distance(player.Position, steeringCheckpointPosition);
                        steeringStuckWindows = displacement < 0.15f ? steeringStuckWindows + 1 : 0;
                        steeringCheckpointPosition = player.Position;
                        nextSteeringCheckpointTime = level.LevelTime + 1f;
                        if (steeringStuckWindows >= 2)
                        {
                            SetEvent("stuck", "Forward displacement stayed below 0.15 units for two checks.", level.LevelTime);
                            FinishOperation(true, "Steering interrupted after detecting a stuck state");
                            return;
                        }
                    }
                }

                if (interrupted || gameOver || reachedTarget || timedOut)
                {
                    string result = interrupted ? "Paused for upgrade selection" : gameOver ? "Paused after player death" : timedOut ? "Simulation interval timed out" : operationResult;
                    if (interrupted)
                        SetEvent("upgrade_open", "A blocking upgrade dialog opened.", level != null ? level.LevelTime : 0f);
                    else if (gameOver)
                        SetEvent("player_death", "The player died during the active horizon.", level != null ? level.LevelTime : 0f);
                    else if (timedOut)
                        SetEvent("timeout", "The simulation horizon exceeded its real-time deadline.", level != null ? level.LevelTime : 0f);
                    else if (steeringActive)
                        SetEvent("horizon_complete", "The low-frequency planning horizon completed.", level != null ? level.LevelTime : 0f);
                    FinishOperation(!timedOut, result);
                }
                return;
            }

            if (operation == OperationKind.Scene)
            {
                bool sceneReady = SceneManager.GetActiveScene().name == targetScene;
                if (sceneReady)
                {
                    settleFrames--;
                    if (settleFrames <= 0)
                    {
                        FinishOperation(true, operationResult);
                        return;
                    }
                }
                if (Time.realtimeSinceStartup >= operationDeadline)
                    FinishOperation(false, "Timed out waiting for scene: " + targetScene);
                return;
            }

            if (operation == OperationKind.Settle)
            {
                settleFrames--;
                if (settleFrames <= 0)
                    FinishOperation(true, operationResult);
                else if (Time.realtimeSinceStartup >= operationDeadline)
                    FinishOperation(false, "Timed out while settling action");
            }
        }

        private void FinishOperation(bool ok, string result)
        {
            QACommand command = activeCommand;
            bool continuePlanning =
                ok && command != null && command.continue_during_planning && directControlActive &&
                eventType == "horizon_complete" && CanContinueDuringPlanning(command);
            operation = OperationKind.None;
            activeCommand = null;
            if (continuePlanning)
            {
                BeginPlanningHold(command);
                Time.timeScale = runTimeScale;
            }
            else
            {
                steeringActive = false;
                directControlActive = false;
                StopPlayer();
                Time.timeScale = 0;
            }
            Complete(command, ok, result);
        }

        private bool CanContinueDuringPlanning(QACommand command)
        {
            Character player = FindObjectOfType<Character>();
            EntityManager entities = FindObjectOfType<EntityManager>();
            AbilitySelectionDialog dialog = FindObjectOfType<AbilitySelectionDialog>();
            if (player == null || entities == null || !player.IsAlive || (dialog != null && dialog.MenuOpen))
                return false;
            if (HealthRatio(player) <= EffectiveHealthInterrupt(command))
                return false;
            float danger = CalculateDanger(player, entities, EffectiveThreatRadius(command), out _);
            float dangerInterrupt = command.interrupt_danger_score > 0
                ? Mathf.Clamp01(command.interrupt_danger_score)
                : 0.85f;
            if (danger >= dangerInterrupt)
                return false;
            Chest nearestChest = NearestChest(player, entities, out _, out float nearestChestDistance);
            float chestInterruptDistance = command.interrupt_chest_distance > 0
                ? Mathf.Clamp(command.interrupt_chest_distance, 0.5f, 20f)
                : 2.5f;
            return nearestChest == null || nearestChestDistance > chestInterruptDistance;
        }

        private void BeginPlanningHold(QACommand command)
        {
            Character player = FindObjectOfType<Character>();
            EntityManager entities = FindObjectOfType<EntityManager>();
            LevelManager level = FindObjectOfType<LevelManager>();
            planningHoldActive = true;
            planningHoldInterrupted = false;
            planningHoldCommand = command;
            steeringActive = true;
            directControlActive = true;
            if (player != null && entities != null)
            {
                steeringInitialChestCount = entities.chests != null ? entities.chests.Count : 0;
                steeringInitialHealthRatio = HealthRatio(player);
                steeringInitialDangerScore = CalculateDanger(player, entities, EffectiveThreatRadius(command), out _);
                Chest nearestChest = NearestChest(player, entities, out _, out float nearestChestDistance);
                steeringInitialChestDistance = nearestChest != null ? nearestChestDistance : float.PositiveInfinity;
                steeringCheckpointPosition = player.Position;
            }
            nextSteeringCheckpointTime = level != null ? level.LevelTime + 1f : 0f;
            steeringStuckWindows = 0;
        }

        private void UpdatePlanningHold()
        {
            Character player = FindObjectOfType<Character>();
            EntityManager entities = FindObjectOfType<EntityManager>();
            LevelManager level = FindObjectOfType<LevelManager>();
            AbilitySelectionDialog dialog = FindObjectOfType<AbilitySelectionDialog>();
            QACommand command = planningHoldCommand;
            if (player == null || entities == null || level == null || command == null)
            {
                InterruptPlanningHold("planning_hold_invalid", "Gameplay objects disappeared while the previous LLM vector was held.", 0f);
                return;
            }

            UpdateDirectSteering(player);
            if (dialog != null && dialog.MenuOpen)
            {
                InterruptPlanningHold("upgrade_open", "A blocking upgrade dialog opened while the next LLM plan was pending.", level.LevelTime);
                return;
            }
            if (!player.IsAlive)
            {
                InterruptPlanningHold("player_death", "The player died while the next LLM plan was pending.", level.LevelTime);
                return;
            }

            int chestCount = entities.chests != null ? entities.chests.Count : 0;
            if (chestCount < steeringInitialChestCount)
            {
                InterruptPlanningHold("chest_collected", "A chest was opened while the next LLM plan was pending.", level.LevelTime);
                return;
            }
            Chest nearestChest = NearestChest(player, entities, out _, out float nearestChestDistance);
            float chestInterruptDistance = command.interrupt_chest_distance > 0
                ? Mathf.Clamp(command.interrupt_chest_distance, 0.5f, 20f)
                : 2.5f;
            if (nearestChest != null && steeringInitialChestDistance > chestInterruptDistance && nearestChestDistance <= chestInterruptDistance)
            {
                InterruptPlanningHold("chest_nearby", "A chest entered close-control range while the next LLM plan was pending.", level.LevelTime);
                return;
            }

            float healthRatio = HealthRatio(player);
            float healthInterrupt = EffectiveHealthInterrupt(command);
            if (steeringInitialHealthRatio > healthInterrupt && healthRatio <= healthInterrupt)
            {
                InterruptPlanningHold("low_health", "Health crossed the interrupt threshold while the next LLM plan was pending.", level.LevelTime);
                return;
            }
            float danger = CalculateDanger(player, entities, EffectiveThreatRadius(command), out _);
            float dangerInterrupt = command.interrupt_danger_score > 0
                ? Mathf.Clamp01(command.interrupt_danger_score)
                : 0.85f;
            if (steeringInitialDangerScore < dangerInterrupt && danger >= dangerInterrupt)
            {
                InterruptPlanningHold("danger_spike", "Danger crossed the interrupt threshold while the next LLM plan was pending.", level.LevelTime);
                return;
            }
            if (level.LevelTime >= nextSteeringCheckpointTime)
            {
                float displacement = Vector2.Distance(player.Position, steeringCheckpointPosition);
                steeringStuckWindows = displacement < 0.15f ? steeringStuckWindows + 1 : 0;
                steeringCheckpointPosition = player.Position;
                nextSteeringCheckpointTime = level.LevelTime + 1f;
                if (steeringStuckWindows >= 2)
                    InterruptPlanningHold("stuck", "Movement stayed below 0.15 units for two checks while planning.", level.LevelTime);
            }
        }

        private void InterruptPlanningHold(string type, string detail, float levelTime)
        {
            CancelPlanningHold(true);
            planningHoldInterrupted = true;
            SetEvent(type, detail, levelTime);
        }

        private void CancelPlanningHold(bool stopPlayer)
        {
            planningHoldActive = false;
            planningHoldCommand = null;
            if (stopPlayer)
            {
                steeringActive = false;
                directControlActive = false;
                StopPlayer();
                Time.timeScale = 0;
            }
        }

        private void Complete(QACommand command, bool ok, string result)
        {
            string commandId = command != null ? command.id : "unknown";
            WriteObservation(commandId, ok, result);
            ClearEvent();
        }

        private void WriteObservation(string commandId, bool ok, string result)
        {
            QAObservation observation = CaptureObservation(commandId, ok, result);
            string json = JsonUtility.ToJson(observation, true);
            string safeCommandId = string.Concat(commandId.Select(character => Path.GetInvalidFileNameChars().Contains(character) ? '_' : character));
            WriteAtomic(Path.Combine(responseDirectory, safeCommandId + ".json"), json);
            File.AppendAllText(eventPath, JsonUtility.ToJson(observation, false) + Environment.NewLine);
        }

        private QAObservation CaptureObservation(string commandId, bool ok, string result)
        {
            Character player = FindObjectOfType<Character>();
            EntityManager entities = FindObjectOfType<EntityManager>();
            LevelManager level = FindObjectOfType<LevelManager>();
            StatsManager stats = FindObjectOfType<StatsManager>();
            Inventory inventory = FindObjectOfType<Inventory>();
            AbilitySelectionDialog abilityDialog = FindObjectOfType<AbilitySelectionDialog>();
            CharacterSelector selector = FindObjectsOfType<CharacterSelector>(true).FirstOrDefault();
            string phase = ObservationPhase(player, abilityDialog, selector);
            bool paused = Mathf.Approximately(Time.timeScale, 0);

            QAObservation observation = new QAObservation
            {
                command_id = commandId,
                ok = ok,
                result = result,
                timestamp_utc = DateTime.UtcNow.ToString("o"),
                mode = mode,
                seed = seed,
                scene = SceneManager.GetActiveScene().name,
                phase = phase,
                paused = paused,
                pause_reason = ObservationPauseReason(paused, phase, result),
                awaiting_agent_command = true,
                time_scale = Time.timeScale,
                frame = Time.frameCount,
                player = CapturePlayer(player),
                world = CaptureWorld(player, entities, level),
                progress = CaptureProgress(level, stats),
                menu = CaptureMenu(player, abilityDialog, selector),
                inventory = CaptureInventory(inventory),
                controller = CaptureController(player, entities),
                event_state = new EventState
                {
                    type = eventType,
                    detail = eventDetail,
                    level_time = level != null ? level.LevelTime : 0f
                },
                available_actions = AvailableActions(player, abilityDialog, selector),
                recent_logs = recentLogs.ToArray()
            };
            return observation;
        }

        private string ObservationPhase(
            Character player,
            AbilitySelectionDialog dialog,
            CharacterSelector selector)
        {
            if (selector != null)
                return "character_select";
            if (dialog != null && dialog.MenuOpen)
                return "upgrade_selection";
            if (player != null && !player.IsAlive)
                return "game_over";
            if (player != null)
                return "active_gameplay";
            return "loading_or_unavailable";
        }

        private string ObservationPauseReason(bool paused, string phase, string result)
        {
            if (!paused)
                return planningHoldActive ? "simulation_running_while_planning" : "simulation_running";
            if (phase == "character_select")
                return "character_select";
            if (phase == "upgrade_selection")
                return "upgrade_dialog";
            if (phase == "game_over")
                return "game_over";
            if (result == "Game paused")
                return "explicit_pause";
            if (!string.IsNullOrEmpty(eventType))
                return "event_decision_boundary";
            return "agent_decision_boundary";
        }

        private PlayerState CapturePlayer(Character player)
        {
            PlayerState state = new PlayerState
            {
                present = player != null,
                position = new VectorState(),
                velocity = new VectorState()
            };
            if (player == null)
                return state;

            Vector2 position = player.Position;
            Vector2 velocity = player.Velocity;
            state.alive = player.IsAlive;
            state.position = new VectorState(position.x, position.y);
            state.velocity = new VectorState(velocity.x, velocity.y);
            state.health = player.CurrentHealth;
            state.max_health = player.MaxHealth;
            state.health_ratio = state.max_health > 0 ? state.health / state.max_health : 0;
            state.level = player.CurrentLevel;
            state.exp = player.CurrentExperience;
            state.next_level_exp = player.NextExperience;
            state.exp_ratio = state.next_level_exp > 0 ? state.exp / state.next_level_exp : 0;
            return state;
        }

        private WorldState CaptureWorld(Character player, EntityManager entities, LevelManager level)
        {
            WorldState state = new WorldState
            {
                nearest_enemy_distance = -1,
                nearest_enemy_vector = new VectorState(),
                nearest_chest_distance = -1,
                nearest_chest_vector = new VectorState(),
                nearest_pickup_distance = -1,
                nearest_pickup_vector = new VectorState(),
                nearest_pickup_kind = "",
                escape_vector = new VectorState(),
                enemy_octants = new int[8],
                qa_entities = new EntityPositionState[0],
                visible_chests = new EntityPositionState[0],
                mini_boss_spawned = level != null && level.MiniBossSpawned,
                final_boss_spawned = level != null && level.FinalBossSpawned
            };
            if (entities == null)
                return state;

            state.enemy_count = entities.LivingMonsters != null ? entities.LivingMonsters.Count : 0;
            state.pickup_count = entities.MagneticCollectables != null ? entities.MagneticCollectables.Count : 0;
            state.chest_count = entities.chests != null ? entities.chests.Count : 0;
            if (player == null)
                return state;

            Vector2 playerPosition = player.Position;
            float nearest = float.PositiveInfinity;
            Vector2 nearestVector = Vector2.zero;
            List<EntityPositionState> details = new List<EntityPositionState>();
            Vector2 escape = Vector2.zero;
            float dangerTotal = 0f;
            if (entities.LivingMonsters != null)
            {
                foreach (Monster monster in entities.LivingMonsters)
                {
                    if (monster == null)
                        continue;
                    Vector2 relative = (Vector2)monster.transform.position - playerPosition;
                    float distance = relative.magnitude;
                    if (distance < nearest)
                    {
                        nearest = distance;
                        nearestVector = relative;
                    }

                    float angle = Mathf.Atan2(relative.y, relative.x) * Mathf.Rad2Deg;
                    int octant = Mathf.RoundToInt(angle / 45f);
                    octant = ((octant % 8) + 8) % 8;
                    state.enemy_octants[octant]++;

                    if (distance < ObservationThreatRadius && distance > 0.001f)
                    {
                        float weight = 1f - Mathf.Clamp01(distance / ObservationThreatRadius);
                        dangerTotal += weight;
                        escape -= relative.normalized * weight;
                        state.nearby_enemy_count++;
                    }

                    if (mode == "qa" && details.Count < MaxQAEntities)
                    {
                        details.Add(new EntityPositionState
                        {
                            id = monster.GetInstanceID(),
                            kind = monster.GetType().Name,
                            x = monster.transform.position.x,
                            y = monster.transform.position.y,
                            relative_x = relative.x,
                            relative_y = relative.y,
                            distance = distance
                        });
                    }
                }
            }

            if (!float.IsPositiveInfinity(nearest))
            {
                state.nearest_enemy_distance = nearest;
                state.nearest_enemy_vector = new VectorState(nearestVector.x, nearestVector.y);
            }

            Chest nearestChest = NearestChest(player, entities, out Vector2 chestVector, out float chestDistance);
            if (nearestChest != null)
            {
                state.nearest_chest_distance = chestDistance;
                state.nearest_chest_vector = new VectorState(chestVector.x, chestVector.y);
            }
            if (entities.chests != null)
            {
                List<EntityPositionState> chestDetails = new List<EntityPositionState>();
                foreach (Chest chest in entities.chests)
                {
                    if (chest == null || !chest.gameObject.activeInHierarchy)
                        continue;
                    Vector2 relative = (Vector2)chest.transform.position - playerPosition;
                    chestDetails.Add(new EntityPositionState
                    {
                        id = chest.GetInstanceID(),
                        kind = "Chest",
                        x = chest.transform.position.x,
                        y = chest.transform.position.y,
                        relative_x = relative.x,
                        relative_y = relative.y,
                        distance = relative.magnitude
                    });
                }
                state.visible_chests = chestDetails.OrderBy(chest => chest.distance).Take(8).ToArray();
            }

            Collectable nearestPickup = NearestPickup(player, entities, out Vector2 pickupVector, out float pickupDistance);
            if (nearestPickup != null)
            {
                state.nearest_pickup_distance = pickupDistance;
                state.nearest_pickup_vector = new VectorState(pickupVector.x, pickupVector.y);
                state.nearest_pickup_kind = nearestPickup.GetType().Name;
            }

            state.danger_score = Mathf.Clamp01(dangerTotal / 4f);
            if (escape.sqrMagnitude > 0.0001f)
                escape.Normalize();
            state.escape_vector = new VectorState(escape.x, escape.y);
            state.qa_entities = details.ToArray();
            return state;
        }

        private ControllerState CaptureController(Character player, EntityManager entities)
        {
            float danger = 0f;
            if (player != null && entities != null)
                danger = CalculateDanger(player, entities, ObservationThreatRadius, out _);
            return new ControllerState
            {
                active = steeringActive,
                skill = currentSkill,
                heading = new VectorState(steeringHeading.x, steeringHeading.y),
                steering = new VectorState(currentSteering.x, currentSteering.y),
                target_kind = steeringTargetKind,
                target_distance = steeringTargetDistance,
                collect_chests = steeringCollectChests,
                danger_score = danger,
                planner_intent = currentPlannerIntent,
                target_id = currentTargetId
            };
        }

        private Chest NearestChest(Character player, EntityManager entities, out Vector2 relative, out float distance)
        {
            Chest nearestChest = null;
            relative = Vector2.zero;
            distance = float.PositiveInfinity;
            if (player == null || entities == null || entities.chests == null)
                return null;
            Vector2 playerPosition = player.Position;
            foreach (Chest chest in entities.chests)
            {
                if (chest == null || !chest.gameObject.activeInHierarchy)
                    continue;
                Vector2 candidate = (Vector2)chest.transform.position - playerPosition;
                float candidateDistance = candidate.magnitude;
                if (candidateDistance < distance)
                {
                    nearestChest = chest;
                    relative = candidate;
                    distance = candidateDistance;
                }
            }
            if (nearestChest == null)
                distance = -1f;
            return nearestChest;
        }

        private Collectable NearestPickup(Character player, EntityManager entities, out Vector2 relative, out float distance)
        {
            Collectable nearestPickup = null;
            relative = Vector2.zero;
            distance = float.PositiveInfinity;
            if (player == null || entities == null || entities.MagneticCollectables == null)
                return null;
            Vector2 playerPosition = player.Position;
            foreach (Collectable pickup in entities.MagneticCollectables)
            {
                if (pickup == null || !pickup.gameObject.activeInHierarchy)
                    continue;
                Vector2 candidate = (Vector2)pickup.transform.position - playerPosition;
                float candidateDistance = candidate.magnitude;
                if (candidateDistance < distance)
                {
                    nearestPickup = pickup;
                    relative = candidate;
                    distance = candidateDistance;
                }
            }
            if (nearestPickup == null)
                distance = -1f;
            return nearestPickup;
        }

        private float CalculateDanger(Character player, EntityManager entities, float threatRadius, out Vector2 escape)
        {
            escape = Vector2.zero;
            if (player == null || entities == null || entities.LivingMonsters == null)
                return 0f;
            float total = 0f;
            Vector2 playerPosition = player.Position;
            foreach (Monster monster in entities.LivingMonsters)
            {
                if (monster == null)
                    continue;
                Vector2 relative = (Vector2)monster.transform.position - playerPosition;
                float distance = relative.magnitude;
                if (distance <= 0.001f || distance >= threatRadius)
                    continue;
                float weight = 1f - Mathf.Clamp01(distance / threatRadius);
                total += weight;
                escape -= relative.normalized * weight;
            }
            if (escape.sqrMagnitude > 0.0001f)
                escape.Normalize();
            return Mathf.Clamp01(total / 4f);
        }

        private float EffectiveThreatRadius(QACommand command)
        {
            return command.threat_radius > 0 ? Mathf.Clamp(command.threat_radius, 1f, 30f) : ObservationThreatRadius;
        }

        private float EffectiveHealthInterrupt(QACommand command)
        {
            return command.interrupt_health_ratio > 0 ? Mathf.Clamp01(command.interrupt_health_ratio) : 0.3f;
        }

        private float HealthRatio(Character player)
        {
            return player != null && player.MaxHealth > 0 ? player.CurrentHealth / player.MaxHealth : 0f;
        }

        private void SetEvent(string type, string detail, float levelTime)
        {
            eventType = type;
            eventDetail = detail;
        }

        private void ClearEvent()
        {
            eventType = "";
            eventDetail = "";
        }

        private ProgressState CaptureProgress(LevelManager level, StatsManager stats)
        {
            return new ProgressState
            {
                level_time = level != null ? level.LevelTime : 0,
                monsters_killed = stats != null ? stats.MonstersKilled : 0,
                damage_dealt = stats != null ? stats.DamageDealt : 0,
                damage_taken = stats != null ? stats.DamageTaken : 0,
                coins_gained = stats != null ? stats.CoinsGained : 0
            };
        }

        private MenuState CaptureMenu(Character player, AbilitySelectionDialog dialog, CharacterSelector selector)
        {
            IReadOnlyList<Ability> abilities = dialog == null
                ? Array.Empty<Ability>()
                : dialog.DisplayedAbilities;
            AbilityChoiceState[] choices = new AbilityChoiceState[abilities.Count];
            for (int i = 0; i < abilities.Count; i++)
            {
                Ability ability = abilities[i];
                choices[i] = new AbilityChoiceState
                {
                    index = i,
                    name = ability.Name,
                    description = ability.Description,
                    level = ability.Level,
                    owned = ability.Owned
                };
            }

            return new MenuState
            {
                upgrade_open = dialog != null && dialog.MenuOpen,
                game_over = player != null && !player.IsAlive && Mathf.Approximately(Time.timeScale, 0),
                character_count = selector != null ? selector.CharacterCount : 0,
                choices = choices
            };
        }

        private InventoryState CaptureInventory(Inventory inventory)
        {
            if (inventory == null)
                return new InventoryState { slots = new InventorySlotState[0] };

            InventorySlotState[] slots = new InventorySlotState[inventory.SlotCount];
            for (int i = 0; i < inventory.SlotCount; i++)
            {
                InventorySlot slot = inventory.GetSlot(i);
                slots[i] = new InventorySlotState
                {
                    index = i,
                    type = slot != null ? slot.ItemTypeName : "",
                    count = slot != null ? slot.Count : 0,
                    pending_count = slot != null ? slot.PendingCount : 0
                };
            }
            return new InventoryState { slots = slots };
        }

        private string[] AvailableActions(Character player, AbilitySelectionDialog dialog, CharacterSelector selector)
        {
            if (selector != null)
                return new[] { "observe", "start_game", "pause", "shutdown" };
            if (dialog != null && dialog.MenuOpen)
                return new[] { "observe", "select_upgrade", "pause", "shutdown" };
            if (player != null && !player.IsAlive)
                return new[] { "observe", "restart", "return_to_menu", "shutdown" };
            if (player != null)
                return new[] { "observe", "direct_steer", "steer", "move", "wait", "use_item", "pause", "restart", "return_to_menu", "shutdown" };
            return new[] { "observe", "pause", "shutdown" };
        }

        private void StopPlayer()
        {
            Character player = FindObjectOfType<Character>();
            if (player == null)
                return;
            player.Move(Vector2.zero);
            player.StopWalkAnimation();
        }

        private void HandleSceneLoaded(Scene scene, LoadSceneMode loadMode)
        {
            UnityEngine.Random.InitState(seed);
        }

        private void HandleLog(string condition, string stackTrace, LogType type)
        {
            if (type != LogType.Warning && type != LogType.Error && type != LogType.Exception && type != LogType.Assert)
                return;
            string text = type + ": " + condition;
            if (!string.IsNullOrWhiteSpace(stackTrace) && (type == LogType.Error || type == LogType.Exception))
                text += "\n" + stackTrace;
            recentLogs.Add(text);
            if (recentLogs.Count > MaxRecentLogs)
                recentLogs.RemoveAt(0);
        }

        private string ReadArgument(string name, string fallback)
        {
            string[] args = Environment.GetCommandLineArgs();
            for (int i = 0; i < args.Length - 1; i++)
            {
                if (args[i] == name)
                    return args[i + 1];
            }
            return fallback;
        }

        private void WriteAtomic(string path, string contents)
        {
            string temp = path + ".tmp";
            File.WriteAllText(temp, contents);
            if (File.Exists(path))
                File.Delete(path);
            File.Move(temp, path);
        }
    }
}
