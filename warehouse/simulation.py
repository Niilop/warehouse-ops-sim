from __future__ import annotations
from collections import deque
from dataclasses import dataclass, field
from warehouse.grid import WarehouseGrid
from warehouse.inventory import Inventory, Item, Order
from warehouse.agent import PickAgent, AgentState


@dataclass
class RestockJob:
    item_id: str
    item: Item
    ready_at_tick: int


@dataclass
class OrderMetrics:
    order_id: str
    items_picked: int
    distance_traveled: int
    ticks_taken: int
    completed_at_tick: int
    wait_ticks: int = 0


@dataclass
class SimMetrics:
    total_orders: int
    total_items: int
    total_distance: int
    total_ticks: int
    lines_per_hour: float   # items / total_ticks * 3600 (1 tick ≈ 1 second scale)
    idle_ticks: int
    wait_ticks_total: int
    stockout_count: int


class Simulation:
    def __init__(
        self,
        grid: WarehouseGrid,
        inventory: Inventory,
        agent: PickAgent,
        restock_delay: int = 10,
    ) -> None:
        self.grid = grid
        self.inventory = inventory
        self.agent = agent
        self.order_queue: deque[Order] = deque()
        self.completed_metrics: list[OrderMetrics] = []
        self.current_tick: int = 0
        self._active_order: Order | None = None
        self._order_start_tick: int = 0
        self._order_start_distance: int = 0

        # Restock queue (1b)
        self.restock_delay = restock_delay
        self.restock_queue: list[RestockJob] = []
        self.stockout_count: int = 0

        # Metrics tracking (1c)
        self.idle_ticks: int = 0
        self._order_enqueue_tick: dict[str, int] = {}
        self._order_wait_ticks: dict[str, int] = {}

    def enqueue_order(self, order: Order) -> None:
        self.order_queue.append(order)
        self._order_enqueue_tick[order.order_id] = self.current_tick

    def step(self) -> bool:
        """Advance simulation by one tick. Returns True while work remains."""
        agent = self.agent

        # Process restock queue — release items whose delay has elapsed
        ready = [j for j in self.restock_queue if j.ready_at_tick <= self.current_tick]
        for job in ready:
            self.restock_queue.remove(job)
            self.inventory.restock(job.item)

        # Dispatch next order when idle
        if agent.state == AgentState.IDLE:
            dispatched = False
            while self.order_queue:
                order = self.order_queue.popleft()
                available_ids = [
                    iid for iid in order.item_ids
                    if iid in self.inventory._item_to_slot
                ]
                missing = len(order.item_ids) - len(available_ids)
                self.stockout_count += missing
                if not available_ids:
                    continue  # entire order stocked out, skip
                # Build a filtered order if some items were stocked out
                if missing:
                    from dataclasses import replace
                    order = replace(order, item_ids=available_ids)
                wait = self.current_tick - self._order_enqueue_tick.get(
                    order.order_id, self.current_tick
                )
                self._order_wait_ticks[order.order_id] = wait
                self._active_order = order
                self._order_start_tick = self.current_tick
                self._order_start_distance = agent.total_distance
                agent.assign_order(order, self.inventory, self.grid.pack_station_pos)
                dispatched = True
                break

            if not dispatched:
                if not self.restock_queue:
                    return False  # queue empty and no pending restocks — done
                # Waiting for restocks; agent idles this tick
                self.idle_ticks += 1
                self.current_tick += 1
                return True

        # Move agent one step
        arrived = not agent.step()  # step() returns False when path exhausted

        # Handle arrival
        if arrived:
            if agent.state == AgentState.MOVING_TO_RACK:
                agent.execute_pick(self.inventory)
            elif agent.state == AgentState.MOVING_TO_STATION:
                deposited = agent.execute_deposit()
                assert self._active_order is not None
                wait_ticks = self._order_wait_ticks.get(self._active_order.order_id, 0)
                self.completed_metrics.append(OrderMetrics(
                    order_id=self._active_order.order_id,
                    items_picked=len(deposited),
                    distance_traveled=agent.total_distance - self._order_start_distance,
                    ticks_taken=self.current_tick - self._order_start_tick,
                    completed_at_tick=self.current_tick,
                    wait_ticks=wait_ticks,
                ))
                self._active_order = None
                for item in deposited:
                    self.restock_queue.append(RestockJob(
                        item_id=item.item_id,
                        item=item,
                        ready_at_tick=self.current_tick + self.restock_delay,
                    ))

        self.current_tick += 1
        return True

    def get_summary(self) -> SimMetrics:
        metrics = self.completed_metrics
        total_items = sum(m.items_picked for m in metrics)
        total_dist = sum(m.distance_traveled for m in metrics)
        total_wait = sum(m.wait_ticks for m in metrics)
        lph = (total_items / self.current_tick * 3600) if self.current_tick > 0 else 0.0
        return SimMetrics(
            total_orders=len(metrics),
            total_items=total_items,
            total_distance=total_dist,
            total_ticks=self.current_tick,
            lines_per_hour=round(lph, 2),
            idle_ticks=self.idle_ticks,
            wait_ticks_total=total_wait,
            stockout_count=self.stockout_count,
        )

    def run(self, max_ticks: int = 10_000) -> list[OrderMetrics]:
        """Run to completion (headless). Returns completed metrics."""
        while self.current_tick < max_ticks:
            if not self.step():
                break
        return self.completed_metrics
