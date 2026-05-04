from warehouse.grid import WarehouseGrid
from warehouse.analysis import ScenarioConfig, run_analysis, plot_analysis


def main() -> None:
    configs = [
        ScenarioConfig(n_items=30, n_families=4, demand_skew=1.5, n_orders=100, label="low-skew"),
        ScenarioConfig(n_items=30, n_families=4, demand_skew=3.5, n_orders=100, label="high-skew"),
        ScenarioConfig(n_items=30, n_families=4, family_affinity=0.3, n_orders=100, label="low-affinity"),
        ScenarioConfig(n_items=30, n_families=4, family_affinity=0.9, n_orders=100, label="high-affinity"),
        ScenarioConfig(n_items=50, n_families=6, demand_skew=2.5, n_orders=100, label="large-catalog"),
    ]

    print("Running warehouse layout analysis...")
    grid = WarehouseGrid.build_default(rows=12, cols=20)
    results = run_analysis(configs, grid)
    plot_analysis(results)


if __name__ == "__main__":
    main()
