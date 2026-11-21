import pytest

from pager import page_count, paginate


@pytest.mark.parametrize("n,per", [(1, 1), (7, 2), (12, 4), (25, 5), (26, 5)])
def test_pages_cover_items_exactly_once(n, per):
    items = list(range(n))
    seen = []
    for p in range(1, page_count(n, per) + 1):
        seen += paginate(items, p, per)
    assert seen == items


def test_page_past_end_is_empty():
    assert paginate(list(range(5)), 9, 2) == []


def test_works_on_tuples_and_strings():
    assert paginate(("a", "b", "c"), 2, 2) == ["c"]
    assert paginate("abcdef", 2, 3) == ["d", "e", "f"]


def test_validation_kept():
    with pytest.raises(ValueError):
        paginate([1], 1, 0)
