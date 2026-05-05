from __future__ import annotations
import math
import time
import numpy as np
from warehouse.inventory import Item, Order


def generate_catalog(
    n_items: int = 40,
    n_families: int = 5,
    demand_skew: float = 2.0,
    seed: int = 42,
) -> list[Item]:
    """
    Creates items with power-law demand weights and family assignments.

    demand_skew: scale of the exponential distribution. Higher = more extreme skew
    (a few items dominate). Weights are normalised to sum to 1.
    Family is assigned round-robin: item i → family F{i % n_families}.
    """
    rng = np.random.default_rng(seed)
    # Raise unit-exponential samples to the power of demand_skew.
    # Exponential(scale) is a linear transform and cancels under normalisation,
    # so using a power instead gives genuinely different distributions:
    # skew > 1 amplifies high values → concentrated; skew < 1 compresses → flat.
    raw_weights = rng.exponential(scale=1.0, size=n_items) ** demand_skew
    total = raw_weights.sum()
    weights = raw_weights / total

    items = []
    for i in range(n_items):
        items.append(Item(
            item_id=f"I{i:03d}",
            name=f"Product-{i:03d}",
            pick_rate=float(weights[i]),
            family=f"F{i % n_families}",
        ))
    return items


def generate_orders(
    items: list[Item],
    n_orders: int = 100,
    items_per_order: int = 4,
    family_affinity: float = 0.7,
    seed: int = 42,
) -> list[Order]:
    """
    Generates orders with demand-weighted sampling and family affinity.

    Same item CAN appear in multiple different orders (items restock after pick).
    Each order contains no duplicates.

    Per order:
      - Pick a theme family weighted by total family demand sum
      - Sample ceil(items_per_order * family_affinity) items from that family
      - Fill remaining slots from the full catalog
      - Both draws are weighted by pick_rate
    """
    if items_per_order > len(items):
        raise ValueError(
            f"items_per_order ({items_per_order}) exceeds catalog size ({len(items)})"
        )

    rng = np.random.default_rng(seed)

    # Group items by family
    families: dict[str, list[Item]] = {}
    for item in items:
        families.setdefault(item.family, []).append(item)

    family_names = list(families.keys())
    family_demand = np.array([
        sum(i.pick_rate for i in families[f]) for f in family_names
    ])
    family_probs = family_demand / family_demand.sum()

    all_weights = np.array([i.pick_rate for i in items])

    orders: list[Order] = []
    for idx in range(n_orders):
        # Choose theme family
        theme_family = family_names[rng.choice(len(family_names), p=family_probs)]
        family_items = families[theme_family]

        n_from_family = math.ceil(items_per_order * family_affinity)
        n_from_family = min(n_from_family, len(family_items), items_per_order)

        chosen: list[Item] = []

        # Sample from theme family
        fam_weights = np.array([i.pick_rate for i in family_items])
        fam_probs = fam_weights / fam_weights.sum()
        fam_indices = rng.choice(len(family_items), size=n_from_family, replace=False, p=fam_probs)
        chosen.extend(family_items[i] for i in fam_indices)

        # Fill remaining slots from the full catalog — computed after the actual family draw
        # so a small theme family never leaves the order short
        n_from_rest = items_per_order - len(chosen)
        if n_from_rest > 0:
            chosen_ids = {i.item_id for i in chosen}
            remaining = [i for i in items if i.item_id not in chosen_ids]
            if remaining:
                rem_weights = np.array([i.pick_rate for i in remaining])
                rem_probs = rem_weights / rem_weights.sum()
                n_pick2 = min(n_from_rest, len(remaining))
                rem_indices = rng.choice(len(remaining), size=n_pick2, replace=False, p=rem_probs)
                chosen.extend(remaining[i] for i in rem_indices)

        orders.append(Order(
            order_id=f"ORD-{idx + 1:04d}",
            item_ids=[i.item_id for i in chosen],
            created_at=time.monotonic(),
        ))

    return orders
