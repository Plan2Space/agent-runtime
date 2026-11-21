"""In-memory inventory with time-limited reservations. The clock is injected."""

from .models import OutOfStock, Reservation


class Inventory:
    def __init__(self, clock):
        self._clock = clock
        self._stock = {}
        self._reservations = {}
        self._seq = 0

    def add_stock(self, sku, qty):
        """Add ``qty`` (>= 1) units of ``sku``."""
        if qty < 1:
            raise ValueError("qty must be >= 1")
        self._stock[sku] = self._stock.get(sku, 0) + qty

    def available(self, sku):
        """Units of ``sku`` in stock minus units held by ACTIVE reservations."""
        now = self._clock()
        held = sum(r.qty for r in self._reservations.values() if r.sku == sku and r.active(now))
        return self._stock.get(sku, 0) - held

    def reserve(self, sku, qty, ttl):
        """Hold ``qty`` units of ``sku`` for ``ttl`` seconds and return the reservation id.

        Raises ValueError if ``qty < 1`` or ``ttl <= 0``, and OutOfStock if ``qty`` is greater
        than ``available(sku)`` at this moment.
        """
        if qty < 1 or ttl <= 0:
            raise ValueError("qty must be >= 1 and ttl > 0")
        if qty > self.available(sku):
            raise OutOfStock(sku)
        self._seq += 1
        rid = f"r{self._seq}"
        self._reservations[rid] = Reservation(rid, sku, qty, self._clock() + ttl)
        return rid

    def commit(self, rid):
        """Turn an ACTIVE reservation into a sale: stock decreases by its qty and it ends.

        Committing an unknown or expired reservation raises KeyError and leaves stock unchanged.
        """
        reservation = self._reservations.get(rid)
        if reservation is None or not reservation.active(self._clock()):
            raise KeyError(rid)
        del self._reservations[rid]
        self._stock[reservation.sku] -= reservation.qty

    def release(self, rid):
        """Cancel a reservation, freeing its units. Unknown ids are ignored (idempotent)."""
        self._reservations.pop(rid, None)
