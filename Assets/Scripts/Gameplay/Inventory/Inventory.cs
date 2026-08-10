using System.Collections;
using System.Collections.Generic;
using UnityEngine;

namespace Vampire
{
    public class Inventory : MonoBehaviour
    {
        [SerializeField] private InventorySlot[] inventorySlots;
        private Dictionary<CollectableType, InventorySlot> inventorySlotByType;

        public int SlotCount => inventorySlots == null ? 0 : inventorySlots.Length;

        public InventorySlot GetSlot(int index)
        {
            if (inventorySlots == null || index < 0 || index >= inventorySlots.Length)
                return null;
            return inventorySlots[index];
        }

        public bool UseItemAt(int index)
        {
            var slot = GetSlot(index);
            if (slot == null || slot.Count == 0)
                return false;
            slot.UseItem();
            return true;
        }

        public void Init()
        {
            inventorySlotByType = new Dictionary<CollectableType, InventorySlot>();
            for (int i = 0; i < inventorySlots.Length; i++)
            {
                InventorySlot inventorySlot = inventorySlots[i];
                inventorySlot.Init();
                inventorySlotByType[inventorySlot.CollectableType] = inventorySlot;
            }
        }

        public bool RoomInInventory(Collectable item)
        {
            if (inventorySlotByType.TryGetValue(item.CollectableType, out InventorySlot inventorySlot))
                return !inventorySlot.IsFull();
            return false;
        }

        public bool TryGetInventorySlot(Collectable item, out InventorySlot inventorySlot)
        {
            if (inventorySlotByType.TryGetValue(item.CollectableType, out inventorySlot))
            {
                return true;
            }
            inventorySlot = null;
            return false;
        }

        public InventorySlot AddItem(Collectable item)
        {
            if (inventorySlotByType.ContainsKey(item.CollectableType) && !inventorySlotByType[item.CollectableType].IsFull())
            {
                inventorySlotByType[item.CollectableType].AddItem(item);
                return inventorySlotByType[item.CollectableType];
            }
            return null;
        }
    }
}
