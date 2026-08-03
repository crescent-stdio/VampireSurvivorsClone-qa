using System;
using System.Collections.Generic;
using UnityEngine;

namespace Vampire
{
    public sealed class QaEpisodeRecorder
    {
        private readonly List<string> discreteEvents = new List<string>();
        private readonly List<Vector2> positions = new List<Vector2>();

        public void RecordObservation(QaObservation observation)
        {
            positions.Add(observation.PlayerPosition);
        }

        public void RecordDiscreteEvent(string eventName)
        {
            if (string.IsNullOrWhiteSpace(eventName))
                throw new ArgumentException("Discrete event name must not be empty.", nameof(eventName));

            discreteEvents.Add(eventName);
        }

        public QaRecordedEpisode Create(QaEpisodeOutcome outcome)
        {
            return new QaRecordedEpisode(outcome, discreteEvents, positions);
        }

        public void Reset()
        {
            discreteEvents.Clear();
            positions.Clear();
        }
    }

    public sealed class QaRecordedEpisode
    {
        public QaRecordedEpisode(QaEpisodeOutcome outcome, IReadOnlyList<string> discreteEvents, IReadOnlyList<Vector2> positions)
        {
            Outcome = outcome;
            DiscreteEvents = new List<string>(discreteEvents ?? new string[0]).AsReadOnly();
            Positions = new List<Vector2>(positions ?? new Vector2[0]).AsReadOnly();
        }

        public QaEpisodeOutcome Outcome { get; }
        public IReadOnlyList<string> DiscreteEvents { get; }
        public IReadOnlyList<Vector2> Positions { get; }
    }

    public sealed class QaReplayComparison
    {
        public QaReplayComparison(bool isMatch, string reason)
        {
            IsMatch = isMatch;
            Reason = reason;
        }

        public bool IsMatch { get; }
        public string Reason { get; }
    }

    public static class QaReplayComparator
    {
        public const float PositionTolerance = 0.05f;

        public static QaReplayComparison Compare(QaRecordedEpisode expected, QaRecordedEpisode actual)
        {
            if (expected == null || actual == null)
                return new QaReplayComparison(false, "missing-episode");
            if (expected.Outcome != actual.Outcome)
                return new QaReplayComparison(false, "outcome");
            if (expected.DiscreteEvents.Count != actual.DiscreteEvents.Count)
                return new QaReplayComparison(false, "discrete-event-count");
            if (expected.Positions.Count != actual.Positions.Count)
                return new QaReplayComparison(false, "position-count");

            for (var index = 0; index < expected.DiscreteEvents.Count; index++)
                if (!string.Equals(expected.DiscreteEvents[index], actual.DiscreteEvents[index], StringComparison.Ordinal))
                    return new QaReplayComparison(false, "discrete-event-" + index);

            for (var index = 0; index < expected.Positions.Count; index++)
            {
                if (!IsFinite(expected.Positions[index]) || !IsFinite(actual.Positions[index]))
                    return new QaReplayComparison(false, "non-finite-position-" + index);
                if (Vector2.Distance(expected.Positions[index], actual.Positions[index]) > PositionTolerance)
                    return new QaReplayComparison(false, "position-" + index);
            }

            return new QaReplayComparison(true, string.Empty);
        }

        private static bool IsFinite(Vector2 position)
        {
            return !float.IsNaN(position.x) && !float.IsInfinity(position.x) &&
                   !float.IsNaN(position.y) && !float.IsInfinity(position.y);
        }
    }
}
