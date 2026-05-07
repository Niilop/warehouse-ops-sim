from __future__ import annotations
import heapq
from dataclasses import dataclass, field
from enum import Enum
from warehouse.grid import WarehouseGrid
from warehouse.inventory import Item, Inventory, Order
from warehouse.batcher import BatchedOrder


class AgentState(Enum):
    IDLE = "idle"
    MOVING_TO_RACK = "moving_to_rack"
    MOVING_TO_STATION = "moving_to_station"
    MOVING_TO_DOCK = "moving_to_dock"
    MOVING_FROM_DOCK = "moving_from_dock"


class StepResult(Enum):
    MOVED = "moved"
    WAITING = "waiting"   # next cell occupied; stays in place
    ARRIVED = "arrived"   # path exhausted


class PickAgent:
    def __init__(self, agent_id: str, start_pos: tuple[int, int], grid: WarehouseGrid) -> None:
        self.agent_id = agent_id
        self.pos: tuple[int, int] = start_pos
        self.grid = grid
        self.carried_items: list[Item] = []
        self.total_distance: int = 0
        self.state = AgentState.IDLE

        self._path: list[tuple[int, int]] = []
        self._task_queue: list[tuple[tuple[int, int], str, str]] = []  # (stand_pos, item_id, order_id)
        self._carried_order_ids: list[str] = []
        self._pack_pos: tuple[int, int] = start_pos
        self._batch: BatchedOrder | None = None
        self._wait_count: int = 0
        self._replan_cooldown: int = 0

        # Replenishment state (dock → slot journey)
        self._repl_item: Item | None = None
        self._repl_qty: int = 0
        self._repl_slot_pos: tuple[int, int] | None = None
        self._repl_dock_pos: tuple[int, int] | None = None
        self._repl_task_id: str | None = None

    # ------------------------------------------------------------------
    # Pathfinding
    # ------------------------------------------------------------------

    def find_path(
        self,
        goal: tuple[int, int],
        occupied_cells: set[tuple[int, int]] | None = None,
    ) -> list[tuple[int, int]]:
        """A* returning list of positions from current pos to goal (exclusive of start).
        occupied_cells: positions to treat as blocked during planning."""
        start = self.pos
        if start == goal:
            return []

        open_heap: list[tuple[int, int, tuple[int, int], list[tuple[int, int]]]] = []
        heapq.heappush(open_heap, (self._h(start, goal), 0, start, []))
        visited: set[tuple[int, int]] = set()

        while open_heap:
            f, g, pos, path = heapq.heappop(open_heap)
            if pos in visited:
                continue
            visited.add(pos)
            new_path = path + [pos]

            if pos == goal:
                return new_path[1:]  # exclude start position

            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                nr, nc = pos[0] + dr, pos[1] + dc
                neighbor = (nr, nc)
                if neighbor in visited:
                    continue
                if not self.grid.is_walkable(nr, nc):
                    continue
                if occupied_cells and neighbor in occupied_cells:
                    continue
                ng = g + 1
                heapq.heappush(open_heap, (ng + self._h(neighbor, goal), ng, neighbor, new_path))

        return []  # no path found

    @staticmethod
    def _h(a: tuple[int, int], b: tuple[int, int]) -> int:
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    # ------------------------------------------------------------------
    # Movement
    # ------------------------------------------------------------------

    def step(self, occupied_cells: set[tuple[int, int]] | None = None) -> StepResult:
        """Advance one cell along _path. Returns StepResult."""
        if self._replan_cooldown > 0:
            self._replan_cooldown -= 1
        if not self._path:
            self._wait_count = 0
            return StepResult.ARRIVED
        next_cell = self._path[0]
        if occupied_cells and next_cell in occupied_cells:
            self._wait_count += 1
            return StepResult.WAITING
        self._wait_count = 0
        self.pos = self._path.pop(0)
        self.total_distance += 1
        return StepResult.MOVED

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def assign_batch(
        self,
        batch: BatchedOrder,
        inventory: Inventory,
        pack_pos: tuple[int, int],
        occupied_cells: set[tuple[int, int]] | None = None,
    ) -> None:
        self._pack_pos = pack_pos
        self._batch = batch

        targets: list[tuple[tuple[int, int], str, str]] = []
        for item_id, order_id in zip(batch.unified_item_ids, batch.item_to_order):
            slot = inventory.get_slot(item_id, agent_pos=self.pos)
            targets.append((slot.stand_pos, item_id, order_id))

        # Greedy nearest-neighbor ordering
        ordered: list[tuple[tuple[int, int], str, str]] = []
        current = self.pos
        remaining = list(targets)
        while remaining:
            remaining.sort(key=lambda t: self._h(current, t[0]))
            chosen = remaining.pop(0)
            ordered.append(chosen)
            current = chosen[0]

        self._task_queue = ordered
        # Path planning ignores other agents — step-level blocking handles collisions.
        # Passing occupied_cells to A* can produce empty paths when all routes are
        # temporarily blocked, causing agents to falsely "arrive" at wrong positions.
        self._advance_to_next_task()

    def assign_order(self, order: Order, inventory: Inventory, pack_pos: tuple[int, int]) -> None:
        self.assign_batch(
            BatchedOrder(
                batch_id=order.order_id,
                orders=[order],
                unified_item_ids=list(order.item_ids),
                item_to_order=[order.order_id for _ in order.item_ids],
            ),
            inventory,
            pack_pos,
        )

    def _advance_to_next_task(
        self, occupied_cells: set[tuple[int, int]] | None = None
    ) -> None:
        if self._task_queue:
            next_stand, _, _ = self._task_queue[0]
            self._path = self.find_path(next_stand, occupied_cells)
            self.state = AgentState.MOVING_TO_RACK
        else:
            self._path = self.find_path(self._pack_pos, occupied_cells)
            self.state = AgentState.MOVING_TO_STATION

    def replan(self, occupied_cells: set[tuple[int, int]] | None = None, *, force: bool = False) -> None:
        """Replan path to current target.

        Tries to route around occupied_cells. Falls back to an unblocked path
        only when force=True (used for the high-wait-count escape hatch) or
        when the agent has no path at all. Staying put is better than choosing
        the same conflicting path again in a livelock situation.
        """
        if self._task_queue:
            goal, _, _ = self._task_queue[0]
        elif self.state == AgentState.MOVING_TO_STATION:
            goal = self._pack_pos
        elif self.state == AgentState.MOVING_TO_DOCK and self._repl_dock_pos:
            goal = self._repl_dock_pos
        elif self.state == AgentState.MOVING_FROM_DOCK and self._repl_slot_pos:
            goal = self._repl_slot_pos
        else:
            return
        if self.pos == goal:
            self._path = []
            return
        new_path = self.find_path(goal, occupied_cells)
        if not new_path and (force or not self._path):
            new_path = self.find_path(goal)
        if new_path:
            self._path = new_path

    def execute_pick(self, inventory: Inventory) -> Item | None:
        """Called when agent arrives at a rack stand_pos. Picks item, advances queue."""
        if not self._task_queue:
            return None
        _, item_id, order_id = self._task_queue.pop(0)
        item = inventory.remove_item(item_id, agent_pos=self.pos)
        self.carried_items.append(item)
        self._carried_order_ids.append(order_id)
        self._advance_to_next_task()
        return item

    def execute_deposit(self) -> tuple[list[Item], dict[str, list[str]]]:
        """Called when agent arrives at pack station. Returns (items, order_id -> [item_ids])."""
        deposited = list(self.carried_items)
        carried_oids = list(self._carried_order_ids)
        self.carried_items = []
        self._carried_order_ids = []
        self.state = AgentState.IDLE
        order_breakdown: dict[str, list[str]] = {}
        for item, oid in zip(deposited, carried_oids):
            order_breakdown.setdefault(oid, []).append(item.item_id)
        self._batch = None
        return deposited, order_breakdown

    # ------------------------------------------------------------------
    # Replenishment (dock → slot)
    # ------------------------------------------------------------------

    def assign_replenishment(
        self,
        item: Item,
        qty: int,
        slot_stand_pos: tuple[int, int],
        dock_pos: tuple[int, int],
        task_id: str,
    ) -> None:
        self._repl_item     = item
        self._repl_qty      = qty
        self._repl_slot_pos = slot_stand_pos
        self._repl_dock_pos = dock_pos
        self._repl_task_id  = task_id
        self._path = self.find_path(dock_pos)
        self.state = AgentState.MOVING_TO_DOCK

    def execute_dock_pickup(self) -> None:
        """Agent has arrived at the dock. Load stock instantly and head to the slot."""
        self.state = AgentState.MOVING_FROM_DOCK
        self._path = self.find_path(self._repl_slot_pos)

    def execute_restock(self, inventory: Inventory) -> Item:
        """Agent has arrived at the slot stand-pos. Deposit replenishment stock."""
        item = self._repl_item
        inventory.restock_bulk(item, self._repl_qty, target_stand_pos=self._repl_slot_pos)
        self._repl_item     = None
        self._repl_qty      = 0
        self._repl_slot_pos = None
        self._repl_dock_pos = None
        self._repl_task_id  = None
        self.state = AgentState.IDLE
        return item  # type: ignore[return-value]
