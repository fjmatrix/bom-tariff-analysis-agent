"""Trace provider-reported tokens without counting cached/reasoning subsets twice."""

import asyncio
import json
from pathlib import Path

from src.console import console_event
from src.events import Events


TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
    "total_tokens",
)


class TokenUsage:
    def __init__(self, path=None, events=None):
        self.path = Path(path) if path is not None else None
        self.events = []
        self.status = "running"
        self.observer = events if events is not None else Events(console_event)

    async def request(self, method, stage, label, **kwargs):
        try:
            response = await method(**kwargs)
        except asyncio.CancelledError:
            self.record(stage, label, kwargs["model"], status="cancelled")
            raise
        except Exception as error:
            self.record(stage, label, kwargs["model"], status=type(error).__name__)
            raise
        self.record(
            stage, label, getattr(response, "model", kwargs["model"]),
            response=response, status=response.status,
        )
        return response

    def record(self, stage, label, model, response=None, status="cache_hit"):
        usage = getattr(response, "usage", None)
        counts = {field: None for field in TOKEN_FIELDS}
        if status == "cache_hit":
            counts = dict.fromkeys(TOKEN_FIELDS, 0)
        elif usage is not None:
            counts.update(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_input_tokens=getattr(
                    getattr(usage, "input_tokens_details", None), "cached_tokens", None,
                ),
                reasoning_tokens=getattr(
                    getattr(usage, "output_tokens_details", None), "reasoning_tokens", None,
                ),
            )
        self.events.append({
            "sequence": len(self.events) + 1, "stage": stage, "label": label,
            "model": model, "response_id": getattr(response, "id", None),
            "status": status, **counts,
        })
        self.save()
        self.observer.emit("usage", status, stage=stage, label=label, counts=counts,
                           totals=self.report()["totals"])

    def totals(self, events):
        return {
            **{field: sum(event[field] or 0 for event in events) for field in TOKEN_FIELDS},
            "api_calls": sum(event["status"] != "cache_hit" for event in events),
            "cache_hits": sum(event["status"] == "cache_hit" for event in events),
            "calls_without_usage": sum(event["total_tokens"] is None for event in events),
        }

    def report(self):
        return {
            "status": self.status,
            "note": "Totals sum reported usage only. Cached input and reasoning are "
                    "subsets of input and output. Missing usage is unknown, not zero. "
                    "SDK-internal retries without returned usage cannot be counted.",
            "totals": self.totals(self.events),
            "stages": {
                stage: self.totals([event for event in self.events if event["stage"] == stage])
                for stage in ("classification", "agent")
            },
            "events": self.events,
        }

    def save(self):
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.report(), indent=2) + "\n", encoding="utf-8")

    def finish(self, status):
        self.status = status
        self.save()
        self.observer.emit("usage_totals", report=self.report())
