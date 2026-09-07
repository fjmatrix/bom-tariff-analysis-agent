"""Apply HTS country rates to classified parts and calculate duty in EUR."""

import csv
import re
from pathlib import Path

from src.classify.classifier import Classification
from src.bom.flatten import Component
from src.duty.rates import duty_rate
from src.hts.index import HtsIndex


def calculate_duty_scenarios(
    components: list[Component],
    classifications: list[Classification],
    index: HtsIndex,
    countries_by_code: dict[str, list[str]],
) -> dict:
    countries_by_code = {
        code.replace(".", ""): sorted({country.strip().upper() for country in countries})
        for code, countries in countries_by_code.items()
    }
    if any(
        not re.fullmatch(r"[A-Z]{2}", country)
        for countries in countries_by_code.values() for country in countries
    ):
        raise ValueError("Countries must be two-letter country codes")
    classified = {row.reference: row for row in classifications}
    rows, current, best, unresolved = [], [], [], []
    for part in components:
        classification = classified.get(part.reference)
        if classification is None or classification.status != "classified":
            reason = classification.reason if classification else "not_classified"
            unresolved.append({"reference": part.reference, "reason": reason})
            continue
        code = index.get(classification.code or "")
        if code is None:
            unresolved.append({"reference": part.reference, "reason": "unknown_hts_code"})
            continue
        origin = part.country_of_origin.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", origin):
            unresolved.append({"reference": part.reference, "reason": "missing_or_invalid_origin"})
            continue
        country_rates = {
            country: duty_rate(code, country)
            for country in sorted(set(countries_by_code.get(code.htsno.replace(".", ""), [])) | {origin})
        }
        if country_rates[origin] is None:
            unresolved.append({"reference": part.reference, "reason": "unsupported_current_origin_rate"})
            continue
        baseline = round(part.extended_cost_eur * country_rates[origin], 2)
        part_rows = []
        for country, rate in sorted(country_rates.items()):
            if rate is None:
                unresolved.append({
                    "reference": part.reference, "country": country,
                    "reason": "unsupported_alternative_rate",
                })
                continue
            duty = round(part.extended_cost_eur * rate, 2)
            row = {
                "reference": part.reference,
                "country": country,
                "duty_eur": duty,
                "savings_eur": round(baseline - duty, 2),
            }
            part_rows.append(row)
            if country == origin:
                current.append(row)
        rows.extend(part_rows)
        cheapest = max(part_rows, key=lambda row: row["savings_eur"])
        if cheapest["savings_eur"] > 0:
            best.append(cheapest)

    total = round(sum(row["duty_eur"] for row in current), 2)
    savings = round(sum(row["savings_eur"] for row in best), 2)
    return {
        "rows": rows,
        "current_duty_eur": total,
        "best_savings_eur": savings,
        "duty_after_best_savings_eur": round(total - savings, 2),
        "ranked_costs": sorted(current, key=lambda row: row["duty_eur"], reverse=True),
        "best_alternatives": sorted(best, key=lambda row: row["savings_eur"], reverse=True),
        "calculated_parts": len(current),
        "total_parts": len(components),
        "unresolved": unresolved,
    }


def write_scenarios(rows: list[dict], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["reference", "country", "duty_eur", "savings_eur"])
        writer.writeheader()
        for row in rows:
            writer.writerow({
                **row,
                "duty_eur": f"{row['duty_eur']:.2f}",
                "savings_eur": f"{row['savings_eur']:.2f}",
            })
