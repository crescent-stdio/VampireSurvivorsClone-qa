namespace Vampire
{
    public interface IQaGameplayController
    {
        QaEpisodeOutcome CurrentOutcome { get; }
        QaObservation CaptureAgentObservation();
        void EnableExternalAgentControl();
        bool SubmitExternalAction(QaAction action);
    }
}
