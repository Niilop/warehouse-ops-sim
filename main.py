import logging
from warehouse.grid import WarehouseGrid
from warehouse.inventory import Inventory
from warehouse.agent import PickAgent
from warehouse.simulation import Simulation
from warehouse.visualizer import WarehouseVisualizer
from warehouse.data_gen import generate_catalog, generate_orders

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    grid = WarehouseGrid.build_default()
    inventory = Inventory(grid)

    items = generate_catalog(n_items=30, n_families=4, demand_skew=2.0, seed=42)
    inventory.seed(items)

    agent = PickAgent(agent_id="A1", start_pos=grid.pack_station_pos, grid=grid)
    sim = Simulation(grid=grid, inventory=inventory, agents=[agent])

    # Generate 10 orders for a visible live demo (items restock after each deposit)
    orders = generate_orders(items, n_orders=10, items_per_order=4, family_affinity=0.7, seed=42)
    for order in orders:
        sim.enqueue_order(order)

    viz = WarehouseVisualizer()
    viz.run_live(sim, tick_delay=0.06)

    viz.console.print(viz.render_metrics_table(sim.completed_metrics))
    viz.plot_metrics(sim.completed_metrics)


if __name__ == "__main__":
    main()
