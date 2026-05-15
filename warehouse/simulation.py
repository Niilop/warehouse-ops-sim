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
    lines_per_hour: float       # total_items / (total_ticks / 3600); 1 tick = 1 second
    lines_per_order: float      # total_items / total_orders
    idle_ticks: int
    wait_ticks_total: int
    stockout_count: int
    avg_agent_utilization: float          # average active fraction across all agents
    stockout_ticks_by_item: dict[str, int]  # item_id → ticks at zero aggregate stock
    n_agents_recommended: int             # M/M/c estimate for 80% target utilization
    optimal_truck_interval_ticks: int     # minimum safe truck interval from observed demand; 0 = n/a
    truck_interval_diagnosis: str         # "ok", "too_long", "too_short", or "n/a"


class Simulation:
    def __init__(
        self,
        grid: WarehouseGrid,
        inventory: Inventory,
        agents: list[PickAgent],
        restock_delay: int = 10,
        repl_batch_size: int = 4,
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
        self.repl_batch_size = repl_batch_size
        self.restock_queue: list[RestockJob] = []
        self.stockout_count: int = 0

        # Metrics tracking
        self.idle_ticks: int = 0
        self._order_enqueue_tick: dict[str, int] = {}
        self._order_wait_ticks: dict[str, int] = {}
        self._agent_ticks_active: dict[str, int] = {a.agent_id: 0 for a in agents}
        self._stockout_ticks: dict[str, int] = {}  # item_id → ticks at zero aggregate stock

        # Epoch reslotting and (s, S) demand learning
        self.epoch_length: int = epoch_length
        self._last_epoch_tick: int = 0
        self._epoch_pick_history: dict[str, list[int]] = {}  # item_id → picks per epoch

        # Dock replenishment tracking — item_ids with an in-flight replenishment task
        self._pending_replenishment: set[str] = set()

        # Wave replenishment: POs accumulate between truck arrivals
        # 0 = immediate dispatch (backward compat); > 0 = batch by truck schedule
        self.truck_interval_ticks: int = 0
        self._pending_po: list[tuple[Item, int]] = []  # (item, qty) awaiting next truck

        # Waiting queue: ORDER_PICK tasks deferred due to genuine stockout (stock == 0)
        self._waiting_tasks: list[Task] = []

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

    def _dispatch_agent(
        self,
        agent: PickAgent,
        occupied: set[tuple[int, int]],
        allow_replenishment: bool = True,
    ) -> bool:
        """Pop the highest-priority available task and assign it to the idle agent.
        Tasks whose stock is insufficient are deferred back onto the heap.
        allow_replenishment=False defers replenishment tasks back without assigning,
        so the agent looks for a pick task instead (used by the dock-throttle).
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

            if task.priority in (TaskType.REPLENISHMENT_URGENT, TaskType.REPLENISHMENT_SCHEDULED):
                if not allow_replenishment:
                    deferred.append(task)
                    continue
                if self.grid.dock_pos is None:
                    continue  # dock removed mid-run; discard stale task
                agent.assign_replenishment(
                    stops=task.payload["stops"],
                    dock_pos=self.grid.dock_pos,
                    task_id=task.task_id,
                )
                dispatched = True
                continue

            batch: BatchedOrder = task.payload["batch"]
            needed = Counter(batch.unified_item_ids)
            if not all(available(iid, cnt) for iid, cnt in needed.items()):
                # Genuine stockout (aggregate stock == 0): park in waiting queue.
                # Contention only (another agent claimed the units): defer on heap.
                if any(self.inventory.stock_level(iid) == 0 for iid in needed):
                    self._waiting_tasks.append(task)
                    self.stockout_count += 1
                else:
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
        if self.grid.dock_pos is None:
            # No dock: teleport items back after restock_delay (legacy batch mode).
            for item in deposited:
                self.restock_queue.append(RestockJob(
                    item_id=item.item_id,
                    item=item,
                    ready_at_tick=self.current_tick + self.restock_delay,
                ))

    # ------------------------------------------------------------------
    # Main simulation loop
    # ------------------------------------------------------------------

    def _update_reorder_params(self, pick_counts: dict[str, int]) -> None:
        """Adapt per-item reorder_point and fill_to from observed demand statistics.

        Only active in dock mode — reorder_point drives dock replenishment triggers;
        in teleport mode it is unused and modifying fill_to would cap stock incorrectly.

        Accumulates per-epoch pick counts, then applies the (s, S) policy once
        at least 3 epochs have been recorded:
          s = μ × L_epochs + z × σ × sqrt(L_epochs)   [reorder point]
          S = s + μ                                     [order-up-to: s + one epoch demand]
        where μ/σ are the epoch-level mean/std of picks and z = 1.65 (95% service level).
        """
        if self.grid.dock_pos is None:
            return
        z = 1.65
        min_epochs = 3

        for iid, slots in self.inventory._original_slots_for.items():
            history = self._epoch_pick_history.setdefault(iid, [])
            history.append(pick_counts.get(iid, 0))
            if len(history) < min_epochs:
                continue

            n = len(history)
            mu = sum(history) / n
            var = sum((x - mu) ** 2 for x in history) / max(1, n - 1)
            sigma = var ** 0.5

            L_epochs = slots[0].lead_time / self.epoch_length
            s = mu * L_epochs + z * sigma * (L_epochs ** 0.5)
            S_target = s + mu

            nf = len(slots)
            for slot in slots:
                # Clamp reorder_point below max_stock so fill_to stays ≤ max_stock.
                slot.reorder_point = max(1, min(slot.max_stock - 1, round(s / nf)))
                slot.fill_to = max(
                    slot.reorder_point + 1,
                    min(slot.max_stock, round(S_target / nf)),
                )
                slot.order_qty = max(1, slot.fill_to - slot.reorder_point)

    def _on_epoch(self) -> None:
        """SA reslot + (s,S) policy update. Only runs when enough data has accumulated."""
        from warehouse.optimizer import slot_distances, reorg_cost, sa_slotting

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

        self._update_reorder_params(pick_counts)

        pack_r, pack_c = self.grid.pack_station_pos
        current_distances: dict[str, int] = {
            iid: min(
                abs(s.stand_pos[0] - pack_r) + abs(s.stand_pos[1] - pack_c)
                for s in slots
            )
            for iid, slots in self.inventory._original_slots_for.items()
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
            for s in self.inventory._original_slots_for.get(iid, []):
                protected_rack_pos.add(s.rack_pos)
        for job in self.restock_queue:
            for s in self.inventory._original_slots_for.get(job.item_id, []):
                protected_rack_pos.add(s.rack_pos)

        # Build slot-index and distance maps.
        slot_obj_to_idx: dict[int, int] = {id(s): i for i, s in enumerate(self.inventory._slots)}
        slot_dist_map: dict[int, int] = {
            idx: dist for idx, dist in slot_distances(self.grid, self.inventory)
        }

        # Build current assignment for non-protected items only.
        current_assignment: dict[str, list[int]] = {}
        for iid, slots in self.inventory._original_slots_for.items():
            if iid in in_flight:
                continue
            if any(s.rack_pos in protected_rack_pos for s in slots):
                continue
            current_assignment[iid] = [slot_obj_to_idx[id(s)] for s in slots]

        # SA optimizer: minimize Σ picks[i] × min_dist[slots[i]].
        new_assignment = sa_slotting(
            current_assignment,
            pick_counts,
            slot_dist_map,
            n_iter=max(500, len(current_assignment) * 20),
            seed=self.current_tick,
        )

        proposed_distances = dict(current_distances)
        item_to_new_slot: dict[str, int] = {}
        for iid, new_idxs in new_assignment.items():
            if new_idxs == current_assignment.get(iid):
                continue
            new_primary_idx = new_idxs[0]
            item_to_new_slot[iid] = new_primary_idx
            new_slot = self.inventory._slots[new_primary_idx]
            proposed_distances[iid] = (
                abs(new_slot.stand_pos[0] - pack_r) + abs(new_slot.stand_pos[1] - pack_c)
            )

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
            id(slot): iid
            for iid, slots in self.inventory._original_slots_for.items()
            for slot in slots
            if slot.stock > 0
        }

        reslot_delay = max(self.restock_delay, 5)
        for iid, new_slot_idx in item_to_new_slot.items():
            if iid in in_flight:
                continue
            new_slot = self.inventory._slots[new_slot_idx]
            if any(s is new_slot for s in self.inventory._original_slots_for.get(iid, [])):
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

    def _unblock_waiting_tasks(self) -> None:
        """Re-queue waiting tasks whose items all have aggregate stock > 0."""
        still_waiting: list[Task] = []
        for task in self._waiting_tasks:
            batch: BatchedOrder = task.payload["batch"]
            if all(
                self.inventory.stock_level(iid) > 0
                for iid in set(batch.unified_item_ids)
            ):
                heapq.heappush(self.task_queue, task)
            else:
                still_waiting.append(task)
        self._waiting_tasks = still_waiting

    def _push_repl_batches(
        self,
        stops: list[tuple[tuple[int, int], "Item", int, bool]],
    ) -> None:
        """Slice a list of (stand_pos, item, qty, urgent) stops into repl_batch_size
        groups and push one Task per group onto the task heap."""
        for i in range(0, len(stops), self.repl_batch_size):
            chunk = stops[i : i + self.repl_batch_size]
            has_urgent = any(s[3] for s in chunk)
            heapq.heappush(self.task_queue, Task(
                priority=TaskType.REPLENISHMENT_URGENT if has_urgent else TaskType.REPLENISHMENT_SCHEDULED,
                created_at=self.current_tick,
                task_id=f"repl-{self.current_tick}-{i}",
                payload={"stops": [(s[0], s[1], s[2]) for s in chunk]},
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
        if due:
            self._unblock_waiting_tasks()

        # With a dock, check whether any slot has dropped below its reorder point.
        if self.grid.dock_pos is not None:
            # Promote any _pending_po items that have stocked out since queuing.
            # Items in _pending_po are also in _pending_replenishment, so
            # check_reorder_triggers would skip them — this is the only path to
            # urgent dispatch before the next scheduled truck arrives.
            if self._pending_po:
                still_pending: list[tuple[Item, int]] = []
                urgent_po: list[tuple[tuple[int, int], Item, int, bool]] = []
                for item, qty in self._pending_po:
                    if self.inventory.stock_level(item.item_id) == 0:
                        homes = self.inventory._original_slots_for[item.item_id]
                        target = min(homes, key=lambda s: s.stock / max(1, s.fill_to))
                        urgent_po.append((target.stand_pos, item, qty, True))
                    else:
                        still_pending.append((item, qty))
                self._pending_po = still_pending
                if urgent_po:
                    self._push_repl_batches(urgent_po)

            # Drain accumulated PO on truck arrival — batch into multi-stop tasks.
            if (
                self.truck_interval_ticks > 0
                and self.current_tick > 0
                and self.current_tick % self.truck_interval_ticks == 0
                and self._pending_po
            ):
                stops_raw: list[tuple[tuple[int, int], Item, int, bool]] = []
                for item, qty in self._pending_po:
                    homes = self.inventory._original_slots_for[item.item_id]
                    target = min(homes, key=lambda s: s.stock / max(1, s.fill_to))
                    stocked_out = self.inventory.stock_level(item.item_id) == 0
                    stops_raw.append((target.stand_pos, item, qty, stocked_out))
                stops_raw.sort(key=lambda s: (0 if s[3] else 1))  # urgent stops first
                self._pending_po.clear()
                self._push_repl_batches(stops_raw)

            # Collect all reorder triggers for this tick, mark pending immediately
            # so the same item cannot appear in two separate batches this tick.
            immediate: list[tuple[tuple[int, int], Item, int, bool]] = []
            for item_id, item, order_qty, urgent in self.inventory.check_reorder_triggers(
                self._pending_replenishment
            ):
                self._pending_replenishment.add(item_id)
                if urgent or self.truck_interval_ticks == 0:
                    homes = self.inventory._original_slots_for[item_id]
                    target = min(homes, key=lambda s: s.stock / max(1, s.fill_to))
                    immediate.append((target.stand_pos, item, order_qty, urgent))
                else:
                    self._pending_po.append((item, order_qty))

            if immediate:
                immediate.sort(key=lambda s: (0 if s[3] else 1))  # urgent first
                self._push_repl_batches(immediate)

    def _dispatch_idle_agents(self) -> None:
        # Re-evaluate waiting tasks before dispatch: contention may have cleared since
        # the last restock event, or multiple restocks completed in the same tick.
        self._unblock_waiting_tasks()

        # Serial dispatch — queue item is popped before the next agent is considered,
        # so two agents cannot claim the same batch/order in the same tick.
        occupied = {a.pos for a in self.agents}

        # Throttle: cap concurrent dock trips to half the fleet (rounded up) so
        # pick agents remain available when ORDER_PICK tasks exist.  When no pick
        # tasks are in the queue at all (everything is in _waiting_tasks) the cap
        # is lifted and every idle agent may do replenishment.
        max_repl = max(1, (len(self.agents) + 1) // 2)
        has_picks = any(t.priority == TaskType.ORDER_PICK for t in self.task_queue)

        for agent in self.agents:
            if agent.state != AgentState.IDLE:
                continue
            in_transit = sum(
                1 for a in self.agents
                if a.state in (AgentState.MOVING_TO_DOCK, AgentState.MOVING_FROM_DOCK)
            )
            # Allow replenishment when under the cap OR when no picks are available anyway.
            allow_repl = (not has_picks) or (in_transit < max_repl)
            self._dispatch_agent(agent, occupied, allow_replenishment=allow_repl)

    def _escape_step(self, agent: "PickAgent", all_positions: set[tuple[int, int]]) -> bool:
        """Move agent one step to any free adjacent cell (not the currently-blocked one).
        Returns True if an escape cell was found and the agent was moved."""
        blocked = agent._path[0] if agent._path else None
        dirs = [(-1, 0), (1, 0), (0, -1), (0, 1)]
        self._arrival_rng.shuffle(dirs)
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
                    # Always try a physical escape step first: moving sideways breaks
                    # head-on livelocks where two agents find mirror-image detours that
                    # also collide.  Fall back to path-replanning if no free cell exists.
                    all_pos = {a.pos for a in self.agents if a is not agent}
                    if not self._escape_step(agent, all_pos):
                        agent.replan(occupied - {agent.pos}, force=(agent._wait_count >= 20))
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
                        agent.replan()
                    elif not self.station_busy:
                        self.station_busy = True
                        self._do_deposit(agent)
                    else:
                        agent._wait_count += 1
                elif agent.state == AgentState.MOVING_TO_DOCK:
                    if agent.pos == self.grid.dock_pos:
                        agent.execute_dock_pickup()
                    else:
                        agent.replan()
                elif agent.state == AgentState.MOVING_FROM_DOCK:
                    if agent.pos == agent._repl_slot_pos:
                        item = agent.execute_restock(self.inventory)
                        self._pending_replenishment.discard(item.item_id)
                        self._unblock_waiting_tasks()
                    else:
                        agent.replan()

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
        # _waiting_tasks is included: if orders are blocked on stockout but a replenishment
        # task is still outstanding (in-flight agent), has_pending stays True via task_queue
        # or the agents themselves being non-IDLE.  Including it here prevents early exit
        # when all agents finish replenishment in the same tick that _unblock fires.
        has_pending = bool(self.restock_queue or self.task_queue or self._pending_po or self._waiting_tasks)
        if all_idle:
            if not has_pending and self.order_arrival_rate <= 0:
                return False
            self.idle_ticks += 1

        # Per-tick metrics
        for a in self.agents:
            if a.state != AgentState.IDLE:
                self._agent_ticks_active[a.agent_id] = self._agent_ticks_active.get(a.agent_id, 0) + 1
        for item_id, slots in self.inventory._original_slots_for.items():
            if sum(s.stock for s in slots) == 0:
                self._stockout_ticks[item_id] = self._stockout_ticks.get(item_id, 0) + 1

        self.current_tick += 1
        return True

    def get_summary(self) -> SimMetrics:
        metrics = self.completed_metrics
        total_items = sum(m.items_picked for m in metrics)
        total_dist = sum(m.distance_traveled for m in metrics)
        total_wait = sum(m.wait_ticks for m in metrics)
        n_orders = len(metrics)
        lph = (total_items / self.current_tick * 3600) if self.current_tick > 0 else 0.0
        lpo = (total_items / n_orders) if n_orders > 0 else 0.0
        if self.agents and self.current_tick > 0:
            util_values = [
                self._agent_ticks_active.get(a.agent_id, 0) / self.current_tick
                for a in self.agents
            ]
            avg_util = sum(util_values) / len(util_values)
        else:
            avg_util = 0.0

        # M/M/c workforce sizing: find minimum agents needed to keep utilization ≤ 80%.
        # ρ ≈ avg_util per agent → c* = ceil(c × avg_util / target_util), min 1.
        _TARGET_UTIL = 0.80
        n_agents = len(self.agents)
        if avg_util > 0 and n_agents > 0:
            n_recommended = max(1, math.ceil(n_agents * avg_util / _TARGET_UTIL))
        else:
            n_recommended = max(1, n_agents)

        # Phase 8d: Optimal truck interval from observed item demand rates.
        # Min drain time across items (buffer / rate) = safe interval upper bound.
        if (
            self.truck_interval_ticks > 0
            and self.current_tick > 0
            and self.grid.dock_pos is not None
        ):
            # Build per-item pick rate (picks/tick) — epoch history if available, else proxy.
            item_rates: dict[str, float] = {}
            if self._epoch_pick_history:
                n_ep = max(len(v) for v in self._epoch_pick_history.values())
                ep_ticks = n_ep * self.epoch_length
                if ep_ticks > 0:
                    for iid, hist in self._epoch_pick_history.items():
                        item_rates[iid] = sum(hist) / ep_ticks
            if not item_rates and self.current_tick > 0:
                all_items: dict[str, Item] = {}
                for iid, slots in self.inventory._original_slots_for.items():
                    for s in slots:
                        if s.item is not None:
                            all_items[iid] = s.item
                            break
                total_pr = sum(it.pick_rate for it in all_items.values()) or 1.0
                ppt = total_items / self.current_tick
                for iid, it in all_items.items():
                    item_rates[iid] = it.pick_rate / total_pr * ppt

            min_drain = float("inf")
            for iid, slots in self.inventory._original_slots_for.items():
                rate = item_rates.get(iid, 0.0)
                if rate <= 0:
                    continue
                agg_fill_to = sum(s.fill_to for s in slots)
                agg_reorder = sum(s.reorder_point for s in slots)
                buffer = max(1, agg_fill_to - agg_reorder)
                drain = buffer / rate
                if drain < min_drain:
                    min_drain = drain

            optimal_interval = (
                max(1, int(min_drain)) if min_drain != float("inf") else self.truck_interval_ticks
            )
            any_stockouts = bool(self._stockout_ticks)
            if any_stockouts or self.truck_interval_ticks > optimal_interval:
                truck_diag = "too_long"
            elif self.truck_interval_ticks < optimal_interval // 2:
                truck_diag = "too_short"
            else:
                truck_diag = "ok"
        else:
            optimal_interval = 0
            truck_diag = "n/a"

        return SimMetrics(
            total_orders=n_orders,
            total_items=total_items,
            total_distance=total_dist,
            total_ticks=self.current_tick,
            lines_per_hour=round(lph, 2),
            lines_per_order=round(lpo, 2),
            idle_ticks=self.idle_ticks,
            wait_ticks_total=total_wait,
            stockout_count=self.stockout_count,
            avg_agent_utilization=round(avg_util, 4),
            stockout_ticks_by_item=dict(self._stockout_ticks),
            n_agents_recommended=n_recommended,
            optimal_truck_interval_ticks=optimal_interval,
            truck_interval_diagnosis=truck_diag,
        )

    def run(self, max_ticks: int = 10_000) -> list[OrderMetrics]:
        """Run to completion (headless). Returns completed metrics."""
        while self.current_tick < max_ticks:
            if not self.step():
                break
        return self.completed_metrics
