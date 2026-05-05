from __future__ import annotations
from collections import Counter, deque
from dataclasses import dataclass, field
from warehouse.grid import WarehouseGrid
from warehouse.inventory import Inventory, Item, Order
from warehouse.agent import PickAgent, AgentState, StepResult
from warehouse.batcher import BatchedOrder


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
        agents: list[PickAgent],
        restock_delay: int = 10,
    ) -> None:
        self.grid = grid
        self.inventory = inventory
        self.agents = agents
        self.order_queue: deque[Order] = deque()
        self.batch_queue: deque[BatchedOrder] = deque()
        self.completed_metrics: list[OrderMetrics] = []
        self.current_tick: int = 0

        # Per-agent batch tracking
        self._agent_batch: dict[str, BatchedOrder | None] = {a.agent_id: None for a in agents}
        self._agent_start_tick: dict[str, int] = {a.agent_id: 0 for a in agents}
        self._agent_start_dist: dict[str, int] = {a.agent_id: 0 for a in agents}

        # Pack station contention
        self.station_busy: bool = False

        # Restock queue
        self.restock_delay = restock_delay
        self.restock_queue: list[RestockJob] = []
        self.stockout_count: int = 0

        # Metrics tracking
        self.idle_ticks: int = 0
        self._order_enqueue_tick: dict[str, int] = {}
        self._order_wait_ticks: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Backward-compat properties (single-agent callers)
    # ------------------------------------------------------------------

    @property
    def agent(self) -> PickAgent:
        return self.agents[0]

    @property
    def _active_batch(self) -> BatchedOrder | None:
        return self._agent_batch.get(self.agents[0].agent_id)

    @property
    def _active_order(self) -> Order | None:
        batch = self._active_batch
        if batch is None:
            return None
        return batch.orders[0] if batch.orders else None

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def enqueue_order(self, order: Order) -> None:
        self.order_queue.append(order)
        self._order_enqueue_tick[order.order_id] = self.current_tick

    def enqueue_batch(self, batch: BatchedOrder) -> None:
        self.batch_queue.append(batch)
        for order in batch.orders:
            self._order_enqueue_tick[order.order_id] = self.current_tick

    # ------------------------------------------------------------------
    # Per-agent dispatch and deposit helpers
    # ------------------------------------------------------------------

    def _dispatch_agent(self, agent: PickAgent, occupied: set[tuple[int, int]]) -> bool:
        """Try to assign the next available batch or order to an idle agent.
        Returns True if something was dispatched."""
        # batch_queue: peek without removing; defer if stock insufficient
        if self.batch_queue:
            front = self.batch_queue[0]
            needed = Counter(front.unified_item_ids)
            if all(self.inventory.stock_level(iid) >= cnt for iid, cnt in needed.items()):
                self.batch_queue.popleft()
                for order in front.orders:
                    wait = self.current_tick - self._order_enqueue_tick.get(
                        order.order_id, self.current_tick
                    )
                    self._order_wait_ticks[order.order_id] = wait
                self._agent_batch[agent.agent_id] = front
                self._agent_start_tick[agent.agent_id] = self.current_tick
                self._agent_start_dist[agent.agent_id] = agent.total_distance
                agent.assign_batch(front, self.inventory, self.grid.pack_station_pos, occupied)
                return True

        # order_queue: try each once; defer unavailable ones to the back
        queue_size = len(self.order_queue)
        skipped = 0
        while self.order_queue and skipped < queue_size:
            order = self.order_queue.popleft()
            needed = Counter(order.item_ids)
            if not all(self.inventory.stock_level(iid) >= cnt for iid, cnt in needed.items()):
                self.order_queue.append(order)
                skipped += 1
                continue
            wait = self.current_tick - self._order_enqueue_tick.get(
                order.order_id, self.current_tick
            )
            self._order_wait_ticks[order.order_id] = wait
            batch = BatchedOrder(
                batch_id=order.order_id,
                orders=[order],
                unified_item_ids=list(order.item_ids),
                item_to_order=[order.order_id for _ in order.item_ids],
            )
            self._agent_batch[agent.agent_id] = batch
            self._agent_start_tick[agent.agent_id] = self.current_tick
            self._agent_start_dist[agent.agent_id] = agent.total_distance
            agent.assign_batch(batch, self.inventory, self.grid.pack_station_pos, occupied)
            return True

        return False

    def _do_deposit(self, agent: PickAgent) -> None:
        """Execute deposit for an agent that has arrived at the pack station."""
        deposited, order_breakdown = agent.execute_deposit()
        batch = self._agent_batch[agent.agent_id]
        assert batch is not None
        batch_distance = agent.total_distance - self._agent_start_dist[agent.agent_id]
        batch_ticks = self.current_tick - self._agent_start_tick[agent.agent_id]
        for oid, item_ids in order_breakdown.items():
            self.completed_metrics.append(OrderMetrics(
                order_id=oid,
                items_picked=len(item_ids),
                distance_traveled=batch_distance,
                ticks_taken=batch_ticks,
                completed_at_tick=self.current_tick,
                wait_ticks=self._order_wait_ticks.pop(oid, 0),
            ))
            self._order_enqueue_tick.pop(oid, None)
        self._agent_batch[agent.agent_id] = None
        for item in deposited:
            self.restock_queue.append(RestockJob(
                item_id=item.item_id,
                item=item,
                ready_at_tick=self.current_tick + self.restock_delay,
            ))

    # ------------------------------------------------------------------
    # Main simulation loop
    # ------------------------------------------------------------------

    def step(self) -> bool:
        """Advance simulation by one tick. Returns True while work remains."""
        # 1. Process restock queue
        due, self.restock_queue = (
            [j for j in self.restock_queue if j.ready_at_tick <= self.current_tick],
            [j for j in self.restock_queue if j.ready_at_tick > self.current_tick],
        )
        for job in due:
            self.inventory.restock(job.item)

        # 2. Reset station contention flag for this tick
        self.station_busy = False

        # 3. Dispatch idle agents serially (serial order = implicit order lock)
        occupied = {a.pos for a in self.agents}
        for agent in self.agents:
            if agent.state == AgentState.IDLE:
                self._dispatch_agent(agent, occupied)

        # 4. Move each active agent and handle arrivals.
        # Recompute occupied before each agent so moves earlier in the loop
        # are visible to agents later in the loop — prevents two agents
        # stepping onto the same cell in the same tick.
        # IDLE agents are excluded: they sit at the pack station and must not
        # block a moving agent from arriving there to deposit.
        for idx, agent in enumerate(self.agents):
            if agent.state == AgentState.IDLE:
                continue

            occupied = {a.pos for a in self.agents if a.state != AgentState.IDLE}
            result = agent.step(occupied - {agent.pos})

            if result == StepResult.WAITING:
                # Stagger replan threshold by agent index so lower-priority agents
                # yield first, preventing simultaneous replans that cause new deadlocks.
                threshold = 3 + 2 * idx
                if agent._wait_count >= threshold:
                    agent.replan(occupied - {agent.pos})
            elif result == StepResult.ARRIVED:
                if agent.state == AgentState.MOVING_TO_RACK:
                    agent.execute_pick(self.inventory)
                elif agent.state == AgentState.MOVING_TO_STATION:
                    if self.station_busy:
                        pass  # hold in MOVING_TO_STATION; retry next tick
                    else:
                        self.station_busy = True
                        self._do_deposit(agent)

        # 5. Termination and idle tracking
        all_idle = all(a.state == AgentState.IDLE for a in self.agents)
        has_pending = bool(self.restock_queue or self.order_queue or self.batch_queue)

        if all_idle:
            if not has_pending:
                return False
            self.idle_ticks += 1

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
