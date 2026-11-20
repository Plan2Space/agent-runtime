import pytest

from inventory import Inventory, OutOfStock


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(stock=10):
    c = Clock()
    i = Inventory(c)
    i.add_stock("A", stock)
    i.add_stock("B", 2)
    return i, c


def test_expiry_boundary_is_exclusive():
    i, c = make()
    i.reserve("A", 5, ttl=10)
    c.t = 9.999
    assert i.available("A") == 5
    c.t = 10.0
    assert i.available("A") == 10


def test_expired_reservations_free_units_for_new_reservations():
    i, c = make()
    i.reserve("A", 10, ttl=5)
    c.t = 6
    i.reserve("A", 10, ttl=5)
    assert i.available("A") == 0


def test_reservations_are_per_sku():
    i, _ = make()
    i.reserve("A", 10, ttl=5)
    i.reserve("B", 2, ttl=5)
    with pytest.raises(OutOfStock):
        i.reserve("B", 1, ttl=5)


def test_exact_available_amount_can_be_reserved():
    i, _ = make()
    i.reserve("A", 6, ttl=5)
    i.reserve("A", 4, ttl=5)
    assert i.available("A") == 0


def test_commit_expired_raises_and_keeps_stock():
    i, c = make()
    rid = i.reserve("A", 4, ttl=5)
    c.t = 5
    with pytest.raises(KeyError):
        i.commit(rid)
    assert i.available("A") == 10


def test_commit_unknown_raises():
    i, _ = make()
    with pytest.raises(KeyError):
        i.commit("nope")


def test_commit_then_release_does_not_return_units():
    i, _ = make()
    rid = i.reserve("A", 3, ttl=5)
    i.commit(rid)
    i.release(rid)
    assert i.available("A") == 7


def test_validation():
    i, _ = make()
    with pytest.raises(ValueError):
        i.reserve("A", 0, ttl=5)
    with pytest.raises(ValueError):
        i.reserve("A", 1, ttl=0)
