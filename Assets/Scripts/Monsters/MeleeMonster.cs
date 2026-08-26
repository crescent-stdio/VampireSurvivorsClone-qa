using System.Collections;
using UnityEngine;
using Vampire.QA;

namespace Vampire
{
    public class MeleeMonster : Monster
    {
        protected new MeleeMonsterBlueprint monsterBlueprint;
        protected float timeSinceLastAttack;
        private float lastRecordedContactDamageTime = -1f;

        public override void Setup(int monsterIndex, Vector2 position, MonsterBlueprint monsterBlueprint, float hpBuff = 0)
        {
            base.Setup(monsterIndex, position, monsterBlueprint, hpBuff);
            this.monsterBlueprint = (MeleeMonsterBlueprint) monsterBlueprint;
            lastRecordedContactDamageTime = -1f;
        }

        protected override void Update()
        {
            base.Update();
            // Attack cooldown
            timeSinceLastAttack += Time.deltaTime;
        }

        protected override void FixedUpdate()
        {
            base.FixedUpdate();
            Vector2 moveDirection = (playerCharacter.transform.position - transform.position).normalized;
            rb.velocity += moveDirection * monsterBlueprint.acceleration * Time.fixedDeltaTime;
            entityManager.Grid.UpdateClient(this);

            // Vector2 f = Vector2.zero;
            // int count = 0;
            // foreach (ISpatialHashGridClient client in entityManager.Grid.FindNearbyInRadius(Position, 2))
            // {
            //     if (client != (ISpatialHashGridClient)this)
            //     {
            //         Vector2 diff = Position - client.Position;
            //         float dist = diff.magnitude;
            //         diff /= dist;
            //         f += diff / dist;
            //         count++;
            //     }
            // }
            // if (count > 0)
            //     f /= count;
            // rb.velocity += f * 0.1f * monsterBlueprint.acceleration * Time.fixedDeltaTime;
            // if (!knockedBack && rb.velocity.magnitude > monsterBlueprint.movespeed)
            //      rb.velocity = rb.velocity.normalized * monsterBlueprint.movespeed;
        }

        void OnCollisionStay2D(Collision2D col)
        {
            float attackInterval = 1.0f / monsterBlueprint.atkspeed;
            if (alive && ((monsterBlueprint.meleeLayer & (1 << col.collider.gameObject.layer)) != 0) && timeSinceLastAttack >= attackInterval)
            {
                QaFaultTelemetry.RecordContactDamage(
                    lastRecordedContactDamageTime,
                    Time.time,
                    attackInterval);
                lastRecordedContactDamageTime = Time.time;
                playerCharacter.TakeDamage(monsterBlueprint.atk);
                if (!QaFaultInjection.SkipContactDamageCooldownReset)
                {
                    timeSinceLastAttack = Mathf.Repeat(timeSinceLastAttack, attackInterval);
                    QaFaultTelemetry.RecordContactDamageCooldownReset();
                }
            }
        }
    }
}
