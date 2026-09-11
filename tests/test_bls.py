"""Published-category mapping, BLS transport, and dated benchmark enrichment."""

import asyncio
import json
from datetime import date

import httpx
import pandas as pd
import pytest

from src.bls import enrich_price_indices
from src.bls.client import fetch_series
from src.bls.mapping import harmonized_candidates, load_bls_harmonized_series
from src.config import BLS_SERIES


def test_reference_loader_trims_padding_and_filters_categories(tmp_path):
    path = tmp_path / "ei.series"
    path.write_text(
        "series_id    \tindex_code\tseries_name\tbegin_year\tbegin_period\n"
        " EIUIP02   \t IP \t Meat \t 1992 \t M12 \n"
        "EIUIP0203 \tIP\tPork\t2025\tM01\n"
        "EIUIP020300\tIP\tToo specific\t2025\tM01\n"
        "EIUIP\tIP\tAll imports\t1992\tM12\n"
        "EIUID02\tID\tExports\t1992\tM12\n"
        "EIUIP99\tID\tWrong family\t1992\tM12\n"
        "\tIP\tMissing ID\t1992\tM12\n"
    )
    previous = pd.get_option("future.infer_string")
    available = load_bls_harmonized_series(str(path))
    assert pd.get_option("future.infer_string") == previous
    assert set(available) == {"02", "0203"}
    assert available["02"] == {"series_id": "EIUIP02", "name": "Meat",
                               "begin_year": "1992", "begin_period": "M12"}
    assert [row["series_id"] for row in harmonized_candidates("0203.11.00.00", available)] == [
        "EIUIP0203", "EIUIP02",
    ]
    assert [row["series_id"] for row in harmonized_candidates("0204.00.00", available)] == ["EIUIP02"]
    assert harmonized_candidates("99000000", available) == []


def test_root_catalog_has_reference_categories():
    available = load_bls_harmonized_series(str(BLS_SERIES))
    assert available["8483"]["series_id"] == "EIUIP8483"
    assert available["84"]["series_id"] == "EIUIP84"
    assert available["8483"]["begin_year"] == "2025"


@pytest.mark.parametrize("code", ["", "8483", "8483XX0000", "８４８３００００", "123456789"])
def test_invalid_hts_is_rejected(code):
    with pytest.raises(ValueError, match="HTS code"):
        harmonized_candidates(code, {})


def observation(period, value, *, footnotes=None):
    return {"year": period[:4], "period": "M" + period[5:], "value": str(value),
            "footnotes": footnotes or []}


def response_for(data):
    return httpx.Response(200, json={"status": "REQUEST_SUCCEEDED", "Results": {
        "series": [{"seriesID": series_id, "data": rows} for series_id, rows in data.items()],
    }})


def enrich(handler, codes, **kwargs):
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await enrich_price_indices(codes, today=date(2026, 9, 10), client=client, **kwargs)
    return asyncio.run(exercise())


def test_enrichment_uses_common_dates_and_falls_back_for_missing_history(monkeypatch):
    monkeypatch.setenv("BLS_API_KEY", "test-key")
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return response_for({
            "EIUIP8483": [observation("2026-07", 104), observation("2025-12", 100)],
            "EIUIP8413": [observation("2025-07", 100), observation("2026-07", 110),
                          observation("2026-08", "-"), observation("2026-13", 999)],
            "EIUIP84": [observation("2026-07", 120, footnotes=[{"code": "P", "text": "Preliminary"}]),
                        observation("2025-07", 100)],
        })

    result = enrich(handler, ["8483.10.50.00", "8483.10.50.00", "8483105000", "8413.11.00.00"])
    assert len(requests) == 1
    assert requests[0]["registrationkey"] == "test-key"
    assert requests[0]["seriesid"] == ["EIUIP84", "EIUIP8413", "EIUIP8483"]
    assert requests[0]["startyear"] == "2024"
    assert result["baseline_period"] == "2025-07"
    assert result["current_period"] == "2026-07"
    fallback = result["indices"]["8483.10.50.00"]
    assert fallback == result["indices"]["8483105000"]
    assert fallback["series_id"] == "EIUIP84"
    assert fallback["baseline_index"] == 100
    assert fallback["current_index"] == 120
    assert "2025-07" in fallback["fallback_reason"]
    assert fallback["attempts"][0]["series_id"] == "EIUIP8483"
    assert fallback["current_footnotes"][0]["code"] == "P"
    assert result["indices"]["8413.11.00.00"]["series_id"] == "EIUIP8413"


def test_explicit_dates_do_not_mix_series_or_shift_missing_months(monkeypatch):
    monkeypatch.setenv("BLS_API_KEY", "test-key")
    result = enrich(lambda request: response_for({
        "EIUIP8413": [observation("2026-07", 120), observation("2025-06", 100)],
        "EIUIP84": [observation("2025-07", 100), observation("2026-06", 110)],
    }), ["8413110000"], baseline_period="2025-07", current_period="2026-07")
    record = result["indices"]["8413110000"]
    assert record["reason"] == "No usable BLS index pair"
    assert len(record["attempts"]) == 2
    assert "current_index" not in record


def test_explicit_shorter_comparison_can_use_new_heading(monkeypatch):
    monkeypatch.setenv("BLS_API_KEY", "test-key")
    result = enrich(lambda request: response_for({
        "EIUIP8483": [observation("2026-07", 104), observation("2025-12", 100)],
    }), ["8483105000"], baseline_period="2025-12", current_period="2026-07")
    record = result["indices"]["8483105000"]
    assert record["series_id"] == "EIUIP8483"
    assert record["fallback_reason"] is None
    assert record["baseline_period"] == "2025-12"


def test_unmapped_codes_and_missing_key_do_not_call_api(monkeypatch):
    monkeypatch.delenv("BLS_API_KEY", raising=False)

    def unexpected(request):
        pytest.fail("Must not call BLS without a key or series")

    result = enrich(unexpected, ["invalid", "99000000", "8413110000"])
    assert "HTS code" in result["indices"]["invalid"]["reason"]
    assert "No published" in result["indices"]["99000000"]["reason"]
    assert result["indices"]["8413110000"]["attempts"][0]["reason"] == "BLS_API_KEY is not configured"
    assert result["current_period"] is None
    assert enrich(unexpected, [])["indices"] == {}


@pytest.mark.parametrize("kwargs", [
    {"baseline_period": "2025-07"},
    {"current_period": "2026-09"},
    {"current_period": "2026-7"},
    {"current_period": "2026-13"},
    {"current_period": "2026-07", "baseline_period": "2026-07"},
    {"current_period": "2026-07", "baseline_period": "2000-07"},
])
def test_invalid_periods_fail_before_network(kwargs):
    with pytest.raises(ValueError):
        enrich(lambda request: pytest.fail("Unexpected API call"), ["8413110000"], **kwargs)


def fetch(handler, ids):
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_series(ids, 2025, 2026, api_key="test-secret", client=client)
    return asyncio.run(exercise())


def test_transport_deduplicates_batches_and_preserves_partial_results():
    sizes = []

    def handler(request):
        ids = json.loads(request.content)["seriesid"]
        sizes.append(len(ids))
        if len(sizes) == 2:
            return httpx.Response(403)
        return response_for({key: [observation("2026-07", 100)] for key in ids})

    ids = [f"EIUIP{number:04d}" for number in range(51)]
    result = fetch(handler, ids + ids)
    assert sizes == [50, 1]
    assert len(result) == 51
    assert result[ids[0]]["observations"]["2026-07"]["value"] == 100
    assert result[ids[-1]]["error"] == "BLS HTTP 403"


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not JSON"),
    httpx.Response(200, json={"status": "REQUEST_SUCCEEDED", "Results": None}),
    httpx.Response(200, json={"status": "REQUEST_FAILED", "message": ["test-secret"]}),
])
def test_bad_responses_are_unavailable_and_do_not_leak_credentials(response):
    result = fetch(lambda request: response, ["EIUIP84"])
    assert "error" in result["EIUIP84"]
    assert "test-secret" not in json.dumps(result)


@pytest.mark.parametrize("failure", ["429", "503", "timeout"])
def test_transient_errors_retry_then_succeed(monkeypatch, failure):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("src.bls.client.asyncio.sleep", sleep)
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            if failure == "timeout":
                raise httpx.ReadTimeout("test-secret", request=request)
            return httpx.Response(int(failure))
        return response_for({"EIUIP84": [observation("2026-07", 100)]})

    assert "observations" in fetch(handler, ["EIUIP84"])["EIUIP84"]
    assert delays == [1, 2]


def test_invalid_monthly_values_are_ignored():
    result = fetch(lambda request: response_for({"EIUIP84": [
        observation("2026-07", "nan"), observation("2026-06", "inf"),
        observation("2026-05", 0), observation("2026-04", -1),
        observation("2026-03", "-"), observation("2026-13", 101),
        observation("2026-02", 103),
    ]}), ["EIUIP84"])
    assert set(result["EIUIP84"]["observations"]) == {"2026-02"}
