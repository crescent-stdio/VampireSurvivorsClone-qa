using System;

namespace Vampire
{
    public interface IQaGameplayController
    {
        event Action<QaEpisodeOutcome> TerminalReached;
        QaEpisodeOutcome CurrentOutcome { get; }
        QaObservation CaptureAgentObservation();
        void EnableExternalAgentControl();
        bool SubmitExternalAction(QaAction action);
        bool AcknowledgeTerminal();
    }
}
