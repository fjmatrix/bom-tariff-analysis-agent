"""Present the existing deterministic business facts and generated brief."""

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Markdown, Static, TabbedContent, TabPane

from src.tui.parts import PartsView, money


class DecisionView(Vertical):
    def compose(self):
        yield Static("Known duty: Pending   |   Modeled savings: Pending\nCoverage: Pending",
                     id="summary", markup=False)
        with TabbedContent():
            with TabPane("Parts", id="parts-tab"):
                yield PartsView()
            with TabPane("Opportunities", id="opportunities-tab"):
                yield Static("Available after duty calculation. Amounts are USD per finished product.",
                             id="opportunity-note", markup=False)
                yield DataTable(id="opportunities", cursor_type="row", zebra_stripes=True)
            with TabPane("Needs review", id="review-tab"):
                yield DataTable(id="review", cursor_type="row", zebra_stripes=True)
            with TabPane("Brief", id="brief-tab"):
                with VerticalScroll():
                    yield Markdown("The decision brief will appear here after analysis.", id="brief")
        yield Static("Results use snapshot HTS rates and assumed Special-program eligibility.\n"
                     "Additional duties and logistics / qualification costs are excluded.",
                     id="assumptions", markup=False)

    def on_mount(self):
        self.query_one("#opportunities", DataTable).add_columns(
            "Part", "From", "To (ties)", "Current duty", "Alt duty", "Savings",
            "Current $/piece", "Quote ceiling $/piece",
        )
        self.query_one("#review", DataTable).add_columns("Part / HTS", "Issue", "Detail")

    def observe(self, event):
        self.query_one(PartsView).observe(event)
        review = self.query_one("#review", DataTable)
        if event.name == "classification" and event.status == "completed":
            row = event.data["classification"]
            if row["status"] != "classified":
                review.add_row(Text(row["reference"]), "Classification needs review", Text(row["reason"]))
        if event.name == "country_lookup" and event.status in ("failed", "skipped"):
            error = event.data.get("error") or event.data.get("ranking", {}).get("error", "Unavailable")
            review.add_row(Text(event.data["hts_code"]), "Origin lookup failed", Text(error))
        if event.name == "brief" and event.status == "completed":
            self.query_one(Markdown).update(event.data["markdown"])
        if event.name != "business_results":
            return
        brief = event.data["brief_data"]
        summary = brief["summary"]
        duty = money(summary["known_current_duty_per_finished_product_usd"])
        savings = money(summary["potential_savings_per_finished_product_usd"])
        exposure = summary["known_exposure_pct_total_bom_cost"]
        exposure_text = "unavailable" if exposure is None else f"{exposure:.2f}% of BOM"
        coverage = summary["covered_bom_cost_pct"]
        coverage_text = "unavailable" if coverage is None else f"{coverage:.1f}% of BOM value"
        self.query_one("#summary", Static).update(
            f"Known duty: {duty} ({exposure_text})   |   Modeled savings: {savings} / finished product\n"
            f"{'PARTIAL COVERAGE' if summary['exposure_is_partial'] else 'Coverage'}: "
            f"{summary['covered_parts']} / {summary['total_parts']} parts · {coverage_text} · Annual: unavailable"
        )
        table = self.query_one("#opportunities", DataTable)
        table.clear()
        for row in brief["sourcing_opportunities"]:
            table.add_row(
                Text(row["reference"]), row["current_origin"], ", ".join(row["equally_saving_origins"]),
                money(row["current_duty_per_finished_product_usd"]),
                money(row["alternative_duty_per_finished_product_usd"]),
                money(row["duty_savings_per_finished_product_usd"]),
                money(row["current_purchase_price_per_piece_usd"], 6),
                money(row["break_even_alternative_purchase_price_per_piece_usd"], 6),
            )
        note = "Best positive-saving origin per part. " if table.row_count else (
            "No positive savings identified among supported, discovered alternatives. "
        )
        self.query_one("#opportunity-note", Static).update(note + brief["break_even_note"])
        review.clear()
        for row in brief["unresolved_parts"]:
            review.add_row(Text(row["reference"]), "Unresolved exposure",
                           Text(f"{row['reason']} · {money(row['bom_cost_usd'])} BOM value"))
        for row in brief["unsupported_alternatives"]:
            review.add_row(Text(row["reference"]), f"Unsupported origin: {row['country']}", Text(row["reason"]))
        for code, error in brief["lookup_errors"].items():
            review.add_row(Text(code), "Origin lookup failed", Text(error))
        if not review.row_count:
            review.add_row("—", "No unresolved calculations", "Verify classification and program eligibility before sourcing.")
        period = brief["trade_period"]
        period_text = f"{period['period_start']}–{period['period_end']}" if period else "No lookups"
        self.query_one("#assumptions", Static).update(
            f"Trade period: {period_text} · Snapshot rates; Special eligibility assumed; additional duties excluded.\n"
            "Quote ceilings exclude logistics / qualification costs. Import origins do not establish supplier availability."
        )
