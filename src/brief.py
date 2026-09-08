"""Calculate decision-brief facts from the BOM and supported duty scenarios."""

from src.duty.rates import duty_rate


def percent(numerator, denominator):
    return round(100 * numerator / denominator, 4) if denominator > 0 else None


def build_brief_data(components, classifications, scenarios, index, country_rankings):
    total_cost = sum(part.extended_cost_usd for part in components)
    classified = {row.reference: row for row in classifications}
    exposures = []
    opportunities = []
    unresolved = []
    alternative_errors = []
    covered_cost = 0
    for part in components:
        scenario = scenarios[part.reference]
        identity = {"reference": part.reference, "name": part.name}
        if "reason" in scenario:
            unresolved.append({**identity, "reason": scenario["reason"],
                               "bom_cost_usd": round(part.extended_cost_usd, 2)})
            continue
        origin = scenario["country_of_origin"]
        countries = scenario["countries"]
        current_duty = countries[origin]["duty_usd"]
        covered_cost += part.extended_cost_usd
        exposures.append({
            **identity, "hts_code": classified[part.reference].code,
            "current_origin": origin, "quantity_per_finished_product": part.quantity,
            "purchase_price_per_piece_usd": part.unit_price_usd,
            "bom_cost_usd": round(part.extended_cost_usd, 2),
            "current_duty_per_finished_product_usd": current_duty,
            "exposure_pct_total_bom_cost": percent(current_duty, total_cost),
            "exposure_pct_part_cost": percent(current_duty, part.extended_cost_usd),
        })
        for country, values in countries.items():
            if "reason" in values:
                alternative_errors.append({**identity, "country": country,
                                           "reason": values["reason"]})
        alternatives = [
            (country, values) for country, values in countries.items()
            if country != origin and values.get("savings_usd", 0) > 0
        ]
        if not alternatives:
            continue
        best_country, best = min(alternatives, key=lambda item: (-item[1]["savings_usd"], item[0]))
        code = index.get(classified[part.reference].code)
        current_rate = duty_rate(code, origin)
        alternative_rate = duty_rate(code, best_country)
        # Match purchase price plus ad-valorem duty at each origin, per piece.
        break_even = part.unit_price_usd * (1 + current_rate) / (1 + alternative_rate)
        opportunities.append({
            **identity, "current_origin": origin, "alternative_origin": best_country,
            "equally_saving_origins": sorted(
                country for country, values in alternatives
                if values["savings_usd"] == best["savings_usd"]
            ),
            "current_duty_rate": current_rate, "alternative_duty_rate": alternative_rate,
            "current_duty_per_finished_product_usd": current_duty,
            "alternative_duty_per_finished_product_usd": best["duty_usd"],
            "duty_savings_per_finished_product_usd": best["savings_usd"],
            "current_purchase_price_per_piece_usd": part.unit_price_usd,
            "break_even_alternative_purchase_price_per_piece_usd": round(break_even, 6),
        })
    current_total = round(sum(row["current_duty_per_finished_product_usd"] for row in exposures), 2)
    savings_total = round(sum(row["duty_savings_per_finished_product_usd"] for row in opportunities), 2)
    # Country discovery uses the same period for every HTS lookup, including failures.
    ranking = next(iter(country_rankings.values()), None)
    return {
        "currency": "USD",
        "basis": "One finished product; purchased parts imported separately.",
        "summary": {
            "bom_cost_per_finished_product_usd": round(total_cost, 2),
            "known_current_duty_per_finished_product_usd": current_total if exposures else None,
            "known_exposure_pct_total_bom_cost": percent(current_total, total_cost) if exposures else None,
            "potential_savings_per_finished_product_usd": savings_total if exposures else None,
            "potentially_addressable_pct_known_exposure": percent(savings_total, current_total),
            "covered_parts": len(exposures), "total_parts": len(components),
            "covered_bom_cost_usd": round(covered_cost, 2),
            "covered_bom_cost_pct": percent(covered_cost, total_cost),
            "exposure_is_partial": bool(unresolved),
        },
        "product_exposure": sorted(exposures, key=lambda row: (
            -row["current_duty_per_finished_product_usd"], row["reference"],
        )),
        "sourcing_opportunities": sorted(opportunities, key=lambda row: (
            -row["duty_savings_per_finished_product_usd"], row["reference"],
        )),
        "unresolved_parts": unresolved,
        "unsupported_alternatives": alternative_errors,
        "trade_period": {
            "period_start": ranking["period_start"],
            "period_end": ranking["period_end"],
        } if ranking is not None else None,
        "lookup_errors": {
            code: ranking["error"] for code, ranking in country_rankings.items()
            if "error" in ranking
        },
        "break_even_note": "Per-piece current purchase price × (1 + current duty rate) / "
                           "(1 + alternative duty rate). Excludes freight, tooling, "
                           "qualification, switching costs, and other unmodeled duties. "
                           "Savings use unchanged BOM prices; break-even is a quote ceiling, "
                           "not an available supplier price. Tied savings are counted once per part.",
    }
