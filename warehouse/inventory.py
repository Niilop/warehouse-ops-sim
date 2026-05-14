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
    fill_to: int = 1            # target stock after replenishment (≤ max_stock)
    reorder_point: int = 0      # trigger replenishment when stock falls below this
    order_qty: int = 1          # units to fetch per replenishment run
    lead_time: int = 50         # estimated dock→slot travel ticks (informational)


@dataclass
class Order:
    order_id: str
    item_ids: list[str]
    created_at: float = field(default_factory=time.monotonic)


class Inventory:
    def __init__(self, grid: WarehouseGrid) -> None:
        self._slots: list[RackSlot] = []
        # item_id → all assigned home slots (present even when stock = 0)
        self._original_slots_for: dict[str, list[RackSlot]] = {}
        self._all_items: dict[str, Item] = {}  # item registry — survives stockout
        self._pack_station_pos: tuple[int, int] = grid.pack_station_pos

        from warehouse.grid import CellType
        for r in range(grid.rows):
            for c in range(grid.cols):
                if grid.grid[r, c] == CellType.RACK:
                    neighbors = grid.get_rack_neighbors(r, c)
                    if neighbors:
                        self._slots.append(RackSlot(rack_pos=(r, c), stand_pos=neighbors[0]))

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def seed(
        self,
        items: list[Item],
        slot_order: list[int] | None = None,
        initial_stock: int = 10,
        orders_per_day: int | None = None,
        items_per_order: int = 4,
        target_dos: int = 5,
        reorder_trigger_days: int = 2,
        target_fill_pct: float = 0.8,
        max_facings_per_sku: int = 1,
    ) -> None:
        """Assign items to rack slots.

        When orders_per_day is provided, slot capacities are derived from a
        Days-of-Supply formula. Otherwise initial_stock is used (legacy mode).

        When max_facings_per_sku > 1, fast-moving items receive multiple slots
        (facings) proportional to their pick_rate relative to the top item.
        slot_order is ignored when max_facings_per_sku > 1.
        """
        # --- Step 1: compute number of facings per item -------------------------
        if max_facings_per_sku > 1 and items:
            max_rate = max(item.pick_rate for item in items) or 1.0
            n_facings_list = [
                max(1, round(item.pick_rate / max_rate * max_facings_per_sku))
                for item in items
            ]
            # Overflow trim: remove one facing from the lowest-pick_rate item
            # that still has >1 facing, until total fits in available slots.
            empty_count = sum(1 for s in self._slots if s.item is None)
            while sum(n_facings_list) > empty_count:
                min_rate, min_idx = float("inf"), -1
                for i, (item, nf) in enumerate(zip(items, n_facings_list)):
                    if nf > 1 and item.pick_rate < min_rate:
                        min_rate, min_idx = item.pick_rate, i
                if min_idx == -1:
                    break
                n_facings_list[min_idx] -= 1
        else:
            n_facings_list = [1] * len(items)

        # --- Step 2: allocate physical slots ------------------------------------
        if slot_order is not None and max_facings_per_sku <= 1:
            # Single-facing with explicit ordering (demand / affinity placement).
            if len(items) > len(slot_order):
                raise ValueError("More items than slot_order entries")
            item_slot_groups: list[tuple[Item, list[RackSlot]]] = [
                (items[i], [self._slots[slot_order[i]]]) for i in range(len(items))
            ]
        elif max_facings_per_sku <= 1:
            # Single-facing, no explicit ordering: stride evenly so items spread
            # across the whole warehouse rather than clustering in one zone.
            empty = [s for s in self._slots if s.item is None]
            n = len(items)
            if n > len(empty):
                raise ValueError(f"More items ({n}) than available slots ({len(empty)})")
            step = len(empty) / n if n < len(empty) else 1
            indices = [int(i * step) for i in range(n)]
            item_slot_groups = [(items[i], [empty[indices[i]]]) for i in range(n)]
        else:
            # Multi-facing: allocate from empty slots sorted by distance to pack
            # station — closest slots to fastest movers.
            empty = [s for s in self._slots if s.item is None]
            total_needed = sum(n_facings_list)
            if total_needed > len(empty):
                raise ValueError(
                    f"Need {total_needed} facing slots but only {len(empty)} available"
                )
            pr, pc = self._pack_station_pos
            empty.sort(key=lambda s: abs(s.stand_pos[0] - pr) + abs(s.stand_pos[1] - pc))

            # Items sorted by pick_rate desc so fast movers get the closest slots.
            order = sorted(range(len(items)), key=lambda i: items[i].pick_rate, reverse=True)
            item_slot_groups = [None] * len(items)  # type: ignore[list-item]
            cursor = 0
            for orig_idx in order:
                nf = n_facings_list[orig_idx]
                item_slot_groups[orig_idx] = (items[orig_idx], empty[cursor: cursor + nf])
                cursor += nf

        # --- Step 3: compute capacity and seed each item -----------------------
        total_pick_rate = sum(item.pick_rate for item in items) or 1.0

        for item, slots in item_slot_groups:
            nf = len(slots)

            if orders_per_day is not None:
                # Days-of-Supply formula.
                # pick_rate_norm: fraction of daily order lines going to this item.
                pick_rate_norm = item.pick_rate / total_pick_rate * items_per_order
                item_daily_demand = orders_per_day * pick_rate_norm

                max_stock_agg = max(1, math.ceil(item_daily_demand * target_dos))
                fill_to_agg = max(1, math.ceil(max_stock_agg * target_fill_pct))
                reorder_agg = max(1, math.ceil(item_daily_demand * reorder_trigger_days))
                order_qty_agg = max(1, fill_to_agg - reorder_agg)
            else:
                # Legacy: fixed initial_stock per slot.
                max_stock_agg = initial_stock * nf
                fill_to_agg = initial_stock * nf
                reorder_agg = max(1, math.ceil(initial_stock * item.pick_rate * 0.4)) * nf
                order_qty_agg = max(1, math.ceil(initial_stock * item.pick_rate * 0.5)) * nf

            for slot in slots:
                slot.item = item
                slot.max_stock = max(1, math.ceil(max_stock_agg / nf))
                slot.fill_to = max(1, math.ceil(fill_to_agg / nf))
                slot.stock = slot.fill_to
                slot.reorder_point = max(1, math.ceil(reorder_agg / nf))
                slot.order_qty = max(1, math.ceil(order_qty_agg / nf))
                slot.lead_time = 50

            self._original_slots_for[item.item_id] = list(slots)
            self._all_items[item.item_id] = item

    # ------------------------------------------------------------------
    # Item lookup
    # ------------------------------------------------------------------

    def get_slot(
        self, item_id: str, agent_pos: tuple[int, int] | None = None
    ) -> RackSlot:
        """Return the nearest facing with stock > 0.
        Falls back to any assigned slot if all facings are empty (caller handles stockout)."""
        slots = self._original_slots_for.get(item_id, [])
        active = [s for s in slots if s.stock > 0]
        if not active:
            return slots[0] if slots else self._slots[0]  # stockout fallback
        if agent_pos is None or len(active) == 1:
            return active[0]
        return min(
            active,
            key=lambda s: abs(s.stand_pos[0] - agent_pos[0]) + abs(s.stand_pos[1] - agent_pos[1]),
        )

    def remove_item(
        self, item_id: str, agent_pos: tuple[int, int] | None = None
    ) -> Item:
        slot = self.get_slot(item_id, agent_pos)
        item = slot.item
        slot.stock -= 1
        if slot.stock == 0:
            slot.item = None
        return item  # type: ignore[return-value]

    def restock(self, item: Item) -> None:
        """Return one unit to the emptiest home facing (teleport / legacy mode)."""
        slots = self._original_slots_for[item.item_id]
        slot = min(slots, key=lambda s: s.stock / max(1, s.fill_to))
        was_empty = slot.stock == 0
        slot.stock = min(slot.stock + 1, slot.fill_to)
        if was_empty:
            slot.item = item

    def restock_bulk(
        self,
        item: Item,
        qty: int,
        target_stand_pos: tuple[int, int] | None = None,
    ) -> None:
        """Deposit qty units into home facings.

        target_stand_pos: if provided, deposit into that specific facing first
        (agent-driven dock replenishment). Otherwise distribute to emptiest facing.
        Deposits are capped at fill_to, not max_stock.
        """
        slots = self._original_slots_for[item.item_id]
        if target_stand_pos is not None:
            target = next((s for s in slots if s.stand_pos == target_stand_pos), slots[0])
            ordered = [target] + [s for s in slots if s is not target]
        else:
            ordered = sorted(slots, key=lambda s: s.stock / max(1, s.fill_to))

        remaining = qty
        for slot in ordered:
            if remaining <= 0:
                break
            space = slot.fill_to - slot.stock
            if space <= 0:
                continue
            add = min(remaining, space)
            was_empty = slot.stock == 0
            slot.stock += add
            remaining -= add
            if was_empty:
                slot.item = item

    def stock_level(self, item_id: str) -> int:
        """Aggregate on-hand units across all facings."""
        return sum(s.stock for s in self._original_slots_for.get(item_id, []))

    def reset(self) -> None:
        for slot in self._slots:
            slot.item = None
            slot.stock = 0
        self._original_slots_for.clear()

    def relocate(self, item_id: str, new_slot_idx: int) -> tuple[Item | None, int]:
        """Reassign the primary home facing for item_id to _slots[new_slot_idx].

        For multi-facing items only the first (primary) facing is relocated;
        full multi-facing reslotting is a Phase 8 concern.
        Updates _original_slots_for immediately so the next restock() call
        sends the item to its new home. If the primary facing has stock, drains
        it and returns (item, units) so the caller can requeue for restocking.
        """
        new_slot = self._slots[new_slot_idx]
        homes = self._original_slots_for.get(item_id)
        if not homes:
            return None, 0

        old_home = homes[0]
        # Carry all capacity fields forward so replenishment continues correctly
        # after a relocation.  new_slot starts with dataclass defaults (e.g.
        # reorder_point=0) which would permanently suppress check_reorder_triggers.
        new_slot.max_stock = max(new_slot.max_stock, old_home.max_stock)
        new_slot.fill_to = max(new_slot.fill_to, old_home.fill_to)
        new_slot.reorder_point = old_home.reorder_point
        new_slot.order_qty = old_home.order_qty
        homes[0] = new_slot

        if old_home.stock > 0:
            item = old_home.item
            units = old_home.stock
            old_home.item = None
            old_home.stock = 0
            return item, units
        return None, 0

    def check_reorder_triggers(
        self, pending: set[str]
    ) -> list[tuple[str, "Item", int, bool]]:
        """Scan all seeded items and return replenishment triggers.

        Aggregates stock and reorder_point across all facings for each item.
        Returns list of (item_id, item, order_qty, urgent) for items whose
        aggregate stock is below their aggregate reorder_point and not already
        pending replenishment. urgent=True when aggregate stock is zero.
        """
        triggers = []
        for item_id, slots in self._original_slots_for.items():
            if item_id in pending or not slots:
                continue
            if slots[0].reorder_point == 0:
                continue
            agg_stock = sum(s.stock for s in slots)
            agg_reorder = sum(s.reorder_point for s in slots)
            if agg_stock < agg_reorder:
                item = self._all_items[item_id]
                agg_order_qty = sum(s.order_qty for s in slots)
                triggers.append((item_id, item, agg_order_qty, agg_stock == 0))
        return triggers

    def available_slots(self) -> list[RackSlot]:
        return [s for s in self._slots if s.item is not None]
