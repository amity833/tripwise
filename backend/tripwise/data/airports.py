from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Airport:
    code: str
    city: str
    country: str
    lat: float
    lon: float
    tier: str  # 'A' | 'B' | 'C'


AIRPORTS: list[Airport] = [
    Airport("TLV", "Tel Aviv", "Israel", 32.01, 34.89, "C"),
    Airport("BER", "Berlin", "Germany", 52.36, 13.5, "B"),
    Airport("LON", "London", "UK", 51.47, -0.45, "A"),
    Airport("NYC", "New York", "USA", 40.64, -73.78, "A"),
    Airport("SFO", "San Francisco", "USA", 37.62, -122.38, "A"),
    Airport("PAR", "Paris", "France", 49.01, 2.55, "B"),
    Airport("AMS", "Amsterdam", "Netherlands", 52.31, 4.76, "B"),
    Airport("MAD", "Madrid", "Spain", 40.47, -3.56, "B"),
    Airport("BCN", "Barcelona", "Spain", 41.3, 2.08, "B"),
    Airport("ROM", "Rome", "Italy", 41.8, 12.25, "B"),
    Airport("ATH", "Athens", "Greece", 37.94, 23.94, "C"),
    Airport("LIS", "Lisbon", "Portugal", 38.77, -9.13, "C"),
    Airport("DXB", "Dubai", "UAE", 25.25, 55.36, "A"),
]

_ALIASES = {
    "lhr": "LON", "jfk": "NYC", "cdg": "PAR", "fco": "ROM", "tel-aviv": "TLV",
    "telaviv": "TLV", "ber": "BER", "nyc": "NYC", "new york city": "NYC",
}


def resolve_airport(text: str) -> Airport | None:
    k = text.strip().lower()
    code = _ALIASES.get(k, k).upper()
    for a in AIRPORTS:
        if a.code == code:
            return a
    for a in AIRPORTS:
        if a.city.lower() == k:
            return a
    return None


SUPPORTED = ", ".join(f"{a.city} ({a.code})" for a in AIRPORTS)


def distance_km(a: Airport, b: Airport) -> float:
    r = math.radians
    h = math.sin(r(b.lat - a.lat) / 2) ** 2 + math.cos(r(a.lat)) * math.cos(r(b.lat)) * math.sin(r(b.lon - a.lon) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))
