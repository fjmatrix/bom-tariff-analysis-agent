"""Apply HTS country rates to classified parts and calculate duty in USD."""

import json
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
    result = {}
    for part in components:
        scenario = {"country_of_origin": part.country_of_origin.strip().upper()}
        result[part.reference] = scenario
        classification = classified.get(part.reference)
        if classification is None or classification.status != "classified":
            reason = classification.reason if classification else "not_classified"
            scenario["reason"] = reason
            continue
        code = index.get(classification.code or "")
        if code is None:
            scenario["reason"] = "unknown_hts_code"
            continue
        origin = scenario["country_of_origin"]
        if not re.fullmatch(r"[A-Z]{2}", origin):
            scenario["reason"] = "missing_or_invalid_origin"
            continue
        country_rates = {
            country: duty_rate(code, country)
            for country in sorted(set(countries_by_code.get(code.htsno.replace(".", ""), [])) | {origin})
        }
        if country_rates[origin] is None:
            scenario["reason"] = "unsupported_current_origin_rate"
            continue
        baseline = round(part.extended_cost_usd * country_rates[origin], 2)
        scenario["countries"] = {}
        for country, rate in sorted(country_rates.items()):
            if rate is None:
                scenario["countries"][country] = {
                    "reason": "unsupported_alternative_rate",
                }
                continue
            duty = round(part.extended_cost_usd * rate, 2)
            scenario["countries"][country] = {
                "duty_usd": duty,
                "savings_usd": round(baseline - duty, 2),
            }
    return result


def write_scenarios(result: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for reference, scenario in result.items():
            fh.write(json.dumps(
                {reference: scenario}, ensure_ascii=False, separators=(",", ":"),
            ) + "\n")
