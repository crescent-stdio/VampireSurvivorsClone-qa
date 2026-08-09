using System;

namespace Vampire
{
    public interface IQaGameplayController
    {
        event Action<QaEpisodeOutcome> TerminalReached;
        QaEpisodeOutcome CurrentOutcome { get; }

        /// <summary>Elapsed-time scale of the active preset, applied when encoding observations.</summary>
        float ObservationElapsedSecondsScale { get; }

        QaObservation CaptureAgentObservation();
        void EnableExternalAgentControl();
        bool SubmitExternalAction(QaAction action);
        bool AcknowledgeTerminal();
    }
}
