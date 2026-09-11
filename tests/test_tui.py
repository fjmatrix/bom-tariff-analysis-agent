"""Headless interaction tests with actual workflow and deterministic fake services."""

import asyncio
import json
from functools import partial

import pytest

pytest.importorskip("textual")

from textual.widgets import DataTable, Input, LoadingIndicator, Markdown, ProgressBar, Select, Static, TabbedContent

from src.config import ROOT
from src.events import Events
from src.run import run
from src.tui.activity import ActivityView
from src.tui.app import BomApp
from src.tui.decisions import DecisionView
from src.tui.parts import PartsView
from test_run import DemoClient, fake_discovery, fake_bls


@pytest.mark.parametrize("size", [(140, 44), (90, 40), (80, 24)])
def test_live_run_navigation_and_business_results(tmp_path, monkeypatch, size):
    monkeypatch.setattr("src.classify.cache.ROOT", tmp_path)

    async def exercise():
        app = BomApp(ROOT / "examples/two_parts.csv", 1, tmp_path,
                     runner=partial(run, client=DemoClient(), country_discovery=fake_discovery, bls_lookup=fake_bls))
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            parts = app.query_one(PartsView)
            assert app.query_one("#parts", DataTable).row_count == 2
            assert app.query_one("#parts", DataTable).size.height >= 4
            assert parts.statuses == {"DEMO-NUT": "✓ Calculated", "DEMO-WASHER": "✓ Calculated"}
            assert "$0.58" in str(app.query_one("#summary", Static).render())
            assert not app.query_one(LoadingIndicator).display
            assert app.query_one(ActivityView).status == "Completed"
            if size[0] >= 105:
                assert app.query_one(ActivityView).region.width == app.query_one(DecisionView).region.width
            assert app.query_one("#opportunities", DataTable).row_count == 1
            app.query_one(Select).value = "duty"
            await pilot.pause()
            assert app.query_one("#parts", DataTable).get_row_at(0)[0].plain == "DEMO-WASHER"
            app.query_one(Input).value = "nut"
            await pilot.pause()
            assert app.query_one("#parts", DataTable).row_count == 1
            assert parts.selected == "DEMO-NUT"
            app.query_one(TabbedContent).active = "brief-tab"
            await pilot.pause()
            assert "Washer current duty" in app.query_one(Markdown).source
            await pilot.press("q")

    asyncio.run(exercise())


def test_cancel_preserves_screen_and_marks_usage(tmp_path, monkeypatch):
    monkeypatch.setattr("src.classify.cache.ROOT", tmp_path)

    async def exercise():
        entered = asyncio.Event()
        client = DemoClient()

        async def waiting(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        client.parse = waiting
        app = BomApp(ROOT / "examples/two_parts.csv", 1, tmp_path,
                     runner=partial(run, client=client, country_discovery=fake_discovery, bls_lookup=fake_bls))
        async with app.run_test() as pilot:
            await asyncio.wait_for(entered.wait(), timeout=5)
            await pilot.pause()
            assert app.query_one(LoadingIndicator).display
            assert "Classifying part 1 / 2" in str(app.query_one("#current", Static).render())
            assert "0 / 2 parts processed" in str(app.query_one("#progress-label", Static).render())
            assert app.query_one(ProgressBar).total == 2
            await app.action_cancel()
            await pilot.pause()
            assert app.query_one(ActivityView).status == "Cancelled"
            assert not app.query_one(LoadingIndicator).display
            assert "– Cancelled" in app.query_one(PartsView).statuses.values()
            assert json.loads((tmp_path / "token_usage.json").read_text())["status"] == "cancelled"

    asyncio.run(exercise())


def test_failed_classification_does_not_advance_progress(tmp_path):
    async def exercise():
        async def idle(*args, **kwargs):
            pass

        app = BomApp("test.csv", 1, tmp_path, runner=idle)
        async with app.run_test(size=(140, 44)) as pilot:
            activity = app.query_one(ActivityView)
            events = Events(activity.observe)
            with pytest.raises(RuntimeError), events.action(
                "classification", reference="B", position=2, total=20,
            ):
                await pilot.pause()
                assert "Classifying part 2 / 20" in str(app.query_one("#current", Static).render())
                assert app.query_one(LoadingIndicator).display
                raise RuntimeError("Classification failed")
            assert app.query_one(ProgressBar).progress == 1
            assert "1 / 20 parts processed" in str(app.query_one("#progress-label", Static).render())
            assert not app.query_one(LoadingIndicator).display

    asyncio.run(exercise())


def test_shared_hts_lookup_updates_each_related_part(tmp_path):
    async def exercise():
        async def idle(*args, **kwargs):
            pass

        app = BomApp("test.csv", 1, tmp_path, runner=idle)
        async with app.run_test() as pilot:
            parts = app.query_one(PartsView)
            parts.classifications = {ref: {"code": "7318.16.00"} for ref in ("A", "B")}
            events = Events(parts.observe)
            with events.action("country_lookup", hts_code="73181600", position=1, total=1):
                assert parts.statuses == {"A": "◌ Ranking origins", "B": "◌ Ranking origins"}
            await pilot.pause()
            assert parts.statuses == {"A": "✓ Origins found", "B": "✓ Origins found"}

    asyncio.run(exercise())


def test_brief_failure_keeps_calculated_results_visible(tmp_path, monkeypatch):
    monkeypatch.setattr("src.classify.cache.ROOT", tmp_path)

    async def exercise():
        client = DemoClient()
        original = client.create

        async def fail_after_calculation(**kwargs):
            if len(client.requests) == 3:
                raise RuntimeError("brief request failed")
            return await original(**kwargs)

        client.create = fail_after_calculation
        app = BomApp(ROOT / "examples/two_parts.csv", 1, tmp_path,
                     runner=partial(run, client=client, country_discovery=fake_discovery, bls_lookup=fake_bls))
        async with app.run_test(size=(140, 44)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one(ActivityView).status == "Failed"
            assert "started" not in app.query_one(ActivityView).stages.values()
            assert app.query_one("#opportunities", DataTable).row_count == 1
            assert "$0.58" in str(app.query_one("#summary", Static).render())
            assert not app.query_one(LoadingIndicator).display

    asyncio.run(exercise())
