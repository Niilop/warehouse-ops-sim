from __future__ import annotations
import math
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


def sa_slotting(
    current_assignment: dict[str, list[int]],
    pick_counts: dict[str, int],
    slot_dist: dict[int, int],
    n_iter: int = 2000,
    t_start: float = 200.0,
    t_final: float = 0.5,
    seed: int | None = None,
) -> dict[str, list[int]]:
    """Simulated annealing slot optimizer.

    Minimizes Σ picks[i] × min_dist[slots[i]] by swapping slot assignments
    between pairs of items with equal facing counts (multi-facing safe).

    current_assignment: item_id → list of slot indices (non-protected items only)
    pick_counts: item_id → recent pick count (epoch window)
    slot_dist: slot_idx → manhattan distance to pack station
    Returns: optimized assignment (same keys, possibly different slot indices).
    """
    if len(current_assignment) < 2:
        return dict(current_assignment)

    rng = random.Random(seed)
    assignment = {iid: list(idxs) for iid, idxs in current_assignment.items()}

    # Only swap items with the same number of facings.
    by_n: dict[int, list[str]] = defaultdict(list)
    for iid, idxs in assignment.items():
        by_n[len(idxs)].append(iid)
    swappable = [lst for lst in by_n.values() if len(lst) >= 2]
    if not swappable:
        return dict(current_assignment)

    def item_cost(iid: str) -> float:
        picks = pick_counts.get(iid, 0)
        return picks * min(slot_dist[idx] for idx in assignment[iid]) if picks else 0.0

    cooling = (t_final / t_start) ** (1.0 / n_iter)
    t = t_start

    for _ in range(n_iter):
        group = rng.choice(swappable)
        a, b = rng.sample(group, 2)

        e_a, e_b = item_cost(a), item_cost(b)
        assignment[a], assignment[b] = assignment[b], assignment[a]
        delta = (item_cost(a) + item_cost(b)) - (e_a + e_b)

        if delta >= 0 and rng.random() >= math.exp(-delta / t):
            assignment[a], assignment[b] = assignment[b], assignment[a]  # revert

        t *= cooling

    return assignment
