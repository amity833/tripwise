"""The six tools the LLM can call. Booking itself is deliberately NOT one of them."""

from __future__ import annotations

from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from ..core.policy import check_policy
from ..core.proposals import create_proposal
from ..data.airports import SUPPORTED, resolve_airport
from ..data.dates import days_between, is_real_date, label, resolve_relative, today_iso
from ..data.flights import search_flights
from ..data.hotels import search_hotels
from .registry import Tool, ToolContext


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# --- search_flights -------------------------------------------------------------------------------------------------

class SearchFlightsArgs(BaseModel):
    origin: str
    destination: str
    date: str
    return_date: str | None = None
    cabin: Literal["economy", "business"] = "economy"


async def _search_flights(a: SearchFlightsArgs, tc: ToolContext) -> Any:
    o, d = resolve_airport(a.origin), resolve_airport(a.destination)
    if not o:
        return {"error": f'Unknown origin "{a.origin}". Supported: {SUPPORTED}'}
    if not d:
        return {"error": f'Unknown destination "{a.destination}". Supported: {SUPPORTED}'}
    if o.code == d.code:
        return {"error": "Origin and destination are the same."}
    if not is_real_date(a.date) or (a.return_date and not is_real_date(a.return_date)):
        return {"error": "Dates must be real calendar dates in YYYY-MM-DD format."}
    today = today_iso(tc.now, tc.tz)
    if days_between(today, a.date) < 0:
        return {"error": f"Departure {a.date} is in the past (today is {today})."}
    if a.return_date and days_between(a.date, a.return_date) < 0:
        return {"error": "Return date is before the departure date."}
    if days_between(today, a.date) > 330:
        return {"error": "Flights can only be searched up to 330 days ahead."}
    return _compact({
        "resolved": _compact({
            "origin": o.city, "destination": d.city, "departure": label(a.date),
            "return": label(a.return_date) if a.return_date else None,
        }),
        "outbound": [f.to_dict() for f in search_flights(o.code, d.code, a.date, a.cabin)],
        "inbound": [f.to_dict() for f in search_flights(d.code, o.code, a.return_date, a.cabin)] if a.return_date else None,
    })


search_flights_tool = Tool(
    name="search_flights",
    description="Search mock flights. Dates are YYYY-MM-DD. Returns outbound (and inbound when return_date is given) options with ids.",
    model=SearchFlightsArgs,
    parameters={
        "type": "object",
        "properties": {
            "origin": {"type": "string", "description": "City or IATA code"}, "destination": {"type": "string"},
            "date": {"type": "string", "description": "Departure date YYYY-MM-DD"}, "return_date": {"type": "string"},
            "cabin": {"type": "string", "enum": ["economy", "business"]},
        },
        "required": ["origin", "destination", "date"],
    },
    run=_search_flights,
)


# --- search_hotels --------------------------------------------------------------------------------------------------

class SearchHotelsArgs(BaseModel):
    city: str
    check_in: str
    check_out: str


async def _search_hotels(a: SearchHotelsArgs, tc: ToolContext) -> Any:
    c = resolve_airport(a.city)
    if not c:
        return {"error": f'Unknown city "{a.city}". Supported: {SUPPORTED}'}
    if not is_real_date(a.check_in) or not is_real_date(a.check_out):
        return {"error": "Dates must be real calendar dates in YYYY-MM-DD format."}
    if days_between(today_iso(tc.now, tc.tz), a.check_in) < 0:
        return {"error": "Check-in is in the past."}
    if days_between(a.check_in, a.check_out) < 1:
        return {"error": "Check-out must be after check-in."}
    return {"city": c.city, "hotels": [h.to_dict() for h in search_hotels(c.code, a.check_in, a.check_out)]}


search_hotels_tool = Tool(
    name="search_hotels",
    description="Search mock hotels in a city for a stay. Dates are YYYY-MM-DD.",
    model=SearchHotelsArgs,
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}, "check_in": {"type": "string"}, "check_out": {"type": "string"}},
        "required": ["city", "check_in", "check_out"],
    },
    run=_search_hotels,
)


# --- check_policy ---------------------------------------------------------------------------------------------------

class CheckPolicyArgs(BaseModel):
    flight_id: str | None = None
    return_flight_id: str | None = None
    hotel_id: str | None = None


async def _check_policy(a: CheckPolicyArgs, tc: ToolContext) -> Any:
    ids = [x for x in (a.flight_id, a.return_flight_id) if x]
    r = check_policy(ids, a.hotel_id, tc.now, tc.tz)
    return _compact({"verdict": r.verdict, "reasons": r.reasons, "suggestion": r.suggestion})


_BOOKING_PARAMS = {"flight_id": {"type": "string"}, "return_flight_id": {"type": "string"}, "hotel_id": {"type": "string"}}

check_policy_tool = Tool(
    name="check_policy",
    description="Check a flight and/or hotel against company travel policy. Returns pass, warn or block with reasons.",
    model=CheckPolicyArgs,
    parameters={"type": "object", "properties": _BOOKING_PARAMS},
    run=_check_policy,
)


# --- propose_booking ------------------------------------------------------------------------------------------------

class ProposeBookingArgs(BaseModel):
    flight_id: str
    return_flight_id: str | None = None
    hotel_id: str | None = None


async def _propose_booking(a: ProposeBookingArgs, tc: ToolContext) -> Any:
    r = await create_proposal(tc.repo, tc.ctx, a.flight_id, a.return_flight_id, a.hotel_id, tc.now, tc.tz)
    if not r.ok:
        return _compact({"error": r.error, "reasons": r.reasons, "suggestion": r.suggestion})
    assert r.proposal is not None
    return {
        "status": "proposed",
        "note": "A confirmation card was shown to the user. Tell them to confirm or cancel. Nothing is booked yet.",
        "summary": r.proposal.payload,
        "warnings": r.warnings,
    }


propose_booking_tool = Tool(
    name="propose_booking",
    description="Create a PENDING proposal for the user to confirm. This does NOT book anything. Policy is re-checked here; ids must come from search results.",
    model=ProposeBookingArgs,
    parameters={"type": "object", "properties": _BOOKING_PARAMS, "required": ["flight_id"]},
    run=_propose_booking,
)


# --- list_bookings --------------------------------------------------------------------------------------------------

class ListBookingsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _list_bookings(_a: ListBookingsArgs, tc: ToolContext) -> Any:
    bookings = await tc.repo.list_bookings(tc.ctx.user_id)
    return {"bookings": [{"reference": b.reference, **b.payload} for b in bookings]}


list_bookings_tool = Tool(
    name="list_bookings",
    description="List the user's confirmed trips across all their conversations.",
    model=ListBookingsArgs,
    parameters={"type": "object", "properties": {}},
    run=_list_bookings,
)


# --- resolve_date ---------------------------------------------------------------------------------------------------

class ResolveDateArgs(BaseModel):
    phrase: str | None = None


async def _resolve_date(a: ResolveDateArgs, tc: ToolContext) -> Any:
    local = tc.now.astimezone(ZoneInfo(tc.tz))
    today = local.date().isoformat()
    out: dict[str, Any] = {"today": today, "todayLabel": label(today), "localTime": local.strftime("%H:%M"), "timezone": tc.tz}
    if a.phrase:
        r = resolve_relative(a.phrase, today)
        if not r:
            return {**out, "error": f'Could not interpret "{a.phrase}". Ask the user for an exact date.'}
        out.update(resolved=r[0], resolvedLabel=label(r[0]), assumption=r[1])
    return out


resolve_date_tool = Tool(
    name="resolve_date",
    description=(
        "Get the current date/time, and optionally convert a relative phrase ('tomorrow', 'next week', 'next month', "
        "'in 10 days', 'next friday') into a real YYYY-MM-DD date. Always use this instead of computing dates yourself."
    ),
    model=ResolveDateArgs,
    parameters={"type": "object", "properties": {"phrase": {"type": "string", "description": "Relative date phrase; omit to just get the current time"}}},
    run=_resolve_date,
)
