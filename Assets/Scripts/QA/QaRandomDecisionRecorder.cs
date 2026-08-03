using System;
using System.Globalization;

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
            var recorder = DecisionRecorded;
            if (recorder == null)
                return;

            recorder("random:" + category + ":" + stableId);
        }

        public static void Record(string category, int stableId)
        {
            var recorder = DecisionRecorded;
            if (recorder == null)
                return;

            recorder("random:" + category + ":" + stableId.ToString(CultureInfo.InvariantCulture));
        }

        public static void Record<T>(string category, int stableId)
        {
            var recorder = DecisionRecorded;
            if (recorder == null)
                return;

            var tableType = typeof(T).FullName ?? typeof(T).Name;
            recorder("random:" + category + ":" + tableType + ":" + stableId.ToString(CultureInfo.InvariantCulture));
        }
    }
}
