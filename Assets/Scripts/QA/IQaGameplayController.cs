namespace Vampire
{
    public interface IQaGameplayController
    {
        QaEpisodeOutcome CurrentOutcome { get; }
        QaObservation CaptureAgentObservation();
        void ApplyAgentAction(QaAction action);
    }
}
