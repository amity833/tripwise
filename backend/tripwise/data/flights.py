from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any

from .airports import distance_km, resolve_airport
from .dates import is_real_date

AIRLINES = ["El Al", "Lufthansa", "Air France", "British Airways", "Emirates", "KLM", "ITA Airways", "Delta"]
DEPARTS = ["06:15", "09:40", "13:25", "18:05", "21:30"]


@dataclass(frozen=True)
class Flight:
    id: str
    origin: str
    destination: str
    date: str
    departTime: str
    arriveTime: str
    durationMin: int
    airline: str
    cabin: str  # 'economy' | 'business'
    priceUsd: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def js_round(x: float) -> int:
    """Math.round semantics (half rounds up), so prices match the old Node server."""
    return math.floor(x + 0.5)


def _hash(s: str) -> int:  # FNV-1a, 32 bit
    h = 2166136261
    for c in s:
        h ^= ord(c)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _hhmm(minutes: int) -> str:
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def flight_id(o: str, d: str, date: str, idx: int, cabin: str) -> str:
    return f"FL-{o}-{d}-{date.replace('-', '')}-{idx}-{'J' if cabin == 'business' else 'Y'}"


def make_flight(o: str, d: str, date: str, idx: int, cabin: str) -> Flight | None:
    """Deterministic: the same route + date + index always yields the same flight."""
    a, b = resolve_airport(o), resolve_airport(d)
    if not a or not b or a.code == b.code or not is_real_date(date) or not isinstance(idx, int) or idx < 0 or idx > 3:
        return None
    seed = _hash(f"{a.code}{b.code}{date}{idx}")
    # FNV-1a barely changes when only the last char differs, so mix bits before using them.
    mixed = _hash(f"{seed}:{idx}") ^ (seed >> 13)
    base = _hash(f"{a.code}{b.code}{date}")  # same for all 4 options of a route/day, so idx offsets keep them distinct
    km = distance_km(a, b)
    duration = js_round((km / 800 + 0.6) * 60 / 5) * 5
    dep = DEPARTS[(base + idx * 2) % len(DEPARTS)]  # idx*2 gives 4 distinct slots out of 5
    dep_min = int(dep[:2]) * 60 + int(dep[3:])
    economy = js_round(80 + km * 0.08 + (mixed % 90))
    return Flight(
        id=flight_id(a.code, b.code, date, idx, cabin), origin=a.code, destination=b.code, date=date,
        departTime=dep, arriveTime=_hhmm(dep_min + duration), durationMin=duration,
        airline=AIRLINES[(base // 5 + idx * 3) % len(AIRLINES)], cabin=cabin,  # idx*3: 4 distinct airlines of 8
        priceUsd=js_round(economy * 3.2) if cabin == "business" else economy,
    )


def search_flights(o: str, d: str, date: str, cabin: str) -> list[Flight]:
    flights = [f for i in range(4) if (f := make_flight(o, d, date, i, cabin))]
    return sorted(flights, key=lambda f: f.priceUsd)


_ID = re.compile(r"FL-([A-Z]{3})-([A-Z]{3})-(\d{8})-(\d)-([YJ])", re.ASCII)


def get_flight(flight: str) -> Flight | None:
    """Resolve a flight id (as supplied by the model) back to a flight, or None if it is not real."""
    m = _ID.fullmatch(flight)
    if not m:
        return None
    s = m.group(3)
    return make_flight(m.group(1), m.group(2), f"{s[:4]}-{s[4:6]}-{s[6:]}", int(m.group(4)), "business" if m.group(5) == "J" else "economy")
