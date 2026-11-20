import pytest

from durations import format_duration, parse_duration


def test_hours_and_minutes():
    assert parse_duration("1h30m") == 5400


def test_uppercase_units_and_whitespace():
    assert parse_duration(" 2M ") == 120


def test_trailing_garbage_is_rejected():
    with pytest.raises(ValueError):
        parse_duration("10sx")


def test_format_round_trip():
    assert format_duration(5415) == "1h30m15s"
    assert parse_duration(format_duration(5415)) == 5415
