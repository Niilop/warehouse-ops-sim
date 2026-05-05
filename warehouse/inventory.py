from __future__ import annotations
import time
from dataclasses import dataclass, field
from warehouse.grid import WarehouseGrid


@dataclass
class Item:
    item_id: str
    name: str
    weight: float = 1.0
    demand_weight: float = 1.0  # relative pick frequency; higher = ordered more often
    family: str = "default"     # product family; items in same family co-occur in orders


@dataclass
class RackSlot:
    rack_pos: tuple[int, int]
    stand_pos: tuple[int, int]  # walkable cell the agent stands at to pick
    item: Item | None = None
    stock: int = 0      # units currently in this slot
    max_stock: int = 1  # slot capacity (for future stock-level optimisation)


@dataclass
class Order:
    order_id: str
    item_ids: list[str]
    created_at: float = field(default_factory=time.monotonic)


class Inventory:
    def __init__(self, grid: WarehouseGrid) -> None:
        self._slots: list[RackSlot] = []
        self._item_to_slot: dict[str, RackSlot] = {}
        self._original_slot_for: dict[str, RackSlot] = {}  # for restock

        from warehouse.grid import CellType
        for r in range(grid.rows):
            for c in range(grid.cols):
                if grid.grid[r, c] == CellType.RACK:
                    neighbors = grid.get_rack_neighbors(r, c)
                    if neighbors:
                        self._slots.append(RackSlot(rack_pos=(r, c), stand_pos=neighbors[0]))

    def seed(
        self,
        items: list[Item],
        slot_order: list[int] | None = None,
        initial_stock: int = 10,
    ) -> None:
        """
        Assign items to rack slots.
        slot_order: optional permutation of slot indices — items[i] → slots[slot_order[i]].
        If None, assigns items round-robin to empty slots in discovery order.
        initial_stock: units to place in each slot; scales with demand in future.
        """
        if slot_order is not None:
            if len(items) > len(slot_order):
                raise ValueError("More items than slot_order entries")
            pairs = [(items[i], self._slots[slot_order[i]]) for i in range(len(items))]
        else:
            empty = [s for s in self._slots if s.item is None]
            if len(items) > len(empty):
                raise ValueError(f"More items ({len(items)}) than available slots ({len(empty)})")
            pairs = [(items[i], empty[i]) for i in range(len(items))]

        for item, slot in pairs:
            slot.item = item
            slot.stock = initial_stock
            slot.max_stock = initial_stock
            self._item_to_slot[item.item_id] = slot
            self._original_slot_for[item.item_id] = slot

    def get_slot(self, item_id: str) -> RackSlot:
        return self._item_to_slot[item_id]

    def remove_item(self, item_id: str) -> Item:
        slot = self._item_to_slot[item_id]
        item = slot.item
        slot.stock -= 1
        if slot.stock == 0:
            self._item_to_slot.pop(item_id)
            slot.item = None
        return item  # type: ignore[return-value]

    def restock(self, item: Item) -> None:
        """Return one unit of an item to its original rack slot."""
        slot = self._original_slot_for[item.item_id]
        was_empty = slot.stock == 0
        slot.stock = min(slot.stock + 1, slot.max_stock)
        if was_empty:
            slot.item = item
            self._item_to_slot[item.item_id] = slot

    def stock_level(self, item_id: str) -> int:
        """Current on-hand units for an item (0 if picked out)."""
        slot = self._original_slot_for.get(item_id)
        return slot.stock if slot else 0

    def reset(self) -> None:
        """Clear all item assignments (used between analysis runs)."""
        for slot in self._slots:
            slot.item = None
        self._item_to_slot.clear()
        self._original_slot_for.clear()

    def available_slots(self) -> list[RackSlot]:
        return [s for s in self._slots if s.item is not None]
