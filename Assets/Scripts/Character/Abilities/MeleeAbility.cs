using UnityEngine;
using Vampire.QA;

namespace Vampire
{
    public abstract class MeleeAbility : Ability
    {
        [Header("Melee Stats")]
        [SerializeField] protected LayerMask targetLayer;
        [SerializeField] protected UpgradeableDamage damage;
        [SerializeField] protected UpgradeableKnockback knockback;
        [SerializeField] protected UpgradeableWeaponCooldown cooldown;
        [SerializeField] protected SpriteRenderer weaponSpriteRenderer;
        protected float timeSinceLastAttack;
        private bool hasAttacked;

        protected override void Use()
        {
            base.Use();
            gameObject.SetActive(true);
            timeSinceLastAttack = cooldown.Value;
        }

        void Update()
        {
            timeSinceLastAttack += Time.deltaTime;
            if (timeSinceLastAttack >= cooldown.Value)
            {
                if (QaFaultInjection.StopWeaponAfterFirstAttack && hasAttacked)
                    return;
                timeSinceLastAttack = Mathf.Repeat(timeSinceLastAttack, cooldown.Value);
                hasAttacked = true;
                QaFaultTelemetry.RecordWeaponAttack(
                    GetInstanceID(),
                    Time.time,
                    cooldown.Value);
                Attack();
            }
        }

        protected abstract void Attack();
    }
}
