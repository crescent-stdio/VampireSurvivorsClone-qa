using System;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using UnityEngine;

namespace Vampire
{
    /// <summary>
    /// A named QA environment configuration generated from <c>config/qa-presets.json</c>.
    /// </summary>
    /// <remarks>
    /// The JSON file is the single source of truth. Shell wrappers and Python agents read
    /// it directly; the editor generator turns it into these assets so a built player
    /// never has to read a repository file at runtime.
    /// </remarks>
    public sealed class QaPresetBlueprint : ScriptableObject
    {
        [SerializeField] private string presetName;
        [SerializeField] private string description;
        [SerializeField] private float levelDurationSeconds = QaLevelTimings.DefaultDurationSeconds;
        [SerializeField] private float minibossSpawnSeconds = QaLevelTimings.DefaultMinibossSpawnSeconds;
        [SerializeField] private float characterHealthMultiplier = 1f;
        [SerializeField] private int characterArmor;
        [SerializeField] private float deadlineSeconds = 150f;
        [SerializeField] private float timeScale = 1f;
        [SerializeField] private float maximumTimeScale = 1f;
        [SerializeField] private float elapsedSecondsScale = 600f;
        [SerializeField] private LevelBlueprint level;
        [SerializeField] private CharacterBlueprint character;

        public string PresetName => presetName;
        public string Description => description;
        public float CharacterHealthMultiplier => characterHealthMultiplier;
        public int CharacterArmor => characterArmor;
        public float DeadlineSeconds => deadlineSeconds;
        public float TimeScale => timeScale;
        public float MaximumTimeScale => maximumTimeScale;
        public float ElapsedSecondsScale => elapsedSecondsScale;
        public LevelBlueprint Level => level;
        public CharacterBlueprint Character => character;

        public QaLevelTimings Timings => new QaLevelTimings(levelDurationSeconds, minibossSpawnSeconds);

        /// <summary>Identify the simulated environment, ignoring wall-clock-only settings.</summary>
        public string Fingerprint => QaPresetFingerprint.Compute(this);

        public void Configure(
            string name,
            string presetDescription,
            QaLevelTimings timings,
            float healthMultiplier,
            int armor,
            float episodeDeadlineSeconds,
            float episodeTimeScale,
            float episodeMaximumTimeScale,
            float observationElapsedSecondsScale,
            LevelBlueprint levelBlueprint,
            CharacterBlueprint characterBlueprint)
        {
            if (string.IsNullOrEmpty(name))
                throw new ArgumentException("A preset requires a name.", nameof(name));

            presetName = name;
            description = presetDescription;
            levelDurationSeconds = timings.DurationSeconds;
            minibossSpawnSeconds = timings.MinibossSpawnSeconds;
            characterHealthMultiplier = healthMultiplier;
            characterArmor = armor;
            deadlineSeconds = episodeDeadlineSeconds;
            timeScale = episodeTimeScale;
            maximumTimeScale = episodeMaximumTimeScale;
            elapsedSecondsScale = observationElapsedSecondsScale;
            level = levelBlueprint;
            character = characterBlueprint;
        }
    }

    /// <summary>
    /// Canonical environment identity shared with <c>qa_agent_runtime.presets</c>.
    /// </summary>
    /// <remarks>
    /// The canonical form deliberately avoids anything whose text differs between
    /// runtimes: fields appear in a fixed order rather than a sorted one, and integral
    /// numbers never carry a trailing decimal. Both test suites pin the resulting digests
    /// as literals so the two implementations cannot drift apart in silence.
    /// </remarks>
    public static class QaPresetFingerprint
    {
        public const string Schema = "qa-preset-environment/v1";
        public const int Length = 16;

        public static string Compute(QaPresetBlueprint preset)
        {
            if (preset == null)
                throw new ArgumentNullException(nameof(preset));
            return Compute(Canonicalize(preset));
        }

        public static string Canonicalize(QaPresetBlueprint preset)
        {
            if (preset == null)
                throw new ArgumentNullException(nameof(preset));

            var timings = preset.Timings;
            var builder = new StringBuilder();
            builder.Append(Schema);
            Append(builder, "level.durationSeconds", timings.DurationSeconds);
            Append(builder, "level.minibossSpawnSeconds", timings.MinibossSpawnSeconds);
            Append(builder, "character.healthMultiplier", preset.CharacterHealthMultiplier);
            Append(builder, "character.armor", preset.CharacterArmor);
            Append(builder, "episode.deadlineSeconds", preset.DeadlineSeconds);
            Append(builder, "observation.elapsedSecondsScale", preset.ElapsedSecondsScale);
            return builder.ToString();
        }

        public static string Compute(string canonical)
        {
            using (var sha256 = SHA256.Create())
            {
                var digest = sha256.ComputeHash(Encoding.UTF8.GetBytes(canonical));
                var text = new StringBuilder(digest.Length * 2);
                foreach (var value in digest)
                    text.Append(value.ToString("x2", CultureInfo.InvariantCulture));
                return text.ToString(0, Length);
            }
        }

        private static void Append(StringBuilder builder, string key, float value)
        {
            builder.Append('\n').Append(key).Append('=').Append(Format(value));
        }

        private static void Append(StringBuilder builder, string key, int value)
        {
            builder.Append('\n').Append(key).Append('=').Append(value.ToString(CultureInfo.InvariantCulture));
        }

        /// <summary>Render a number the way Python's <c>format_value</c> does.</summary>
        private static string Format(float value)
        {
            if (float.IsNaN(value) || float.IsInfinity(value))
                throw new ArgumentOutOfRangeException(nameof(value), value, "Preset values must be finite.");
            if (Math.Abs(value % 1f) < float.Epsilon)
                return ((long)value).ToString(CultureInfo.InvariantCulture);
            return value.ToString("R", CultureInfo.InvariantCulture);
        }
    }
}
