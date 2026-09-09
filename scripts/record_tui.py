"""Capture the real TUI replaying saved analysis, without making API calls.

Run: .venv/bin/python -m scripts.record_tui --frames-dir /tmp/bom-tui-frames
Each SVG frame represents 200 ms; the replay ends with five seconds on Brief.
The saved analysis defaults to out/20_parts, with examples/20_parts.csv as input.
"""

import argparse
import asyncio
import csv
import json
import os
from dataclasses import asdict
from pathlib import Path
from time import monotonic

from textual.widgets import Static, TabbedContent

from src.bom.flatten import Bom
from src.events import Events
from src.tui.activity import ActivityView
from src.tui.app import BomApp, Observation


def read_jsonl(path):
    result = {}
    for line in path.read_text().splitlines():
        result.update(json.loads(line))
    return result


async def capture(args):
    async def idle(*unused, **kwargs):
        pass

    saved = args.results
    classifications = list(csv.DictReader((saved / "classified.csv").open()))
    rankings = read_jsonl(saved / "trade_countries.jsonl")
    scenarios = read_jsonl(saved / "scenarios.jsonl")
    brief = json.loads((saved / "brief_data.json").read_text())
    markdown = (saved / "brief.md").read_text()
    parts = Bom.load(args.bom).components()
    args.frames_dir.mkdir(parents=True, exist_ok=True)
    os.environ.pop("NO_COLOR", None)
    app = BomApp(args.bom, 5, saved, runner=idle)
    async with app.run_test(size=(160, 46)) as pilot:
        events = Events(lambda event: app.on_observation(Observation(event)))
        app.query_one("#paths", Static).update(
            f"{args.bom.name}  →  out/20_parts  ·  SAVED ANALYSIS REPLAY · condensed timing"
        )
        frame = 0

        async def hold(count):
            nonlocal frame
            for _ in range(count):
                activity = app.query_one(ActivityView)
                activity.started_at = monotonic() - frame / 5
                if activity.finished_at is not None:
                    activity.finished_at = activity.started_at + 7.2
                activity.tick()
                await pilot.pause(0.05)
                (args.frames_dir / f"{frame:03}.svg").write_text(
                    app.export_screenshot(title="BOM Tariff Exposure Agent")
                )
                frame += 1

        with events.action("run"):
            with events.action("load") as result:
                with events.action("Loading BOM"):
                    await hold(4)
                result["components"] = [
                    {**asdict(part), "extended_cost_usd": part.extended_cost_usd}
                    for part in parts
                ]
            with events.action("tool", tool="classify_bom"):
                for position, row in enumerate(classifications, 1):
                    with events.action("classification", reference=row["reference"],
                                       position=position, total=len(classifications)) as result:
                        await hold(1)
                        result["classification"] = row
            with events.action("Resolving HTS / origin"):
                await hold(4)
            with events.action("tool", tool="find_top_import_countries"):
                with events.action("Fetching tariff data"):
                    await hold(2)
                for position, (code, ranking) in enumerate(rankings.items(), 1):
                    with events.action("country_lookup", hts_code=code,
                                       position=position, total=len(rankings)) as result:
                        if position <= 4:
                            await hold(1)
                        result["ranking"] = ranking
            with events.action("tool", tool="calculate_duty_scenarios"):
                with events.action("Calculating exposure"):
                    await hold(4)
                events.emit("business_results", brief_data=brief, scenarios=scenarios)
            with events.action("brief") as result:
                with events.action("Generating recommendations"):
                    await hold(5)
                result["markdown"] = markdown
        app.query_one(TabbedContent).active = "brief-tab"
        await hold(25)
        print(f"Captured {frame} frames at 200 ms each ({frame / 5:.1f}s)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bom", type=Path, default=Path("examples/20_parts.csv"))
    parser.add_argument("--results", type=Path, default=Path("out/20_parts"))
    parser.add_argument("--frames-dir", type=Path, required=True)
    asyncio.run(capture(parser.parse_args()))
