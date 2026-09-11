"""Map HTS codes and enrich them with comparable BLS index observations."""

import os
from datetime import date, timedelta

from src.bls.client import fetch_series
from src.bls.mapping import harmonized_candidates, load_bls_harmonized_series
from src.config import BLS_SERIES


async def enrich_price_indices(hts_codes, *, baseline_period=None, current_period=None,
                               today=None, series_path=BLS_SERIES, client=None):
    today = today or date.today()
    cutoff = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    for period in (baseline_period, current_period):
        if period is not None:
            try:
                parsed = date.fromisoformat(period + "-01")
            except (ValueError, TypeError):
                raise ValueError("BLS periods must use YYYY-MM") from None
            if parsed.strftime("%Y-%m") != period or period > cutoff:
                raise ValueError("BLS periods must be completed months in YYYY-MM format")
    if baseline_period and not current_period:
        raise ValueError("An explicit BLS baseline requires a current period")
    end_year = int((current_period or cutoff)[:4])
    start_year = int(baseline_period[:4]) if baseline_period else end_year - 2
    if baseline_period and baseline_period >= current_period:
        raise ValueError("BLS baseline must precede the current period")
    if end_year - start_year >= 20:
        raise ValueError("BLS comparison must fit within 20 calendar years")

    available = load_bls_harmonized_series(str(series_path))
    candidates = {}
    indices = {}
    for code in dict.fromkeys(hts_codes):
        try:
            candidates[code] = harmonized_candidates(code, available)
        except ValueError as error:
            indices[code] = {"hts_code": code, "reason": str(error)}
            continue
        if not candidates[code]:
            indices[code] = {"hts_code": code, "reason": "No published Harmonized import category"}
    fetched = await fetch_series(
        [row["series_id"] for rows in candidates.values() for row in rows],
        start_year, end_year, api_key=os.getenv("BLS_API_KEY"), client=client,
    )
    if current_period is None:
        # Use one reporting month; a sparse series must not shift its own dates.
        current_period = max((
            period for series in fetched.values() for period in series.get("observations", {})
            if period <= cutoff
        ), default=None)
    if current_period and baseline_period is None:
        baseline_period = f"{int(current_period[:4]) - 1:04d}{current_period[4:]}"

    for code, rows in candidates.items():
        if not rows:
            continue
        attempts = []
        for row in rows:
            series = fetched[row["series_id"]]
            observations = series.get("observations", {})
            baseline = observations.get(baseline_period)
            current = observations.get(current_period)
            if "error" in series:
                reason = series["error"]
            elif not current_period:
                reason = "No monthly BLS observations available"
            elif not baseline or not current:
                missing = [period for period, value in ((baseline_period, baseline),
                                                        (current_period, current)) if not value]
                reason = "Missing BLS observations: " + ", ".join(missing)
            else:
                indices[code] = {
                    **row, "baseline_index": baseline["value"], "current_index": current["value"],
                    "baseline_period": baseline_period, "current_period": current_period,
                    "baseline_footnotes": baseline["footnotes"], "current_footnotes": current["footnotes"],
                    "fallback_reason": ("; ".join(item["reason"] for item in attempts)
                                        or "No published 4-digit category") if len(row["category"]) == 2 else None,
                    "attempts": attempts,
                }
                break
            attempts.append({"series_id": row["series_id"], "reason": reason})
        else:
            indices[code] = {"hts_code": rows[0]["hts_code"], "reason": "No usable BLS index pair",
                             "attempts": attempts}
    return {"baseline_period": baseline_period, "current_period": current_period, "indices": indices}
