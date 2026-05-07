from __future__ import annotations
import math
import time
from dataclasses import dataclass, field
from warehouse.grid import WarehouseGrid


@dataclass
class Item:
    item_id: str
    name: str
    weight: float = 1.0
    pick_rate: float = 1.0  # normalized pick frequency (0–1); higher = ordered more often
    family: str = "default"     # product family; items in same family co-occur in orders


@dataclass
class RackSlot:
    rack_pos: tuple[int, int]
    stand_pos: tuple[int, int]  # walkable cell the agent stands at to pick
    item: Item | None = None
    stock: int = 0
    max_stock: int = 1
    reorder_point: int = 0   # trigger replenishment when stock falls below this
    order_qty: int = 1       # units to fetch per replenishment run
    lead_time: int = 50      # estimated dock→slot travel ticks (informational)


@dataclass
class Order:
    order_id: str
    item_ids: list[str]
    created_at: float = field(default_factory=time.monotonic)


class Inventory:
    def __init__(self, grid: WarehouseGrid) -> None:
        self._slots: list[RackSlot] = []
        self._item_to_slot: dict[str, RackSlot] = {}
        self._original_slot_for: dict[str, RackSlot] = {}
        self._all_items: dict[str, Item] = {}  # item registry — survives stockout

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
            n = len(items)
            if n > len(empty):
                raise ValueError(f"More items ({n}) than available slots ({len(empty)})")
            # Stride evenly so items spread across the whole warehouse,
            # not just the first rows discovered top-to-bottom.
            step = len(empty) / n if n < len(empty) else 1
            indices = [int(i * step) for i in range(n)]
            pairs = [(items[i], empty[indices[i]]) for i in range(n)]

        for item, slot in pairs:
            slot.item = item
            slot.stock = initial_stock
            slot.max_stock = initial_stock
            # Reorder parameters scaled by pick_rate and initial_stock.
            # Trigger replenishment at ~40% stock; fetch ~50% of initial stock per run.
            slot.reorder_point = max(1, math.ceil(initial_stock * item.pick_rate * 0.4))
            slot.order_qty     = max(1, math.ceil(initial_stock * item.pick_rate * 0.5))
            self._item_to_slot[item.item_id] = slot
            self._original_slot_for[item.item_id] = slot
            self._all_items[item.item_id] = item

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
        """Current on-hand units for an item (0 if picked out or in transit)."""
        slot = self._item_to_slot.get(item_id)
        return slot.stock if slot else 0

    def reset(self) -> None:
        """Clear all item assignments (used between analysis runs)."""
        for slot in self._slots:
            slot.item = None
        self._item_to_slot.clear()
        self._original_slot_for.clear()

    def relocate(self, item_id: str, new_slot_idx: int) -> tuple[Item | None, int]:
        """Reassign the home slot for item_id to _slots[new_slot_idx].

        Updates _original_slot_for immediately so the next restock() call sends
        the item to its new home. If the item is currently in stock, drains it
        from its physical slot and returns (item, units) so the caller can
        requeue it for restocking; returns (None, 0) if the item is in transit.
        """
        new_slot = self._slots[new_slot_idx]
        # Propagate slot capacity: unassigned slots default to max_stock=1, which
        # would cap the item's circulation to 1 unit permanently. Carry the old
        # capacity forward so the item's full initial_stock remains in circulation.
        old_home = self._original_slot_for.get(item_id)
        if old_home is not None and old_home.max_stock > new_slot.max_stock:
            new_slot.max_stock = old_home.max_stock
        self._original_slot_for[item_id] = new_slot
        if item_id in self._item_to_slot:
            old_slot = self._item_to_slot.pop(item_id)
            item = old_slot.item
            units = old_slot.stock
            if old_slot.max_stock > new_slot.max_stock:
                new_slot.max_stock = old_slot.max_stock
            old_slot.item = None
            old_slot.stock = 0
            return item, units
        return None, 0

    def restock_bulk(self, item: Item, qty: int) -> None:
        """Deposit `qty` units of an item into its original slot (used by dock replenishment)."""
        slot = self._original_slot_for[item.item_id]
        for _ in range(qty):
            was_empty = slot.stock == 0
            slot.stock = min(slot.stock + 1, slot.max_stock)
            if was_empty:
                slot.item = item
                self._item_to_slot[item.item_id] = slot

    def check_reorder_triggers(
        self, pending: set[str]
    ) -> list[tuple[str, "Item", int, bool]]:
        """Scan all seeded slots and return replenishment triggers.

        Returns list of (item_id, item, order_qty, urgent) for items whose
        stock is below their reorder_point and not already pending replenishment.
        urgent=True when stock is zero (slot is completely empty).
        """
        triggers = []
        for item_id, slot in self._original_slot_for.items():
            if item_id in pending or slot.reorder_point == 0:
                continue
            if slot.stock < slot.reorder_point:
                item = self._all_items[item_id]
                triggers.append((item_id, item, slot.order_qty, slot.stock == 0))
        return triggers

    def available_slots(self) -> list[RackSlot]:
        return [s for s in self._slots if s.item is not None]
