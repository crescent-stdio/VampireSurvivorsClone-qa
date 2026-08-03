using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using UnityEngine;

namespace Vampire
{
    [Serializable]
    public sealed class QaActionTraceEntry
    {
        public int Sequence;
        public int Tick;
        public float Time;
        public float MovementX;
        public float MovementY;
        public int AbilityChoice;

        public QaActionTraceEntry(int sequence, int tick, float time, QaAction action)
        {
            Sequence = sequence;
            Tick = tick;
            Time = time;
            MovementX = action.Movement.x;
            MovementY = action.Movement.y;
            AbilityChoice = action.AbilityChoice;
        }
    }

    [Serializable]
    public sealed class QaTelemetryEntry
    {
        public int Tick;
        public float Time;
        public string Event;

        public QaTelemetryEntry(int tick, float time, string eventName)
        {
            Tick = tick;
            Time = time;
            Event = eventName;
        }
    }

    [Serializable]
    public sealed class QaArtifactLine
    {
        public string Schema;
        public string Kind;
        public int Seed;
        public int Tick;
        public int Sequence;
        public float Time;
        public QaEpisodeOutcome Outcome;
        public string Event;
        public float MovementX;
        public float MovementY;
        public int AbilityChoice;
    }

    [Serializable]
    public sealed class QaEpisodeSummary
    {
        public string Schema;
        public int Seed;
        public QaEpisodeOutcome Outcome;
        public float ElapsedSeconds;
        public int KillCount;
        public int FinalLevel;
        public string FailureReason;
        public int RecordedPositionCount;
        public int DiscreteEventCount;
    }

    public sealed class QaArtifactPaths
    {
        public string ActionTracePath { get; }
        public string TelemetryPath { get; }
        public string SummaryPath { get; }

        public QaArtifactPaths(string actionTracePath, string telemetryPath, string summaryPath)
        {
            ActionTracePath = actionTracePath;
            TelemetryPath = telemetryPath;
            SummaryPath = summaryPath;
        }
    }

    public sealed class QaArtifactWriteResult
    {
        private QaArtifactWriteResult(bool success, QaArtifactPaths paths, string failureReason)
        {
            Success = success;
            Paths = paths;
            FailureReason = failureReason;
        }

        public bool Success { get; }
        public QaArtifactPaths Paths { get; }
        public string FailureReason { get; }

        public static QaArtifactWriteResult Succeeded(QaArtifactPaths paths)
        {
            return new QaArtifactWriteResult(true, paths, string.Empty);
        }

        public static QaArtifactWriteResult Failed(string reason)
        {
            return new QaArtifactWriteResult(false, null, reason ?? string.Empty);
        }
    }

    public interface IQaArtifactWriter
    {
        QaArtifactWriteResult Write(
            QaEpisodeResult result,
            IReadOnlyList<QaActionTraceEntry> trace,
            IReadOnlyList<QaTelemetryEntry> telemetry,
            QaRecordedEpisode recordedEpisode);
    }

    public interface IQaArtifactFileSystem
    {
        void CreateDirectory(string path);
        void WriteAllText(string path, string contents);
        void MoveReplace(string sourcePath, string destinationPath);
        void DeleteFile(string path);
    }

    public sealed class QaArtifactWriter : IQaArtifactWriter
    {
        public const string SchemaVersion = "qa-episode/v1";
        private const string DefaultDirectory = "QAArtifacts";

        private readonly string directory;
        private readonly int seed;
        private readonly IQaArtifactFileSystem fileSystem;
        private readonly string invalidDirectoryReason;

        public QaArtifactWriter(string directory, int seed, IQaArtifactFileSystem fileSystem = null)
        {
            this.seed = seed;
            this.fileSystem = fileSystem ?? new SystemQaArtifactFileSystem();
            if (!TryNormalizeDirectory(directory, out this.directory))
                invalidDirectoryReason = "Artifact directory must be a relative path inside the launch directory.";
        }

        public QaArtifactWriteResult Write(
            QaEpisodeResult result,
            IReadOnlyList<QaActionTraceEntry> trace,
            IReadOnlyList<QaTelemetryEntry> telemetry,
            QaRecordedEpisode recordedEpisode)
        {
            if (invalidDirectoryReason != null)
                return QaArtifactWriteResult.Failed(invalidDirectoryReason);

            try
            {
                fileSystem.CreateDirectory(directory);
                var prefix = "episode-" + seed.ToString("D8");
                var actionPath = Path.Combine(directory, prefix + "-actions.jsonl");
                var telemetryPath = Path.Combine(directory, prefix + "-telemetry.jsonl");
                var summaryPath = Path.Combine(directory, prefix + "-summary.json");
                WriteAtomically(actionPath, SerializeLines(trace, entry => new QaArtifactLine
                {
                    Schema = SchemaVersion, Kind = "action", Seed = seed, Tick = entry.Tick, Sequence = entry.Sequence,
                    Time = entry.Time, MovementX = entry.MovementX, MovementY = entry.MovementY, AbilityChoice = entry.AbilityChoice
                }));
                WriteAtomically(telemetryPath, SerializeLines(telemetry, entry => new QaArtifactLine
                {
                    Schema = SchemaVersion, Kind = "telemetry", Seed = seed, Tick = entry.Tick, Time = entry.Time,
                    Outcome = result.Outcome, Event = entry.Event
                }));
                WriteAtomically(summaryPath, JsonUtility.ToJson(new QaEpisodeSummary
                {
                    Schema = SchemaVersion,
                    Seed = result.Seed,
                    Outcome = result.Outcome,
                    ElapsedSeconds = result.ElapsedSeconds,
                    KillCount = result.KillCount,
                    FinalLevel = result.FinalLevel,
                    FailureReason = result.FailureReason,
                    RecordedPositionCount = recordedEpisode.Positions.Count,
                    DiscreteEventCount = recordedEpisode.DiscreteEvents.Count
                }));
                return QaArtifactWriteResult.Succeeded(new QaArtifactPaths(actionPath, telemetryPath, summaryPath));
            }
            catch (Exception exception)
            {
                return QaArtifactWriteResult.Failed(exception.Message);
            }
        }

        private void WriteAtomically(string finalPath, string contents)
        {
            var temporaryPath = finalPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                fileSystem.WriteAllText(temporaryPath, contents);
                fileSystem.MoveReplace(temporaryPath, finalPath);
            }
            finally
            {
                fileSystem.DeleteFile(temporaryPath);
            }
        }

        private static string SerializeLines<T>(IReadOnlyList<T> entries, Func<T, QaArtifactLine> toLine)
        {
            var builder = new StringBuilder();
            for (var index = 0; index < entries.Count; index++)
                builder.Append(JsonUtility.ToJson(toLine(entries[index]))).Append('\n');
            return builder.ToString();
        }

        private static bool TryNormalizeDirectory(string candidate, out string normalized)
        {
            if (string.IsNullOrWhiteSpace(candidate))
            {
                normalized = DefaultDirectory;
                return true;
            }

            if (Path.IsPathRooted(candidate) || candidate.IndexOf(':') >= 0)
            {
                normalized = null;
                return false;
            }

            var segments = candidate.Replace('\\', '/').Split('/');
            var safeSegments = new List<string>();
            foreach (var segment in segments)
            {
                if (segment == "" || segment == ".")
                    continue;
                if (segment == "..")
                {
                    normalized = null;
                    return false;
                }
                safeSegments.Add(segment);
            }

            normalized = safeSegments.Count == 0 ? DefaultDirectory : string.Join("/", safeSegments.ToArray());
            return true;
        }
    }

    internal sealed class SystemQaArtifactFileSystem : IQaArtifactFileSystem
    {
        private static readonly UTF8Encoding Utf8WithoutBom = new UTF8Encoding(false);

        public void CreateDirectory(string path)
        {
            Directory.CreateDirectory(path);
        }

        public void WriteAllText(string path, string contents)
        {
            File.WriteAllText(path, contents, Utf8WithoutBom);
        }

        public void MoveReplace(string sourcePath, string destinationPath)
        {
            if (File.Exists(destinationPath))
                File.Replace(sourcePath, destinationPath, null);
            else
                File.Move(sourcePath, destinationPath);
        }

        public void DeleteFile(string path)
        {
            if (File.Exists(path))
                File.Delete(path);
        }
    }
}
