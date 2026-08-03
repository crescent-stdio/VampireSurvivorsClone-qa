using System;
using NUnit.Framework;
using UnityEditor;
using UnityEngine;

namespace Vampire.Tests.EditMode
{
    public class QaFixtureSnapshotTests
    {
        [Test]
        public void Fixture_snapshot_restores_qa_asset_after_the_test_body_throws()
        {
            var qaLevel = AssetDatabase.LoadAssetAtPath<LevelBlueprint>("Assets/Blueprints/QA/QA Level 1.asset");
            var originalValue = qaLevel.initialExpGemCount;

            Assert.Throws<InvalidOperationException>(() =>
            {
                using (QaFixtureSnapshot.Capture())
                {
                    qaLevel.initialExpGemCount = -123;
                    EditorUtility.SetDirty(qaLevel);
                    AssetDatabase.SaveAssets();
                    throw new InvalidOperationException("forced test failure");
                }
            });

            Assert.That(AssetDatabase.LoadAssetAtPath<LevelBlueprint>("Assets/Blueprints/QA/QA Level 1.asset").initialExpGemCount, Is.EqualTo(originalValue));
        }
    }
}
