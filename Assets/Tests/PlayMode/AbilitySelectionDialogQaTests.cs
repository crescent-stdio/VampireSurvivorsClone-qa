using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using NUnit.Framework;
using UnityEngine;
using UnityEngine.TestTools;
using Object = UnityEngine.Object;

namespace Vampire.Tests.PlayMode
{
    public class AbilitySelectionDialogQaTests
    {
        [UnityTest]
        public IEnumerator Open_pauses_by_default_and_non_pausing_mode_preserves_time_scale()
        {
            var defaultDialog = CreateDialog();
            Assert.That(defaultDialog.PauseOnOpen, Is.True);
            Time.timeScale = 1f;
            defaultDialog.Open(false);
            Assert.That(defaultDialog.TimeScaleWhenCloseStarted, Is.Zero);
            Assert.That(Time.timeScale, Is.EqualTo(1f));
            DestroyDialog(defaultDialog);

            var qaDialog = CreateDialog();
            qaDialog.PauseOnOpen = false;
            Time.timeScale = 0.35f;
            qaDialog.Open(false);
            Assert.That(qaDialog.TimeScaleWhenCloseStarted, Is.EqualTo(0.35f));
            Assert.That(Time.timeScale, Is.EqualTo(0.35f));
            DestroyDialog(qaDialog);

            Time.timeScale = 1f;
            yield return null;
        }

        [UnityTest]
        public IEnumerator TrySelectOption_rejects_invalid_state_and_selects_a_displayed_ability_once()
        {
            var dialog = CreateDialog();

            Assert.That(dialog.TrySelectOption(-1), Is.False);
            Assert.That(dialog.TrySelectOption(0), Is.False);

            var abilityObject = new GameObject("Test Ability");
            var ability = abilityObject.AddComponent<CountingAbility>();
            SetPrivateField(dialog, "displayedAbilities", new List<Ability> { ability });
            SetPrivateField(dialog, "menuOpen", true);

            Assert.That(dialog.TrySelectOption(0), Is.True);
            Assert.That(dialog.TrySelectOption(0), Is.False);
            Assert.That(ability.SelectionCount, Is.EqualTo(1));
            Assert.That(dialog.MenuOpen, Is.False);

            Object.Destroy(dialog.gameObject);
            Object.Destroy(abilityObject);
            yield return null;
        }

        private static TrackingAbilitySelectionDialog CreateDialog()
        {
            var dialogObject = new GameObject("Ability Selection Dialog");
            var dialog = dialogObject.AddComponent<TrackingAbilitySelectionDialog>();
            var pauseMenu = new GameObject("Pause Menu").AddComponent<PauseMenu>();
            var particles = new GameObject("Particles");
            pauseMenu.transform.SetParent(dialog.transform);
            particles.transform.SetParent(dialog.transform);
            particles.SetActive(false);

            var abilityManager = new GameObject("Ability Manager").AddComponent<AbilityManager>();
            abilityManager.transform.SetParent(dialog.transform);
            var character = (Character)System.Runtime.Serialization.FormatterServices.GetUninitializedObject(typeof(Character));
            var characterBlueprint = ScriptableObject.CreateInstance<CharacterBlueprint>();
            characterBlueprint.startingAbilities = Array.Empty<GameObject>();
            SetPrivateField(character, "characterBlueprint", characterBlueprint);
            var levelBlueprint = ScriptableObject.CreateInstance<LevelBlueprint>();
            levelBlueprint.abilityPrefabs = Array.Empty<GameObject>();
            abilityManager.Init(levelBlueprint, null, character, abilityManager);

            SetPrivateField(dialog, "pauseMenu", pauseMenu);
            SetPrivateField(dialog, "particles", particles);
            dialog.Init(abilityManager, null, null);
            return dialog;
        }

        private static void DestroyDialog(TrackingAbilitySelectionDialog dialog)
        {
            Object.Destroy(dialog.gameObject);
        }

        private static void SetPrivateField(object target, string fieldName, object value)
        {
            var field = target.GetType().BaseType.GetField(fieldName, BindingFlags.Instance | BindingFlags.NonPublic)
                ?? target.GetType().GetField(fieldName, BindingFlags.Instance | BindingFlags.NonPublic);
            Assert.That(field, Is.Not.Null, $"Missing private field: {fieldName}");
            field.SetValue(target, value);
        }

        private sealed class TrackingAbilitySelectionDialog : AbilitySelectionDialog
        {
            public float TimeScaleWhenCloseStarted { get; private set; } = -1f;

            public override void Close()
            {
                TimeScaleWhenCloseStarted = Time.timeScale;
                base.Close();
            }
        }

        private sealed class CountingAbility : Ability
        {
            public int SelectionCount { get; private set; }

            public override void Select()
            {
                SelectionCount++;
            }
        }

    }
}
