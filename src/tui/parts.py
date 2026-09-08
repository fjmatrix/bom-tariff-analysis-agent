"""Significant purchased parts and their observable analysis state."""

from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Checkbox, DataTable, Input, Select, Static


def money(value, decimals=2):
    return "Unavailable" if value is None else f"${value:,.{decimals}f}"


class PartsView(Vertical):
    def __init__(self):
        super().__init__()
        self.components = {}
        self.classifications = {}
        self.rankings = {}
        self.scenarios = {}
        self.statuses = {}
        self.cached = set()
        self.exposures = {}
        self.savings = {}
        self.selected = None

    def compose(self):
        with Horizontal(id="part-controls"):
            yield Input(placeholder="Search reference or description", id="search")
            yield Select([("BOM value", "value"), ("Duty exposure", "duty"),
                          ("Potential savings", "savings")], value="value",
                         allow_blank=False, id="sort")
            yield Checkbox("All parts", id="all-parts")
        yield Static("Waiting for BOM validation", id="part-share", markup=False)
        yield DataTable(id="parts", cursor_type="row", zebra_stripes=True)
        with VerticalScroll(id="part-detail-scroll"):
            yield Static("Select a part to inspect its evidence and country comparisons.",
                         id="part-detail", markup=False)

    def on_mount(self):
        table = self.query_one(DataTable)
        table.fixed_columns = 1
        for label, width in [("Part", 14), ("BOM value", 10), ("Origin", 6), ("Status", 17),
                             ("Duty", 11), ("Savings", 11), ("Description", 30), ("% BOM", 7)]:
            table.add_column(label, width=width)

    def observe(self, event):
        data = event.data
        if event.name == "load" and event.status == "completed":
            self.components = {part["reference"]: part for part in data["components"]}
        elif event.name == "usage" and event.status == "cache_hit":
            self.cached.add(data["label"])
        elif event.name == "classification":
            reference = data["reference"]
            if event.status == "completed":
                row = data["classification"]
                self.classifications[reference] = row
                self.statuses[reference] = (
                    "✓ Cached" if reference in self.cached else "✓ Classified"
                ) if row["status"] == "classified" else "! Needs review"
            else:
                self.statuses[reference] = {
                    "started": "◌ Classifying", "failed": "✕ Failed", "cancelled": "– Cancelled",
                }[event.status]
        elif event.name == "country_lookup":
            code = data["hts_code"]
            self.rankings[code] = data.get("ranking", {"error": data.get("error", event.status)})
            for reference, row in self.classifications.items():
                if (row["code"] or "").replace(".", "") == code:
                    self.statuses[reference] = {
                        "started": "◌ Ranking origins", "completed": "✓ Origins found",
                        "failed": "! Lookup failed", "skipped": "! Lookup skipped",
                        "cancelled": "– Cancelled",
                    }[event.status]
        elif event.name == "business_results":
            self.scenarios = data["scenarios"]
            brief = data["brief_data"]
            self.exposures = {row["reference"]: row["current_duty_per_finished_product_usd"]
                              for row in brief["product_exposure"]}
            self.savings = dict.fromkeys(self.exposures, 0)
            self.savings.update({row["reference"]: row["duty_savings_per_finished_product_usd"]
                                 for row in brief["sourcing_opportunities"]})
            for reference, scenario in self.scenarios.items():
                self.statuses[reference] = "! Needs review" if "reason" in scenario else "✓ Calculated"
        else:
            return
        self.refresh_parts()

    def on_input_changed(self, event):
        self.refresh_parts()

    def on_select_changed(self, event):
        self.refresh_parts()

    def on_checkbox_changed(self, event):
        self.refresh_parts()

    def refresh_parts(self):
        table = self.query_one(DataTable)
        if not table.columns:
            return
        query = self.query_one(Input).value.casefold()
        sort = self.query_one(Select).value
        rows = [part for part in self.components.values()
                if query in f"{part['reference']} {part['name']}".casefold()]
        metric = self.exposures if sort == "duty" else self.savings
        rows.sort(key=lambda part: (
            -(part["extended_cost_usd"] if sort == "value" else metric.get(part["reference"], -1)),
            part["reference"],
        ))
        if not self.query_one(Checkbox).value:
            rows = rows[:10]
        total = sum(part["extended_cost_usd"] for part in self.components.values())
        share = sum(part["extended_cost_usd"] for part in rows) / total * 100 if total else 0
        self.query_one("#part-share", Static).update(
            f"Showing {len(rows)} / {len(self.components)} parts · {share:.1f}% of BOM value · "
            "All parts are analyzed"
        )
        table.clear()
        for part in rows:
            reference = part["reference"]
            status = self.statuses.get(reference, "· Queued")
            color = "yellow" if status.startswith("!") else "cyan" if status.startswith("◌") else ""
            table.add_row(
                Text(reference), money(part["extended_cost_usd"]),
                Text(part["country_of_origin"] or "Unknown"), Text(status, style=color),
                money(self.exposures.get(reference)) if self.scenarios else "Pending",
                money(self.savings.get(reference, 0)) if reference in self.exposures else (
                    "Unavailable" if self.scenarios else "Pending"
                ), Text(part["name"]),
                f"{part['extended_cost_usd'] / total * 100:.1f}%" if total else "—", key=reference,
            )
        references = [part["reference"] for part in rows]
        self.selected = self.selected if self.selected in references else next(iter(references), None)
        if self.selected is not None:
            table.move_cursor(row=references.index(self.selected))
        self.show_detail()

    def on_data_table_row_highlighted(self, event):
        self.selected = event.row_key.value
        self.show_detail()

    def show_detail(self):
        part = self.components.get(self.selected)
        if part is None:
            self.query_one("#part-detail", Static).update("No matching parts.")
            return
        row = self.classifications.get(self.selected, {})
        code = (row.get("code") or "").replace(".", "")
        lines = [
            f"{part['reference']} · {part['name']}",
            f"{part['quantity']:g} pieces / finished product · {money(part['unit_price_usd'], 6)} / piece",
            "Assembly: " + "; ".join(" > ".join(path) for path in part["assembly_paths"]),
            f"HTS: {row.get('code') or 'Pending / unresolved'} · {row.get('reason', '')}",
            f"Evidence: {row.get('evidence') or 'Pending'}",
        ]
        if code in self.rankings:
            ranking = self.rankings[code]
            if "error" in ranking:
                lines.append(f"Import origins: {ranking['error']}")
            else:
                lines.append(f"Trade period: {ranking['period_start']}–{ranking['period_end']}")
                lines.append("Import customs value: " + ", ".join(
                    f"{country} {money(values['customs_value_usd'])}"
                    for country, values in ranking["countries"].items()
                ))
        if self.selected in self.scenarios:
            scenario = self.scenarios[self.selected]
            if "reason" in scenario:
                lines.append(f"Unresolved exposure: {scenario['reason']}")
            else:
                lines.append("Duty / savings per finished product:")
                for country, values in scenario["countries"].items():
                    detail = values["reason"] if "reason" in values else (
                        f"{money(values['duty_usd'])} duty · {money(values['savings_usd'])} savings"
                    )
                    lines.append(f"  {country}: {detail}")
        self.query_one("#part-detail", Static).update("\n".join(lines))
