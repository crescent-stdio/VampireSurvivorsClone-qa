using System;

namespace Vampire
{
    /// <summary>
    /// Optional QA event sink. Gameplay selection remains unchanged when no QA controller is subscribed.
    /// </summary>
    public static class QaRandomDecisionRecorder
    {
        public static event Action<string> DecisionRecorded;

        public static void Record(string category, string stableId)
        {
            DecisionRecorded?.Invoke("random:" + category + ":" + stableId);
        }
    }
}
