from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..data.airports import resolve_airport
from ..data.dates import days_between, today_iso
from ..data.flights import Flight, get_flight, make_flight
from ..data.hotels import Hotel, get_hotel

_POLICY = json.loads((Path(__file__).parent.parent / "data" / "policy.json").read_text(encoding="utf-8"))
ECONOMY_ONLY_UNDER_HOURS: int = _POLICY["economyOnlyUnderHours"]
HOTEL_CAP_BY_TIER: dict[str, int] = _POLICY["hotelNightlyCapUsdByTier"]
SOFT_WARNING_DAYS_AHEAD: int = _POLICY["softWarningDaysAhead"]


@dataclass
class PolicyResult:
    verdict: str  # 'pass' | 'warn' | 'block'
    reasons: list[str]
    suggestion: str | None = None
    flights: list[Flight] = field(default_factory=list)
    hotel: Hotel | None = None


def check_policy(flight_ids: list[str] | None, hotel_id: str | None, now: datetime, tz: str) -> PolicyResult:
    """Pure policy engine. Re-run server-side on propose and confirm; never trust an earlier model call."""
    blocks: list[str] = []
    warns: list[str] = []
    suggestion: str | None = None
    flights: list[Flight] = []
    today = today_iso(now, tz)

    for fid in flight_ids or []:
        f = get_flight(fid)
        if not f:
            blocks.append(f'Unknown flight id "{fid}".')
            continue
        flights.append(f)
        if days_between(today, f.date) < 0:
            blocks.append(f"Flight {fid} departs in the past.")
        if f.cabin == "business" and f.durationMin < ECONOMY_ONLY_UNDER_HOURS * 60:
            blocks.append(
                f"Business class is not allowed on flights under {ECONOMY_ONLY_UNDER_HOURS} hours (this one is {f.durationMin / 60:.1f}h)."
            )
            eco = make_flight(f.origin, f.destination, f.date, int(fid.split("-")[4]), "economy")
            if eco:
                suggestion = f"Compliant alternative: economy flight {eco.id} at ${eco.priceUsd}."
    if flights:
        ahead = days_between(today, flights[0].date)
        if 0 <= ahead < SOFT_WARNING_DAYS_AHEAD:
            warns.append(f"Booked only {ahead} day(s) ahead; policy recommends {SOFT_WARNING_DAYS_AHEAD}+ days.")

    hotel: Hotel | None = None
    if hotel_id:
        hotel = get_hotel(hotel_id)
        if not hotel:
            blocks.append(f'Unknown hotel id "{hotel_id}".')
        else:
            airport = resolve_airport(hotel.city)
            cap = HOTEL_CAP_BY_TIER[airport.tier if airport else "C"]
            if hotel.nightlyUsd > cap:
                blocks.append(f"Hotel is ${hotel.nightlyUsd}/night, above the ${cap}/night cap for {hotel.city}.")
                suggestion = (suggestion + " " if suggestion else "") + f"Choose a hotel at or under ${cap}/night."

    verdict = "block" if blocks else "warn" if warns else "pass"
    return PolicyResult(verdict, [*blocks, *warns], suggestion, flights, hotel)
