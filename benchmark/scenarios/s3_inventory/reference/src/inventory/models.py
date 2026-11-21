"""Domain records for the inventory."""

from dataclasses import dataclass


class OutOfStock(Exception):
    """Raised when a reservation asks for more units than are available."""


@dataclass(frozen=True)
class Reservation:
    id: str
    sku: str
    qty: int
    expires_at: float

    def active(self, now):
        """A reservation holds stock strictly before its expiry time and not after."""
        return now < self.expires_at
