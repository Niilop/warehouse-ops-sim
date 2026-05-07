from __future__ import annotations
from enum import IntEnum
from dataclasses import dataclass, field


class TaskType(IntEnum):
    REPLENISHMENT_URGENT    = 1
    ORDER_PICK              = 2
    REPLENISHMENT_SCHEDULED = 3
    RESLOT                  = 4
    CYCLE_COUNT             = 5


@dataclass(order=True)
class Task:
    priority: TaskType
    created_at: int
    task_id: str   = field(compare=False)
    payload: dict  = field(compare=False)
