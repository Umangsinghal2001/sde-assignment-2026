from datetime import datetime
from functools import lru_cache

D0 = datetime.fromisoformat("2027-01-04T00:00:00+05:30")


@lru_cache(maxsize=4096)
def minute_of(ts: str) -> int:
    return int((datetime.fromisoformat(ts) - D0).total_seconds() // 60)


def label(minute: int) -> str:
    """-90 -> 'D-1 22:30', 870 -> 'D0 14:30'."""
    d, m = divmod(minute, 1440)
    return f"D{d} {m // 60:02d}:{m % 60:02d}"


def parse_label(s: str) -> int:
    """'D3 14:00' -> sim minute."""
    day, hm = s.strip().split()
    h, m = hm.split(":")
    return int(day.lstrip("Dd")) * 1440 + int(h) * 60 + int(m)
