from __future__ import annotations
import itertools
from enum import IntEnum
from dataclasses import dataclass, field


class TaskType(IntEnum):
    REPLENISHMENT_URGENT    = 1
    ORDER_PICK              = 2
    REPLENISHMENT_SCHEDULED = 3
    RESLOT                  = 4
    CYCLE_COUNT             = 5


_seq_counter = itertools.count()


@dataclass(order=True)
class Task:
    priority: TaskType
    created_at: int
    task_id: str    = field(compare=False, default="")
    payload: dict   = field(compare=False, default_factory=dict)
    seq: int        = field(default_factory=lambda: next(_seq_counter), compare=True)
