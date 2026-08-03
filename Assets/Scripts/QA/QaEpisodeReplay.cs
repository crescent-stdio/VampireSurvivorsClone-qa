using System;
using System.Collections.Generic;
using System.IO;
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

    /// <summary>
    /// Serializable replay input. Summary artifacts carry these fields so they can be replayed directly.
    /// </summary>
    [Serializable]
    public sealed class QaReplayTrace
    {
        public const string SchemaVersion = "qa-replay/v1";

        public string Schema = SchemaVersion;
        public QaEpisodeOutcome Outcome;
        public QaActionTraceEntry[] Actions;
        public string[] DiscreteEvents;
        public Vector2[] Positions;

        public QaRecordedEpisode ToRecordedEpisode()
        {
            return new QaRecordedEpisode(Outcome, DiscreteEvents ?? Array.Empty<string>(), Positions ?? Array.Empty<Vector2>());
        }

        public static bool IsReplayRequested(string[] arguments)
        {
            if (arguments == null)
                return false;
            foreach (var argument in arguments)
                if (string.Equals(argument, "-qaMode=replay", StringComparison.Ordinal))
                    return true;
            return false;
        }

        public static bool TryLoadFromCommandLine(string[] arguments, out QaReplayTrace trace, out string error)
        {
            trace = null;
            error = string.Empty;
            string path = null;
            if (arguments != null)
            {
                foreach (var argument in arguments)
                    if (argument != null && argument.StartsWith("-qaReplayPath=", StringComparison.Ordinal))
                        path = argument.Substring("-qaReplayPath=".Length);
            }

            if (string.IsNullOrWhiteSpace(path))
            {
                error = "Replay mode requires -qaReplayPath=<summary.json or replay trace>.";
                return false;
            }

            return TryLoad(path, out trace, out error);
        }

        public static bool TryLoad(string path, out QaReplayTrace trace, out string error)
        {
            trace = null;
            error = string.Empty;
            if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
            {
                error = "Replay trace does not exist.";
                return false;
            }

            try
            {
                var json = File.ReadAllText(path);
                var directTrace = JsonUtility.FromJson<QaReplayTrace>(json);
                if (directTrace != null && directTrace.Schema == SchemaVersion)
                {
                    trace = directTrace;
                    return true;
                }

                var summary = JsonUtility.FromJson<QaEpisodeSummary>(json);
                if (summary != null && summary.Schema == QaArtifactWriter.SchemaVersion && summary.ReplayActions != null)
                {
                    trace = new QaReplayTrace
                    {
                        Outcome = summary.Outcome,
                        Actions = summary.ReplayActions,
                        DiscreteEvents = summary.ReplayDiscreteEvents,
                        Positions = summary.ReplayPositions
                    };
                    return true;
                }

                error = "Replay trace schema is unsupported.";
                return false;
            }
            catch (Exception exception)
            {
                error = "Replay trace could not be read: " + exception.Message;
                return false;
            }
        }
    }

    public sealed class QaReplayPolicy : IQaPolicy
    {
        private readonly QaActionTraceEntry[] actions;
        private int actionIndex;

        public QaReplayPolicy(QaActionTraceEntry[] actions)
        {
            this.actions = actions ?? Array.Empty<QaActionTraceEntry>();
        }

        public QaAction Decide(QaObservation observation)
        {
            if (actionIndex >= actions.Length || actions[actionIndex] == null)
                return new QaAction(Vector2.zero);

            var action = actions[actionIndex++];
            return new QaAction(new Vector2(action.MovementX, action.MovementY), action.AbilityChoice);
        }
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
