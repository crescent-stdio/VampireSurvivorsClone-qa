using System;
using System.Linq;
using UnityEngine;

namespace Vampire
{
    /// <summary>
    /// Parses <c>config/qa-presets.json</c>, the single source of truth for QA presets.
    /// </summary>
    /// <remarks>
    /// Parsing is separated from file access so it stays testable and so a built player
    /// never reads a repository file: the editor generator supplies the text and turns the
    /// result into <see cref="QaPresetBlueprint"/> assets, which are what the player uses.
    ///
    /// Presets are stored as an array rather than an object keyed by name so Unity's
    /// built-in <see cref="JsonUtility"/> can read them. Newtonsoft exists here only as a
    /// transitive Addressables dependency, and project code should not rely on a package
    /// it does not declare.
    /// </remarks>
    public static class QaPresetDefinitions
    {
        public const string DefinitionPath = "config/qa-presets.json";
        public const string Schema = "qa-presets/v1";
        public const string DefaultPresetName = "smoke";

        public static QaPresetDefinition[] Parse(string json)
        {
            if (string.IsNullOrEmpty(json))
                throw new InvalidOperationException("Preset definitions are empty.");

            QaPresetDocument document;
            try
            {
                document = JsonUtility.FromJson<QaPresetDocument>(json);
            }
            catch (ArgumentException error)
            {
                throw new InvalidOperationException("Preset definitions are not valid JSON: " + error.Message, error);
            }

            if (document == null || document.schema != Schema)
                throw new InvalidOperationException(
                    "Preset definitions must declare schema " + Schema + "; got " +
                    (document == null ? "<none>" : document.schema));
            if (document.presets == null || document.presets.Length == 0)
                throw new InvalidOperationException("Preset definitions must contain at least one preset.");

            var names = document.presets.Select(preset => preset.name).ToArray();
            if (names.Any(string.IsNullOrEmpty))
                throw new InvalidOperationException("Every preset must declare a non-empty name.");
            if (names.Distinct(StringComparer.Ordinal).Count() != names.Length)
                throw new InvalidOperationException("Preset names must be unique.");
            if (!names.Contains(DefaultPresetName, StringComparer.Ordinal))
                throw new InvalidOperationException("Preset definitions must include " + DefaultPresetName + ".");

            foreach (var preset in document.presets)
                preset.Validate();
            return document.presets;
        }

        public static QaPresetDefinition Require(QaPresetDefinition[] definitions, string name)
        {
            var definition = definitions?.FirstOrDefault(
                candidate => string.Equals(candidate.name, name, StringComparison.Ordinal));
            if (definition == null)
                throw new InvalidOperationException("Preset definitions must include " + name + ".");
            return definition;
        }

        [Serializable]
        private sealed class QaPresetDocument
        {
            public string schema;
            public QaPresetDefinition[] presets;
        }
    }

    [Serializable]
    public sealed class QaPresetDefinition
    {
        public string name;
        public string description;
        public QaPresetLevelSection level;
        public QaPresetCharacterSection character;
        public QaPresetEpisodeSection episode;
        public QaPresetObservationSection observation;

        public QaLevelTimings Timings => new QaLevelTimings(level.durationSeconds, level.minibossSpawnSeconds);

        public void Validate()
        {
            if (level == null || character == null || episode == null || observation == null)
                throw new InvalidOperationException("Preset " + name + " is missing a required section.");

            // QaLevelTimings validates the duration and the miniboss ordering on construction.
            var timings = Timings;
            if (character.healthMultiplier <= 0f)
                throw new InvalidOperationException("Preset " + name + " must use a positive health multiplier.");
            if (character.armor < 0)
                throw new InvalidOperationException("Preset " + name + " must not use negative armor.");
            if (episode.deadlineSeconds <= timings.DurationSeconds)
                throw new InvalidOperationException(
                    "Preset " + name + " must allow time to defeat the final boss: its deadline must exceed the level duration.");
            if (episode.timeScale <= 0f || episode.maximumTimeScale <= 0f)
                throw new InvalidOperationException("Preset " + name + " must use positive time scales.");
            if (episode.timeScale > episode.maximumTimeScale)
                throw new InvalidOperationException("Preset " + name + " time scale exceeds its maximum.");
            if (observation.elapsedSecondsScale <= 0f)
                throw new InvalidOperationException("Preset " + name + " must use a positive elapsed seconds scale.");
        }
    }

    [Serializable]
    public sealed class QaPresetLevelSection
    {
        public float durationSeconds;
        public float minibossSpawnSeconds;
    }

    [Serializable]
    public sealed class QaPresetCharacterSection
    {
        public float healthMultiplier;
        public int armor;
    }

    [Serializable]
    public sealed class QaPresetEpisodeSection
    {
        public float deadlineSeconds;
        public float timeScale;
        public float maximumTimeScale;
    }

    [Serializable]
    public sealed class QaPresetObservationSection
    {
        public float elapsedSecondsScale;
    }
}
