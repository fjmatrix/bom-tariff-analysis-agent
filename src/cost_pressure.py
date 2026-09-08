"""Index-implied cost pressure from mock benchmarks and existing BOM spend."""

from math import isfinite

from src.bls_mock import get_price_index


def _summarize(items):
    valid = [item for item in items if item["index_implied_cost_pressure"] is not None]
    spend = sum(item["baseline_spend"] for item in valid)
    pressure = sum(item["index_implied_cost_pressure"] for item in valid)
    # Ratios use fractional units (0.02 = 2%); round floating-point boundary noise.
    change = round(pressure / spend, 12) if spend > 0 else None
    trend = "Unknown"
    if change is not None:
        trend = "Rising" if change > 0.02 else "Falling" if change < -0.02 else "Stable"
    return {
        "total_baseline_spend": spend,
        "total_index_implied_cost_pressure": pressure if valid else None,
        "weighted_index_change_pct": change,
        "input_price_trend": trend,
        "items_with_index": sum(item["index_change"] is not None for item in items),
        "items_without_index": sum(item["index_change"] is None for item in items),
        "items_with_valid_spend_and_index": len(valid),
        "items_with_positive_pressure": sum(item["index_implied_cost_pressure"] > 0 for item in valid),
        "items_with_negative_pressure": sum(item["index_implied_cost_pressure"] < 0 for item in valid),
    }


def build_cost_pressure(components, bom=None):
    items = []
    grouped = {}
    for part in bom.leaves if bom is not None else components:
        record = get_price_index(part.reference)
        change = None
        if record is not None:
            current, baseline = record["current_index"], record["baseline_index"]
            if all(isinstance(value, (int, float)) and isfinite(value) and value > 0
                   for value in (current, baseline)):
                change = round(current / baseline - 1, 12)
        spend = None
        if all(isinstance(value, (int, float)) and isfinite(value) and value >= 0
               for value in (part.quantity, part.unit_price_usd)):
            spend = part.extended_cost_usd
            if not isfinite(spend):
                spend = None
        item = {
            "component_reference": part.reference, "name": part.name,
            "baseline_spend": spend, "index_change": change,
            "index_implied_cost_pressure": spend * change
            if spend is not None and change is not None else None,
        }
        items.append(item)
        if bom is not None:
            item.update(line=part.line, parent_line=part.parent_line)
            # Use row identity: the same reference can occur under different parents.
            line = part.parent_line
            while line is not None:
                grouped.setdefault(line, []).append(item)
                line = bom.rows[line].parent_line
            if part.parent_line is None:
                grouped[part.line] = [item]
    headings = []
    products = []
    if bom is not None:
        for row in bom.rows:
            if row.has_child_bom or row.parent_line is None:
                rollup = {
                    "line": row.line, "parent_line": row.parent_line,
                    "component_reference": row.reference, "name": row.name,
                    **_summarize(grouped.get(row.line, [])),
                }
                (products if row.parent_line is None else headings).append(rollup)
    return {
        "source": "Deterministic mock BLS indices; not real BLS data.",
        "basis": "USD per finished product; counts are purchased BOM row occurrences "
                 "when hierarchy is available, otherwise aggregated components. "
                 "Weighted changes are fractional ratios (0.02 = 2%).",
        "summary": _summarize(items),
        "items": items, "headings": headings, "products": products,
        "note": "Index-implied cost pressure is based on a market index benchmark, "
                "not observed supplier price changes.",
    }


def classify_cost_pressure(exposure_pct, trend):
    tariff = "Unknown" if exposure_pct is None else (
        "High" if exposure_pct >= 5 else "Medium" if exposure_pct > 0 else "Low"
    )
    if tariff == "High" or (tariff == "Medium" and trend == "Rising"):
        pressure = "HIGH"
    elif tariff in ("Medium", "Unknown") or trend in ("Rising", "Unknown"):
        pressure = "MEDIUM"
    else:
        pressure = "LOW"
    return tariff, pressure
