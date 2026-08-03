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

        [Test]
        public void Fixture_snapshot_restores_the_qa_character_bytes_after_the_test_body_throws()
        {
            const string path = "Assets/Blueprints/QA/QA Main Character.asset";
            var character = AssetDatabase.LoadAssetAtPath<CharacterBlueprint>(path);
            var originalHp = character.hp;

            Assert.Throws<InvalidOperationException>(() =>
            {
                using (QaFixtureSnapshot.Capture())
                {
                    character.hp = -999f;
                    EditorUtility.SetDirty(character);
                    AssetDatabase.SaveAssets();
                    throw new InvalidOperationException("forced test failure");
                }
            });

            Assert.That(AssetDatabase.LoadAssetAtPath<CharacterBlueprint>(path).hp, Is.EqualTo(originalHp));
        }
    }
}
