"""
Quick smoke tests covering data generation, batchers, simulation completion,
and the server WebSocket message flow.

Run with:  pytest tests/test_sim.py -v
"""
import asyncio
import json
import pytest

from warehouse.grid import WarehouseGrid
from warehouse.inventory import Inventory
from warehouse.agent import PickAgent
from warehouse.simulation import Simulation
from warehouse.batcher import FIFOBatcher, ZoneBatcher, GreedyTSPBatcher
from warehouse.data_gen import generate_catalog, generate_orders


# ── helpers ───────────────────────────────────────────────────────────────────

DEFAULT = WarehouseGrid.build_default()
QUAD    = WarehouseGrid.build_quad()


def catalog(n_items=30, n_families=4, seed=42):
    return generate_catalog(n_items=n_items, n_families=n_families, seed=seed)


def orders(items, n=8, per_order=4, seed=42):
    return generate_orders(items, n_orders=n, items_per_order=per_order, seed=seed)


def run_sim(grid, items, orders, strategy="none", batch_size=2, restock_delay=0):
    inventory = Inventory(grid)
    inventory.seed(items)
    agent = PickAgent("A1", grid.pack_station_pos, grid)
    sim = Simulation(grid, inventory, agent, restock_delay=restock_delay)

    if strategy == "zone":
        for b in ZoneBatcher(grid, inventory, max_batch_size=batch_size).batch(orders):
            sim.enqueue_batch(b)
    elif strategy == "tsp":
        for b in GreedyTSPBatcher(inventory, grid, max_batch_size=batch_size).batch(orders):
            sim.enqueue_batch(b)
    else:
        for o in orders:
            sim.enqueue_order(o)

    sim.run()
    return sim


class FakeWS:
    def __init__(self): self.msgs = []
    async def send_text(self, t): self.msgs.append(json.loads(t))


def run_server(strategy="none", batch_size=2, n_orders=6, grid=None, seed=42):
    from server import _run_simulation
    grid = grid or DEFAULT

    async def _run():
        ws = FakeWS()
        await _run_simulation(ws, {
            "layout": grid.to_dict(),
            "n_orders": n_orders, "n_items": 30, "items_per_order": 4,
            "n_families": 4, "demand_skew": 2.0, "family_affinity": 0.7,
            "batch_strategy": strategy, "batch_size": batch_size,
            "seed": seed, "tick_delay_ms": 0,
        })
        return ws.msgs

    return asyncio.run(_run())


# ── data generation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("n_items,n_families,per_order", [
    (30, 4, 4),
    (10, 6, 4),   # small families — old bug produced short orders
    (20, 5, 3),
    (50, 8, 5),
])
def test_orders_always_exact_size(n_items, n_families, per_order):
    items = catalog(n_items=n_items, n_families=n_families)
    result = orders(items, n=20, per_order=per_order)
    bad = [o for o in result if len(o.item_ids) != per_order]
    assert not bad, f"{len(bad)} orders have wrong size: {[(o.order_id, len(o.item_ids)) for o in bad]}"


def test_orders_no_duplicates_within_order():
    items = catalog()
    result = orders(items)
    for o in result:
        assert len(o.item_ids) == len(set(o.item_ids)), f"Duplicate items in {o.order_id}"


# ── batchers ──────────────────────────────────────────────────────────────────

def test_fifo_one_batch_per_order():
    items = catalog()
    batches = FIFOBatcher().batch(orders(items))
    assert all(len(b.orders) == 1 for b in batches)
    assert len(batches) == 8


@pytest.mark.parametrize("batch_size", [2, 3, 4])
def test_zone_batcher_groups_orders(batch_size):
    items = catalog()
    os = orders(items, n=10)
    inv = Inventory(DEFAULT); inv.seed(items)
    batches = ZoneBatcher(DEFAULT, inv, max_batch_size=batch_size).batch(os)
    assert len(batches) < len(os), "ZoneBatcher produced no grouping"
    assert all(len(b.orders) <= batch_size for b in batches)


@pytest.mark.parametrize("batch_size", [2, 3, 4])
def test_tsp_batcher_reduces_distance(batch_size):
    items = catalog()
    os = orders(items, n=10)

    # Compare agent.total_distance (actual steps walked), not the sum of per-order
    # distance_traveled — batched runs attribute the full trip distance to every order
    # in the batch, so the per-order sum is inflated vs FIFO.
    sim_fifo = run_sim(DEFAULT, items, os, strategy="none")
    sim_tsp  = run_sim(DEFAULT, items, os, strategy="tsp", batch_size=batch_size)

    dist_fifo = sim_fifo.agent.total_distance
    dist_tsp  = sim_tsp.agent.total_distance
    assert dist_tsp <= dist_fifo, f"TSP ({dist_tsp}) > FIFO ({dist_fifo}) at batch_size={batch_size}"


def test_zone_batcher_works_after_round_trip_serialisation():
    """from_dict without zone_map_list must still produce a usable zone map."""
    # Simulate what the browser client sends — no zone_map_list key
    d = DEFAULT.to_dict()
    del d["zone_map_list"]
    grid = WarehouseGrid.from_dict(d)
    assert grid.zone_map, "zone_map is empty after from_dict without zone_map_list"

    items = catalog()
    inv = Inventory(grid); inv.seed(items)
    os = orders(items, n=10)
    batches = ZoneBatcher(grid, inv, max_batch_size=2).batch(os)
    assert len(batches) < len(os), "ZoneBatcher produced no grouping on round-tripped grid"


def test_batched_order_pick_count_equals_items_across_orders():
    """Each order's items all appear in unified_item_ids — no deduplication."""
    items = catalog()
    os = orders(items, n=6)
    inv = Inventory(DEFAULT); inv.seed(items)
    for b in GreedyTSPBatcher(inv, DEFAULT, max_batch_size=3).batch(os):
        expected = sum(len(o.item_ids) for o in b.orders)
        assert len(b.unified_item_ids) == expected, \
            f"Batch {b.batch_id}: expected {expected} picks, got {len(b.unified_item_ids)}"
        assert len(b.item_to_order) == expected, \
            f"Batch {b.batch_id}: item_to_order length {len(b.item_to_order)} != {expected}"


# ── simulation completion ─────────────────────────────────────────────────────

@pytest.mark.parametrize("n_orders", [1, 5, 20])
def test_all_orders_complete(n_orders):
    items = catalog()
    sim = run_sim(DEFAULT, items, orders(items, n=n_orders))
    assert len(sim.completed_metrics) == n_orders


@pytest.mark.parametrize("n_orders", [1, 5, 20])
def test_all_orders_complete_quad(n_orders):
    items = catalog(n_items=60, n_families=6)
    sim = run_sim(QUAD, items, orders(items, n=n_orders))
    assert len(sim.completed_metrics) == n_orders


@pytest.mark.parametrize("n_orders,per_order", [
    (5,  4),
    (10, 4),
    (20, 3),
])
def test_fifo_total_items_equals_orders_times_per_order(n_orders, per_order):
    items = catalog()
    os = orders(items, n=n_orders, per_order=per_order)
    sim = run_sim(DEFAULT, items, os)
    total = sum(m.items_picked for m in sim.completed_metrics)
    assert total == n_orders * per_order, \
        f"Expected {n_orders * per_order} items total, got {total}"


@pytest.mark.parametrize("strategy,batch_size", [
    ("none", 1),
    ("zone", 2),
    ("tsp",  2),
    ("tsp",  4),
])
def test_all_orders_get_metrics(strategy, batch_size):
    n_orders, per_order = 8, 4
    items = catalog()
    os = orders(items, n=n_orders, per_order=per_order)
    sim = run_sim(DEFAULT, items, os, strategy=strategy, batch_size=batch_size, restock_delay=0)
    assert len(sim.completed_metrics) == n_orders, \
        f"strategy={strategy} bs={batch_size}: {len(sim.completed_metrics)} metrics for {n_orders} orders"
    total = sum(m.items_picked for m in sim.completed_metrics)
    expected = n_orders * per_order
    assert total == expected, \
        f"strategy={strategy} bs={batch_size}: total items {total} != {expected}"


def test_summary_totals_consistent():
    items = catalog()
    sim = run_sim(DEFAULT, items, orders(items, n=10))
    s = sim.get_summary()
    assert s.total_orders == len(sim.completed_metrics)
    assert s.total_items  == sum(m.items_picked for m in sim.completed_metrics)
    assert s.total_distance == sum(m.distance_traveled for m in sim.completed_metrics)


# ── server WebSocket messages ─────────────────────────────────────────────────

@pytest.mark.parametrize("strategy,batch_size", [
    ("none", 1),
    ("zone", 2),
    ("tsp",  2),
    ("tsp",  3),
])
def test_server_emits_one_complete_per_order(strategy, batch_size):
    n = 6
    msgs = run_server(strategy=strategy, batch_size=batch_size, n_orders=n)
    completions = [m for m in msgs if m["type"] == "order_complete"]
    assert len(completions) == n, \
        f"strategy={strategy}: got {len(completions)} order_complete, expected {n}"


def test_server_orders_ready_correct_n_items():
    msgs = run_server(strategy="tsp", batch_size=2, n_orders=6)
    ready = next(m for m in msgs if m["type"] == "orders_ready")
    bad = [o for o in ready["orders"] if o["n_items"] != 4]
    assert not bad, f"Wrong n_items in orders_ready: {bad}"


def test_server_orders_ready_grouped_by_batch():
    msgs = run_server(strategy="tsp", batch_size=2, n_orders=6)
    ready = next(m for m in msgs if m["type"] == "orders_ready")
    batch_ids = [o["batch_id"] for o in ready["orders"]]
    last_seen: dict[str, int] = {}
    for i, bid in enumerate(batch_ids):
        if bid in last_seen and last_seen[bid] != i - 1:
            pytest.fail(f"Batch {bid} not consecutive in orders_ready: {batch_ids}")
        last_seen[bid] = i


def test_server_complete_message_present():
    msgs = run_server(n_orders=4)
    assert any(m["type"] == "complete" for m in msgs)
    complete = next(m for m in msgs if m["type"] == "complete")
    assert complete["total_orders"] == 4
    assert complete["total_ticks"] > 0


@pytest.mark.parametrize("strategy", ["none", "zone", "tsp"])
def test_server_tick_has_active_batch(strategy):
    msgs = run_server(strategy=strategy, batch_size=2, n_orders=4)
    ticks = [m for m in msgs if m["type"] == "tick"]
    assert ticks, "No tick messages received"
    assert all("active_batch" in t for t in ticks), "Some tick frames missing active_batch key"
