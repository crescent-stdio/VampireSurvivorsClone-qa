using UnityEngine;
using Vampire.QA;

namespace Vampire
{
    public class Bomb : Collectable
    {
        [SerializeField] protected float bombDamage;

        protected override void OnCollected()
        {
            if (!QaFaultInjection.SuppressItemEffect)
                entityManager.DamageAllVisibileEnemies(bombDamage);
            Destroy(gameObject);
        }
    }
}
