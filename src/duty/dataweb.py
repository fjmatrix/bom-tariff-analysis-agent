"""Find leading U.S. import origins for classified HTS codes."""

import argparse
import asyncio
import json
import os
from contextlib import nullcontext
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic
from weakref import WeakKeyDictionary

import httpx

from src import config  # Load DATAWEB_API_KEY from the repository's .env.
from src.events import Events

REPORT_URL = "https://datawebws.usitc.gov/dataweb/api/v2/report2/runReport"
COUNTRIES_URL = "https://datawebws.usitc.gov/dataweb/api/v2/country/getAllCountries"
REPORT_MAX_ATTEMPTS = 5
REPORT_BACKOFF_SECONDS = 2.0
REPORT_RATE_LIMIT_BACKOFF_SECONDS = 15.0
REPORT_MIN_GAP_SECONDS = 5.0


def _retry_after_seconds(value: str | None) -> float:
    if value is None:
        return 0.0
    try:
        return max(0, int(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


class _ReportRunner:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_run = 0.0

    async def post(
        self, client: httpx.AsyncClient, token: str, payload: dict, *, events=None,
    ) -> httpx.Response:
        async with self.lock:
            for attempt in range(REPORT_MAX_ATTEMPTS):
                delay = self.next_run - monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    response = await client.post(
                        REPORT_URL,
                        headers={"Authorization": f"Bearer {token}"},
                        json=payload,
                        timeout=120,
                    )
                finally:
                    self.next_run = monotonic() + REPORT_MIN_GAP_SECONDS
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    backoff = (REPORT_RATE_LIMIT_BACKOFF_SECONDS
                               if response.status_code == 429 else REPORT_BACKOFF_SECONDS)
                    delay = max(
                        REPORT_MIN_GAP_SECONDS,
                        backoff * 2 ** attempt,
                        _retry_after_seconds(response.headers.get("Retry-After")),
                    )
                    # Preserve the cooldown for the next code even after retries run out.
                    self.next_run = monotonic() + delay
                    if attempt < REPORT_MAX_ATTEMPTS - 1:
                        if events is not None:
                            events.emit(
                                "dataweb_retry", "retrying",
                                hts_code=payload["searchOptions"]["commodities"]["commoditiesManual"],
                                status_code=response.status_code,
                                attempt=attempt + 2, max_attempts=REPORT_MAX_ATTEMPTS,
                                delay_seconds=delay,
                                retry_after=response.headers.get("Retry-After"),
                            )
                        continue
                    raise httpx.HTTPStatusError(
                        f"DataWeb report failed with HTTP {response.status_code} after "
                        f"{REPORT_MAX_ATTEMPTS} attempts; cooldown {delay:g}s",
                        request=response.request, response=response,
                    )
                response.raise_for_status()
                return response


# Share pacing across clients without sharing asyncio locks between event loops.
_report_runners: WeakKeyDictionary = WeakKeyDictionary()


async def _post_report(
    client: httpx.AsyncClient, token: str, payload: dict, *, events=None,
) -> httpx.Response:
    loop = asyncio.get_running_loop()
    if loop not in _report_runners:
        _report_runners[loop] = _ReportRunner()
    return await _report_runners[loop].post(client, token, payload, events=events)


def _code(hts_code: str) -> str:
    code = hts_code.strip().replace(".", "")
    if not code.isascii() or not code.isdigit() or len(code) not in (8, 10):
        raise ValueError("HTS code must contain 8 or 10 digits, optionally dotted")
    return code


def trailing_12_months(today: date | None = None) -> tuple[str, str]:
    """Return the first and last month in the 12 complete months before today."""
    today = today or date.today()
    end_index = today.year * 12 + today.month - 2
    start_index = end_index - 11
    start_year, start_month = divmod(start_index, 12)
    end_year, end_month = divmod(end_index, 12)
    return f"{start_month + 1:02d}/{start_year}", f"{end_month + 1:02d}/{end_year}"


def _payload(hts_code: str, start: str, end: str) -> dict:
    code = _code(hts_code)
    return {
        "reportOptions": {"tradeType": "Import", "classificationSystem": "HTS"},
        "searchOptions": {
            "commodities": {
                "aggregation": "Break Out Commodities",
                "codeDisplayFormat": "YES",
                "commodities": [code],
                "commoditiesManual": code,
                "commoditySelectType": "list",
                "granularity": str(len(code)),
                "commodityGroups": {"systemGroups": [], "userGroups": []},
            },
            "countries": {
                "aggregation": "Break Out Countries",
                "countries": [],
                "countriesSelectType": "all",
                "countryGroups": {"systemGroups": [], "userGroups": []},
            },
            "componentSettings": {
                "dataToReport": ["CONS_CUSTOMS_VALUE"],
                "scale": "1",
                "timeframeSelectType": "specificDateRange",
                "years": [],
                "startDate": start,
                "endDate": end,
                "startMonth": None,
                "endMonth": None,
                "yearsTimeline": "Annual",
            },
            "MiscGroup": {
                "districts": {
                    "aggregation": "Aggregate District",
                    "districts": [],
                    "districtsSelectType": "all",
                },
                "importPrograms": {"importPrograms": [], "programsSelectType": "all"},
                "extImportPrograms": {
                    "aggregation": "Aggregate CSC",
                    "extImportPrograms": [],
                    "programsSelectType": "all",
                },
                "provisionCodes": {
                    "aggregation": "Aggregate RPCODE",
                    "provisionCodesSelectType": "all",
                    "rateProvisionCodes": [],
                },
            },
        },
        "sortingAndDataFormat": {
            "DataSort": {"columnOrder": ["COUNTRY", f"HTS{len(code)} & DESCRIPTION"]},
            "reportCustomizations": {"totalRecords": "20000"},
        },
    }


def _report_rows(report: dict) -> list[tuple[str, int]]:
    try:
        group = report["tables"][0]["row_groups"][0]
        columns = group["columnInfo"]
        rows = group["rowsNew"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("DataWeb returned an unsupported report shape") from exc

    country_index = next(
        (i for i, column in enumerate(columns) if column.get("type") == "country"), None
    )
    value_indexes = [
        i for i, column in enumerate(columns) if column.get("type") == "data"
    ]
    if country_index is None or not value_indexes:
        raise ValueError("DataWeb report is missing country or value columns")

    parsed = []
    for row in rows:
        values = [entry.get("value", "") for entry in row.get("rowEntries", [])]
        try:
            country = values[country_index].strip()
            value = sum(int(values[i].replace(",", "").strip()) for i in value_indexes)
        except (IndexError, AttributeError, ValueError) as exc:
            raise ValueError("DataWeb returned a nonnumeric customs value") from exc
        if country and value > 0:
            parsed.append((country, value))
    return parsed


async def _country_codes(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get(COUNTRIES_URL, timeout=30)
    response.raise_for_status()
    options = response.json().get("options")
    if not isinstance(options, list):
        raise ValueError("DataWeb returned no country list")
    return {
        option["name"].rsplit(" - ", 2)[0]: option["iso2"]
        for option in options
        if option.get("name") and option.get("iso2")
    }


async def get_imports_by_country(
    hts_code: str,
    start: str,
    end: str,
    country_codes: dict[str, str] | None = None,
    *, client: httpx.AsyncClient | None = None, events=None,
) -> list[dict]:
    """Return consumption customs value by origin over the requested months."""
    code = _code(hts_code)
    token = os.environ.get("DATAWEB_API_KEY")
    if not token:
        raise ValueError("DATAWEB_API_KEY is not configured")
    async with nullcontext(client) if client is not None else httpx.AsyncClient() as client:
        response = await _post_report(client, token, _payload(code, start, end), events=events)
        report = response.json().get("dto")
        if isinstance(report, dict) and (report.get("errors") or report.get("needMoreTime")):
            raise ValueError("DataWeb could not complete the report")
        if not isinstance(report, dict) or not report.get("tables"):
            raise ValueError("DataWeb returned no report tables")

        if country_codes is None:
            country_codes = await _country_codes(client)
    result = []
    for name, value in _report_rows(report):
        country = country_codes.get(name)
        if country is None:
            raise ValueError(f"DataWeb country has no ISO-2 mapping: {name}")
        result.append({
            "country": country,
            "customs_value_usd": value,
        })
    return sorted(result, key=lambda row: (-row["customs_value_usd"], row["country"]))


async def discover_top_import_countries(
    hts_codes: list[str], top_n: int, today: date | None = None,
    *, events=None,
) -> dict:
    """Query each distinct code and retain its top origins by customs value."""
    if top_n < 1:
        raise ValueError("top_n must be at least 1")
    start, end = trailing_12_months(today)
    codes = sorted({_code(code) for code in hts_codes})
    if not codes:
        return {}
    result = {
        code: {"period_start": start, "period_end": end, "countries": {}}
        for code in codes
    }
    events = events if events is not None else Events()
    async with httpx.AsyncClient() as client:
        try:
            with events.action("country_metadata"):
                country_codes = await _country_codes(client)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            for position, (code, ranking) in enumerate(result.items(), 1):
                ranking["error"] = str(exc)
                events.emit("country_lookup", "skipped", hts_code=code,
                            position=position, total=len(codes), ranking=ranking)
            return result
        for position, code in enumerate(codes, 1):
            try:
                with events.action("country_lookup", hts_code=code,
                                   position=position, total=len(codes)) as outcome:
                    countries = await get_imports_by_country(
                        code, start, end, country_codes, client=client, events=events,
                    )
                    result[code]["countries"] = {
                        row["country"]: {"customs_value_usd": row["customs_value_usd"]}
                        for row in countries[:top_n]
                    }
                    outcome["ranking"] = result[code]
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                result[code]["error"] = str(exc)
                continue
    return result


def write_country_rankings(result: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for code, ranking in result.items():
            fh.write(json.dumps(
                {code: ranking}, ensure_ascii=False, separators=(",", ":"),
            ) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hts_code")
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(
        asyncio.run(discover_top_import_countries([args.hts_code], args.top)), indent=2,
    ))
