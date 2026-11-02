"""Test date and time type encoding/decoding."""

from datetime import date, time as time_type
import json
import pytest

from ferrum.queryset import _encode_bind_value, _decode_bound_param


def test_encode_date():
    """Date objects should be encoded with __ferrum_date marker."""
    d = date(2025, 1, 15)
    result = _encode_bind_value(d)
    
    assert result["type"] == "text"
    assert result["value"] == "2025-01-15"
    assert result.get("__ferrum_date") is True


def test_decode_date():
    """Encoded dates should decode back to Python date objects."""
    encoded = {"type": "text", "value": "2025-01-15", "__ferrum_date": True}
    json_str = json.dumps(encoded)
    
    result = _decode_bound_param(json_str)
    
    assert isinstance(result, date)
    assert result == date(2025, 1, 15)


def test_encode_time():
    """Time objects should be encoded with __ferrum_time marker."""
    t = time_type(14, 30, 45)
    result = _encode_bind_value(t)
    
    assert result["type"] == "text"
    assert result["value"] == "14:30:45"
    assert result.get("__ferrum_time") is True


def test_decode_time():
    """Encoded times should decode back to Python time objects."""
    encoded = {"type": "text", "value": "14:30:45", "__ferrum_time": True}
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
    assert "__ferrum_date" not in result
    
    json_str = json.dumps(result)
    decoded = _decode_bound_param(json_str)
    
    assert isinstance(decoded, str)
    assert decoded == text
