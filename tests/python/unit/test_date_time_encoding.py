"""Test date and time type encoding/decoding."""

import json
from datetime import date
from datetime import time as time_type

from ferrum.queryset import _decode_bound_param, _encode_bind_value


def test_encode_date():
    """Date objects should be encoded as date type."""
    d = date(2025, 1, 15)
    result = _encode_bind_value(d)

    assert result["type"] == "date"
    assert result["value"] == "2025-01-15"


def test_decode_date():
    """Encoded dates should decode back to Python date objects."""
    encoded = {"type": "date", "value": "2025-01-15"}
    json_str = json.dumps(encoded)

    result = _decode_bound_param(json_str)

    assert isinstance(result, date)
    assert result == date(2025, 1, 15)


def test_encode_time():
    """Time objects should be encoded as time type."""
    t = time_type(14, 30, 45)
    result = _encode_bind_value(t)

    assert result["type"] == "time"
    assert result["value"] == "14:30:45"


def test_decode_time():
    """Encoded times should decode back to Python time objects."""
    encoded = {"type": "time", "value": "14:30:45"}
    json_str = json.dumps(encoded)

    result = _decode_bound_param(json_str)

    assert isinstance(result, time_type)
    assert result == time_type(14, 30, 45)


def test_regular_text_unchanged():
    """Regular text should not be affected by date/time handling."""
    text = "2025-01-15"
    result = _encode_bind_value(text)

    assert result["type"] == "text"
    assert result["value"] == text

    json_str = json.dumps(result)
    decoded = _decode_bound_param(json_str)

    assert isinstance(decoded, str)
    assert decoded == text
