from __future__ import annotations
import heapq
from dataclasses import dataclass, field
from enum import Enum
from warehouse.grid import WarehouseGrid
from warehouse.inventory import Item, Inventory, Order


class AgentState(Enum):
    IDLE = "idle"
    MOVING_TO_RACK = "moving_to_rack"
    MOVING_TO_STATION = "moving_to_station"


class PickAgent:
    def __init__(self, agent_id: str, start_pos: tuple[int, int], grid: WarehouseGrid) -> None:
        self.agent_id = agent_id
        self.pos: tuple[int, int] = start_pos
        self.grid = grid
        self.carried_items: list[Item] = []
        self.total_distance: int = 0
        self.state = AgentState.IDLE

        self._path: list[tuple[int, int]] = []
        self._task_queue: list[tuple[tuple[int, int], str]] = []  # (stand_pos, item_id)
        self._pack_pos: tuple[int, int] = start_pos

    # ------------------------------------------------------------------
    # Pathfinding
    # ------------------------------------------------------------------

    def find_path(self, goal: tuple[int, int]) -> list[tuple[int, int]]:
        """A* returning list of positions from current pos to goal (exclusive of start)."""
        start = self.pos
        if start == goal:
            return []

        # heap: (f, g, pos, path)
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
                if neighbor not in visited and self.grid.is_walkable(nr, nc):
                    ng = g + 1
                    heapq.heappush(open_heap, (ng + self._h(neighbor, goal), ng, neighbor, new_path))

        return []  # no path found

    @staticmethod
    def _h(a: tuple[int, int], b: tuple[int, int]) -> int:
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    # ------------------------------------------------------------------
    # Movement
    # ------------------------------------------------------------------

    def step(self) -> bool:
        """Advance one cell along _path. Returns True if moved, False if arrived."""
        if not self._path:
            return False
        self.pos = self._path.pop(0)
        self.total_distance += 1
        return True

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def assign_order(self, order: Order, inventory: Inventory, pack_pos: tuple[int, int]) -> None:
        self._pack_pos = pack_pos

        # Build list of (stand_pos, item_id) for all items in the order
        targets: list[tuple[tuple[int, int], str]] = []
        for item_id in order.item_ids:
            slot = inventory.get_slot(item_id)
            targets.append((slot.stand_pos, item_id))

        # Greedy nearest-neighbor ordering
        ordered: list[tuple[tuple[int, int], str]] = []
        current = self.pos
        remaining = list(targets)
        while remaining:
            remaining.sort(key=lambda t: self._h(current, t[0]))
            chosen = remaining.pop(0)
            ordered.append(chosen)
            current = chosen[0]

        self._task_queue = ordered
        self._advance_to_next_task()

    def _advance_to_next_task(self) -> None:
        if self._task_queue:
            next_stand, _ = self._task_queue[0]
            self._path = self.find_path(next_stand)
            self.state = AgentState.MOVING_TO_RACK
        else:
            self._path = self.find_path(self._pack_pos)
            self.state = AgentState.MOVING_TO_STATION

    def execute_pick(self, inventory: Inventory) -> Item | None:
        """Called when agent arrives at a rack stand_pos. Picks item, advances queue."""
        if not self._task_queue:
            return None
        _, item_id = self._task_queue.pop(0)
        item = inventory.remove_item(item_id)
        self.carried_items.append(item)
        self._advance_to_next_task()
        return item

    def execute_deposit(self) -> list[Item]:
        """Called when agent arrives at pack station. Clears carried items."""
        deposited = list(self.carried_items)
        self.carried_items = []
        self.state = AgentState.IDLE
        return deposited
