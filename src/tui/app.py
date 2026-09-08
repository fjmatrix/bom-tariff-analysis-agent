"""Textual owns rendering and cancellation; the runner owns analysis."""

import asyncio
from pathlib import Path

from textual import work
from textual.app import App
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Footer, Header, Static
from textual.worker import WorkerCancelled, WorkerFailed

from src.events import WorkflowEvent
from src.run import run
from src.tui.activity import ActivityView
from src.tui.decisions import DecisionView


class Observation(Message):
    def __init__(self, event: WorkflowEvent):
        super().__init__()
        self.event = event


class BomApp(App):
    TITLE = "BOM TARIFF ANALYSIS"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("c", "cancel", "Cancel analysis"),
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
    ]

    def __init__(self, bom_path, top_countries, out_dir, *, runner=run):
        super().__init__()
        self.bom_path = Path(bom_path)
        self.top_countries = top_countries
        self.out_dir = Path(out_dir)
        self.runner = runner
        self.analysis_worker = None

    def compose(self):
        yield Header()
        yield Static(f"{self.bom_path.name}  →  {self.out_dir.resolve()}", id="paths", markup=False)
        with Horizontal(id="workspace"):
            yield DecisionView()
            yield ActivityView()
        yield Footer()

    def on_mount(self):
        self.theme = "textual-dark"
        self.analysis_worker = self.analyze()

    def on_resize(self, event):
        self.set_class(event.size.width < 105, "compact")

    @work
    async def analyze(self):
        try:
            await self.runner(
                self.bom_path, self.top_countries, self.out_dir,
                on_event=lambda event: self.post_message(Observation(event)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # The runner emits failure events; retain the screen and any calculated results.
            self.notify(str(error), title="Analysis failed", severity="error", timeout=10)

    def on_observation(self, message):
        self.query_one(ActivityView).observe(message.event)
        self.query_one(DecisionView).observe(message.event)

    async def action_cancel(self):
        worker = self.analysis_worker
        if worker is not None and not worker.is_finished:
            worker.cancel()
            try:
                await worker.wait()
            except (WorkerCancelled, WorkerFailed):
                pass

    async def action_quit(self):
        await self.action_cancel()
        self.exit()
