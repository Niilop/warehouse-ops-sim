from __future__ import annotations
from dataclasses import dataclass, field

from warehouse.grid import WarehouseGrid
from warehouse.inventory import Inventory, Item, Order
from warehouse.agent import PickAgent
from warehouse.simulation import Simulation, OrderMetrics
from warehouse.batcher import FIFOBatcher, ZoneBatcher, GreedyTSPBatcher
from warehouse.data_gen import generate_catalog, generate_orders
from warehouse.optimizer import slot_distances, random_placement, demand_placement, affinity_placement


@dataclass
class ScenarioConfig:
    n_items: int = 40
    n_families: int = 5
    demand_skew: float = 2.0
    family_affinity: float = 0.7
    n_orders: int = 100
    items_per_order: int = 4
    seed: int = 42
    label: str = ""


@dataclass
class ScenarioResult:
    config: ScenarioConfig
    strategy: str
    total_distance: int
    total_ticks: int
    avg_distance_per_order: float
    metrics: list[OrderMetrics] = field(default_factory=list)


def _run_scenario(
    grid: WarehouseGrid,
    placement: list[Item],
    sorted_slot_indices: list[int],
    orders: list[Order],
    max_ticks: int = 200_000,
) -> list[OrderMetrics]:
    """Spin up a fresh inventory, seed with given placement, run all orders headlessly."""
    inventory = Inventory(grid)
    # Only seed as many items as there are sorted slot indices
    n = min(len(placement), len(sorted_slot_indices))
    inventory.seed(placement[:n], slot_order=sorted_slot_indices[:n])

    agent = PickAgent("A1", grid.pack_station_pos, grid)
    sim = Simulation(grid, inventory, [agent])
    for order in orders:
        sim.enqueue_order(order)
    return sim.run(max_ticks=max_ticks)


def run_analysis(
    configs: list[ScenarioConfig],
    grid: WarehouseGrid | None = None,
) -> list[ScenarioResult]:
    """
    For each config runs three strategies (random, demand, affinity) and collects results.
    Returns a flat list of ScenarioResult — len = len(configs) * 3.
    """
    if grid is None:
        grid = WarehouseGrid.build_default()

    results: list[ScenarioResult] = []

    for cfg in configs:
        print(f"  Running config: {cfg.label or cfg}")
        items = generate_catalog(
            n_items=cfg.n_items,
            n_families=cfg.n_families,
            demand_skew=cfg.demand_skew,
            seed=cfg.seed,
        )
        orders = generate_orders(
            items=items,
            n_orders=cfg.n_orders,
            items_per_order=cfg.items_per_order,
            family_affinity=cfg.family_affinity,
            seed=cfg.seed,
        )

        # Precompute slot order (closest → farthest)
        sorted_slots = slot_distances(grid, Inventory(grid))
        sorted_slot_indices = [idx for idx, _ in sorted_slots]

        strategies = {
            "random":   random_placement(items, seed=cfg.seed),
            "demand":   demand_placement(items),
            "affinity": affinity_placement(items, orders),
        }

        for strategy_name, placement in strategies.items():
            print(f"    strategy={strategy_name} ...", end=" ", flush=True)
            metrics = _run_scenario(grid, placement, sorted_slot_indices, orders)
            total_dist = sum(m.distance_traveled for m in metrics)
            total_ticks = sum(m.ticks_taken for m in metrics)
            avg = total_dist / len(metrics) if metrics else 0.0
            results.append(ScenarioResult(
                config=cfg,
                strategy=strategy_name,
                total_distance=total_dist,
                total_ticks=total_ticks,
                avg_distance_per_order=avg,
                metrics=metrics,
            ))
            print(f"avg_dist={avg:.1f}")

    return results


def plot_analysis(results: list[ScenarioResult]) -> None:
    """Grouped bar chart (one group per config, three bars per strategy) + rich summary table."""
    _print_summary_table(results)

    try:
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError:
        print("matplotlib not installed — skipping chart.")
        return

    configs = list(dict.fromkeys(r.config.label or str(i) for i, r in enumerate(results)))
    strategies = ["random", "demand", "affinity"]
    colors = {"random": "#888888", "demand": "#4c8fcc", "affinity": "#e87040"}

    # Build data matrix: rows = strategies, cols = configs
    data: dict[str, list[float]] = {s: [] for s in strategies}
    for cfg_label in configs:
        for strategy in strategies:
            match = next(
                (r for r in results if (r.config.label or "") == cfg_label and r.strategy == strategy),
                None,
            )
            data[strategy].append(match.avg_distance_per_order if match else 0.0)

    x = np.arange(len(configs))
    width = 0.25
    fig, ax = plt.subplots(figsize=(max(8, len(configs) * 2), 5))

    for i, strategy in enumerate(strategies):
        offset = (i - 1) * width
        bars = ax.bar(x + offset, data[strategy], width, label=strategy, color=colors[strategy])
        ax.bar_label(bars, fmt="%.0f", padding=2, fontsize=8)

    ax.set_xlabel("Scenario")
    ax.set_ylabel("Avg distance per order (grid cells)")
    ax.set_title("Warehouse Layout Optimization — Strategy Comparison")
    ax.set_xticks(x)
    ax.set_xticklabels(configs, rotation=20, ha="right")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    plt.tight_layout()
    plt.show()


def _print_summary_table(results: list[ScenarioResult]) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box
    except ImportError:
        for r in results:
            print(f"{r.config.label:20s} {r.strategy:10s} avg_dist={r.avg_distance_per_order:.1f}")
        return

    console = Console()
    table = Table(title="Analysis Results", box=box.SIMPLE_HEAVY)
    table.add_column("Scenario", style="cyan")
    table.add_column("Strategy", style="bold")
    table.add_column("Orders", justify="right")
    table.add_column("Avg Dist", justify="right")
    table.add_column("Total Dist", justify="right")
    table.add_column("vs Random", justify="right")

    # Group by config for relative comparison
    config_labels = list(dict.fromkeys(r.config.label for r in results))
    for label in config_labels:
        group = [r for r in results if r.config.label == label]
        random_dist = next((r.avg_distance_per_order for r in group if r.strategy == "random"), None)
        for r in group:
            if random_dist and random_dist > 0:
                pct = (r.avg_distance_per_order - random_dist) / random_dist * 100
                vs_random = f"{pct:+.1f}%"
                color = "green" if pct < -1 else ("red" if pct > 1 else "")
                vs_random_styled = f"[{color}]{vs_random}[/{color}]" if color else vs_random
            else:
                vs_random_styled = "–"
            table.add_row(
                r.config.label,
                r.strategy,
                str(len(r.metrics)),
                f"{r.avg_distance_per_order:.1f}",
                str(r.total_distance),
                vs_random_styled,
            )

    console.print(table)


# ---------------------------------------------------------------------------
# Phase 2: Batch analysis
# ---------------------------------------------------------------------------

@dataclass
class BatchScenarioConfig:
    base: ScenarioConfig
    batch_strategy: str = "fifo"   # "fifo" | "zone" | "tsp"
    max_batch_size: int = 1
    min_zone_overlap: int = 2


def run_batch_analysis(
    configs: list[ScenarioConfig],
    batch_sizes: list[int] | range = range(1, 7),
    grid: WarehouseGrid | None = None,
) -> dict[tuple[str, int], list[ScenarioResult]]:
    """
    For each config, runs FIFO / Zone / TSP strategies across each batch size.
    Returns a dict keyed by (strategy, batch_size) -> list[ScenarioResult].
    Uses demand placement for all scenarios (controls for slot-assignment variance).
    """
    if grid is None:
        grid = WarehouseGrid.build_default()

    results: dict[tuple[str, int], list[ScenarioResult]] = {}

    for cfg in configs:
        print(f"  Batch analysis config: {cfg.label or cfg}")
        items = generate_catalog(
            n_items=cfg.n_items, n_families=cfg.n_families,
            demand_skew=cfg.demand_skew, seed=cfg.seed,
        )
        orders = generate_orders(
            items=items, n_orders=cfg.n_orders, items_per_order=cfg.items_per_order,
            family_affinity=cfg.family_affinity, seed=cfg.seed,
        )
        sorted_slots = slot_distances(grid, Inventory(grid))
        sorted_slot_indices = [idx for idx, _ in sorted_slots]
        placement = demand_placement(items)

        for batch_size in batch_sizes:
            for strategy_name in ("fifo", "zone", "tsp"):
                inventory = Inventory(grid)
                n = min(len(placement), len(sorted_slot_indices))
                inventory.seed(placement[:n], slot_order=sorted_slot_indices[:n])

                if strategy_name == "fifo":
                    batcher: FIFOBatcher | ZoneBatcher | GreedyTSPBatcher = FIFOBatcher()
                elif strategy_name == "zone":
                    batcher = ZoneBatcher(grid, inventory, max_batch_size=batch_size)
                else:
                    batcher = GreedyTSPBatcher(inventory, grid, max_batch_size=batch_size)

                batches = batcher.batch(orders)
                agent = PickAgent("A1", grid.pack_station_pos, grid)
                sim = Simulation(grid, inventory, [agent])
                for batch in batches:
                    sim.enqueue_batch(batch)
                metrics = sim.run()

                total_dist = sum(m.distance_traveled for m in metrics)
                total_ticks = sum(m.ticks_taken for m in metrics)
                avg = total_dist / len(metrics) if metrics else 0.0
                key = (strategy_name, batch_size)
                results.setdefault(key, []).append(ScenarioResult(
                    config=cfg,
                    strategy=f"{strategy_name}_b{batch_size}",
                    total_distance=total_dist,
                    total_ticks=total_ticks,
                    avg_distance_per_order=avg,
                    metrics=metrics,
                ))

    return results
