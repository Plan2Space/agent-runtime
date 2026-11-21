import pytest

from inventory import Inventory, OutOfStock


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def inv():
    clock = Clock()
    i = Inventory(clock)
    i.add_stock("A", 10)
    return i, clock


def test_reservation_holds_stock_until_it_expires(inv):
    i, clock = inv
    i.reserve("A", 4, ttl=60)
    assert i.available("A") == 6
    clock.t += 60
    assert i.available("A") == 10


def test_cannot_reserve_more_than_available(inv):
    i, _ = inv
    i.reserve("A", 7, ttl=60)
    with pytest.raises(OutOfStock):
        i.reserve("A", 5, ttl=60)


def test_release_is_idempotent(inv):
    i, _ = inv
    rid = i.reserve("A", 3, ttl=60)
    i.release(rid)
    i.release(rid)
    i.release("does-not-exist")
    assert i.available("A") == 10


def test_commit_active_reservation_reduces_stock(inv):
    i, _ = inv
    rid = i.reserve("A", 3, ttl=60)
    i.commit(rid)
    assert i.available("A") == 7
