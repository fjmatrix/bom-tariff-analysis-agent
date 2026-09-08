import pytest

pytest.importorskip("textual")

from src.events import Events
from src.tui.activity import describe


def test_dataweb_retry_description_includes_wait_and_attempt():
    event = Events().emit(
        "dataweb_retry", "retrying", hts_code="8536694020", status_code=429,
        attempt=3, max_attempts=5, delay_seconds=30,
    )
    assert describe(event) == "DataWeb HTTP 429 · HTS 8536694020 · retry 3/5 in 30s"
