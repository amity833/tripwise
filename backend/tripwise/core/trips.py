"""Plain-text summary of booked trips, shared by the channels and the dev model."""

from __future__ import annotations

from typing import Any

from ..data.airports import resolve_airport
from ..data.dates import label
from .models import Booking


def _city(code: str) -> str:
    airport = resolve_airport(code)
    return airport.city if airport else code


def describe_trip(reference: str, payload: dict[str, Any]) -> str:
    """Reference and price, then one line per flight and one for the hotel. **bold** is understood by both channels."""
    flights: list[dict[str, Any]] = payload.get("flights") or []
    hotel: dict[str, Any] | None = payload.get("hotel")
    destination = f" to {_city(flights[0]['destination'])}" if flights else ""
    lines = [f"**{reference}**{destination} · ${payload.get('totalUsd', '?')}"]
    lines += [
        f"✈️ {_city(f['origin'])} → {_city(f['destination'])} · {label(f['date'])} {f['departTime']}→{f['arriveTime']} · {f['airline']}"
        for f in flights
    ]
    if hotel:
        lines.append(f"🏨 {hotel['name']} · {label(hotel['checkIn'])} → {label(hotel['checkOut'])}")
    # Two trailing spaces keep the line breaks when the web app renders this as Markdown.
    return "  \n".join(lines)


def describe_trips(bookings: list[Booking]) -> str:
    return "\n\n".join(describe_trip(b.reference, b.payload) for b in bookings)
