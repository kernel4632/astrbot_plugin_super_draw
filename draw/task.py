"""简单的并发任务管理。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DrawRequest:
    user_id: str
    origin: str
    message_id: str
    prompt: str
    images: list[bytes] = field(default_factory=list)
    from_tool: bool = False
    source: Any = None
    urls: list[str] = field(default_factory=list)
    message_text: str = ""


@dataclass(slots=True)
class DrawJob:
    id: str
    request: DrawRequest
    reserved_points: int
    model: Any
    task: asyncio.Task[None] | None = None


class Task:
    def __init__(self, limit: int = 3) -> None:
        self.limit = max(1, limit)
        self.tasks: set[asyncio.Task[Any]] = set()

    def start(self, work: Awaitable[Any]) -> asyncio.Task[Any] | None:
        if self.active() >= self.limit:
            return None

        task = asyncio.create_task(work)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def cancel(self, task: asyncio.Task[Any]) -> bool:
        if task in self.tasks and not task.done():
            task.cancel()
            return True
        return False

    def active(self) -> int:
        return sum(not task.done() for task in self.tasks)

    def clean(self) -> None:
        self.tasks = {task for task in self.tasks if not task.done()}


task = Task()

__all__ = ["task"]
