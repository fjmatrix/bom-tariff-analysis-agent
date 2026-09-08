"""Framework-independent, per-run workflow observations."""

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


@dataclass(frozen=True)
class WorkflowEvent:
    sequence: int
    timestamp: str
    name: str
    status: str
    action_id: int | None
    data: dict


class Events:
    def __init__(self, on_event: Callable[[WorkflowEvent], None] | None = None):
        self.on_event = on_event
        self.sequence = 0

    def emit(self, name, status="completed", *, action_id=None, **data):
        self.sequence += 1
        event = WorkflowEvent(
            self.sequence, datetime.now(timezone.utc).isoformat(),
            name, status, action_id, data,
        )
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                # Presentation failures must not interrupt analysis or artifact writes.
                logging.getLogger(__name__).exception("Workflow observer failed")
        return event

    @contextmanager
    def action(self, name, **data):
        action_id = self.sequence + 1
        self.emit(name, "started", action_id=action_id, **data)
        result = {}
        try:
            yield result
        except asyncio.CancelledError:
            self.emit(name, "cancelled", action_id=action_id, **data)
            raise
        except Exception as error:
            self.emit(name, "failed", action_id=action_id, **data, error=str(error))
            raise
        else:
            status = result.pop("status", "completed")
            self.emit(name, status, action_id=action_id, **{**data, **result})
