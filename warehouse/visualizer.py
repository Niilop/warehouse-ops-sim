from __future__ import annotations
import time
from warehouse.grid import WarehouseGrid, CellType
from warehouse.agent import PickAgent, AgentState
from warehouse.inventory import Order
from warehouse.simulation import Simulation, OrderMetrics

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.layout import Layout
from rich.live import Live
from rich import box

# Cell display: (characters, background style)
_CELL = {
    CellType.EMPTY:        ("  ", "on grey23"),
    CellType.RACK:         ("[]", "on dark_orange3"),
    CellType.AISLE:        ("  ", "on grey11"),
    CellType.PACK_STATION: ("PS", "on green4"),
}
_AGENT_BG = "on blue"
_AGENT_CARRYING_BG = "on bright_blue"


class WarehouseVisualizer:
    def __init__(self) -> None:
        self.console = Console()

    def render_grid(
        self,
        grid: WarehouseGrid,
        agent: PickAgent,
        current_tick: int,
        active_order: Order | None,
    ) -> Panel:
        table = Table(show_header=False, show_edge=False, padding=(0, 0), box=None)
        for _ in range(grid.cols):
            table.add_column(no_wrap=True)

        for r in range(grid.rows):
            row_cells: list[Text] = []
            for c in range(grid.cols):
                ct = CellType(grid.grid[r, c])
                chars, style = _CELL[ct]
                if (r, c) == agent.pos:
                    bg = _AGENT_CARRYING_BG if agent.carried_items else _AGENT_BG
                    cell = Text(f"{'A' * len(chars)}", style=f"bold white {bg}")
                else:
                    cell = Text(chars, style=style)
                row_cells.append(cell)
            table.add_row(*row_cells)

        order_str = active_order.order_id if active_order else "–"
        carrying = ", ".join(i.item_id for i in agent.carried_items) or "nothing"
        title = (
            f"Tick [bold]{current_tick}[/bold]  |  "
            f"Order: [cyan]{order_str}[/cyan]  |  "
            f"Carrying: [yellow]{carrying}[/yellow]  |  "
            f"State: [green]{agent.state.value}[/green]"
        )
        return Panel(table, title=title, border_style="bright_black")

    def render_metrics_table(self, metrics: list[OrderMetrics]) -> Table:
        table = Table(title="Order Metrics", box=box.SIMPLE_HEAVY, show_footer=True)
        table.add_column("Order ID", style="cyan")
        table.add_column("Items", justify="right",
                         footer=str(sum(m.items_picked for m in metrics)))
        table.add_column("Distance", justify="right",
                         footer=str(sum(m.distance_traveled for m in metrics)))
        table.add_column("Ticks", justify="right",
                         footer=str(sum(m.ticks_taken for m in metrics)))
        table.add_column("Completed At", justify="right")

        for m in metrics:
            table.add_row(
                m.order_id,
                str(m.items_picked),
                str(m.distance_traveled),
                str(m.ticks_taken),
                str(m.completed_at_tick),
            )
        return table

    def run_live(self, sim: Simulation, tick_delay: float = 0.05) -> None:
        layout = Layout()
        layout.split_column(
            Layout(name="grid", ratio=3),
            Layout(name="metrics", ratio=1),
        )

        with Live(layout, console=self.console, refresh_per_second=30, screen=False):
            while True:
                layout["grid"].update(
                    self.render_grid(sim.grid, sim.agent, sim.current_tick, sim._active_order)
                )
                layout["metrics"].update(
                    Panel(self.render_metrics_table(sim.completed_metrics),
                          border_style="bright_black")
                )
                has_more = sim.step()
                time.sleep(tick_delay)
                if not has_more:
                    # Final render
                    layout["grid"].update(
                        self.render_grid(sim.grid, sim.agent, sim.current_tick, sim._active_order)
                    )
                    layout["metrics"].update(
                        Panel(self.render_metrics_table(sim.completed_metrics),
                              border_style="bright_black")
                    )
                    time.sleep(0.5)
                    break

    def plot_metrics(self, metrics: list[OrderMetrics]) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.console.print("[yellow]matplotlib not installed — skipping chart.[/yellow]")
            return

        if not metrics:
            return

        order_ids = [m.order_id for m in metrics]
        x = range(len(metrics))

        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))
        fig.suptitle("Warehouse Pick Agent — Order Metrics")

        ax1.bar(x, [m.ticks_taken for m in metrics], color="steelblue")
        ax1.set_xticks(list(x))
        ax1.set_xticklabels(order_ids, rotation=30, ha="right")
        ax1.set_ylabel("Ticks")
        ax1.set_title("Ticks per Order")

        ax2.bar(x, [m.distance_traveled for m in metrics], color="darkorange")
        ax2.set_xticks(list(x))
        ax2.set_xticklabels(order_ids, rotation=30, ha="right")
        ax2.set_ylabel("Grid Cells")
        ax2.set_title("Distance per Order")

        plt.tight_layout()
        plt.show()
