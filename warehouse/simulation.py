from __future__ import annotations
import heapq
import math
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable
from warehouse.grid import WarehouseGrid
from warehouse.inventory import Inventory, Item, Order
from warehouse.agent import PickAgent, AgentState, StepResult
from warehouse.batcher import BatchedOrder
from warehouse.task import Task, TaskType


def _poisson_draw(lam: float, rng: random.Random) -> int:
    """Exact Poisson draw via Knuth's algorithm. Returns 0 for lam <= 0."""
    if lam <= 0:
        return 0
    L = math.exp(-lam)
    k, p = 0, 1.0
    while p > L:
        k += 1
        p *= rng.random()
    return k - 1


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
    item_ids: list[str] = field(default_factory=list)


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
        epoch_length: int = 100,
        order_arrival_rate: float = 0.0,
        order_generator: Callable[[], Order] | None = None,
        day_length: int = 480,
        day_multipliers: list[float] | None = None,
        arrival_seed: int | None = None,
    ) -> None:
        self.grid = grid
        self.inventory = inventory
        self.agents = agents
        self.task_queue: list[Task] = []   # heapq; pop gives highest-priority task
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

        # Epoch reslotting
        self.epoch_length: int = epoch_length
        self._last_epoch_tick: int = 0

        # Streaming / Poisson arrivals (order_arrival_rate=0 means batch-upfront mode)
        self.order_arrival_rate: float = order_arrival_rate
        self.order_generator: Callable[[], Order] | None = order_generator
        self.day_length: int = day_length
        self.day_multipliers: list[float] = day_multipliers or [1.0] * day_length
        self._arrival_rng: random.Random = random.Random(arrival_seed)

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
        self._order_enqueue_tick[order.order_id] = self.current_tick
        batch = BatchedOrder(
            batch_id=order.order_id,
            orders=[order],
            unified_item_ids=list(order.item_ids),
            item_to_order=[order.order_id for _ in order.item_ids],
        )
        heapq.heappush(self.task_queue, Task(
            priority=TaskType.ORDER_PICK,
            created_at=self.current_tick,
            task_id=order.order_id,
            payload={"batch": batch},
        ))

    def enqueue_batch(self, batch: BatchedOrder) -> None:
        for order in batch.orders:
            self._order_enqueue_tick[order.order_id] = self.current_tick
        heapq.heappush(self.task_queue, Task(
            priority=TaskType.ORDER_PICK,
            created_at=self.current_tick,
            task_id=batch.batch_id,
            payload={"batch": batch},
        ))

    # ------------------------------------------------------------------
    # Per-agent dispatch and deposit helpers
    # ------------------------------------------------------------------

    def _dispatch_agent(self, agent: PickAgent, occupied: set[tuple[int, int]]) -> bool:
        """Pop the highest-priority available task and assign it to the idle agent.
        Tasks whose stock is insufficient are deferred back onto the heap.
        Returns True if something was dispatched."""
        claimed: Counter[str] = Counter()
        for a in self.agents:
            if a is not agent:
                for _, iid, _ in a._task_queue:
                    claimed[iid] += 1

        def available(iid: str, cnt: int) -> bool:
            return self.inventory.stock_level(iid) - claimed[iid] >= cnt

        deferred: list[Task] = []
        dispatched = False

        while self.task_queue and not dispatched:
            task = heapq.heappop(self.task_queue)
            batch: BatchedOrder = task.payload["batch"]
            needed = Counter(batch.unified_item_ids)
            if not all(available(iid, cnt) for iid, cnt in needed.items()):
                deferred.append(task)
                continue
            for order in batch.orders:
                wait = self.current_tick - self._order_enqueue_tick.get(
                    order.order_id, self.current_tick
                )
                self._order_wait_ticks[order.order_id] = wait
            self._agent_batch[agent.agent_id] = batch
            self._agent_start_tick[agent.agent_id] = self.current_tick
            self._agent_start_dist[agent.agent_id] = agent.total_distance
            agent.assign_batch(batch, self.inventory, self.grid.pack_station_pos, occupied)
            dispatched = True

        for task in deferred:
            heapq.heappush(self.task_queue, task)

        return dispatched

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
                item_ids=item_ids,
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

    def _on_epoch(self) -> None:
        """Greedy reslot: if recent pick data justifies it, move high-demand items
        closer to the pack station. Only runs when enough data has accumulated."""
        from warehouse.optimizer import slot_distances, reorg_cost

        self._last_epoch_tick = self.current_tick

        since = self.current_tick - self.epoch_length
        pick_counts: dict[str, int] = {}
        recent_count = 0
        for m in self.completed_metrics:
            if m.completed_at_tick >= since:
                recent_count += 1
                for iid in m.item_ids:
                    pick_counts[iid] = pick_counts.get(iid, 0) + 1

        if recent_count < 5 or not pick_counts:
            return

        pack_r, pack_c = self.grid.pack_station_pos
        current_distances: dict[str, int] = {
            iid: abs(slot.stand_pos[0] - pack_r) + abs(slot.stand_pos[1] - pack_c)
            for iid, slot in self.inventory._original_slot_for.items()
        }
        if not current_distances:
            return

        # Compute in-flight items before slot assignment so we can protect their home slots.
        # An in-flight item's home slot must not be reassigned to another item: if it were,
        # the eventual restock() call would find was_empty=False, skip _item_to_slot
        # registration, and permanently lose the item.
        #
        # Two categories need protection:
        #   1. Items in agent task queues / carried: cannot be relocated (agent already
        #      has a path to their current stand_pos).
        #   2. Items in restock_queue: their _original_slot_for is the destination of a
        #      pending restock job. If a subsequent epoch reassigned that slot to another
        #      item, two different items would both restock to the same slot and one would
        #      be silently dropped.
        in_flight: set[str] = set()
        for a in self.agents:
            for _, iid, _ in a._task_queue:
                in_flight.add(iid)
            for item in a.carried_items:
                in_flight.add(item.item_id)

        protected_rack_pos: set[tuple[int, int]] = set()
        for iid in in_flight:
            home = self.inventory._original_slot_for.get(iid)
            if home is not None:
                protected_rack_pos.add(home.rack_pos)
        for job in self.restock_queue:
            home = self.inventory._original_slot_for.get(job.item_id)
            if home is not None:
                protected_rack_pos.add(home.rack_pos)

        sorted_slots = slot_distances(self.grid, self.inventory)
        sorted_items = sorted(
            current_distances, key=lambda iid: pick_counts.get(iid, 0), reverse=True
        )

        available_slots = [
            (idx, dist) for idx, dist in sorted_slots
            if self.inventory._slots[idx].rack_pos not in protected_rack_pos
        ]

        proposed_distances = dict(current_distances)
        item_to_new_slot: dict[str, int] = {}
        for rank, iid in enumerate(sorted_items):
            if rank < len(available_slots):
                new_slot_idx, new_dist = available_slots[rank]
                proposed_distances[iid] = new_dist
                item_to_new_slot[iid] = new_slot_idx

        pick_rates = {iid: cnt / recent_count for iid, cnt in pick_counts.items()}
        cost = reorg_cost(current_distances, proposed_distances, self.restock_delay, pick_rates)

        if cost["payback_period_ticks"] >= 2 * self.epoch_length:
            return

        # Pre-build slot → current occupant map so we can guard against restocking collisions.
        # When epoch assigns slot_Y to item F, slot_Y might still hold item E. If E is also
        # being relocated (in item_to_new_slot), relocate() will drain E from slot_Y in the
        # same epoch tick so slot_Y will be empty when F restocks later. If E is NOT being
        # relocated (rank >= len(available_slots)), slot_Y keeps E's stock and F's restock
        # call finds was_empty=False, silently skips _item_to_slot registration, and F is lost.
        # Guard: skip any relocation whose target slot has such a non-relocated occupant.
        slot_id_to_item: dict[int, str] = {
            id(slot): iid for iid, slot in self.inventory._item_to_slot.items()
        }

        reslot_delay = max(self.restock_delay, 5)
        for iid, new_slot_idx in item_to_new_slot.items():
            if iid in in_flight:
                continue
            new_slot = self.inventory._slots[new_slot_idx]
            if self.inventory._original_slot_for.get(iid) is new_slot:
                continue
            occupant = slot_id_to_item.get(id(new_slot))
            if occupant is not None and occupant not in item_to_new_slot:
                continue
            item, units = self.inventory.relocate(iid, new_slot_idx)
            if item is not None and units > 0:
                for _ in range(units):
                    self.restock_queue.append(RestockJob(
                        item_id=iid,
                        item=item,
                        ready_at_tick=self.current_tick + reslot_delay,
                    ))

    def _process_arrivals(self) -> None:
        if self.order_arrival_rate <= 0 or self.order_generator is None:
            return
        rate = self.order_arrival_rate * self.day_multipliers[self.current_tick % self.day_length]
        for _ in range(_poisson_draw(rate, self._arrival_rng)):
            self.enqueue_order(self.order_generator())

    def _process_restocks(self) -> None:
        due, self.restock_queue = (
            [j for j in self.restock_queue if j.ready_at_tick <= self.current_tick],
            [j for j in self.restock_queue if j.ready_at_tick > self.current_tick],
        )
        for job in due:
            self.inventory.restock(job.item)

    def _dispatch_idle_agents(self) -> None:
        # Serial dispatch — queue item is popped before the next agent is considered,
        # so two agents cannot claim the same batch/order in the same tick.
        occupied = {a.pos for a in self.agents}
        for agent in self.agents:
            if agent.state == AgentState.IDLE:
                self._dispatch_agent(agent, occupied)

    def _escape_step(self, agent: "PickAgent", all_positions: set[tuple[int, int]]) -> bool:
        """Move agent one step to any free adjacent cell (not the currently-blocked one).
        Returns True if an escape cell was found and the agent was moved."""
        blocked = agent._path[0] if agent._path else None
        dirs = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        random.shuffle(dirs)
        for dr, dc in dirs:
            cell = (agent.pos[0] + dr, agent.pos[1] + dc)
            if cell == blocked:
                continue
            if not agent.grid.is_walkable(*cell):
                continue
            if cell in all_positions:
                continue
            agent.pos = cell
            agent.total_distance += 1
            agent._wait_count = 0
            agent.replan()
            return True
        return False

    def _handle_agent_movement(self) -> None:
        # Recompute occupied before each agent so moves earlier in the loop are
        # visible to later agents — prevents two agents stepping onto the same cell.
        # IDLE agents are excluded so they don't block arrivals at the pack station.
        for idx, agent in enumerate(self.agents):
            if agent.state == AgentState.IDLE:
                continue

            occupied = {a.pos for a in self.agents if a.state != AgentState.IDLE}
            result = agent.step(occupied - {agent.pos})

            if result == StepResult.WAITING:
                # Stagger threshold by index: higher-index agents yield sooner,
                # breaking symmetric deadlocks without both agents replanning at once.
                threshold = 3 + 2 * idx
                if agent._wait_count >= threshold and agent._replan_cooldown == 0:
                    if agent._wait_count >= 20:
                        # Physical escape: step sideways to a free cell, then replan.
                        # This breaks circular deadlocks that path-replanning alone cannot resolve.
                        all_pos = {a.pos for a in self.agents if a is not agent}
                        if not self._escape_step(agent, all_pos):
                            agent.replan(occupied - {agent.pos}, force=True)
                    else:
                        agent.replan(occupied - {agent.pos})
                    agent._replan_cooldown = 8
            elif result == StepResult.ARRIVED:
                if agent.state == AgentState.MOVING_TO_RACK:
                    # Only pick if actually at the target stand position.
                    # A stale/empty path (e.g. after an escape replan) can fire
                    # ARRIVED from the wrong cell — replan to recover instead.
                    expected = agent._task_queue[0][0] if agent._task_queue else None
                    if expected is not None and agent.pos == expected:
                        agent.execute_pick(self.inventory)
                    elif expected is not None:
                        agent.replan()
                elif agent.state == AgentState.MOVING_TO_STATION:
                    if agent.pos != self.grid.pack_station_pos:
                        # Path exhausted before reaching the station — replan.
                        agent.replan()
                    elif not self.station_busy:
                        self.station_busy = True
                        self._do_deposit(agent)
                    else:
                        # At the station but it is busy this tick.  Count as
                        # waiting so wait_count accumulates and the escape step
                        # can push the agent aside if the queue grows large.
                        agent._wait_count += 1

    def step(self) -> bool:
        """Advance simulation by one tick. Returns True while work remains.
        In streaming mode (order_arrival_rate > 0) never self-terminates — caller
        is responsible for capping by tick count or completed order count."""
        self._process_restocks()
        self._process_arrivals()
        if (
            self.epoch_length > 0
            and self.current_tick > 0
            and self.current_tick - self._last_epoch_tick >= self.epoch_length
        ):
            self._on_epoch()
        self.station_busy = False
        self._dispatch_idle_agents()
        self._handle_agent_movement()

        all_idle = all(a.state == AgentState.IDLE for a in self.agents)
        has_pending = bool(self.restock_queue or self.task_queue)
        if all_idle:
            if not has_pending and self.order_arrival_rate <= 0:
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
