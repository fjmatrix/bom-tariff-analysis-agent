"""HTS Column 1 rates under assumed special-program eligibility.

Country membership plus a listed program is the scenario eligibility rule for
now; this does not verify shipment rules of origin or an importer claim.
Group preferences (A/A*/A+, B, D, E), S+, Column 2, Chapter 99, and additional
duties are not implemented. Extend duty_rate when those rules are introduced.
Program symbols: https://www.usitc.gov/faq/question/what_do_all_columns_mean.htm
"""

import re

from src.hts.index import HtsRecord, parse_rate

# Explicit mapping: HTS symbols are program identifiers, not country codes.
COUNTRY_PROGRAM = {
    "AU": "AU", "BH": "BH", "CL": "CL", "CO": "CO", "IL": "IL",
    "JO": "JO", "JP": "JP", "KR": "KR", "MA": "MA", "OM": "OM",
    "PA": "PA", "PE": "PE", "SG": "SG",
    "CA": "S", "MX": "S",
    "CR": "P", "DO": "P", "SV": "P", "GT": "P", "HN": "P", "NI": "P",
}


def qualifies_for_special_program(code: HtsRecord, country: str) -> bool:
    """Assume origin requirements are met when the mapped program is listed."""
    program = COUNTRY_PROGRAM.get(country.strip().upper())
    return program is not None and any(
        program in {symbol.strip() for symbol in symbols.split(",")}
        for symbols in re.findall(r"\(([^()]*)\)", code.special)
    )


def duty_rate(code: HtsRecord, country: str) -> float | None:
    """Return a decimal rate; None means an unsupported or missing rate."""
    if qualifies_for_special_program(code, country):
        program = COUNTRY_PROGRAM[country.strip().upper()]
        for rate, symbols in re.findall(r"([^()]+)\(([^()]*)\)", code.special):
            if program in {symbol.strip() for symbol in symbols.split(",")}:
                return parse_rate(rate.strip())
        return None
    return parse_rate(code.general)
