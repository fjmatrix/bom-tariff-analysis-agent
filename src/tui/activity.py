"""Action lifecycle, honest progress counts, and a bounded activity history."""

from time import monotonic

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import LoadingIndicator, ProgressBar, RichLog, Static


STAGES = {
    "load": "Load BOM", "classify_bom": "Classify",
    "find_top_import_countries": "Rank origin countries by import value",
    "calculate_duty_scenarios": "Calculate", "brief": "Brief",
}
SYMBOLS = {"started": "◌", "completed": "✓", "failed": "✕",
           "cancelled": "–", "rejected": "!", "skipped": "!"}


def describe(event):
    data = event.data
    if event.name == "tool":
        label = data["tool"]
        if data.get("reused"):
            label += " · reusing completed result"
        if "error" in data.get("result", {}):
            label += ": " + data["result"]["error"]
        return label
    if event.name == "classification":
        label = f"Classify {data['reference']}"
        if event.status == "started":
            label = f"Classifying part {data['position']} / {data['total']} · {data['reference']}"
        if "classification" in data:
            row = data["classification"]
            label += f" → {row['code'] or row['reason']}"
        return label
    if event.name == "country_lookup":
        label = f"Ranking origin countries by import value · HTS {data['hts_code']}"
        if "ranking" in data and "countries" in data["ranking"]:
            label += f" → {len(data['ranking']['countries'])} origins"
        return label
    if event.name == "dataweb_retry":
        return (f"DataWeb HTTP {data['status_code']} · HTS {data['hts_code']} · "
                f"retry {data['attempt']}/{data['max_attempts']} in {data['delay_seconds']:g}s")
    if event.name == "agent":
        label = "Preparing decision brief / agent response" if data["results_ready"] else "Waiting for agent response"
        if event.status == "completed":
            functions = data.get("functions", [])
            label = ("Agent - selected tool: " + ", ".join(f"{function}()" for function in functions)
                     if functions else "Agent responded - no function call")
        return f"{label} - turn {data['turn']}"
    return {"load": "Validate and aggregate BOM", "country_metadata": "Load country directory",
            "brief": "Save decision brief", "run": "Analysis"}.get(event.name, event.name)


class ActivityView(Vertical):
    def __init__(self):
        super().__init__(id="activity")
        self.active = {}
        self.stages = {}
        self.started_at = monotonic()
        self.finished_at = None
        self.status = "Starting"

    def compose(self):
        yield Static("LIVE WORKFLOW", classes="section-label")
        yield Static(id="stages", markup=False)
        with Horizontal(id="current-action"):
            yield LoadingIndicator(id="spinner")
            yield Static("Starting analysis", id="current", markup=False)
        yield Static("", id="progress-label", markup=False)
        yield ProgressBar(total=None, show_eta=False, id="progress")
        yield RichLog(id="history", wrap=True, markup=False, max_lines=600, min_width=1)
        yield Static("Tokens: awaiting provider usage", id="tokens", markup=False)

    def on_mount(self):
        self.set_interval(0.2, self.tick)
        self.render_stages()

    def render_stages(self):
        self.query_one("#stages", Static).update("\n".join(
            f"{SYMBOLS.get(self.stages.get(key), '·')}  {label}" for key, label in STAGES.items()
        ))

    def tick(self):
        end = self.finished_at if self.finished_at is not None else monotonic()
        self.app.sub_title = f"{self.status} · {int(end - self.started_at)}s"
        if self.active:
            event, started = next(reversed(self.active.values()))
            self.query_one("#current", Static).update(
                f"{describe(event)}\n{int(monotonic() - started)}s elapsed"
            )

    def observe(self, event):
        data = event.data
        if event.name in ("usage", "usage_totals"):
            totals = data["totals"] if event.name == "usage" else data["report"]["totals"]
            self.query_one("#tokens", Static).update(
                f"{totals['total_tokens']:,} reported tokens · {totals['api_calls']} calls\n"
                f"{totals['cache_hits']} cache hits · {totals['calls_without_usage']} calls with unknown usage"
            )
            if event.status == "cache_hit":
                self.query_one(RichLog).write(Text(f"✓ Cached classification · {data['label']}", style="dim"))
            return
        if event.name == "business_results":
            self.query_one(RichLog).write(Text("✓ Exposure and sourcing results available", style="green"))
            return
        if event.name == "run":
            self.status = "Running" if event.status == "started" else event.status.capitalize()
            if event.status != "started":
                self.finished_at = monotonic()
                self.active.clear()
                for stage, status in self.stages.items():
                    if status == "started":
                        self.stages[stage] = event.status
                self.query_one("#current", Static).update(
                    self.status + (f"\n{data['error']}" if "error" in data else "")
                )
                self.query_one("#progress", ProgressBar).display = False
                self.query_one("#progress-label", Static).update(
                    "Results remain available" if event.status != "completed" else "Analysis complete"
                )
        else:
            if event.status == "started":
                self.active[event.action_id] = (event, monotonic())
            else:
                self.active.pop(event.action_id, None)
            stage = data.get("tool", event.name)
            if stage in STAGES and not (event.status == "rejected" and self.stages.get(stage) == "completed"):
                self.stages[stage] = event.status
            if event.name == "agent" and data["results_ready"]:
                self.stages["brief"] = "started" if event.status == "completed" else event.status
            if "position" in data:
                completed = data["position"] - (event.status != "completed")
                label = "parts processed" if event.name == "classification" else "HTS lookups processed"
                self.query_one("#progress-label", Static).update(
                    f"{completed} / {data['total']} {label} · {completed / data['total']:.0%}"
                )
                self.query_one("#progress", ProgressBar).update(total=data["total"], progress=completed)
            elif event.status == "started":
                self.query_one("#progress-label", Static).update("Waiting for result")
                self.query_one("#progress", ProgressBar).update(total=None, progress=0)
            if not self.active and event.status != "started":
                self.query_one("#current", Static).update(describe(event))
        self.query_one("#spinner", LoadingIndicator).display = bool(self.active)
        self.render_stages()
        symbol = SYMBOLS.get(event.status, "·")
        color = {
            "started": "bold cyan", "completed": "green", "failed": "bold red",
            "rejected": "yellow", "skipped": "yellow", "cancelled": "yellow",
        }.get(event.status, "")
        detail = f" · {data['error']}" if "error" in data else ""
        self.query_one(RichLog).write(Text(f"{symbol} {describe(event)}{detail}", style=color))
        self.tick()
