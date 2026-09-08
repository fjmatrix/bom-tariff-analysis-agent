"""Plain terminal rendering, independent of the workflow and Textual."""

import json

from src.events import WorkflowEvent


def console_event(event: WorkflowEvent):
    data = event.data
    if event.name == "usage":
        counts = data["counts"]
        detail = ", ".join(f"{field}={value if value is not None else 'unknown'}"
                           for field, value in counts.items())
        print(f"Tokens [{data['stage']} {data['label']}] {event.status}: {detail}", flush=True)
    elif event.name == "usage_totals":
        report = data["report"]
        for label, counts in [*report["stages"].items(), ("run", report["totals"])]:
            print(f"Token totals [{label}]: {json.dumps(counts)}", flush=True)
    elif event.name == "tool":
        if event.status == "started":
            print(f"\nAgent calls {data['tool']}()", flush=True)
        elif "result" in data:
            output = json.dumps(data["result"], ensure_ascii=False, separators=(",", ":"))
            print(f"Tool result: {output}", flush=True)
    elif event.name == "brief" and event.status == "completed":
        print(f"Wrote results to {data['out_dir']}", flush=True)
