using NUnit.Framework;
using UnityEngine;
using Vampire.QA;

namespace Vampire.Tests.EditMode
{
    /// <summary>
    /// Covers the per-frame survival assist used by --policy hybrid. The assist must be
    /// invisible to --policy llm so both arms can share one player build.
    /// </summary>
    public sealed class QaBridgeAssistTests
    {
        [Test]
        public void Assist_is_off_by_default_on_a_deserialized_command()
        {
            QACommand command = JsonUtility.FromJson<QACommand>(
                "{\"id\":\"c-1\",\"action\":\"direct_steer\",\"x\":1,\"y\":0,\"duration\":5}");

            Assert.That(command.assist_avoidance, Is.False);
            Assert.That(command.assist_survival_weight, Is.EqualTo(0f));
        }

        [Test]
        public void Blend_without_a_threat_returns_the_commanded_vector()
        {
            Vector2 blended = QABridge.BlendSurvivalAssist(
                Vector2.right, Vector2.zero, danger: 0f, weight: 0.6f, magnitude: 1f);

            Assert.That(blended.x, Is.EqualTo(1f).Within(0.0001f));
            Assert.That(blended.y, Is.EqualTo(0f).Within(0.0001f));
        }

        [Test]
        public void Blend_with_a_zero_weight_returns_the_commanded_vector()
        {
            Vector2 blended = QABridge.BlendSurvivalAssist(
                Vector2.right, Vector2.up, danger: 1f, weight: 0f, magnitude: 1f);

            Assert.That(blended.x, Is.EqualTo(1f).Within(0.0001f));
            Assert.That(blended.y, Is.EqualTo(0f).Within(0.0001f));
        }

        [Test]
        public void Blend_preserves_the_requested_magnitude()
        {
            Vector2 blended = QABridge.BlendSurvivalAssist(
                Vector2.right, Vector2.up, danger: 1f, weight: 0.6f, magnitude: 0.5f);

            Assert.That(blended.magnitude, Is.EqualTo(0.5f).Within(0.0001f));
        }

        [Test]
        public void Blend_deflects_toward_the_escape_direction_under_danger()
        {
            // escape is perpendicular to the commanded heading; at danger 1.0 the term is
            // 0.6 * clamp01(0.35 + 1.0) = 0.6, so the result rotates by atan(0.6) ~= 30.96 deg.
            Vector2 blended = QABridge.BlendSurvivalAssist(
                Vector2.right, Vector2.up, danger: 1f, weight: 0.6f, magnitude: 1f);

            Assert.That(Vector2.Angle(Vector2.right, blended), Is.EqualTo(30.96f).Within(0.1f));
            Assert.That(blended.y, Is.GreaterThan(0f));
        }

        [Test]
        public void Blend_can_fully_reverse_when_the_escape_opposes_the_commanded_vector()
        {
            // The motivating failure: a saturated threat field directly ahead. Nothing may
            // clamp the correction back toward the commanded heading.
            Vector2 blended = QABridge.BlendSurvivalAssist(
                Vector2.right, Vector2.left, danger: 1f, weight: 2f, magnitude: 1f);

            Assert.That(blended.x, Is.LessThan(0f));
        }
    }
}
