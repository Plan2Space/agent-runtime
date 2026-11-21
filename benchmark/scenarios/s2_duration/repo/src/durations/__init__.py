"""Parse and format human durations like ``1h30m`` or ``45s``."""

import re

_PATTERN = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")

_UNIT_SECONDS = {"h": 3600, "m": 3600, "s": 1}


def parse_duration(text):
    """Return the number of seconds in ``text``.

    Accepted form: optional hours, minutes and seconds components, in this order
    (``h``, ``m``, ``s``), at least one present, e.g. ``"1h30m"``, ``"90s"``, ``"2h5s"``.
    Units are case-insensitive and surrounding whitespace is ignored.
    Anything else (empty string, unknown unit, wrong order, stray characters) raises ValueError.
    """
    match = _PATTERN.match(text)
    if not match or not any(match.groups()):
        raise ValueError(f"invalid duration: {text!r}")
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * _UNIT_SECONDS["h"] + minutes * _UNIT_SECONDS["m"] + seconds * _UNIT_SECONDS["s"]


def format_duration(seconds):
    """Inverse of parse_duration for non-negative ints: 5415 -> '1h30m15s', 0 -> '0s'."""
    if seconds < 0:
        raise ValueError("negative duration")
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    out = (f"{h}h" if h else "") + (f"{m}m" if m else "") + (f"{s}s" if s else "")
    return out or "0s"
