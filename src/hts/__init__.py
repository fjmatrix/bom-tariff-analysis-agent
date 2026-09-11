"""Shared HTS identifier handling."""


def normalize_hts_code(hts_code: str) -> str:
    code = hts_code.strip().replace(".", "")
    if not code.isascii() or not code.isdigit() or len(code) not in (8, 10):
        raise ValueError("HTS code must contain 8 or 10 digits, optionally dotted")
    return code
