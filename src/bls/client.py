"""BLS transport and monthly observations, independent of BOM calculations."""

import asyncio
from math import isfinite

import httpx


BLS_API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"


async def fetch_series(series_ids, start_year, end_year, *, api_key, client=None):
    ids = sorted(set(series_ids))
    if not ids:
        return {}
    if not api_key:
        return {series_id: {"error": "BLS_API_KEY is not configured"} for series_id in ids}
    if not 1 <= end_year - start_year + 1 <= 20:
        raise ValueError("BLS requests must span 1 to 20 calendar years")
    if client is None:
        async with httpx.AsyncClient(timeout=30) as owned_client:
            return await fetch_series(ids, start_year, end_year,
                                      api_key=api_key, client=owned_client)
    result = {}
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        payload = {"seriesid": batch, "startyear": str(start_year),
                   "endyear": str(end_year), "registrationkey": api_key}
        error = "BLS request failed"
        for attempt in range(3):
            try:
                response = await client.post(BLS_API_URL, json=payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                error = f"BLS HTTP {exc.response.status_code}"
                if exc.response.status_code != 429 and exc.response.status_code < 500:
                    break
            except httpx.RequestError:
                # Do not expose request objects or credentials in errors/artifacts.
                error = "BLS connection failed or timed out"
            else:
                try:
                    data = response.json()
                    if data.get("status") != "REQUEST_SUCCEEDED":
                        messages = "; ".join(data.get("message") or [])
                        error = "BLS: " + messages.replace(api_key, "[redacted]") if messages else (
                            "BLS rejected the request; check key registration and API quota"
                        )
                        break
                    series = data["Results"]["series"]
                    parsed = {row["seriesID"]: {"observations": _monthly(row["data"])}
                              for row in series if row["seriesID"] in batch}
                except (ValueError, KeyError, TypeError, AttributeError):
                    error = "BLS returned a malformed response"
                    break
                result.update({series_id: parsed.get(series_id, {"error": "BLS omitted the series"})
                               for series_id in batch})
                break
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        for series_id in batch:
            result.setdefault(series_id, {"error": error})
    return result


def _monthly(rows):
    observations = {}
    for row in rows:
        period = row.get("period", "")
        if period not in {f"M{month:02d}" for month in range(1, 13)}:
            continue
        try:
            year = int(row["year"])
            value = float(row["value"])
        except (ValueError, TypeError, KeyError):
            continue
        if not 1 <= year <= 9999 or not isfinite(value) or value <= 0:
            continue
        observations[f"{year:04d}-{period[1:]}"] = {
            "value": value, "footnotes": row.get("footnotes") or [],
        }
    return observations
