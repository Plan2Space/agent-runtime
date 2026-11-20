"""Pagination helper."""


def paginate(items, page, per_page):
    """Return the items of a 1-based ``page`` with ``per_page`` items per page.

    - ``page`` 1 is the first page.
    - A page past the end returns an empty list.
    - ``page < 1`` or ``per_page < 1`` raises ValueError.
    """
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be >= 1")
    start = (page - 1) * per_page
    return list(items[start:start + per_page])


def page_count(total, per_page):
    """Number of pages needed for ``total`` items (0 items -> 0 pages)."""
    if per_page < 1:
        raise ValueError("per_page must be >= 1")
    return (total + per_page - 1) // per_page
