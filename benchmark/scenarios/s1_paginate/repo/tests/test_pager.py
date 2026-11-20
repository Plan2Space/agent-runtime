import pytest

from pager import page_count, paginate


def test_first_page_starts_at_first_item():
    assert paginate(list(range(10)), 1, 3) == [0, 1, 2]


def test_last_page_is_partial():
    assert paginate(list(range(10)), 4, 3) == [9]


def test_invalid_page_raises():
    with pytest.raises(ValueError):
        paginate([1, 2], 0, 1)


def test_page_count():
    assert page_count(10, 3) == 4
    assert page_count(0, 3) == 0
