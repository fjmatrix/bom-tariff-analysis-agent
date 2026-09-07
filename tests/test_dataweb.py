from unittest.mock import patch

import httpx
import pytest

from src.duty.dataweb import get_imports_by_country


def test_report_request(monkeypatch):
    monkeypatch.setenv("DATAWEB_API_KEY", "test-token")
    report = {"tables": [{"row_groups": []}], "errors": []}
    response = httpx.Response(
        200, json={"dto": report}, request=httpx.Request("POST", "https://example.com")
    )
    with patch("src.duty.dataweb.httpx.post", return_value=response) as post:
        assert get_imports_by_country("7318.15.60", 2025) == report
    request = post.call_args.kwargs
    assert request["headers"]["Authorization"] == "Bearer test-token"
    options = request["json"]["searchOptions"]
    assert options["commodities"]["commodities"] == ["73181560"]
    assert options["commodities"]["granularity"] == "8"
    assert options["countries"]["aggregation"] == "Break Out Countries"
    assert options["componentSettings"]["dataToReport"] == ["CONS_CUSTOMS_VALUE"]
    assert options["componentSettings"]["years"] == ["2025"]


@pytest.mark.parametrize("code", ["", "7318", "7318156x", "７３１８１５６０"])
def test_invalid_code_does_not_call_api(code):
    with patch("src.duty.dataweb.httpx.post") as post:
        with pytest.raises(ValueError, match="HTS code"):
            get_imports_by_country(code)
        post.assert_not_called()


@pytest.mark.parametrize(
    "status, body, error",
    [
        (401, {}, httpx.HTTPStatusError),
        (200, {"dto": None}, ValueError),
        (200, {"dto": {"tables": [{}], "errors": ["Invalid query"]}}, ValueError),
        (200, {"dto": {"tables": [{}], "needMoreTime": True}}, ValueError),
    ],
)
def test_failed_report(monkeypatch, status, body, error):
    monkeypatch.setenv("DATAWEB_API_KEY", "test-token")
    response = httpx.Response(
        status, json=body, request=httpx.Request("POST", "https://example.com")
    )
    with patch("src.duty.dataweb.httpx.post", return_value=response):
        with pytest.raises(error):
            get_imports_by_country("73181560")
