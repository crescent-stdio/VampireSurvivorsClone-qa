using Unity.MLAgents;
using Unity.MLAgents.Actuators;
using Unity.MLAgents.Sensors;
using UnityEngine;

namespace Vampire
{
    public class QaGameplayAgent : Agent
    {
        public const string BehaviorName = "QaGameplay";
        public const int ContinuousActionCount = 2;
        public const int AbilityBranchSize = 5;
        public static readonly int[] DiscreteBranchSizes = { AbilityBranchSize };
        public static ActionSpec ExpectedActionSpec => new ActionSpec(ContinuousActionCount, new[] { AbilityBranchSize });

        [SerializeField] private QaEpisodeController controller;
        private IQaGameplayController controllerOverride;

        private readonly QaRewardTracker rewardTracker = new QaRewardTracker();
        private readonly ScriptedQaPolicy heuristicPolicy = new ScriptedQaPolicy();
        private bool terminalHandled;

        public override void Initialize()
        {
            var gameplayController = GetController();
            if (gameplayController == null)
                throw new System.InvalidOperationException("QaGameplayAgent requires a QaEpisodeController.");

            gameplayController.EnableExternalAgentControl();
        }

        public void ConfigureControllerForTesting(IQaGameplayController testController)
        {
            controllerOverride = testController;
        }

        public override void OnEpisodeBegin()
        {
            terminalHandled = false;
            rewardTracker.Reset();
        }

        public override void CollectObservations(VectorSensor sensor)
        {
            var values = QaGameplayObservationEncoder.Encode(CaptureObservation());
            for (var index = 0; index < QaGameplayObservationEncoder.ObservationSize; index++)
                sensor.AddObservation(values[index]);
        }

        public override void OnActionReceived(ActionBuffers actions)
        {
            if (terminalHandled)
                return;

            var action = QaAgentActionMapper.Map(ToArray(actions.ContinuousActions), ToArray(actions.DiscreteActions));
            var gameplayController = RequireController();
            gameplayController.SubmitExternalAction(action);

            var observation = CaptureObservation();
            AddProgressReward(observation);
            CompleteIfTerminal(gameplayController.CurrentOutcome);
        }

        public override void Heuristic(in ActionBuffers actionsOut)
        {
            var action = heuristicPolicy.Decide(CaptureObservation());
            var continuousActions = actionsOut.ContinuousActions;
            var discreteActions = actionsOut.DiscreteActions;
            if (continuousActions.Length > 0)
                continuousActions[0] = action.Movement.x;
            if (continuousActions.Length > 1)
                continuousActions[1] = action.Movement.y;
            if (discreteActions.Length > 0)
                discreteActions[0] = action.AbilityChoice + 1;
        }

        private QaObservation CaptureObservation()
        {
            return RequireController().CaptureAgentObservation();
        }

        private void AddProgressReward(QaObservation observation)
        {
            var reward = rewardTracker.Evaluate(new QaRewardSnapshot(
                observation.PlayerLevel,
                observation.KillCount,
                CreateStateBucket(observation),
                observation.DamageTaken,
                observation.ElapsedSeconds));
            if (reward != 0f)
                ApplyQaReward(reward);
        }

        private void CompleteIfTerminal(QaEpisodeOutcome outcome)
        {
            if (terminalHandled || outcome == QaEpisodeOutcome.InProgress)
                return;

            terminalHandled = true;
            ApplyQaReward(QaRewardTracker.TerminalReward(outcome));
            EndQaEpisode();
        }

        private static int CreateStateBucket(QaObservation observation)
        {
            return (int)observation.LevelPhase;
        }

        protected virtual void ApplyQaReward(float reward)
        {
            AddReward(reward);
        }

        protected virtual void EndQaEpisode()
        {
            EndEpisode();
        }

        private IQaGameplayController RequireController()
        {
            var gameplayController = GetController();
            if (gameplayController == null)
                throw new System.InvalidOperationException("QaGameplayAgent requires a QaEpisodeController.");
            return gameplayController;
        }

        private IQaGameplayController GetController()
        {
            return controllerOverride ?? (IQaGameplayController)controller;
        }

        private static float[] ToArray(ActionSegment<float> actions)
        {
            var values = new float[actions.Length];
            for (var index = 0; index < actions.Length; index++) values[index] = actions[index];
            return values;
        }

        private static int[] ToArray(ActionSegment<int> actions)
        {
            var values = new int[actions.Length];
            for (var index = 0; index < actions.Length; index++) values[index] = actions[index];
            return values;
        }
    }
}
