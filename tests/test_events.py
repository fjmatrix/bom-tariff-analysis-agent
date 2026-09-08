"""Observers cannot change workflow success, and actions always terminate."""

import asyncio

import pytest

from src.events import Events


@pytest.mark.parametrize("error,status", [
    (RuntimeError("failed request"), "failed"),
    (asyncio.CancelledError(), "cancelled"),
])
def test_action_pairs_failures_and_cancellation(error, status):
    observed = []
    with pytest.raises(type(error)):
        with Events(observed.append).action("classification", reference="PART"):
            raise error
    assert [event.status for event in observed] == ["started", status]
    assert observed[0].action_id == observed[1].action_id
    assert observed[1].data["reference"] == "PART"


def test_observer_exception_does_not_interrupt_core(caplog):
    def broken(event):
        raise RuntimeError("renderer unavailable")

    with Events(broken).action("run") as outcome:
        outcome["value"] = 42
    assert "Workflow observer failed" in caplog.text
