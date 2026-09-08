import asyncio
import json
from datetime import date
from unittest.mock import patch

import httpx
import pytest

from src.duty.dataweb import (
    discover_top_import_countries,
    get_imports_by_country,
    trailing_12_months,
    write_country_rankings,
)


def response(rows, status=200, errors=None):
    columns = [
        {"type": "country"},
        {"type": "hts"},
        {"type": "description"},
        {"type": "data"},
        {"type": "data"},
    ]
    report = {
        "errors": errors or [],
        "tables": [{"row_groups": [{
            "columnInfo": columns,
            "rowsNew": [
                {"rowEntries": [{"value": value} for value in row]} for row in rows
            ],
        }]}],
    }
    return httpx.Response(
        status, json={"dto": report}, request=httpx.Request("POST", "https://example.com"),
    )


@pytest.mark.parametrize("today,expected", [
    (date(2026, 9, 7), ("09/2025", "08/2026")),
    (date(2026, 1, 1), ("01/2025", "12/2025")),
])
def test_trailing_twelve_complete_months(today, expected):
    assert trailing_12_months(today) == expected


def test_report_request_sums_partial_years_and_ranks_countries(monkeypatch):
    monkeypatch.setenv("DATAWEB_API_KEY", "test-token")
    rows = [
        ["Canada", "73182100", "Washers", "100", "1,500"],
        ["China", "73182100", "Washers", "2,000", "0"],
        ["Japan", "73182100", "Washers", "0", "0"],
    ]
    with patch("src.duty.dataweb.httpx.AsyncClient.post", return_value=response(rows)) as post:
        result = asyncio.run(get_imports_by_country(
            "7318.21.00", "09/2025", "08/2026",
            {"Canada": "CA", "China": "CN", "Japan": "JP"},
        ))
    assert result == [
        {"country": "CN", "customs_value_usd": 2000},
        {"country": "CA", "customs_value_usd": 1600},
    ]
    request = post.call_args.kwargs
    assert request["headers"]["Authorization"] == "Bearer test-token"
    settings = request["json"]["searchOptions"]["componentSettings"]
    assert settings == {
        "dataToReport": ["CONS_CUSTOMS_VALUE"],
        "scale": "1",
        "timeframeSelectType": "specificDateRange",
        "years": [],
        "startDate": "09/2025",
        "endDate": "08/2026",
        "startMonth": None,
        "endMonth": None,
        "yearsTimeline": "Annual",
    }
    commodity = request["json"]["searchOptions"]["commodities"]
    assert commodity["commodities"] == ["73182100"]
    assert commodity["granularity"] == "8"
    assert request["json"]["searchOptions"]["countries"]["aggregation"] == "Break Out Countries"


@pytest.mark.parametrize("code", ["", "7318", "7318156x", "７３１８１５６０"])
def test_invalid_code_does_not_call_api(monkeypatch, code):
    monkeypatch.setenv("DATAWEB_API_KEY", "test-token")
    with patch("src.duty.dataweb.httpx.AsyncClient.post") as post:
        with pytest.raises(ValueError, match="HTS code"):
            asyncio.run(get_imports_by_country(code, "09/2025", "08/2026"))
        post.assert_not_called()


def test_missing_key_does_not_call_api(monkeypatch):
    monkeypatch.delenv("DATAWEB_API_KEY", raising=False)
    with patch("src.duty.dataweb.httpx.AsyncClient.post") as post:
        with pytest.raises(ValueError, match="DATAWEB_API_KEY"):
            asyncio.run(get_imports_by_country("73182100", "09/2025", "08/2026"))
        post.assert_not_called()


@pytest.mark.parametrize("reply,error", [
    (response([], status=401), httpx.HTTPStatusError),
    (httpx.Response(
        200, json={"dto": None}, request=httpx.Request("POST", "https://example.com"),
    ), ValueError),
    (response([], errors=["Invalid query"]), ValueError),
])
def test_failed_report(monkeypatch, reply, error):
    monkeypatch.setenv("DATAWEB_API_KEY", "test-token")
    with patch("src.duty.dataweb.httpx.AsyncClient.post", return_value=reply):
        with pytest.raises(error):
            asyncio.run(get_imports_by_country("73182100", "09/2025", "08/2026", {}))


@pytest.mark.parametrize("rows,error", [
    ([["Unknown", "73182100", "Washers", "1", "2"]], "ISO-2"),
    ([["Canada", "73182100", "Washers", "*", "2"]], "nonnumeric"),
])
def test_unmapped_country_and_suppressed_value_fail_explicitly(monkeypatch, rows, error):
    monkeypatch.setenv("DATAWEB_API_KEY", "test-token")
    with patch("src.duty.dataweb.httpx.AsyncClient.post", return_value=response(rows)):
        with pytest.raises(ValueError, match=error):
            asyncio.run(get_imports_by_country(
                "73182100", "09/2025", "08/2026", {"Canada": "CA"},
            ))


def test_discovery_deduplicates_codes_limits_each_ranking_and_keeps_errors():
    values = {
        "7318160060": [
            {"country": "CN", "customs_value_usd": 20},
            {"country": "CA", "customs_value_usd": 10},
        ],
    }

    async def imports(code, start, end, country_codes, *, client):
        if code == "7318210030":
            raise ValueError("no rows")
        return values[code]

    with patch("src.duty.dataweb._country_codes", return_value={"China": "CN"}), \
         patch("src.duty.dataweb.get_imports_by_country", side_effect=imports) as get:
        result = asyncio.run(discover_top_import_countries(
            ["7318.21.00.30", "7318.16.00.60", "7318.16.00.60"],
            1,
            date(2026, 9, 7),
        ))
    assert get.call_count == 2
    assert result == {
        "7318160060": {
            "period_start": "09/2025", "period_end": "08/2026",
            "countries": {"CN": {"customs_value_usd": 20}},
        },
        "7318210030": {
            "period_start": "09/2025", "period_end": "08/2026",
            "countries": {}, "error": "no rows",
        },
    }


def test_no_classified_codes_skip_dataweb():
    with patch("src.duty.dataweb._country_codes") as countries:
        result = asyncio.run(discover_top_import_countries([], 5, date(2026, 9, 7)))
    countries.assert_not_called()
    assert result == {}


def test_discovery_reuses_and_closes_client_for_mapping_and_reports(monkeypatch):
    monkeypatch.setenv("DATAWEB_API_KEY", "test-token")
    methods = []

    async def respond(request):
        await asyncio.sleep(0)
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={
                "options": [{"name": "Canada - 1220 - CA", "iso2": "CA"}],
            })
        return response([["Canada", "73182100", "Washers", "1", "2"]])

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with patch("src.duty.dataweb.httpx.AsyncClient", return_value=client) as factory:
        result = asyncio.run(discover_top_import_countries(["73182100", "73181600"], 1))
    factory.assert_called_once_with()
    assert methods == ["GET", "POST", "POST"]
    assert all(row["countries"] == {"CA": {"customs_value_usd": 3}} for row in result.values())
    assert client.is_closed


def test_country_mapping_failure_preserves_per_code_errors():
    with patch("src.duty.dataweb._country_codes", side_effect=ValueError("unavailable")), \
         patch("src.duty.dataweb.get_imports_by_country") as get:
        result = asyncio.run(discover_top_import_countries(["7318.21.00", "7318.16.00"], 5))
    get.assert_not_called()
    assert set(result) == {"73182100", "73181600"}
    assert all(row["countries"] == {} and row["error"] == "unavailable" for row in result.values())


def test_lookup_events_deduplicate_codes_and_capture_partial_failure():
    from src.events import Events

    observed = []
    with patch("src.duty.dataweb._country_codes", return_value={}), \
         patch("src.duty.dataweb.get_imports_by_country", side_effect=[
             [{"country": "CA", "customs_value_usd": 123}], ValueError("no rows"),
         ]):
        result = asyncio.run(discover_top_import_countries(
            ["7318.16.00", "73181600", "73182100"], 5, events=Events(observed.append),
        ))
    lookups = [event for event in observed if event.name == "country_lookup"]
    assert [event.status for event in lookups] == ["started", "completed", "started", "failed"]
    assert all(event.data["total"] == 2 for event in lookups)
    assert result["73182100"]["error"] == "no rows"
    assert lookups[1].data["ranking"]["countries"] == {"CA": {"customs_value_usd": 123}}


def test_metadata_failure_reports_all_lookups_skipped():
    from src.events import Events

    observed = []
    with patch("src.duty.dataweb._country_codes", side_effect=ValueError("unavailable")):
        asyncio.run(discover_top_import_countries(
            ["73182100", "73181600"], 5, events=Events(observed.append),
        ))
    assert [(event.name, event.status) for event in observed] == [
        ("country_metadata", "started"), ("country_metadata", "failed"),
        ("country_lookup", "skipped"), ("country_lookup", "skipped"),
    ]


def test_country_rankings_jsonl_preserves_values_period_and_errors(tmp_path):
    result = {
        "7318210030": {
            "period_start": "09/2025", "period_end": "08/2026",
            "countries": {"DE": {"customs_value_usd": 123}},
        },
        "7318160060": {
            "period_start": "09/2025", "period_end": "08/2026",
            "countries": {}, "error": "unavailable",
        },
    }
    path = tmp_path / "nested/trade_countries.jsonl"
    write_country_rankings(result, path)
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {code: ranking} for code, ranking in result.items()
    ]
    write_country_rankings({}, path)
    assert path.read_text() == ""
