"""Fetch annual consumption customs value by origin for one HTS code."""

import argparse
import json
import os

import httpx

from src import config  # Load the repository's .env through the shared config.


def get_imports_by_country(hts_code: str, year: int = 2025) -> dict:
    """Return DataWeb's report DTO; values are USD, countries are separate."""
    code = hts_code.strip().replace(".", "")
    if not code.isascii() or not code.isdigit() or len(code) not in (8, 10):
        raise ValueError("HTS code must contain 8 or 10 digits, optionally dotted")
    token = os.environ["DATAWEB_API_KEY"]
    payload = {
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
                "timeframeSelectType": "fullYears",
                "years": [str(year)],
                "yearsTimeline": "Annual",
            },
            "MiscGroup": {
                "districts": {
                    "aggregation": "Aggregate District",
                    "districts": [],
                    "districtsSelectType": "all",
                },
                "importPrograms": {
                    "importPrograms": [],
                    "programsSelectType": "all",
                },
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
    response = httpx.post(
        "https://datawebws.usitc.gov/dataweb/api/v2/report2/runReport",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=120,
    )
    response.raise_for_status()
    report = response.json().get("dto")
    if isinstance(report, dict) and (report.get("errors") or report.get("needMoreTime")):
        raise ValueError("DataWeb could not complete the report")
    if not isinstance(report, dict) or not report.get("tables"):
        raise ValueError("DataWeb returned no report tables")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hts_code")
    parser.add_argument("--year", type=int, default=2025)
    args = parser.parse_args()
    print(json.dumps(get_imports_by_country(args.hts_code, args.year), indent=2))
