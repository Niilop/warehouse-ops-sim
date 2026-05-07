from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol

from warehouse.inventory import Order, Inventory
from warehouse.grid import WarehouseGrid


@dataclass
class BatchedOrder:
    batch_id: str
    orders: list[Order]
    unified_item_ids: list[str]  # pick list; one entry per pick (duplicates allowed)
    item_to_order: list[str]     # parallel to unified_item_ids: order_id for each pick


class BatchStrategy(Protocol):
    def batch(self, orders: list[Order]) -> list[BatchedOrder]: ...


def _make_batch(orders: list[Order]) -> BatchedOrder:
    if not orders:
        raise ValueError("Cannot create a batch from an empty order list")
    batch_id = orders[0].order_id if len(orders) == 1 else f"B-{orders[0].order_id}"
    unified: list[str] = []
    item_to_order: list[str] = []
    for order in orders:
        for iid in order.item_ids:
            unified.append(iid)
            item_to_order.append(order.order_id)
    return BatchedOrder(
        batch_id=batch_id,
        orders=orders,
        unified_item_ids=unified,
        item_to_order=item_to_order,
    )


class FIFOBatcher:
    """One BatchedOrder per Order — preserves current single-order behaviour."""

    def batch(self, orders: list[Order]) -> list[BatchedOrder]:
        return [
            BatchedOrder(
                batch_id=order.order_id,
                orders=[order],
                unified_item_ids=list(order.item_ids),
                item_to_order=[order.order_id for _ in order.item_ids],
            )
            for order in orders
        ]


class ZoneBatcher:
    """Greedily merges orders that share ≥ min_zone_overlap rack zones, up to max_batch_size."""

    def __init__(
        self,
        grid: WarehouseGrid,
        inventory: Inventory,
        min_zone_overlap: int = 1,
        max_batch_size: int = 4,
    ) -> None:
        self.grid = grid
        self.inventory = inventory
        self.min_zone_overlap = min_zone_overlap
        self.max_batch_size = max_batch_size

    def _order_zones(self, order: Order) -> set[str]:
        zones: set[str] = set()
        for iid in order.item_ids:
            for slot in self.inventory._original_slots_for.get(iid, []):
                zone = self.grid.zone_map.get(slot.rack_pos)
                if zone:
                    zones.add(zone)
        return zones

    def batch(self, orders: list[Order]) -> list[BatchedOrder]:
        remaining = list(orders)
        batches: list[BatchedOrder] = []
        while remaining:
            anchor = remaining.pop(0)
            anchor_zones = self._order_zones(anchor)
            group = [anchor]
            i = 0
            while i < len(remaining) and len(group) < self.max_batch_size:
                if len(anchor_zones & self._order_zones(remaining[i])) >= self.min_zone_overlap:
                    group.append(remaining.pop(i))
                else:
                    i += 1
            batches.append(_make_batch(group))
        return batches


class GreedyTSPBatcher:
    """Groups orders by geographic proximity (centroid distance), up to max_batch_size."""

    def __init__(
        self,
        inventory: Inventory,
        grid: WarehouseGrid,
        max_batch_size: int = 4,
    ) -> None:
        self.inventory = inventory
        self.grid = grid
        self.max_batch_size = max_batch_size
        self._pack_pos = grid.pack_station_pos

    def _centroid(self, order: Order) -> tuple[float, float]:
        positions = [
            slot.stand_pos
            for iid in order.item_ids
            for slot in self.inventory._original_slots_for.get(iid, [])[:1]
        ]
        if not positions:
            return (float(self._pack_pos[0]), float(self._pack_pos[1]))
        return (
            sum(p[0] for p in positions) / len(positions),
            sum(p[1] for p in positions) / len(positions),
        )

    def batch(self, orders: list[Order]) -> list[BatchedOrder]:
        remaining = list(orders)
        batches: list[BatchedOrder] = []
        while remaining:
            anchor = remaining.pop(0)
            anchor_c = self._centroid(anchor)
            group = [anchor]

            candidates = sorted(
                enumerate(remaining),
                key=lambda t: abs(self._centroid(t[1])[0] - anchor_c[0])
                + abs(self._centroid(t[1])[1] - anchor_c[1]),
            )
            added: set[int] = set()
            for idx, _ in candidates:
                if len(group) >= self.max_batch_size:
                    break
                group.append(remaining[idx])
                added.add(idx)

            remaining = [o for i, o in enumerate(remaining) if i not in added]
            batches.append(_make_batch(group))
        return batches
