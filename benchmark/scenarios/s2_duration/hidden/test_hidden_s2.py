import pytest

from durations import format_duration, parse_duration


@pytest.mark.parametrize("text,expected", [("45s", 45), ("1H5m7S", 3907), ("2h5s", 7205), ("\t3h\n", 10800),
                                           ("0m", 0), ("100m", 6000)])
def test_valid(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "5x", "30m1h", "h", "1h 30m", "-5s", "1.5h", "5s5s"])
def test_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)


@pytest.mark.parametrize("n", [0, 1, 59, 60, 61, 3599, 3600, 86399, 90061])
def test_round_trip(n):
    assert parse_duration(format_duration(n)) == n
