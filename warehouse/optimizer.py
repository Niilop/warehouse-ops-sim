from __future__ import annotations
import random
from collections import defaultdict
from warehouse.grid import WarehouseGrid
from warehouse.inventory import Item, Inventory, Order


def slot_distances(grid: WarehouseGrid, inventory: Inventory) -> list[tuple[int, int]]:
    """
    Returns list of (slot_index, manhattan_distance_to_pack_station) for every slot,
    sorted ascending by distance. slot_index indexes into inventory._slots.
    """
    pr, pc = grid.pack_station_pos
    result = []
    for i, slot in enumerate(inventory._slots):
        sr, sc = slot.stand_pos
        dist = abs(sr - pr) + abs(sc - pc)
        result.append((i, dist))
    result.sort(key=lambda x: x[1])
    return result


def random_placement(items: list[Item], seed: int = 0) -> list[Item]:
    """Returns a shuffled copy of items (random slot assignment order)."""
    shuffled = list(items)
    random.Random(seed).shuffle(shuffled)
    return shuffled


def demand_placement(items: list[Item]) -> list[Item]:
    """
    Returns items sorted by pick_rate descending.
    When passed to Inventory.seed() with slot_order from slot_distances(),
    the highest-demand items land in the closest slots to the pack station.
    """
    return sorted(items, key=lambda i: i.pick_rate, reverse=True)


def affinity_placement(items: list[Item], orders: list[Order]) -> list[Item]:
    """
    Greedy placement using demand + co-occurrence affinity.

    1. Build co-occurrence matrix: co[id_a][id_b] = number of orders containing both
    2. Greedily fill slots closest-to-farthest:
       - Score each unplaced item = pick_rate + alpha * co-occurrence with already-placed items
       - Pick highest-scoring item for the current slot
    Returns items in placement order (item[0] → closest slot, item[-1] → farthest).
    """
    alpha = 1.0

    # Build co-occurrence counts
    co: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for order in orders:
        ids = order.item_ids
        for a in ids:
            for b in ids:
                if a != b:
                    co[a][b] += 1

    # Normalise co-occurrence so it's on a similar scale to pick_rate
    max_co = max((v for inner in co.values() for v in inner.values()), default=1)

    unplaced = list(items)
    placed: list[Item] = []

    while unplaced:
        best: Item | None = None
        best_score = -1.0
        placed_ids = {i.item_id for i in placed}

        for candidate in unplaced:
            affinity_score = sum(
                co[candidate.item_id].get(pid, 0) / max_co
                for pid in placed_ids
            )
            score = candidate.pick_rate + alpha * affinity_score
            if score > best_score:
                best_score = score
                best = candidate

        assert best is not None
        placed.append(best)
        unplaced.remove(best)

    return placed


def reorg_cost(
    current_distances: dict[str, int],
    proposed_distances: dict[str, int],
    restock_delay: int,
    items_pick_rate: dict[str, float],
) -> dict:
    """Estimate the cost/benefit of a proposed item reslotting.

    disruption_ticks  = moved_items × restock_delay   (items briefly unavailable)
    expected_savings  = Σ pick_rate[i] × (cur_dist[i] - prop_dist[i]) for moved i
    payback_period    = disruption / savings  (inf when savings ≤ 0)

    Returns dict with keys: moved_items, disruption_ticks,
    expected_savings_per_tick, payback_period_ticks.
    """
    moved = {
        iid for iid in current_distances
        if proposed_distances.get(iid, current_distances[iid]) != current_distances[iid]
    }
    disruption_ticks = len(moved) * restock_delay
    expected_savings = sum(
        items_pick_rate.get(iid, 0.0)
        * (current_distances[iid] - proposed_distances.get(iid, current_distances[iid]))
        for iid in moved
    )
    payback = disruption_ticks / expected_savings if expected_savings > 0 else float("inf")
    return {
        "moved_items": len(moved),
        "disruption_ticks": disruption_ticks,
        "expected_savings_per_tick": round(expected_savings, 4),
        "payback_period_ticks": round(payback, 1),
    }
