"""Background task utilities."""

import asyncio

from loguru import logger


def track_task(task: asyncio.Task, task_set: set[asyncio.Task], name: str = "") -> None:
    """Register *task* in *task_set* with an exception-logging done callback.

    Replaces the pattern of ``task_set.add(task)`` + ``task.add_done_callback(task_set.discard)``
    which silently swallows exceptions from background tasks.
    """
    task_set.add(task)

    def _on_done(t: asyncio.Task) -> None:
        task_set.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc:
                logger.error(
                    "Background task '{}' failed: {}: {}",
                    name or t.get_name(),
                    type(exc).__name__,
                    exc,
                )

    task.add_done_callback(_on_done)
